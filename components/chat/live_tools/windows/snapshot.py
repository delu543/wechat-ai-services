"""Bounded online Windows snapshots using SQLite's native WAL coordination locks.

Read-only handles and shared LockFileEx locks at SQLite's SHM bytes 120..122
exclude writers/checkpointers for the short copy. No source bytes are changed.
Decryption and WAL replay occur only after releasing every live lock.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import secrets
import time

from live_tools.platform_support import private_chmod, private_mkdir, require_no_reparse, support_root
from live_tools import wechat_safe_snapshot as core
from live_tools.windows.router import require_same
from live_tools.windows.state import load_initialization, write_account_profile


class OnlineRefreshError(RuntimeError):
    pass


@contextmanager
def shm_lease(database: Path, relative: str):
    import msvcrt
    import pywintypes
    import win32con
    import win32file
    shm = Path(str(database) + "-shm")
    require_no_reparse(shm)
    handle = win32file.CreateFile(str(shm), win32con.GENERIC_READ,
                                 win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                                 None, win32con.OPEN_EXISTING, win32con.FILE_ATTRIBUTE_NORMAL, None)
    descriptor = msvcrt.open_osfhandle(handle.Detach(), os.O_RDONLY | os.O_BINARY)
    overlap = pywintypes.OVERLAPPED()
    overlap.Offset = core.WAL_SHM_LOCK_OFFSET
    raw_handle = msvcrt.get_osfhandle(descriptor)
    locked = False
    try:
        deadline = time.monotonic() + 2
        while True:
            try:
                # FAIL_IMMEDIATELY, without EXCLUSIVE_LOCK: three shared bytes.
                win32file.LockFileEx(raw_handle, 1, 3, 0, overlap)
                locked = True
                break
            except pywintypes.error as error:
                if error.winerror != 33 or time.monotonic() >= deadline:
                    raise OnlineRefreshError("微信数据库忙或不支持协调锁；未复制，稍后重试") from None
                time.sleep(0.025)
        yield core.OnlineWalLease(relative, shm, descriptor,
                                 core.FileFingerprint.from_stat(os.fstat(descriptor)))
    finally:
        try:
            if locked:
                win32file.UnlockFileEx(raw_handle, 3, 0, overlap)
        finally:
            os.close(descriptor)


@contextmanager
def database_lease(database, relative):
    if Path(str(database) + "-shm").exists():
        with shm_lease(database, relative) as lease:
            yield lease
        return
    # An inactive shard may have no SHM. Deny-write/delete sharing on the DB
    # and its existing WAL is a native exclusion proof, not a timestamp guess.
    import win32con
    import win32file
    with ExitStack() as held:
        for path in (database, Path(str(database) + "-wal")):
            require_no_reparse(path)
            if path != database and not path.exists():
                continue
            handle = win32file.CreateFile(str(path), win32con.GENERIC_READ,
                                         win32con.FILE_SHARE_READ, None, win32con.OPEN_EXISTING,
                                         win32con.FILE_ATTRIBUTE_NORMAL, None)
            held.callback(handle.Close)
        if Path(str(database) + "-shm").exists():
            raise OnlineRefreshError("数据库切换为在线状态；已停止本次复制")
        yield None


def _copy_under_lock(source, destination, fingerprint, deadline):
    private_mkdir(destination.parent)
    with source.open("rb") as reader, destination.open("xb") as writer:
        private_chmod(writer.fileno(), 0o600)
        if core.FileFingerprint.from_stat(os.fstat(reader.fileno())) != fingerprint:
            raise OnlineRefreshError("快照源身份变化")
        while True:
            if time.monotonic() >= deadline:
                raise OnlineRefreshError("在线复制达到 30 秒锁定上限；已释放锁，未发布")
            data = reader.read(1024 * 1024)
            if not data:
                break
            writer.write(data)
        writer.flush()
        os.fsync(writer.fileno())
        if (core.FileFingerprint.from_stat(os.fstat(reader.fileno())) != fingerprint
                or core.FileFingerprint.from_stat(source.stat()) != fingerprint
                or writer.tell() != fingerprint.size):
            raise OnlineRefreshError("在线复制期间源文件变化；未发布")


def create_snapshot(binding, state=None):
    state = state or load_initialization(binding.account_ref)
    require_same(binding)
    if state.get("db_base") != str(binding.db_base) or not state.get("snapshot_retention_approved"):
        raise OnlineRefreshError("当前账号尚未确认保留私有快照；请显式 setup")
    targets = state.get("targets", {})
    if not targets or len(targets) > 256:
        raise OnlineRefreshError("初始化目标集合无效")
    # Refresh every initialized message shard, not just old chat matches: a chat
    # can move into another shard. New uninitialized shards fail, never omit.
    from live_tools.windows.setup_support import inventory, target_set
    expected = target_set(inventory(binding), state["types"])
    if set(expected) != set(targets):
        raise OnlineRefreshError("数据库分片已变化；需显式扩展初始化，不能遗漏新消息")
    sources = {}
    keys = {}
    for relative, entry in targets.items():
        relative = core.normalize_database_request(relative)
        source, fingerprint = core.resolve_source(binding.db_base, relative)
        require_no_reparse(source)
        with source.open("rb") as handle:
            page = handle.read(core.PAGE_SIZE)
        if page[:16].hex() != entry["salt"]:
            raise OnlineRefreshError("数据库账号或加密状态已改变；需要重新初始化")
        key = bytes.fromhex(entry["key"])
        core._validate_first_plain_page(core._decrypt_page(page, 1, key))
        sources[relative] = (source, fingerprint)
        keys[relative] = key
    run = support_root() / "snapshots" / binding.account_ref / ("run-" + secrets.token_hex(16))
    private_mkdir(run)
    anchors = {}
    with ExitStack() as stack:
        deadline = time.monotonic() + 30
        for relative, (source, _) in sources.items():
            if time.monotonic() >= deadline:
                raise OnlineRefreshError("协调锁达到时间上限；未发布")
            lease = stack.enter_context(database_lease(source, relative))
            if lease is not None:
                anchor, wal_fingerprint = core._read_locked_online_wal_anchor(lease, source)
            else:
                anchor = None
                wal = Path(str(source) + "-wal")
                wal_fingerprint = core._assert_regular(wal, "已关闭分片 WAL") if wal.exists() else None
            fingerprint = core._assert_regular(source, "在线数据库")
            anchors[relative] = (lease, anchor, wal_fingerprint, fingerprint)
        for relative, (source, _) in sources.items():
            lease, anchor, wal_fingerprint, fingerprint = anchors[relative]
            copied = run / "copied" / relative
            _copy_under_lock(source, copied, fingerprint, deadline)
            copied_wal = Path(str(copied) + "-wal")
            if wal_fingerprint is not None:
                _copy_under_lock(Path(str(source) + "-wal"), copied_wal, wal_fingerprint, deadline)
            else:
                with copied_wal.open("xb") as handle:
                    private_chmod(handle.fileno(), 0o600)
            if lease is not None and core._read_locked_online_wal_anchor(lease, source) != (anchor, wal_fingerprint):
                raise OnlineRefreshError("协调快照状态变化；未发布")
    require_same(binding)
    for relative in sources:
        copied = run / "copied" / relative
        core.materialize_copied_database_with_wal(
            copied, Path(str(copied) + "-wal"), run / "encrypted" / relative,
            online_anchor=anchors[relative][1],
        )
        core.decrypt_snapshot_database(run / "encrypted", relative, run / "decrypted", keys[relative])
    require_same(binding)
    from content_vault.cli import doctor
    vault = run / "decrypted"
    profile = {"schema_version": 2, "account_ref": binding.account_ref,
               "vault_dir": str(vault), "account_root": str(binding.db_base.parent), "swift_bin": None}
    report = doctor(str(vault), account_root=profile["account_root"])
    if not report["ready_for_scan"]:
        raise OnlineRefreshError("新快照未通过扫描检查；保留原 profile")
    write_account_profile(binding.account_ref, profile)
    return {"status": "online-refresh-complete", "profile_updated": True,
            "database_count": len(targets), "snapshot_mode": "windows_sqlite_shm_coordinated_copy",
            "page_hmac_verified": False}


def refresh_online_snapshot(binding, profile, *, kinds, chat_id):
    try:
        if profile.get("account_ref") != binding.account_ref or not chat_id or not kinds:
            raise OnlineRefreshError("在线刷新请求与账号不匹配")
        state = load_initialization(binding.account_ref)
        if "all" not in state["types"] and not set(kinds) <= set(state["types"]):
            raise OnlineRefreshError("请求内容超出初始化范围；请显式扩展 setup")
        return create_snapshot(binding, state)
    except OnlineRefreshError:
        raise
    except Exception:
        raise OnlineRefreshError("Windows 快照无法安全完成；未发布，请检查初始化和微信状态") from None
