#!/usr/bin/env python3
"""User-local doctor and installer for the Codex workflow."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from replay_mp3_studio.platform_support import SUPPORTED_SYSTEMS, application_root  # noqa: E402


APP_ROOT = application_root()
RUNTIME_ROOT = APP_ROOT / "runtime"
SKILL_ROOT = Path.home() / ".codex" / "skills" / "weixin-replay-to-mp3"
MARKER = ".managed-by-weixin-replay-to-mp3"
MIN_PYTHON = (3, 10)
COPY_DIRS = ("replay_mp3_studio", "outputs", "tools", "video-audio-extractor")
COPY_FILES = (
    "weixin_replay_cli.py",
    "main.py",
    "requirements-macos.txt",
    "requirements-windows.txt",
    "VERSION",
)


def version() -> str:
    return (SOURCE_ROOT / "VERSION").read_text(encoding="utf-8").strip()


def owned(path: Path) -> bool:
    return (path / MARKER).is_file()


def installed_ffmpeg() -> str:
    candidates = sorted(
        [
            *(RUNTIME_ROOT / "work" / "venv" / "lib").glob(
                "python*/site-packages/imageio_ffmpeg/binaries/ffmpeg-*"
            ),
            *(RUNTIME_ROOT / "work" / "venv" / "Lib" / "site-packages").glob(
                "imageio_ffmpeg/binaries/ffmpeg-*"
            ),
        ]
    )
    return str(candidates[0]) if candidates else ""


def installed_runtime_tool(name: str) -> str:
    venv = RUNTIME_ROOT / "work" / "venv"
    filenames = {
        "yt-dlp": (venv / "bin" / "yt-dlp", venv / "Scripts" / "yt-dlp.exe"),
        "deno": (venv / "bin" / "deno", venv / "Scripts" / "deno.exe"),
    }
    return next((str(path) for path in filenames.get(name, ()) if path.is_file()), "")


def installed_web_tools() -> dict[str, str]:
    return {
        "yt_dlp": installed_runtime_tool("yt-dlp"),
        "deno": installed_runtime_tool("deno"),
    }


def venv_python(venv: Path, system: str | None = None) -> Path:
    return (
        venv / "Scripts" / "python.exe"
        if (system or platform.system()) == "Windows"
        else venv / "bin" / "python"
    )


def requirements_path(system: str | None = None) -> Path:
    selected = system or platform.system()
    if selected == "Windows":
        return RUNTIME_ROOT / "requirements-windows.txt"
    return RUNTIME_ROOT / "requirements-macos.txt"


def run_preflight(root: Path, ffmpeg: str = "") -> dict[str, Any]:
    cli = root / "weixin_replay_cli.py"
    if not cli.is_file():
        return {"ready": False, "error": "runtime_missing"}
    env = os.environ.copy()
    if ffmpeg:
        env["FFMPEG"] = ffmpeg
    proc = subprocess.run(
        [sys.executable, str(cli), "preflight"],
        cwd=str(root),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"ready": False, "error": "preflight_output_invalid"}
    payload["exit_code"] = proc.returncode
    return payload


def doctor_payload() -> dict[str, Any]:
    runtime_installed = owned(RUNTIME_ROOT) and (RUNTIME_ROOT / "weixin_replay_cli.py").is_file()
    skill_installed = owned(SKILL_ROOT) and (SKILL_ROOT / "SKILL.md").is_file()
    root = RUNTIME_ROOT if runtime_installed else SOURCE_ROOT
    ffmpeg = installed_ffmpeg() if runtime_installed else ""
    preflight = run_preflight(root, ffmpeg)
    selected_system = platform.system()
    if selected_system not in SUPPORTED_SYSTEMS:
        state = "unsupported_platform"
    elif runtime_installed and skill_installed and preflight.get("ready"):
        state = "ready"
    else:
        state = "needs_install"
    return {
        "state": state,
        "version": version(),
        "platform": selected_system,
        "runtime_installed": runtime_installed,
        "skill_installed": skill_installed,
        "runtime_root": str(RUNTIME_ROOT),
        "skill_root": str(SKILL_ROOT),
        "preflight": preflight,
    }


def cmd_doctor(args: argparse.Namespace) -> int:
    payload = doctor_payload()
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if payload["state"] == "ready" else 2


def ensure_owned_or_new(path: Path) -> None:
    if path.exists() and any(path.iterdir()) and not owned(path):
        raise RuntimeError(f"Refusing to overwrite an unmanaged directory: {path}")
    path.mkdir(parents=True, exist_ok=True)


def copy_runtime() -> None:
    ensure_owned_or_new(RUNTIME_ROOT)
    for name in COPY_FILES:
        source = SOURCE_ROOT / name
        if not source.is_file():
            raise RuntimeError(f"Missing release file: {source}")
        shutil.copy2(source, RUNTIME_ROOT / name)
    for name in COPY_DIRS:
        source = SOURCE_ROOT / name
        if not source.is_dir():
            raise RuntimeError(f"Missing release directory: {source}")
        shutil.copytree(
            source,
            RUNTIME_ROOT / name,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
    (RUNTIME_ROOT / MARKER).write_text(version() + "\n", encoding="utf-8")


def ensure_ffmpeg(skip_deps: bool) -> str:
    env_ffmpeg = os.environ.get("FFMPEG", "")
    external_ffmpeg = env_ffmpeg if env_ffmpeg and Path(env_ffmpeg).is_file() else ""
    external_ffmpeg = external_ffmpeg or (shutil.which("ffmpeg") or "")
    existing = external_ffmpeg or installed_ffmpeg()
    web_tools = installed_web_tools()
    if existing and all(web_tools.values()):
        return existing
    if skip_deps:
        missing = [
            name
            for name, available in (
                ("ffmpeg", existing),
                ("yt-dlp", web_tools["yt_dlp"]),
                ("deno", web_tools["deno"]),
            )
            if not available
        ]
        raise RuntimeError(
            "Pinned runtime dependencies are missing and installation was skipped: "
            + ", ".join(missing)
        )
    venv = RUNTIME_ROOT / "work" / "venv"
    venv.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = venv_python(venv)
    pip_cache = APP_ROOT / "cache" / "pip"
    pip_cache.mkdir(parents=True, exist_ok=True)
    pip_env = os.environ.copy()
    pip_env["PIP_CACHE_DIR"] = str(pip_cache)
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--require-hashes",
            "-r",
            str(requirements_path()),
        ],
        check=True,
        env=pip_env,
    )
    result = external_ffmpeg or installed_ffmpeg()
    if not result:
        raise RuntimeError("Pinned ffmpeg installation completed without a usable binary.")
    web_tools = installed_web_tools()
    if not all(web_tools.values()):
        raise RuntimeError("Pinned web-link dependencies completed without usable yt-dlp and Deno tools.")
    return result


def copy_skill() -> None:
    source = SOURCE_ROOT / "portable_skill" / "weixin-replay-to-mp3"
    if not source.is_dir():
        raise RuntimeError(f"Missing portable skill: {source}")
    ensure_owned_or_new(SKILL_ROOT)
    shutil.copytree(
        source,
        SKILL_ROOT,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )
    (SKILL_ROOT / MARKER).write_text(version() + "\n", encoding="utf-8")


def tighten_permissions() -> None:
    if platform.system() == "Windows":
        # The default root is inside the current user's LocalAppData profile
        # and inherits that account's NTFS ACL. POSIX modes are not an ACL on
        # Windows, so do not pretend chmod provides the same boundary.
        return
    for root in (APP_ROOT, RUNTIME_ROOT, RUNTIME_ROOT / "work"):
        if root.exists():
            root.chmod(0o700)
    for path in (RUNTIME_ROOT / MARKER, SKILL_ROOT / MARKER):
        if path.exists():
            path.chmod(0o600)


def cmd_install(args: argparse.Namespace) -> int:
    if platform.system() not in SUPPORTED_SYSTEMS:
        raise RuntimeError("This release supports macOS and Windows local runtimes only.")
    if sys.version_info < MIN_PYTHON:
        raise RuntimeError("Python 3.10 or newer is required for cross-platform web links.")
    copy_runtime()
    ffmpeg = ensure_ffmpeg(args.skip_deps)
    copy_skill()
    tighten_permissions()
    payload = doctor_payload()
    payload["installed_ffmpeg"] = ffmpeg
    payload["installed_web_tools"] = installed_web_tools()
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if payload["state"] == "ready" else 2


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Read-only readiness check")
    doctor.set_defaults(func=cmd_doctor)
    install = commands.add_parser("install", help="Install user-local runtime and Skill")
    install.add_argument("--skip-deps", action="store_true")
    install.set_defaults(func=cmd_install)
    return parser


def main() -> int:
    args = make_parser().parse_args()
    try:
        return int(args.func(args))
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
