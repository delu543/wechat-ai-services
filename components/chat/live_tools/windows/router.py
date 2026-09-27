"""Read-only exact process-handle routing for official Windows WeChat 4.x.

No directory census, historical-account ranking, screenshots or UI interaction.
Incomplete/ambiguous process evidence is a blocker, not a best-guess account.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

from live_tools.platform_support import current_sid, require_no_reparse

TARGET = re.compile(r"(?:contact/contact\.db|message/(?:message_\d+|media_\d+|message_resource)\.db)\Z")
METHOD = "official-windows-process-open-files-two-sample"


class AccountRoutingError(RuntimeError):
    def __init__(self, code="unavailable", *, samples_completed=0):
        self.code = code
        self.samples_completed = samples_completed
        super().__init__("无法唯一确认当前官方微信账号；请保持微信登录且打开任意聊天后重试")


@dataclass(frozen=True, repr=False)
class Binding:
    account_ref: str
    db_base: Path = field(repr=False)
    pid: int = field(repr=False)
    started: float = field(repr=False)
    executable: Path = field(repr=False)
    version: str
    categories: tuple[str, ...]

    def __repr__(self):
        return "<WindowsBinding private>"

    def public_report(self):
        return {"status": "unique", "selected": True, "method": METHOD,
                "samples_completed": 2, "official_process_count": 1,
                "held_categories": list(self.categories),
                "core_evidence": {"contact": True, "message": True},
                "writes_performed": False}


def system_powershell() -> Path:
    import win32api
    return Path(win32api.GetSystemDirectory()) / "WindowsPowerShell/v1.0/powershell.exe"


def verify_executable(path: Path) -> str:
    require_no_reparse(path)
    if path.name.casefold() not in {"weixin.exe", "wechat.exe"} or not path.is_file():
        raise AccountRoutingError()
    before = path.stat()
    result = subprocess.run(
        [str(system_powershell()), "-NoProfile", "-NonInteractive", "-File",
         str(Path(__file__).with_name("verify_wechat.ps1")), "-Executable", str(path)],
        capture_output=True, timeout=20, check=False,
    )
    try:
        report = json.loads(result.stdout.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        raise AccountRoutingError() from None
    after = path.stat()
    if (result.returncode or report.get("valid") is not True or report.get("major") != 4
            or (before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_ino, after.st_size, after.st_mtime_ns)):
        raise AccountRoutingError()
    return str(report["version"])


def process_owned_by_current_user(pid: int) -> bool:
    import win32api
    import win32con
    import win32security as security
    process = win32api.OpenProcess(0x1000, False, pid)
    try:
        token = security.OpenProcessToken(process, win32con.TOKEN_QUERY)
        try:
            return security.GetTokenInformation(token, security.TokenUser)[0] == current_sid()
        finally:
            token.Close()
    finally:
        process.Close()


def roots_from_open_files(paths):
    roots = {}
    for value in paths:
        path = Path(value)
        # Exact db/shm/wal filenames only. Never derive an account from a folder hint.
        name = re.sub(r"-(?:wal|shm)$", "", path.name)
        base_file = path.with_name(name)
        if base_file.parent.parent.name.casefold() != "db_storage":
            continue
        relative = f"{base_file.parent.name}/{name}"
        if not TARGET.fullmatch(relative):
            continue
        require_no_reparse(base_file)
        base = base_file.parent.parent
        roots.setdefault(base, set()).add("contact" if relative.startswith("contact/")
                                          else "message" if name.startswith("message_")
                                          and name != "message_resource.db" else "media")
    return roots


def _sample():
    import psutil
    bindings = []
    for process in psutil.process_iter(["pid", "name"]):
        if (process.info["name"] or "").casefold() not in {"weixin.exe", "wechat.exe"}:
            continue
        try:
            if not process_owned_by_current_user(process.pid):
                continue
            started = process.create_time()
            executable = Path(process.exe())
            version = verify_executable(executable)
            roots = roots_from_open_files(item.path for item in process.open_files())
            if len(roots) > 1:
                raise AccountRoutingError("multiple-active-accounts")
            for base, categories in roots.items():
                if not {"contact", "message"} <= categories:
                    raise AccountRoutingError()
                contact = base / "contact/contact.db"
                with contact.open("rb") as handle:
                    salt = handle.read(16)
                if len(salt) != 16 or salt == b"SQLite format 3\0":
                    raise AccountRoutingError()
                ref = "account-" + hashlib.sha256(b"wechat-account-ref-v1\0" + salt).hexdigest()[:12]
                if process.create_time() != started or Path(process.exe()) != executable:
                    raise AccountRoutingError("unstable")
                bindings.append(Binding(ref, base, process.pid, started, executable,
                                        version, tuple(sorted(categories))))
        except psutil.NoSuchProcess:
            raise AccountRoutingError("unstable") from None
        except (OSError, psutil.AccessDenied, subprocess.SubprocessError):
            raise AccountRoutingError() from None
    if len(bindings) != 1:
        raise AccountRoutingError("multiple-active-accounts" if bindings else "no-active-account")
    return bindings[0]


def bind_active_account(*, sampler=None, pause=None):
    sampler = sampler or _sample
    first = sampler()
    (pause or time.sleep)(0.2)
    second = sampler()
    if first != second:
        raise AccountRoutingError("unstable", samples_completed=2)
    return second


def require_same(binding):
    current = bind_active_account()
    if current != binding:
        raise AccountRoutingError("unstable", samples_completed=2)
    return current
