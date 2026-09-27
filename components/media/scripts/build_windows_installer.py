#!/usr/bin/env python3
"""Build the deterministic self-contained Windows PowerShell installer."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scripts" / "install-windows.template.ps1"
OUTPUT = ROOT / "install-windows.ps1"
BUNDLE_ROOT = "weixin-replay-to-mp3-bundle"
INCLUDE_FILES = (
    "AGENTS.md",
    "LICENSE",
    "PRIVACY.md",
    "README.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "VERSION",
    "main.py",
    "requirements-macos.txt",
    "requirements-windows.txt",
    "scripts/bootstrap.py",
    "weixin_replay_cli.py",
)
INCLUDE_DIRS = (
    "outputs",
    "portable_skill",
    "replay_mp3_studio",
    "tools",
    "video-audio-extractor",
)
IGNORED_NAMES = {".DS_Store", "__pycache__"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_files() -> list[tuple[str, bytes]]:
    selected: dict[str, bytes] = {}
    for relative in INCLUDE_FILES:
        path = ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"required bundle file is missing or unsafe: {relative}")
        selected[relative] = path.read_bytes()
    for directory in INCLUDE_DIRS:
        root = ROOT / directory
        if not root.is_dir() or root.is_symlink():
            raise RuntimeError(f"required bundle directory is missing or unsafe: {directory}")
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative_path = path.relative_to(ROOT)
            if path.is_symlink() or any(part in IGNORED_NAMES for part in relative_path.parts):
                continue
            if path.suffix.lower() in IGNORED_SUFFIXES:
                continue
            relative = relative_path.as_posix()
            selected[relative] = path.read_bytes()
    return sorted(selected.items())


def zip_entry(name: str, data: bytes) -> tuple[zipfile.ZipInfo, bytes]:
    info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info, data


def build_bundle() -> tuple[bytes, dict[str, object]]:
    files = source_files()
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    manifest: dict[str, object] = {
        "format": 1,
        "version": version,
        "root": BUNDLE_ROOT,
        "files": [
            {"path": path, "bytes": len(data), "sha256": sha256(data)}
            for path, data in files
        ],
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode(
        "ascii"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        entries = [("bundle-manifest.json", manifest_bytes), *files]
        for relative, data in entries:
            info, payload = zip_entry(f"{BUNDLE_ROOT}/{relative}", data)
            archive.writestr(info, payload, compress_type=zipfile.ZIP_STORED)
    return buffer.getvalue(), manifest


def render_installer() -> tuple[bytes, dict[str, object]]:
    bundle, manifest = build_bundle()
    template = TEMPLATE.read_text(encoding="ascii")
    encoded_bundle = base64.b64encode(bundle).decode("ascii")
    replacements = {
        "__EMBEDDED_SOURCE_VERSION__": str(manifest["version"]),
        "__EMBEDDED_SOURCE_SHA256__": sha256(bundle),
        "__EMBEDDED_SOURCE_BYTES__": str(len(bundle)),
        "__EMBEDDED_SOURCE_BASE64__": "\n".join(
            encoded_bundle[offset : offset + 120] for offset in range(0, len(encoded_bundle), 120)
        ),
    }
    for marker, value in replacements.items():
        if template.count(marker) != 1:
            raise RuntimeError(f"template marker must appear exactly once: {marker}")
        template = template.replace(marker, value)
    output = template.encode("ascii")
    metadata = {
        "version": manifest["version"],
        "bundle_bytes": len(bundle),
        "bundle_sha256": sha256(bundle),
        "installer_bytes": len(output),
        "installer_sha256": sha256(output),
        "source_file_count": len(manifest["files"]),
    }
    return output, metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed installer is stale")
    parser.add_argument("--metadata", action="store_true", help="print deterministic metadata")
    args = parser.parse_args()
    rendered, metadata = render_installer()
    if args.check:
        current = OUTPUT.read_bytes() if OUTPUT.is_file() else b""
        if current != rendered:
            print(json.dumps({"status": "stale", **metadata}, indent=2), file=sys.stderr)
            return 1
    else:
        temporary = OUTPUT.with_suffix(".ps1.tmp")
        temporary.write_bytes(rendered)
        os.replace(temporary, OUTPUT)
    if args.metadata or args.check:
        print(json.dumps({"status": "current", **metadata}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
