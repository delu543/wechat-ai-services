#!/usr/bin/env python3
"""Use an authorized wxshares/geek API key to convert Weixin Channels media to MP3."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path


API_URL = "https://api.wxshares.com/api/qsy/sphzy"


def fetch(payload: dict[str, str]) -> dict:
    url = API_URL + "?" + urllib.parse.urlencode(payload)
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json,text/plain,*/*",
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", required=True, help="wxshares API key")
    parser.add_argument("--object-id", required=True, help="Weixin Channels objectId")
    parser.add_argument("--object-nonce-id", required=True, help="Weixin Channels objectNonceId")
    parser.add_argument("--output", required=True)
    parser.add_argument("--metadata", default="")
    parser.add_argument("--source", default="true", help="Request source URL when supported")
    args = parser.parse_args()

    metadata_path = Path(args.metadata).expanduser().resolve() if args.metadata else Path(args.output).with_suffix(".wxshares.json")
    metadata_path.parent.mkdir(parents=True, exist_ok=True)

    response = fetch(
        {
            "key": args.key,
            "objectId": args.object_id,
            "objectNonceId": args.object_nonce_id,
            "source": args.source,
        }
    )
    metadata_path.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")

    if response.get("code") not in (0, "0"):
        raise SystemExit(f"wxshares request failed: {response}")

    extractor = Path(__file__).with_name("extract_media_from_artifact.py")
    subprocess.run(
        [sys.executable, str(extractor), str(metadata_path), "--output", args.output],
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
