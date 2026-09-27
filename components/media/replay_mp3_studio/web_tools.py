from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT


def _runtime_root(root: Path | None = None) -> Path:
    return (root or PROJECT_ROOT).expanduser().resolve()


def venv_python_candidates(root: Path | None = None) -> tuple[Path, ...]:
    venv = _runtime_root(root) / "work" / "venv"
    return (venv / "bin" / "python", venv / "Scripts" / "python.exe")


def yt_dlp_command(root: Path | None = None) -> list[str]:
    configured = os.environ.get("YT_DLP", "").strip()
    if configured and Path(configured).expanduser().is_file():
        return [str(Path(configured).expanduser().resolve())]
    runtime = _runtime_root(root)
    candidates = (
        runtime / "work" / "venv" / "bin" / "yt-dlp",
        runtime / "work" / "venv" / "Scripts" / "yt-dlp.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return [str(candidate)]
    for python in venv_python_candidates(runtime):
        if python.is_file():
            return [str(python), "-m", "yt_dlp"]
    system = shutil.which("yt-dlp")
    if system:
        return [system]
    return [sys.executable, "-m", "yt_dlp"]


def yt_dlp_version(root: Path | None = None) -> str:
    try:
        proc = subprocess.run(
            [*yt_dlp_command(root), "--version"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip().splitlines()[0] if proc.returncode == 0 and proc.stdout.strip() else ""


def javascript_runtime(root: Path | None = None) -> tuple[str, str]:
    runtime = _runtime_root(root)
    configured_deno = os.environ.get("DENO", "").strip()
    configured_node = os.environ.get("NODE", "").strip()
    candidates = (
        ("deno", Path(configured_deno).expanduser() if configured_deno else None),
        ("deno", runtime / "work" / "venv" / "bin" / "deno"),
        ("deno", runtime / "work" / "venv" / "Scripts" / "deno.exe"),
    )
    for kind, candidate in candidates:
        if candidate is not None and candidate.is_file():
            return kind, str(candidate.resolve())
    system_deno = shutil.which("deno")
    if system_deno:
        return "deno", system_deno
    if configured_node and Path(configured_node).expanduser().is_file():
        return "node", str(Path(configured_node).expanduser().resolve())
    system_node = shutil.which("node")
    if system_node:
        return "node", system_node
    return "", ""


def javascript_runtime_arguments(root: Path | None = None) -> list[str]:
    kind, executable = javascript_runtime(root)
    return ["--js-runtimes", f"{kind}:{executable}"] if kind and executable else []


def web_tools_status(root: Path | None = None) -> dict[str, Any]:
    version = yt_dlp_version(root)
    js_kind, js_path = javascript_runtime(root)
    return {
        "yt_dlp_ready": bool(version),
        "yt_dlp_version": version,
        "javascript_runtime_ready": bool(js_kind and js_path),
        "javascript_runtime": js_kind,
    }
