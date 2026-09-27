#!/usr/bin/env python3
"""Use an authorized JustOne API token to convert a WeChat Channels link to MP3."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://api.justoneapi.com"
MEDIA_HINT_KEYS = (
    "download_url",
    "downloadUrl",
    "play_url",
    "playUrl",
    "video_url",
    "videoUrl",
    "url",
    "src",
)


def request_json(base_url: str, path: str, params: dict[str, str]) -> dict[str, Any]:
    url = base_url.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json,text/plain,*/*",
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw)


def response_ok(payload: dict[str, Any]) -> bool:
    code = payload.get("code")
    return code in (0, "0", None) and not str(payload.get("error", "")).strip()


def walk(value: Any) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    if isinstance(value, dict):
        current: dict[str, str] = {}
        for key, item in value.items():
            lower = str(key).replace("_", "").lower()
            if isinstance(item, (str, int)):
                text = str(item)
                if lower in {"objectid", "object"} and text.isdigit():
                    current["object_id"] = text
                elif lower in {"objectnonceid", "objectnonce", "nonceid"}:
                    current["object_nonce_id"] = text
                elif lower in {"exportid", "dynamicexportid"}:
                    current["export_id"] = text
            found.extend(walk(item))
        if current:
            found.append(current)
    elif isinstance(value, list):
        for item in value:
            found.extend(walk(item))
    return found


def merge_identifier_pairs(pairs: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen = set()
    for pair in pairs:
        object_id = str(pair.get("object_id") or "").strip()
        object_nonce_id = str(pair.get("object_nonce_id") or "").strip()
        export_id = str(pair.get("export_id") or "").strip()
        key = (object_id, object_nonce_id, export_id)
        if not any(key) or key in seen:
            continue
        seen.add(key)
        clean: dict[str, str] = {}
        if object_id:
            clean["object_id"] = object_id
        if object_nonce_id:
            clean["object_nonce_id"] = object_nonce_id
        if export_id:
            clean["export_id"] = export_id
        out.append(clean)
    return out


def find_media_url(value: Any) -> str:
    if isinstance(value, dict):
        for key in MEDIA_HINT_KEYS:
            item = value.get(key)
            if isinstance(item, str) and item.startswith("http") and looks_media_like(item):
                return item
        for item in value.values():
            found = find_media_url(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_media_url(item)
            if found:
                return found
    elif isinstance(value, str) and value.startswith("http") and looks_media_like(value):
        return value
    return ""


def looks_media_like(url: str) -> bool:
    lower = url.lower()
    return any(token in lower for token in (".mp4", ".m3u8", ".m4a", ".mp3", ".aac", "stodownload", "snscosdownload"))


def first_pair(payload: dict[str, Any]) -> dict[str, str]:
    pairs = merge_identifier_pairs(walk(payload))
    if not pairs:
        raise SystemExit("JustOne response did not include objectId/objectNonceId.")
    return pairs[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", default=os.environ.get("JUSTONE_API_KEY") or os.environ.get("JUSTONE_TOKEN") or "")
    parser.add_argument("--base-url", default=os.environ.get("JUSTONE_BASE_URL") or DEFAULT_BASE_URL)
    parser.add_argument("--link", default="")
    parser.add_argument("--export-id", default="")
    parser.add_argument("--object-id", default="")
    parser.add_argument("--object-nonce-id", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--metadata", default="")
    args = parser.parse_args()

    if not args.token:
        raise SystemExit("Provide --token or set JUSTONE_API_KEY/JUSTONE_TOKEN.")
    if not any((args.link, args.export_id, args.object_id)):
        raise SystemExit("Provide --link, --export-id, or --object-id.")

    metadata: dict[str, Any] = {}
    object_id = args.object_id
    object_nonce_id = args.object_nonce_id

    if not object_id and args.link:
        basic = request_json(
            args.base_url,
            "/api/weixin-channels/get-video-basic-info/v1",
            {"token": args.token, "feedInfo": args.link},
        )
        metadata["basic_info"] = basic
        if response_ok(basic):
            pair = first_pair(basic)
            object_id = pair.get("object_id", "")
            object_nonce_id = object_nonce_id or pair.get("object_nonce_id", "")

    if not object_id and args.export_id:
        converted = request_json(
            args.base_url,
            "/api/weixin-channels/convert-export-id/v1",
            {"token": args.token, "exportId": args.export_id},
        )
        metadata["export_id_conversion"] = converted
        if not response_ok(converted):
            raise SystemExit(f"JustOne exportId conversion failed with code {converted.get('code')}.")
        pair = first_pair(converted)
        object_id = pair.get("object_id", "")
        object_nonce_id = object_nonce_id or pair.get("object_nonce_id", "")

    if not object_id:
        raise SystemExit("JustOne route could not resolve an objectId.")

    params = {"token": args.token, "objectId": object_id}
    if object_nonce_id:
        params["objectNonceId"] = object_nonce_id
    download = request_json(args.base_url, "/api/weixin-channels/get-video-download-url/v1", params)
    metadata["download_url"] = download
    if not response_ok(download):
        raise SystemExit(f"JustOne download URL request failed with code {download.get('code')}.")

    metadata_path = Path(args.metadata).expanduser().resolve() if args.metadata else Path(args.output).with_suffix(".justone.json")
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    media_url = find_media_url(download)
    if not media_url:
        raise SystemExit(f"No media URL found in JustOne response. Saved: {metadata_path}")

    converter = Path(__file__).with_name("media_url_to_mp3.py")
    subprocess.run([sys.executable, str(converter), media_url, "--output", args.output], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
