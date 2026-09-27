#!/usr/bin/env python3
"""Use an authorized Apify token to resolve a WeChat Channels exportId and convert to MP3."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ACTOR = "sian.agency~wechat-channels-scraper"


def run_actor(token: str, payload: dict[str, Any]) -> Any:
    query = urllib.parse.urlencode({"token": token})
    url = f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items?{query}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"Apify request failed: HTTP {exc.code} {exc.reason}: {body}") from exc


def find_video_url(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("videoUrl", "downloadUrl", "download_url", "playUrl", "url"):
            item = value.get(key)
            if isinstance(item, str) and item.startswith("http"):
                return item
        for item in value.values():
            found = find_video_url(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_video_url(item)
            if found:
                return found
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", default=os.environ.get("APIFY_TOKEN", ""), help="Apify API token")
    parser.add_argument(
        "--export-id",
        required=True,
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--metadata", default="")
    args = parser.parse_args()
    if not args.token:
        raise SystemExit("Provide --token or set APIFY_TOKEN.")

    payload = {"operation": "convertExportId", "exportId": args.export_id}
    result = run_actor(args.token, payload)
    metadata_path = Path(args.metadata).expanduser().resolve() if args.metadata else Path(args.output).with_suffix(".apify.json")
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    media_url = find_video_url(result)
    if not media_url:
        raise SystemExit(f"No videoUrl found in Apify response. Saved: {metadata_path}")

    converter = Path(__file__).with_name("media_url_to_mp3.py")
    subprocess.run(
        [sys.executable, str(converter), media_url, "--output", args.output],
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
