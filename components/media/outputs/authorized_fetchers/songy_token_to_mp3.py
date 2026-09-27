#!/usr/bin/env python3
"""Use an authorized Songy Bearer token or contents JSON to convert course media to MP3."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any


API_BASE = "https://bandu-api.songy.info"
MEDIA_KEYS = {"raw_url", "url", "audio_url", "video_url", "play_url"}
MEDIA_EXTS = (
    ".m3u8",
    ".mp3",
    ".m4a",
    ".aac",
    ".wav",
    ".ogg",
    ".opus",
    ".weba",
    ".mp4",
    ".mov",
    ".webm",
)


def fetch_contents(course_id: str, token: str) -> Any:
    req = urllib.request.Request(
        f"{API_BASE}/v2/courses/{course_id}/contents",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Origin": "https://webapp.songy.info",
            "Referer": "https://webapp.songy.info/",
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def walk(value: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in MEDIA_KEYS and isinstance(item, str) and item:
                urls.append(item)
            urls.extend(walk(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(walk(item))
    return urls


def score_url(url: str) -> int:
    lower = url.lower().split("?", 1)[0]
    if lower.endswith((".mp3", ".m4a", ".aac")):
        return 0
    if lower.endswith(".m3u8"):
        return 1
    if lower.endswith((".mp4", ".mov", ".webm")):
        return 2
    return 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--course-id", default="784")
    parser.add_argument("--token", default="", help="Authorized Songy Bearer token")
    parser.add_argument("--contents-json", default="", help="Saved /v2/courses/{id}/contents JSON")
    parser.add_argument("--output", required=True)
    parser.add_argument("--metadata", default="")
    args = parser.parse_args()

    if args.contents_json:
        payload = json.loads(Path(args.contents_json).read_text(encoding="utf-8"))
    else:
        if not args.token:
            raise SystemExit("Provide --token or --contents-json.")
        payload = fetch_contents(args.course_id, args.token)

    urls = []
    seen = set()
    for url in walk(payload):
        if url in seen:
            continue
        seen.add(url)
        if url.lower().split("?", 1)[0].endswith(MEDIA_EXTS) or "m3u8" in url.lower():
            urls.append(url)

    if not urls:
        raise SystemExit("No media URL found in Songy contents payload.")
    urls.sort(key=score_url)

    metadata = {
        "course_id": args.course_id,
        "selected_media_url": urls[0],
        "candidate_media_urls": urls,
        "payload": payload,
    }
    metadata_path = Path(args.metadata).expanduser().resolve() if args.metadata else Path(args.output).with_suffix(".json")
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    converter = Path(__file__).with_name("media_url_to_mp3.py")
    subprocess.run(
        [sys.executable, str(converter), urls[0], "--output", args.output],
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
