"""Small, fail-closed operating-system adapters for the public workflow.

The media/source pipeline is platform-neutral.  Desktop WeChat control is not:
macOS has the guarded File Transfer Assistant implementation, while Windows is
limited to user-confirmed playback plus bounded runtime-file observation.
"""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path
from typing import Mapping, Sequence


RUNTIME_ROOTS_ENV = "WEIXIN_REPLAY_RUNTIME_ROOTS"
SUPPORTED_SYSTEMS = {"Darwin", "Windows"}


def current_system(value: str | None = None) -> str:
    return str(value or platform.system()).strip() or "Unknown"


def application_root(
    home: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
) -> Path:
    resolved_home = Path(home if home is not None else Path.home()).expanduser().resolve()
    env = os.environ if environ is None else environ
    selected = current_system(system)
    if selected == "Windows":
        local_app_data = str(env.get("LOCALAPPDATA") or "").strip()
        base = Path(local_app_data).expanduser() if local_app_data else resolved_home / "AppData" / "Local"
        return (base / "WeixinReplayToMP3").resolve()
    if selected == "Darwin":
        return (resolved_home / "Library" / "Application Support" / "WeixinReplayToMP3").resolve()
    return (resolved_home / ".local" / "share" / "WeixinReplayToMP3").resolve()


def _dedupe_paths(values: Sequence[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        key = os.path.normcase(str(value.expanduser()))
        if key in seen:
            continue
        seen.add(key)
        result.append(value.expanduser())
    return tuple(result)


def _split_path_list(raw: str, *, system: str) -> list[Path]:
    if not raw.strip():
        return []
    separator = ";" if system == "Windows" else os.pathsep
    return [Path(part.strip()).expanduser() for part in raw.split(separator) if part.strip()]


def _configured_runtime_roots(
    *,
    environ: Mapping[str, str],
    system: str,
) -> list[Path]:
    return _split_path_list(str(environ.get(RUNTIME_ROOTS_ENV) or ""), system=system)


def _windows_runtime_roots(
    home: Path,
    *,
    environ: Mapping[str, str],
) -> list[Path]:
    roaming_value = str(environ.get("APPDATA") or "").strip()
    local_value = str(environ.get("LOCALAPPDATA") or "").strip()
    roaming = Path(roaming_value).expanduser() if roaming_value else home / "AppData" / "Roaming"
    local = Path(local_value).expanduser() if local_value else home / "AppData" / "Local"
    return [
        roaming / "Tencent" / "xwechat" / "radium",
        local / "Tencent" / "xwechat" / "radium",
        roaming / "Tencent" / "WeChat" / "XPlugin" / "Plugins" / "RadiumWMPF",
        local / "Tencent" / "WeChat" / "XPlugin" / "Plugins" / "RadiumWMPF",
        roaming / "Tencent" / "WeChat" / "radium",
        local / "Tencent" / "WeChat" / "radium",
    ]


def weixin_marker_scan_roots(
    home: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
) -> tuple[Path, ...]:
    """Return playback/runtime roots only; never chat/contact database roots."""

    resolved_home = Path(home if home is not None else Path.home()).expanduser().resolve()
    env = os.environ if environ is None else environ
    selected = current_system(system)
    configured = _configured_runtime_roots(environ=env, system=selected)
    if selected == "Windows":
        return _dedupe_paths(
            [*configured, *_windows_runtime_roots(resolved_home, environ=env)]
        )
    if selected == "Darwin":
        defaults = [
            resolved_home
            / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium",
            resolved_home
            / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/net/cdncomm",
            resolved_home
            / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/net/kvcomm",
            resolved_home
            / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/log/radium",
            resolved_home
            / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/log/player",
            resolved_home
            / "Library/Group Containers/5A4RE8SF68.com.tencent.xinWeChat/Library/Caches",
        ]
        return _dedupe_paths([*configured, *defaults])
    return _dedupe_paths(configured)


def weixin_recent_source_roots(
    home: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
) -> tuple[Path, ...]:
    """Roots used by the target-bound causal source snapshot."""

    resolved_home = Path(home if home is not None else Path.home()).expanduser().resolve()
    env = os.environ if environ is None else environ
    selected = current_system(system)
    configured = _configured_runtime_roots(environ=env, system=selected)
    if selected == "Windows":
        return _dedupe_paths(
            [*configured, *_windows_runtime_roots(resolved_home, environ=env)]
        )
    if selected == "Darwin":
        base = resolved_home / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data"
        defaults = [
            base / "radium/web/profiles",
            base / "net/cdncomm",
            base / "net/kvcomm",
            base / "log/radium",
            base / "log/player",
        ]
        return _dedupe_paths([*configured, *defaults])
    return _dedupe_paths(configured)


def weixin_cache_audit_roots(
    home: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
) -> tuple[Path, ...]:
    """Legacy cache-audit roots, kept byte-for-byte compatible on macOS."""

    resolved_home = Path(home if home is not None else Path.home()).expanduser().resolve()
    env = os.environ if environ is None else environ
    selected = current_system(system)
    configured = _configured_runtime_roots(environ=env, system=selected)
    if selected == "Windows":
        return _dedupe_paths(
            [*configured, *_windows_runtime_roots(resolved_home, environ=env)]
        )
    if selected == "Darwin":
        data = resolved_home / "Library/Containers/com.tencent.xinWeChat/Data"
        defaults = [
            data / "tmp",
            data / "Documents/app_data/radium",
            data / "Documents/app_data/net/cdncomm",
        ]
        return _dedupe_paths([*configured, *defaults])
    return _dedupe_paths(configured)


def weixin_runtime_roots(
    home: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
) -> tuple[Path, ...]:
    """Backward-compatible name for the bounded marker-scan roots."""

    return weixin_marker_scan_roots(home, environ=environ, system=system)


def wechat_install_candidates(
    home: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
) -> tuple[Path, ...]:
    resolved_home = Path(home if home is not None else Path.home()).expanduser().resolve()
    env = os.environ if environ is None else environ
    selected = current_system(system)
    if selected == "Darwin":
        return (
            Path("/Applications/WeChat.app"),
            Path("/Applications/微信.app"),
            resolved_home / "Applications" / "WeChat.app",
            resolved_home / "Applications" / "微信.app",
        )
    if selected != "Windows":
        return ()
    bases: list[Path] = []
    for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        value = str(env.get(name) or "").strip()
        if value:
            bases.append(Path(value).expanduser())
    bases.append(resolved_home / "AppData" / "Local")
    candidates: list[Path] = []
    for base in bases:
        candidates.extend(
            [
                base / "Tencent" / "Weixin" / "Weixin.exe",
                base / "Tencent" / "WeChat" / "WeChat.exe",
                base / "Programs" / "Tencent" / "Weixin" / "Weixin.exe",
                base / "Programs" / "Tencent" / "WeChat" / "WeChat.exe",
            ]
        )
    return _dedupe_paths(candidates)


def wechat_process_running(
    *,
    system: str | None = None,
    runner=subprocess.run,
) -> bool:
    selected = current_system(system)
    try:
        if selected == "Darwin":
            return runner(
                ["pgrep", "-x", "WeChat"],
                capture_output=True,
                text=True,
                check=False,
            ).returncode == 0
        if selected == "Windows":
            proc = runner(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
            )
            output = str(proc.stdout or "").casefold()
            return proc.returncode == 0 and any(name in output for name in ("weixin.exe", "wechat.exe"))
    except (OSError, subprocess.SubprocessError):
        return False
    return False


def wechat_installed_or_running(*, system: str | None = None) -> tuple[bool, bool]:
    selected = current_system(system)
    running = wechat_process_running(system=selected)
    installed = running or any(path.exists() for path in wechat_install_candidates(system=selected))
    return installed, running


def desktop_automation_mode(system: str | None = None) -> str:
    selected = current_system(system)
    if selected == "Darwin":
        return "guarded_file_transfer_assistant"
    if selected == "Windows":
        return "user_confirmed_manual_playback"
    return "unsupported"
