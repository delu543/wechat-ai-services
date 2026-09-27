#!/usr/bin/env python3
"""Render the literal, checkout-independent Windows first-message capsule."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "docs" / "WINDOWS_OFFLINE_RELEASE.json"
BOOTSTRAP_TEMPLATE = ROOT / "scripts" / "bootstrap-windows-portable.template.ps1"
PROMPT_TEMPLATE = ROOT / "scripts" / "WINDOWS_FIRST_PROMPT.template.md"
BOOTSTRAP_OUTPUT = ROOT / "bootstrap-windows-portable.ps1"
PROMPT_OUTPUT = ROOT / "docs" / "WINDOWS_FIRST_PROMPT.md"


def render() -> tuple[bytes, str, dict[str, object]]:
    release = json.loads(RELEASE.read_text(encoding="utf-8"))
    bootstrap = BOOTSTRAP_TEMPLATE.read_text(encoding="ascii")
    bootstrap_values = {
        "__PORTABLE_VERSION__": release["version"],
        "__PORTABLE_ASSET_NAME__": release["asset_name"],
        "__PORTABLE_ASSET_URL__": release["url"],
        "__PORTABLE_ASSET_BYTES__": str(release["bytes"]),
        "__PORTABLE_ASSET_SHA256__": release["sha256"],
    }
    for marker, value in bootstrap_values.items():
        if bootstrap.count(marker) < 1:
            raise RuntimeError(f"bootstrap marker is missing: {marker}")
        bootstrap = bootstrap.replace(marker, str(value))
    bootstrap_bytes = bootstrap.encode("ascii")
    encoded = base64.b64encode(bootstrap_bytes).decode("ascii")
    wrapped = "\n".join(encoded[index : index + 120] for index in range(0, len(encoded), 120))
    prompt = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    prompt_values = {
        "__PORTABLE_VERSION__": release["version"],
        "__PORTABLE_ASSET_NAME__": release["asset_name"],
        "__BOOTSTRAP_BYTES__": str(len(bootstrap_bytes)),
        "__BOOTSTRAP_SHA256__": hashlib.sha256(bootstrap_bytes).hexdigest(),
        "__BOOTSTRAP_BASE64__": wrapped,
    }
    for marker, value in prompt_values.items():
        if prompt.count(marker) < 1:
            raise RuntimeError(f"prompt marker is missing: {marker}")
        prompt = prompt.replace(marker, str(value))
    metadata = {
        "version": release["version"],
        "asset": release["asset_name"],
        "asset_bytes": release["bytes"],
        "asset_sha256": release["sha256"],
        "bootstrap_bytes": len(bootstrap_bytes),
        "bootstrap_sha256": hashlib.sha256(bootstrap_bytes).hexdigest(),
    }
    return bootstrap_bytes, prompt, metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    bootstrap, prompt, metadata = render()
    if args.check:
        if BOOTSTRAP_OUTPUT.read_bytes() != bootstrap or PROMPT_OUTPUT.read_text(
            encoding="utf-8"
        ) != prompt:
            print(json.dumps({"status": "stale", **metadata}, indent=2))
            return 1
    else:
        bootstrap_tmp = BOOTSTRAP_OUTPUT.with_suffix(".ps1.tmp")
        prompt_tmp = PROMPT_OUTPUT.with_suffix(".md.tmp")
        bootstrap_tmp.write_bytes(bootstrap)
        prompt_tmp.write_text(prompt, encoding="utf-8")
        os.replace(bootstrap_tmp, BOOTSTRAP_OUTPUT)
        os.replace(prompt_tmp, PROMPT_OUTPUT)
    print(json.dumps({"status": "current", **metadata}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
