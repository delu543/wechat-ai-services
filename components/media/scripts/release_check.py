#!/usr/bin/env python3
"""Fail-closed clean-source and offline regression gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_ROOT_DIRS = {
    ".github",
    ".codex",
    "docs",
    "outputs",
    "portable_skill",
    "replay_mp3_studio",
    "scripts",
    "tests",
    "tools",
    "video-audio-extractor",
}
PRIVATE_DIRS = {
    ".git",
    ".codex",
    "build",
    "dist",
    "work",
    "library",
    "incoming",
    "reports",
    "__pycache__",
}
MEDIA_SUFFIXES = {
    ".mp3",
    ".mp4",
    ".m4a",
    ".aac",
    ".wav",
    ".mov",
    ".webm",
    ".mkv",
    ".har",
    ".part",
}
MAX_SOURCE_BYTES = 2_000_000
TEXT_SUFFIXES = {
    "",
    ".c",
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".swift",
    ".txt",
    ".yml",
    ".yaml",
}


def candidate_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if any(part in PRIVATE_DIRS for part in relative.parts):
            continue
        if path.is_symlink():
            files.append(path)
        elif path.is_file():
            files.append(path)
    return sorted(files)


def secret_patterns() -> list[tuple[str, re.Pattern[str]]]:
    return [
        ("github_token", re.compile(r"(?:ghp" + r"_|github_pat" + r"_)[A-Za-z0-9_]{20,}")),
        ("openai_key", re.compile(r"sk" + r"-[A-Za-z0-9_-]{20,}")),
        ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
        (
            "private_key",
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?" + r"PRIVATE KEY-----"),
        ),
    ]


def scan() -> dict[str, Any]:
    errors: list[str] = []
    checked = 0
    for path in candidate_files():
        relative = path.relative_to(ROOT)
        checked += 1
        if path.is_symlink():
            errors.append(f"symlink_not_allowed:{relative}")
            continue
        if relative.parts[0] not in ALLOWED_ROOT_DIRS and len(relative.parts) > 1:
            errors.append(f"unexpected_root_directory:{relative.parts[0]}")
        if path.suffix.lower() in MEDIA_SUFFIXES:
            errors.append(f"media_or_artifact_not_allowed:{relative}")
        size = path.stat().st_size
        if size > MAX_SOURCE_BYTES:
            errors.append(f"source_file_too_large:{relative}:{size}")
        if path.suffix.lower() not in TEXT_SUFFIXES:
            errors.append(f"unexpected_binary_or_suffix:{relative}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            errors.append(f"non_utf8_source:{relative}")
            continue
        developer_markers = (str(Path.home()) + "/Documents/Codex/",)
        if any(marker in text for marker in developer_markers):
            errors.append(f"absolute_developer_path:{relative}")
        for label, pattern in secret_patterns():
            if pattern.search(text):
                errors.append(f"secret_pattern:{label}:{relative}")
    required = [
        ".gitattributes",
        "AGENTS.md",
        "README.md",
        "LICENSE",
        "PRIVACY.md",
        "SECURITY.md",
        "docs/CAPABILITY_MAP.md",
        "docs/WINDOWS_FIRST_PROMPT.md",
        "docs/WINDOWS_INSTALL.md",
        "docs/WINDOWS_OFFLINE_RELEASE.json",
        "portable_skill/weixin-replay-to-mp3/SKILL.md",
        "requirements-windows.txt",
        "bootstrap-windows.ps1",
        "bootstrap-windows-portable.ps1",
        "install-windows.ps1",
        "scripts/bootstrap-windows.template.ps1",
        "scripts/bootstrap-windows-portable.template.ps1",
        "scripts/build_windows_bootstrap.py",
        "scripts/build_windows_first_prompt.py",
        "scripts/build_windows_installer.py",
        "scripts/build_windows_offline_bundle.py",
        "scripts/install-windows.template.ps1",
        "scripts/install-windows-offline.template.ps1",
        "scripts/WINDOWS_FIRST_PROMPT.template.md",
        "scripts/windows-portable.lock.json",
        "tools/install_offline_wheels.py",
        "weixin_replay_cli.py",
    ]
    for name in required:
        if not (ROOT / name).is_file():
            errors.append(f"missing_required_file:{name}")
    release_path = ROOT / "docs" / "WINDOWS_OFFLINE_RELEASE.json"
    portable_bootstrap = ROOT / "bootstrap-windows-portable.ps1"
    first_prompt = ROOT / "docs" / "WINDOWS_FIRST_PROMPT.md"
    if release_path.is_file():
        try:
            release = json.loads(release_path.read_text(encoding="utf-8"))
            current_version = (ROOT / "VERSION").read_text(encoding="ascii").strip()
            if release.get("version") != current_version:
                errors.append("windows_offline_release_version_stale")
            if int(release.get("bytes", 0)) < 50_000_000:
                errors.append("windows_offline_release_too_small_for_embedded_dependencies")
            if not re.fullmatch(r"[0-9a-f]{64}", str(release.get("sha256", ""))):
                errors.append("windows_offline_release_sha256_invalid")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            errors.append("windows_offline_release_manifest_invalid")
    if portable_bootstrap.is_file() and first_prompt.is_file():
        bootstrap_sha256 = hashlib.sha256(portable_bootstrap.read_bytes()).hexdigest()
        if bootstrap_sha256 not in first_prompt.read_text(encoding="utf-8"):
            errors.append("windows_portable_bootstrap_prompt_stale")
    return {"checked_files": checked, "errors": sorted(set(errors))}


def run_tests() -> dict[str, Any]:
    env = os.environ.copy()
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return {
        "exit_code": proc.returncode,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-20000:],
    }


def run_generated_checks() -> dict[str, Any]:
    commands = (
        [sys.executable, str(ROOT / "scripts" / "build_windows_installer.py"), "--check"],
        [sys.executable, str(ROOT / "scripts" / "build_windows_bootstrap.py"), "--check"],
        [sys.executable, str(ROOT / "scripts" / "build_windows_first_prompt.py"), "--check"],
    )
    outputs: list[str] = []
    errors: list[str] = []
    exit_code = 0
    for command in commands:
        proc = subprocess.run(
            command,
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            check=False,
        )
        outputs.append(proc.stdout)
        errors.append(proc.stderr)
        if proc.returncode != 0:
            exit_code = proc.returncode
    return {
        "exit_code": exit_code,
        "stdout_tail": "".join(outputs)[-8000:],
        "stderr_tail": "".join(errors)[-8000:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan-only", action="store_true")
    args = parser.parse_args()
    result: dict[str, Any] = {"scan": scan()}
    if not args.scan_only:
        result["generated_checks"] = run_generated_checks()
        result["tests"] = run_tests()
    failed = bool(result["scan"]["errors"]) or (
        not args.scan_only
        and (
            result["generated_checks"]["exit_code"] != 0
            or result["tests"]["exit_code"] != 0
        )
    )
    result["status"] = "failed" if failed else "passed"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
