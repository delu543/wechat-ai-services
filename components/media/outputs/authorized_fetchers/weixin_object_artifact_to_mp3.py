#!/usr/bin/env python3
"""Convert Weixin objectId/objectNonceId artifacts to MP3 with authorized APIs."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


OBJECT_ID_KEYS = {"objectid", "object_id"}
OBJECT_NONCE_KEYS = {"objectnonceid", "object_nonce_id", "objectnonce", "object_nonce"}
V2_NAME_KEYS = {"v2name", "v2_name", "username"}
EXPORT_ID_KEYS = {"exportid", "export_id", "dynamicexportid", "dynamic_export_id"}

OBJECT_ID_RE = re.compile(
    r"(?:object[_-]?[iI]d|objectId)[\"'\s:=<>/]+(?:<!\[CDATA\[)?([0-9]{8,30})",
    re.I,
)
OBJECT_NONCE_RE = re.compile(
    r"(?:object[_-]?[nN]once[_-]?[iI]d|objectNonceId)[\"'\s:=<>/]+(?:<!\[CDATA\[)?([0-9A-Za-z_:-]{8,80})",
    re.I,
)
CSV_PAIR_RE = re.compile(r"\b([0-9]{8,30}),([0-9A-Za-z_:-]{8,80})\b")
BASE64_RE = re.compile(r"\b[A-Za-z0-9+/]{24,}={0,2}\b")
EXPORT_ID_RE = re.compile(r"\bexport/[0-9A-Za-z_-]{20,120}\b")


def normalize_key(key: str) -> str:
    return key.replace("-", "_").lower()


def walk_json(value: Any) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    if isinstance(value, dict):
        current: dict[str, str] = {}
        for key, item in value.items():
            normalized = normalize_key(str(key))
            if isinstance(item, (str, int)):
                text = str(item)
                if normalized in OBJECT_ID_KEYS:
                    current["object_id"] = text
                elif normalized in OBJECT_NONCE_KEYS:
                    current["object_nonce_id"] = text
                elif normalized in V2_NAME_KEYS:
                    current["v2_name"] = text
                elif normalized in EXPORT_ID_KEYS:
                    current["export_id"] = text
            pairs.extend(walk_json(item))
        if current:
            pairs.append(current)
    elif isinstance(value, list):
        for item in value:
            pairs.extend(walk_json(item))
    elif isinstance(value, str):
        pairs.extend(extract_from_text(value))
    return pairs


def extract_from_text(text: str) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    ids = OBJECT_ID_RE.findall(text)
    nonces = OBJECT_NONCE_RE.findall(text)
    if ids:
        if nonces:
            for idx, object_id in enumerate(ids):
                pairs.append(
                    {
                        "object_id": object_id,
                        "object_nonce_id": nonces[min(idx, len(nonces) - 1)],
                    }
                )
        else:
            pairs.extend({"object_id": object_id} for object_id in ids)

    for object_id, object_nonce_id in CSV_PAIR_RE.findall(text):
        pairs.append({"object_id": object_id, "object_nonce_id": object_nonce_id})

    for export_id in EXPORT_ID_RE.findall(text):
        pairs.append({"export_id": export_id})

    for token in BASE64_RE.findall(text):
        try:
            decoded = base64.b64decode(token + "=" * (-len(token) % 4), validate=False)
            decoded_text = decoded.decode("utf-8", "ignore")
        except Exception:
            continue
        if decoded_text and decoded_text != text:
            pairs.extend(extract_from_text(decoded_text))
    return pairs


def unique_pairs(pairs: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen = set()
    for pair in pairs:
        object_id = str(pair.get("object_id") or "").strip()
        object_nonce_id = str(pair.get("object_nonce_id") or "").strip()
        v2_name = str(pair.get("v2_name") or "").strip()
        export_id = str(pair.get("export_id") or "").strip()
        if not object_id and not v2_name and not export_id:
            continue
        key = (object_id, object_nonce_id, v2_name, export_id)
        if key in seen:
            continue
        seen.add(key)
        cleaned = {}
        if object_id:
            cleaned["object_id"] = object_id
        if object_nonce_id:
            cleaned["object_nonce_id"] = object_nonce_id
        if v2_name:
            cleaned["v2_name"] = v2_name
        if export_id:
            cleaned["export_id"] = export_id
        merged.append(cleaned)
    return merged


def extract_pairs(path: Path) -> list[dict[str, str]]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    pairs = extract_from_text(raw)
    try:
        pairs.extend(walk_json(json.loads(raw)))
    except Exception:
        pass
    return unique_pairs(pairs)


def env_first(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def try_direct_media(artifact: Path, output: Path) -> bool:
    extractor = Path(__file__).with_name("extract_media_from_artifact.py")
    result = subprocess.run(
        [sys.executable, str(extractor), str(artifact), "--output", str(output)],
        text=True,
    )
    return result.returncode == 0


def run_provider(args: argparse.Namespace, pair: dict[str, str], output: Path) -> None:
    provider = args.provider
    wxshares_key = args.wxshares_key or env_first("WXSHARES_KEY")
    dajiala_key = args.dajiala_key or env_first("DAJIALA_KEY", "JZL_KEY")

    if provider == "auto":
        if wxshares_key and pair.get("object_nonce_id"):
            provider = "wxshares"
        elif dajiala_key and (pair.get("object_id") or pair.get("export_id")):
            provider = "dajiala"
        else:
            raise SystemExit(
                "Found Weixin identifiers but no usable authorized key. "
                "Set WXSHARES_KEY, DAJIALA_KEY, or pass --provider/--key options."
            )

    if provider == "wxshares":
        if not wxshares_key:
            raise SystemExit("WXSHARES_KEY or --wxshares-key is required.")
        if not pair.get("object_id") or not pair.get("object_nonce_id"):
            raise SystemExit("wxshares requires both object_id and object_nonce_id.")
        script = Path(__file__).with_name("weixin_wxshares_to_mp3.py")
        subprocess.run(
            [
                sys.executable,
                str(script),
                "--key",
                wxshares_key,
                "--object-id",
                pair["object_id"],
                "--object-nonce-id",
                pair["object_nonce_id"],
                "--output",
                str(output),
            ],
            check=True,
        )
        return

    if provider == "dajiala":
        if not dajiala_key:
            raise SystemExit("DAJIALA_KEY/JZL_KEY or --dajiala-key is required.")
        if not pair.get("object_id") and not pair.get("export_id"):
            raise SystemExit("Dajiala/Jizhile requires object_id or export_id.")
        script = Path(__file__).with_name("weixin_dajiala_to_mp3.py")
        cmd = [
            sys.executable,
            str(script),
            "--key",
            dajiala_key,
            "--output",
            str(output),
        ]
        if pair.get("object_id"):
            cmd.extend(["--object-id", pair["object_id"]])
        elif pair.get("export_id"):
            cmd.extend(["--export-id", pair["export_id"]])
        if args.verifycode:
            cmd.extend(["--verifycode", args.verifycode])
        if pair.get("object_nonce_id"):
            cmd.extend(["--object-nonce-id", pair["object_nonce_id"]])
        subprocess.run(cmd, check=True)
        return

    raise SystemExit(f"Unsupported provider: {provider}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", help="Authorized XML/JSON/HAR/text artifact")
    parser.add_argument("--output", default="")
    parser.add_argument("--provider", choices=["auto", "wxshares", "dajiala"], default="auto")
    parser.add_argument("--wxshares-key", default="")
    parser.add_argument("--dajiala-key", default="")
    parser.add_argument("--verifycode", default="")
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--skip-direct-media", action="store_true")
    args = parser.parse_args()

    artifact = Path(args.artifact).expanduser().resolve()
    if args.list_only:
        pairs = extract_pairs(artifact)
        if not pairs:
            raise SystemExit("No objectId/objectNonceId/exportId identifiers found.")
        for idx, pair in enumerate(pairs, 1):
            print(f"{idx}. {json.dumps(pair, ensure_ascii=False)}")
        return 0

    if not args.output:
        raise SystemExit("--output is required unless --list-only is used.")
    output = Path(args.output).expanduser().resolve()

    if not args.skip_direct_media and try_direct_media(artifact, output):
        return 0

    pairs = extract_pairs(artifact)
    if not pairs:
        raise SystemExit("No media URL or objectId/objectNonceId/exportId identifier found.")
    run_provider(args, pairs[0], output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
