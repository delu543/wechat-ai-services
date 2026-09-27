#!/usr/bin/env python3
"""Use an authorized Dajiala/Jizhile key to convert a WeChat Channels link to MP3."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path


API_URL = "https://www.dajiala.com/fbmain/monitor/v3/wxvideo"


def post_json(payload: dict) -> dict:
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json,text/plain,*/*",
            "User-Agent": "Mozilla/5.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", default=os.environ.get("DAJIALA_KEY") or os.environ.get("JZL_KEY") or "", help="Dajiala/Jizhile API key")
    parser.add_argument("--verifycode", default="")
    parser.add_argument("--link", default="", help="WeChat Channels share link or short id")
    parser.add_argument("--export-id", default="", help="Weixin dynamic exportId, e.g. export/UzFf...")
    parser.add_argument("--object-id", default="")
    parser.add_argument("--object-nonce-id", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--metadata", default="")
    args = parser.parse_args()
    if not args.key:
        raise SystemExit("Provide --key or set DAJIALA_KEY/JZL_KEY.")

    object_id = args.object_id
    object_nonce_id = args.object_nonce_id
    metadata: dict[str, object] = {}

    if not object_id:
        if args.export_id:
            base_payload = {
                "key": args.key,
                "verifycode": args.verifycode,
                "type": 7,
                "export_id": args.export_id,
            }
            base = post_json(base_payload)
            metadata["type7"] = base
            data = base.get("data") if isinstance(base.get("data"), dict) else base
            if base.get("code") not in (0, "0"):
                raise SystemExit(f"type=7 failed: {base}")
            object_id = str(data.get("object_id") or data.get("objectId") or "")
            object_nonce_id = str(
                data.get("object_nonce_id") or data.get("objectNonceId") or object_nonce_id or ""
            )
            if not object_id:
                raise SystemExit(f"type=7 returned no object_id: {base}")
        elif args.link:
            base_payload = {
                "key": args.key,
                "verifycode": args.verifycode,
                "type": 12,
                "feed_info": args.link,
            }
            base = post_json(base_payload)
            metadata["type12"] = base
            data = base.get("data") if isinstance(base.get("data"), dict) else base
            if base.get("code") not in (0, "0"):
                raise SystemExit(f"type=12 failed: {base}")
            object_id = str(data.get("object_id") or data.get("objectId") or "")
            object_nonce_id = str(
                data.get("object_nonce_id") or data.get("objectNonceId") or object_nonce_id or ""
            )
            if not object_id:
                raise SystemExit(f"type=12 returned no object_id: {base}")
        else:
            raise SystemExit("Provide --link, --export-id, or --object-id.")

    media_payload = {
        "key": args.key,
        "verifycode": args.verifycode,
        "type": 3,
        "object_id": object_id,
    }
    if object_nonce_id:
        media_payload["object_nonce_id"] = object_nonce_id
    media = post_json(media_payload)
    metadata["type3"] = media
    data = media.get("data") if isinstance(media.get("data"), dict) else media
    if media.get("code") not in (0, "0"):
        raise SystemExit(f"type=3 failed: {media}")
    media_url = data.get("download_url") or data.get("play_url") or data.get("url")
    if not media_url:
        raise SystemExit(f"No media URL in type=3 response: {media}")

    metadata_path = Path(args.metadata).expanduser().resolve() if args.metadata else Path(args.output).with_suffix(".json")
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    converter = Path(__file__).with_name("media_url_to_mp3.py")
    subprocess.run(
        [sys.executable, str(converter), str(media_url), "--output", args.output],
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
