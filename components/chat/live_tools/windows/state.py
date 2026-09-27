"""Account-scoped Windows state: protected ACLs and current-user DPAPI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets

from live_tools.platform_support import is_private, private_chmod, private_mkdir, require_no_reparse, support_root

REF = re.compile(r"account-[0-9a-f]{12}\Z")
ENTROPY = b"WeChatAIServicesChat/Windows/v1"


class ProfileError(RuntimeError):
    pass


def account_directory(account_ref, *, create=False):
    if not isinstance(account_ref, str) or not REF.fullmatch(account_ref):
        raise ProfileError("账号状态标识无效")
    path = support_root() / "accounts" / account_ref
    if create:
        private_mkdir(path)
    else:
        require_no_reparse(path)
        if not path.is_dir() or not is_private(path):
            raise ProfileError("当前账号尚未初始化，或私有状态权限不安全")
    return path


def read_private(path: Path) -> bytes:
    require_no_reparse(path)
    before = path.stat()
    if not path.is_file() or not 0 < before.st_size <= 1_048_576 or not is_private(path):
        raise ProfileError("本机私有状态无效")
    data = path.read_bytes()
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ProfileError("本机私有状态读取期间发生变化")
    return data


def write_private(path: Path, data: bytes) -> None:
    private_mkdir(path.parent)
    require_no_reparse(path)
    if path.exists() and not is_private(path):
        raise ProfileError("拒绝覆盖不安全的状态文件")
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(12))
    with temporary.open("xb") as handle:
        private_chmod(handle.fileno(), 0o600)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    if not is_private(path):
        raise ProfileError("私有状态发布验证失败")


def write_json(path, value):
    write_private(path, (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))


def load_account_profile(account_ref):
    try:
        value = json.loads(read_private(account_directory(account_ref) / "profile.json"))
        if set(value) != {"schema_version", "account_ref", "vault_dir", "account_root", "swift_bin"}:
            raise ValueError()
        if value["schema_version"] != 2 or value["account_ref"] != account_ref or value["swift_bin"] is not None:
            raise ValueError()
        vault = Path(value["vault_dir"])
        media = Path(value["account_root"])
        require_no_reparse(vault)
        require_no_reparse(media)
        allowed = support_root() / "snapshots" / account_ref
        if not vault.is_relative_to(allowed) or not vault.is_dir() or not is_private(vault) or not media.is_dir():
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError, KeyError):
        raise ProfileError("当前账号 profile 缺失或不安全；需要显式 setup") from None


def write_account_profile(account_ref, value):
    if value.get("account_ref") != account_ref or value.get("schema_version") != 2:
        raise ProfileError("profile 与当前账号不匹配")
    write_json(account_directory(account_ref) / "profile.json", value)


def store_initialization(account_ref, payload):
    import win32crypt
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    encrypted = win32crypt.CryptProtectData(encoded, "WeChat Local Export", ENTROPY + account_ref.encode(), None, None, 1)
    write_private(account_directory(account_ref, create=True) / "initialization.dpapi", encrypted)


def load_initialization(account_ref):
    import win32crypt
    try:
        encrypted = read_private(account_directory(account_ref) / "initialization.dpapi")
        _, encoded = win32crypt.CryptUnprotectData(encrypted, ENTROPY + account_ref.encode(), None, None, 1)
        payload = json.loads(encoded)
        if payload.get("account_ref") != account_ref or payload.get("schema_version") != 1:
            raise ValueError()
        return payload
    except Exception:
        raise ProfileError("当前 Windows 用户无法验证该账号初始化；请显式运行 setup") from None
