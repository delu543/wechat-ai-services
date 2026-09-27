#!/usr/bin/env python3
"""Build the deterministic Windows acquisition bootstrap from the installer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scripts" / "bootstrap-windows.template.ps1"
INSTALLER = ROOT / "install-windows.ps1"
OUTPUT = ROOT / "bootstrap-windows.ps1"


def digest(data: bytes, algorithm: str = "sha256") -> str:
    return hashlib.new(algorithm, data).hexdigest()


def git_blob_sha(data: bytes) -> str:
    return digest(f"blob {len(data)}\0".encode("ascii") + data, "sha1")


def render_bootstrap() -> tuple[bytes, dict[str, object]]:
    installer = INSTALLER.read_bytes()
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    template = TEMPLATE.read_text(encoding="ascii")
    replacements = {
        "__INSTALLER_VERSION__": version,
        "__INSTALLER_BYTES__": str(len(installer)),
        "__INSTALLER_SHA256__": digest(installer),
        "__INSTALLER_GIT_BLOB_SHA__": git_blob_sha(installer),
    }
    for marker, value in replacements.items():
        if template.count(marker) < 1:
            raise RuntimeError(f"template marker is missing: {marker}")
        template = template.replace(marker, value)
    output = template.encode("ascii")
    metadata = {
        "version": version,
        "installer_bytes": len(installer),
        "installer_sha256": digest(installer),
        "installer_git_blob_sha": git_blob_sha(installer),
        "bootstrap_bytes": len(output),
        "bootstrap_sha256": digest(output),
    }
    return output, metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed bootstrap is stale")
    parser.add_argument("--metadata", action="store_true", help="print deterministic metadata")
    args = parser.parse_args()
    rendered, metadata = render_bootstrap()
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
