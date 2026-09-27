#!/usr/bin/env python3
"""Extract media URLs from HAR/JSON/text artifacts and optionally convert to MP3."""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
import os
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any


MEDIA_KEYS = {
    "raw_url",
    "download_url",
    "videoUrl",
    "video_url",
    "play_url",
    "playable_url",
    "renderReplayUrl",
    "renderReplayHlsUrl",
    "replayUrl",
    "replayHlsUrl",
    "hlsUrl",
    "hls_url",
    "url",
    "src",
}
MEDIA_PATTERN = re.compile(
    r"https?://[^\s\"'<>\\]+?(?:\.m3u8|\.mp4|\.mp3|\.m4a|\.aac|\.wav|\.ogg|\.opus|\.webm|\.ts|stodownload|snsvideodownload|snscosdownload)[^\s\"'<>\\]*",
    re.I,
)
ENCODED_MEDIA_PATTERN = re.compile(
    r"https?%3A%2F%2F[^\s\"'<>\\]+?(?:\\.m3u8|\\.mp4|\\.mp3|\\.m4a|\\.aac|\\.wav|\\.ogg|\\.opus|\\.webm|\\.ts|stodownload|snsvideodownload|snscosdownload)[^\s\"'<>\\]*",
    re.I,
)


def decode_har_content(content: dict[str, Any]) -> str:
    text = content.get("text")
    if not isinstance(text, str):
        return ""
    if content.get("encoding") == "base64":
        try:
            return base64.b64decode(text).decode("utf-8", "replace")
        except Exception:
            return ""
    return text


def walk(value: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        if "log" in value and isinstance(value["log"], dict):
            entries = value["log"].get("entries") or []
            for entry in entries:
                request = entry.get("request") or {}
                response = entry.get("response") or {}
                if isinstance(request.get("url"), str):
                    urls.append(request["url"])
                urls.extend(walk(request))
                urls.extend(walk(response))
                content_text = decode_har_content(response.get("content") or {})
                urls.extend(extract_from_text(content_text))
        for key, item in value.items():
            if key in MEDIA_KEYS and isinstance(item, str):
                urls.append(item)
            urls.extend(walk(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(walk(item))
    elif isinstance(value, str):
        urls.extend(extract_from_text(value))
    return urls


def extract_from_text(text: str) -> list[str]:
    if not text:
        return []
    urls: list[str] = []
    for variant in text_variants(text):
        urls.extend(match.group(0) for match in MEDIA_PATTERN.finditer(variant))
        urls.extend(urllib.parse.unquote(match.group(0)) for match in ENCODED_MEDIA_PATTERN.finditer(variant))
    return [clean_url(url) for url in urls]


def text_variants(text: str) -> list[str]:
    variants = [text]
    html_decoded = html.unescape(text)
    if html_decoded != text:
        variants.append(html_decoded)

    for value in list(variants):
        slash_decoded = (
            value.replace("\\/", "/")
            .replace("\\u002F", "/")
            .replace("\\u002f", "/")
            .replace("\\u003A", ":")
            .replace("\\u003a", ":")
            .replace("\\u0026", "&")
            .replace("\\u0026amp;", "&")
        )
        if slash_decoded != value:
            variants.append(slash_decoded)

    for value in list(variants):
        decoded = value
        for _ in range(2):
            next_decoded = urllib.parse.unquote(decoded)
            if next_decoded == decoded:
                break
            decoded = next_decoded
        if decoded != value:
            variants.append(decoded)

    unique: list[str] = []
    seen = set()
    for value in variants:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def clean_url(url: str) -> str:
    cleaned = html.unescape(url).replace("\\/", "/")
    cleaned = (
        cleaned.replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\u003A", ":")
        .replace("\\u003a", ":")
        .replace("\\u0026", "&")
    )
    return cleaned


def score(url: str) -> int:
    lower = url.lower().split("?", 1)[0]
    if lower.endswith((".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus")):
        return 0
    if lower.endswith(".m3u8"):
        return 1
    if lower.endswith((".mp4", ".webm")) or any(
        marker in lower for marker in ("stodownload", "snsvideodownload", "snscosdownload")
    ):
        return 2
    return 3


def find_ffmpeg() -> str:
    env = os.environ.get("FFMPEG")
    if env and Path(env).exists():
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    root = Path(__file__).resolve().parents[2]
    candidates = sorted(
        [
            *(root / "work" / "venv" / "lib").glob(
                "python*/site-packages/imageio_ffmpeg/binaries/ffmpeg-*"
            ),
            *(root / "work" / "venv" / "Lib" / "site-packages").glob(
                "imageio_ffmpeg/binaries/ffmpeg-*"
            ),
        ]
    )
    if candidates:
        return str(candidates[0])
    raise SystemExit("ffmpeg not found. Set FFMPEG=/path/to/ffmpeg.")


def redact_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?<redacted>" if parsed.query else url
    return url


def probe_url(url: str) -> int:
    ffmpeg = find_ffmpeg()
    proc = subprocess.run([ffmpeg, "-hide_banner", "-i", url], text=True, capture_output=True, timeout=30)
    stdout = (proc.stdout or "").replace(url, "<redacted-url>")
    stderr = (proc.stderr or "").replace(url, "<redacted-url>")
    payload = {
        "url": redact_url(url),
        "returncode": proc.returncode,
        "audio": "Audio:" in f"{stdout}\n{stderr}",
        "video": "Video:" in f"{stdout}\n{stderr}",
        "stderr_tail": stderr[-1600:],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["audio"] or payload["video"] else 2


def convert_first_working(urls: list[str], output: str) -> str:
    converter = Path(__file__).with_name("media_url_to_mp3.py")
    last_error: subprocess.CalledProcessError | None = None
    for url in urls:
        try:
            subprocess.run(
                [sys.executable, str(converter), url, "--output", output],
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            last_error = exc
            out = Path(output).expanduser()
            if out.exists() and out.stat().st_size == 0:
                out.unlink()
            continue
        out = Path(output).expanduser()
        if out.exists() and out.stat().st_size > 0:
            return url
    if last_error is not None:
        raise last_error
    raise SystemExit("No media URLs found.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", nargs="?", help="HAR, JSON, or text file")
    parser.add_argument("--output", default="", help="If set, convert the best URL to this MP3")
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--probe-only-url", default="", help="Probe one URL with ffmpeg and print redacted JSON.")
    args = parser.parse_args()

    if args.probe_only_url:
        return probe_url(args.probe_only_url)

    if not args.artifact:
        raise SystemExit("artifact is required unless --probe-only-url is used.")

    artifact = Path(args.artifact).expanduser().resolve()
    raw = artifact.read_text(encoding="utf-8", errors="replace")
    try:
        payload = json.loads(raw)
        urls = walk(payload)
    except Exception:
        urls = extract_from_text(raw)

    unique: list[str] = []
    seen = set()
    for url in urls:
        cleaned = url.strip().strip('",')
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        lower_cleaned = cleaned.lower()
        if MEDIA_PATTERN.search(cleaned) or lower_cleaned.split("?", 1)[0].endswith(
            (".m3u8", ".mp4", ".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".webm", ".ts")
        ) or any(marker in lower_cleaned for marker in ("stodownload", "snsvideodownload", "snscosdownload")):
            unique.append(cleaned)
    unique.sort(key=score)

    if not unique:
        raise SystemExit("No media URLs found.")
    for idx, url in enumerate(unique, 1):
        print(f"{idx}. {redact_url(url)}")

    if args.output and not args.list_only:
        selected = convert_first_working(unique, args.output)
        print(f"Selected media URL: {redact_url(selected)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
