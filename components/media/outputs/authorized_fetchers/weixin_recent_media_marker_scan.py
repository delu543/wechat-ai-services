#!/usr/bin/env python3
"""Scan recent WeChat playback-side files for media URL markers.

The scan is local-only and intentionally avoids chat/contact databases. It is
meant to reproduce the successful low-intrusion route where a playing Channels
replay exposed a temporary `stodownload` media URL inside Radium playback data.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from replay_mp3_studio.platform_support import current_system, weixin_marker_scan_roots  # noqa: E402


SCAN_ROOTS = list(weixin_marker_scan_roots())
SKIP_NAMES = {
    "account web data",
    "chat",
    "contact",
    "conversation",
    "cookies",
    "db_storage",
    "favicons",
    "history",
    "login data",
    "message",
    "msgattach",
    "session",
    "visited links",
    "web data",
}
SKIP_PREFIXES = ("chat_", "chat-", "contact_", "contact-", "message_", "message-")
MEDIA_MARKERS = (
    "finder.video.qq.com",
    "wxapp.tc.qq.com",
    "snsvideodownload",
    "stodownload",
    ".m3u8",
    ".mp4",
    ".m4a",
    "renderReplay",
    "renderReplayUrl",
    "renderReplayHlsUrl",
    "decodeKey",
    "decode_key",
    "decodekey",
    "urlToken",
    "url_token",
    "object_desc",
    "feedID",
    "objectId",
)
MEDIA_URL_HINTS = (
    "finder.video.qq.com",
    "wxapp.tc.qq.com",
    "snsvideodownload",
    "stodownload",
    ".m3u8",
    ".mp4",
    ".m4a",
    ".aac",
)
RAW_URL_RE = re.compile(rb"https?://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]{12,}")
ENCODED_URL_RE = re.compile(rb"https?%3A%2F%2F[A-Za-z0-9._~%:/?#\[\]@!$&()*+,;=%-]{12,}", re.I)


def safe_rel(path: Path) -> str:
    try:
        relative = path.expanduser().resolve().relative_to(Path.home().resolve())
    except (OSError, ValueError):
        return str(path)
    return "~/" + relative.as_posix()


def should_skip(path: Path) -> bool:
    for raw_part in path.parts:
        part = raw_part.casefold()
        if part in SKIP_NAMES or part.startswith(SKIP_PREFIXES):
            return True
    return False


def iter_recent_files(since: float, min_size: int, max_size: int) -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if not should_skip(Path(dirpath) / name)]
            for name in filenames:
                path = Path(dirpath) / name
                if should_skip(path):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_mtime < since or stat.st_size < min_size or stat.st_size > max_size:
                    continue
                files.append(path)
    return sorted(files, key=lambda item: item.stat().st_mtime, reverse=True)


def decode_fragment(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="ignore")
    return unquote(text)


def clean_url(url: str) -> str:
    url = url.strip().strip("\"'<>")
    for sep in ("\x00", "\x01", "\x02", "\x03", "\x04", "\n", "\r", "\t"):
        if sep in url:
            url = url.split(sep, 1)[0]
    return url.rstrip(").,;\"'")


def redact_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except Exception:
        return "<unparseable-url>"
    path = parsed.path
    if len(path) > 90:
        path = path[:45] + "..." + path[-30:]
    return f"{parsed.scheme}://{parsed.netloc}{path}?<redacted>" if parsed.query else f"{parsed.scheme}://{parsed.netloc}{path}"


def scan_file(path: Path, max_read_bytes: int) -> dict:
    stat = path.stat()
    with path.open("rb") as fh:
        data = fh.read(max_read_bytes)
    lower_text = data.decode("utf-8", errors="ignore")
    hits = [marker for marker in MEDIA_MARKERS if marker.lower() in lower_text.lower()]
    urls: list[str] = []
    for regex in (RAW_URL_RE, ENCODED_URL_RE):
        for match in regex.finditer(data):
            url = clean_url(decode_fragment(match.group(0)))
            if not url.startswith(("http://", "https://")):
                continue
            if any(hint in url.lower() for hint in MEDIA_URL_HINTS):
                urls.append(url)
    unique_urls = []
    seen = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        unique_urls.append(url)
    return {
        "path": str(path),
        "relative_path": safe_rel(path),
        "bytes": stat.st_size,
        "mtime": stat.st_mtime,
        "hits": hits,
        "url_count": len(unique_urls),
        "redacted_urls": [redact_url(url) for url in unique_urls[:20]],
        "urls": unique_urls,
    }


def probe_url(url: str, timeout: float = 18) -> dict:
    converter = ROOT / "outputs" / "authorized_fetchers" / "extract_media_from_artifact.py"
    # Keep probing lightweight and local to ffmpeg. The URL itself remains only
    # in the JSON artifact; console output uses redacted form.
    command = [
        sys.executable,
        str(converter),
        "--probe-only-url",
        url,
    ]
    if not converter.exists():
        return {"url": url, "redacted_url": redact_url(url), "error": "probe_helper_missing"}
    try:
        proc = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    except Exception as exc:
        return {"url": url, "redacted_url": redact_url(url), "error": str(exc)}
    return {
        "url": url,
        "redacted_url": redact_url(url),
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-1000:],
        "stderr_tail": proc.stderr[-1000:].replace(url, "<redacted-url>"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since-minutes", type=float, default=30)
    parser.add_argument("--min-size", type=int, default=1024)
    parser.add_argument("--max-size", type=int, default=120_000_000)
    parser.add_argument("--max-read-bytes", type=int, default=80_000_000)
    parser.add_argument("--output", default=str(ROOT / "work/sensitive-artifacts/weixin_recent_media_marker_scan.json"))
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()

    since = time.time() - args.since_minutes * 60
    results = []
    for path in iter_recent_files(since, args.min_size, args.max_size):
        try:
            result = scan_file(path, args.max_read_bytes)
        except OSError:
            continue
        if result["hits"] or result["url_count"]:
            results.append(result)

    all_urls = []
    seen_urls = set()
    for result in results:
        for url in result["urls"]:
            if url not in seen_urls:
                seen_urls.add(url)
                all_urls.append(url)

    report = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "platform": current_system(),
        "since_minutes": args.since_minutes,
        "roots": [str(root) for root in SCAN_ROOTS if root.exists()],
        "files_with_hits": results,
        "candidate_url_count": len(all_urls),
        "redacted_candidate_urls": [redact_url(url) for url in all_urls[:50]],
        "candidate_urls": all_urls,
        "probes": [],
    }
    if args.probe:
        report["probes"] = [probe_url(url) for url in all_urls[:10]]

    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"scanned_files_with_hits={len(results)} candidate_urls={len(all_urls)} report={output}")
    for item in results[:12]:
        print(
            f"{item['url_count']} url(s), hits={','.join(item['hits']) or '-'} "
            f"bytes={item['bytes']} {item['relative_path']}"
        )
        for url in item["redacted_urls"][:3]:
            print(f"  {url}")
    return 0 if all_urls else 2


if __name__ == "__main__":
    raise SystemExit(main())
