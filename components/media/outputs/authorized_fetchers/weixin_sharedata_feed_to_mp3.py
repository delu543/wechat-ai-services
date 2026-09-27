#!/usr/bin/env python3
"""Try Weixin Channels feed URLs saved by the embedded browser Share Data DB.

This is a low-intrusion route: it reads only the WeChat webview browsing
metadata database, copies snapshots into local sensitive artifacts, extracts
feed-page token/eid pairs, and asks the public feed API whether a playable media
URL is available for the authorized page state.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from extract_media_from_artifact import walk as walk_media_urls


ROOT = Path(__file__).resolve().parents[2]
SHARE_DATA_GLOBS = [
    Path.home()
    / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium/web/profiles/*/Share Data",
]
FEED_API = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"
RAW_URL_RE = re.compile(r"https?://[^\s\"'<>\\\x00]+", re.I)
ENCODED_URL_RE = re.compile(r"https?%3A%2F%2F[^\s\"'<>\\\x00]+", re.I)
EXPORT_ID_RE = re.compile(r"\bexport/[0-9A-Za-z_-]{20,180}\b")
MEDIA_EXTS = (".m3u8", ".mp4", ".m4a", ".mp3", ".aac", ".wav", ".ogg", ".opus", ".webm")
TOKEN_RE = re.compile(
    r"(?:generalToken|token|h5AuthToken|sessionid)[\"'\\:=\s]{1,24}"
    r"([A-Za-z0-9._%+\-/=]{24,900})",
    re.I,
)
QUERY_TOKEN_RE = re.compile(r"(?:[?&]|%3[fF]|%26)(?:generalToken|token|sessionid)=([A-Za-z0-9._%+\-/=]{24,900})", re.I)
GENERIC_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9+/_=-]{80,900}")
TOKEN_STORAGE_DIRS = (
    "Local Storage/leveldb",
    "IndexedDB",
    "Service Worker/CacheStorage",
    "Shared Dictionary/cache",
)


@dataclass
class FeedCandidate:
    source_db: str
    row_id: int
    page_url: str
    token: str
    export_id: str
    exportkey: str
    pass_ticket: str
    wx_header: str
    reason: str


@dataclass
class TokenCandidate:
    token: str
    source_path: str
    key_hint: str
    mtime: float


def redact_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return "<unparseable-url>"
    path = parsed.path
    if len(path) > 100:
        path = path[:50] + "..." + path[-35:]
    query = urllib.parse.parse_qs(parsed.query)
    keys = ",".join(sorted(query.keys())[:12])
    return f"{parsed.scheme}://{parsed.netloc}{path}?<{keys or 'redacted'}>"


def redact_export_id(export_id: str) -> str:
    if len(export_id) <= 22:
        return "<redacted-export-id>"
    return export_id[:12] + "..." + export_id[-8:]


def token_fingerprint(token: str) -> dict[str, Any]:
    return {
        "sha256_12": hashlib.sha256(token.encode("utf-8")).hexdigest()[:12],
        "length": len(token),
    }


def safe_rel(path: Path) -> str:
    text = str(path)
    home = str(Path.home())
    if text.startswith(home + "/"):
        return "~/" + text[len(home) + 1 :]
    return text


def text_variants(text: str) -> list[str]:
    variants = [text]
    for value in list(variants):
        decoded = value
        for _ in range(3):
            next_decoded = urllib.parse.unquote(decoded)
            if next_decoded == decoded:
                break
            decoded = next_decoded
        if decoded != value:
            variants.append(decoded)
        slash_decoded = (
            value.replace("\\/", "/")
            .replace("\\u002F", "/")
            .replace("\\u002f", "/")
            .replace("\\u003A", ":")
            .replace("\\u003a", ":")
            .replace("\\u0026", "&")
        )
        if slash_decoded != value:
            variants.append(slash_decoded)
    unique: list[str] = []
    seen: set[str] = set()
    for value in variants:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def urls_from_text(text: str) -> list[str]:
    urls: list[str] = []
    for variant in text_variants(text):
        urls.extend(match.group(0) for match in RAW_URL_RE.finditer(variant))
        urls.extend(urllib.parse.unquote(match.group(0)) for match in ENCODED_URL_RE.finditer(variant))
    clean: list[str] = []
    seen: set[str] = set()
    for url in urls:
        value = url.strip().strip('",;')
        if value and value not in seen:
            seen.add(value)
            clean.append(value)
    return clean


def find_share_data_files() -> list[Path]:
    files: list[Path] = []
    for pattern in SHARE_DATA_GLOBS:
        files.extend(Path(path) for path in glob.glob(str(pattern)) if Path(path).is_file())
    return sorted(set(files), key=lambda item: item.stat().st_mtime_ns, reverse=True)


def iter_token_storage_files(since: float, max_size: int) -> list[Path]:
    files: list[Path] = []
    for profile in (Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium/web/profiles").glob("*"):
        if not profile.is_dir():
            continue
        for rel_dir in TOKEN_STORAGE_DIRS:
            root = profile / rel_dir
            if not root.exists():
                continue
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    path = Path(dirpath) / name
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    if stat.st_mtime < since or stat.st_size <= 0 or stat.st_size > max_size:
                        continue
                    files.append(path)
    return sorted(files, key=lambda item: item.stat().st_mtime_ns, reverse=True)


def clean_token(raw: str) -> str:
    value = urllib.parse.unquote(raw).strip().strip("\"'<>;,)")
    return value


def plausible_token(value: str) -> bool:
    if len(value) < 24 or len(value) > 900:
        return False
    lower = value.lower()
    if lower.startswith(("http", "export/")):
        return False
    if lower.startswith(("function", "javascript", "webpack")):
        return False
    if lower in {"undefined", "null", "true", "false"}:
        return False
    if "." in value and "/" in value:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._%+\-/=]+", value))


def extract_token_candidates(
    since_minutes: float,
    max_file_bytes: int,
    max_candidates: int,
) -> list[TokenCandidate]:
    since = time.time() - since_minutes * 60
    candidates: list[TokenCandidate] = []
    for path in iter_token_storage_files(since, max_file_bytes):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        text = data.decode("utf-8", "ignore")
        for regex, hint in ((TOKEN_RE, "field"), (QUERY_TOKEN_RE, "query")):
            for match in regex.finditer(text):
                token = clean_token(match.group(1))
                if not plausible_token(token):
                    continue
                candidates.append(
                    TokenCandidate(
                        token=token,
                        source_path=str(path),
                        key_hint=hint,
                        mtime=path.stat().st_mtime,
                    )
                )
        for match in GENERIC_LONG_TOKEN_RE.finditer(text):
            token = clean_token(match.group(0))
            if not plausible_token(token):
                continue
            candidates.append(
                TokenCandidate(
                    token=token,
                    source_path=str(path),
                    key_hint="generic-long-token",
                    mtime=path.stat().st_mtime,
                )
            )
    unique: list[TokenCandidate] = []
    seen: set[str] = set()
    for candidate in sorted(candidates, key=lambda item: item.mtime, reverse=True):
        if candidate.token in seen:
            continue
        seen.add(candidate.token)
        unique.append(candidate)
        if len(unique) >= max_candidates:
            break
    return unique


def snapshot_db(path: Path, artifact_dir: Path) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", safe_rel(path)).strip("-._")
    dest = artifact_dir / f"{int(time.time())}-{safe_name}.sqlite"
    shutil.copy2(path, dest)
    return dest


def decode_blob(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def query_fields_from_url(url: str) -> tuple[str, str, str, str, str]:
    variants = text_variants(url)
    for variant in variants:
        parsed = urllib.parse.urlsplit(variant)
        query = urllib.parse.parse_qs(parsed.query)
        token = (query.get("token") or query.get("generalToken") or [""])[0]
        export_id = (query.get("eid") or query.get("exportId") or [""])[0]
        exportkey = (query.get("exportkey") or query.get("exportKey") or [""])[0]
        pass_ticket = (query.get("pass_ticket") or query.get("passTicket") or [""])[0]
        wx_header = (query.get("wx_header") or query.get("wxHeader") or [""])[0]
        if token or export_id or exportkey or pass_ticket:
            return token, export_id, exportkey, pass_ticket, wx_header
    export_match = EXPORT_ID_RE.search("\n".join(variants))
    return "", export_match.group(0) if export_match else "", "", "", ""


def candidates_from_snapshot(snapshot: Path, source_db: Path) -> list[FeedCandidate]:
    conn = sqlite3.connect(str(snapshot))
    try:
        rows = conn.execute("select id, url, real_url, share_data from share_data_table").fetchall()
    finally:
        conn.close()
    candidates: list[FeedCandidate] = []
    for row_id, url, real_url, share_data in rows:
        chunks = [str(url or ""), str(real_url or ""), decode_blob(share_data)]
        all_urls: list[str] = []
        for chunk in chunks:
            all_urls.extend(urls_from_text(chunk))
        if url:
            all_urls.append(str(url))
        if real_url:
            all_urls.append(str(real_url))
        for page_url in all_urls:
            lower = page_url.lower()
            if "channels.weixin.qq.com" not in lower or "/pages/feed" not in lower:
                continue
            token, export_id, exportkey, pass_ticket, wx_header = query_fields_from_url(page_url)
            if not export_id:
                for chunk in chunks:
                    match = EXPORT_ID_RE.search("\n".join(text_variants(chunk)))
                    if match:
                        export_id = match.group(0)
                        break
            if not token and not export_id and not exportkey:
                continue
            candidates.append(
                FeedCandidate(
                    source_db=str(source_db),
                    row_id=int(row_id),
                    page_url=page_url,
                    token=token,
                    export_id=export_id,
                    exportkey=exportkey,
                    pass_ticket=pass_ticket,
                    wx_header=wx_header,
                    reason="feed_page_url",
                )
            )
    unique: list[FeedCandidate] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = (candidate.token, candidate.export_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def request_feed(candidate: FeedCandidate, timeout: int) -> tuple[int, Any]:
    rid = str(int(time.time() * 1000))
    params = {
        "_rid": rid,
        "_pageUrl": candidate.page_url,
    }
    if candidate.exportkey:
        params["exportkey"] = candidate.exportkey
    if candidate.pass_ticket:
        params["pass_ticket"] = candidate.pass_ticket
    api_url = FEED_API + "?" + urllib.parse.urlencode(params)
    payload = {
        "baseReq": {"generalToken": candidate.token},
        "exportId": candidate.export_id,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        api_url,
        data=body,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": "https://channels.weixin.qq.com",
            "Referer": candidate.page_url,
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
            ),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(raw)
    except Exception as exc:
        return 0, {"error": str(exc)}


def media_urls(payload: Any) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for url in walk_media_urls(payload):
        lower = url.lower().split("?", 1)[0]
        if url in seen:
            continue
        if lower.endswith(MEDIA_EXTS) or "m3u8" in lower or "stodownload" in lower:
            seen.add(url)
            urls.append(url)
    return sorted(urls, key=score_url)


def score_url(url: str) -> int:
    lower = url.lower().split("?", 1)[0]
    if lower.endswith((".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus")):
        return 0
    if lower.endswith(".m3u8"):
        return 1
    if lower.endswith(".mp4") or "stodownload" in lower:
        return 2
    return 3


def convert(url: str, output: Path) -> None:
    script = Path(__file__).with_name("media_url_to_mp3.py")
    subprocess.run([sys.executable, str(script), url, "--output", str(output)], check=True)


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def feed_attempt_summary(candidate: FeedCandidate, token_source: TokenCandidate | None) -> dict[str, Any]:
    summary = {
        "source_db": safe_rel(Path(candidate.source_db)),
        "row_id": candidate.row_id,
        "has_token": bool(candidate.token),
        "has_exportkey": bool(candidate.exportkey),
        "has_pass_ticket": bool(candidate.pass_ticket),
        "has_wx_header": bool(candidate.wx_header),
        "export_id": candidate.export_id,
        "exportkey": candidate.exportkey,
        "pass_ticket": candidate.pass_ticket,
        "wx_header": candidate.wx_header,
        "page_url": candidate.page_url,
    }
    if token_source:
        summary["token_candidate"] = {
            **token_fingerprint(token_source.token),
            "source_path": safe_rel(Path(token_source.source_path)),
            "key_hint": token_source.key_hint,
        }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "outputs" / "weixin_video_channel.mp3"))
    parser.add_argument("--artifact-dir", default=str(ROOT / "work/sensitive-artifacts/weixin-sharedata-feed"))
    parser.add_argument("--report", default="")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--scan-token-storage", action="store_true")
    parser.add_argument("--token-since-minutes", type=float, default=720)
    parser.add_argument("--token-max-file-bytes", type=int, default=8_000_000)
    parser.add_argument("--max-token-candidates", type=int, default=40)
    args = parser.parse_args()

    artifact_dir = Path(args.artifact_dir).expanduser().resolve()
    report_path = Path(args.report).expanduser().resolve() if args.report else artifact_dir / "report.json"
    output = Path(args.output).expanduser().resolve()
    report: dict[str, Any] = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "share_data_files": [],
        "snapshots": [],
        "candidate_count": 0,
        "redacted_candidates": [],
        "token_candidates": [],
        "attempts": [],
        "result": {"status": "not_found"},
    }

    all_candidates: list[FeedCandidate] = []
    for db_path in find_share_data_files():
        report["share_data_files"].append(safe_rel(db_path))
        try:
            snapshot = snapshot_db(db_path, artifact_dir / "snapshots")
            report["snapshots"].append(str(snapshot))
            all_candidates.extend(candidates_from_snapshot(snapshot, db_path))
        except Exception as exc:
            report["attempts"].append({"source_db": safe_rel(db_path), "error": str(exc)})

    unique: list[FeedCandidate] = []
    seen: set[tuple[str, str]] = set()
    for candidate in all_candidates:
        key = (candidate.token, candidate.export_id, candidate.exportkey, candidate.pass_ticket)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)

    report["candidate_count"] = len(unique)
    report["redacted_candidates"] = [
        {
            "source_db": safe_rel(Path(candidate.source_db)),
            "row_id": candidate.row_id,
            "page_url": redact_url(candidate.page_url),
            "has_token": bool(candidate.token),
            "has_exportkey": bool(candidate.exportkey),
            "has_pass_ticket": bool(candidate.pass_ticket),
            "has_wx_header": bool(candidate.wx_header),
            "export_id": redact_export_id(candidate.export_id),
        }
        for candidate in unique
    ]

    token_candidates: list[TokenCandidate] = []
    if args.scan_token_storage:
        token_candidates = extract_token_candidates(
            since_minutes=args.token_since_minutes,
            max_file_bytes=args.token_max_file_bytes,
            max_candidates=args.max_token_candidates,
        )
        report["token_candidates"] = [
            {
                **token_fingerprint(candidate.token),
                "source_path": safe_rel(Path(candidate.source_path)),
                "key_hint": candidate.key_hint,
            }
            for candidate in token_candidates
        ]

    for candidate in unique:
        candidates_to_try: list[tuple[FeedCandidate, TokenCandidate | None]] = [(candidate, None)]
        if not candidate.token:
            candidates_to_try.extend(
                (
                    FeedCandidate(
                        source_db=candidate.source_db,
                        row_id=candidate.row_id,
                        page_url=candidate.page_url,
                        token=token_candidate.token,
                        export_id=candidate.export_id,
                        exportkey=candidate.exportkey,
                        pass_ticket=candidate.pass_ticket,
                        wx_header=candidate.wx_header,
                        reason=f"{candidate.reason}+token_storage",
                    ),
                    token_candidate,
                )
                for token_candidate in token_candidates
            )
        for attempt_candidate, token_source in candidates_to_try:
            status, payload = request_feed(attempt_candidate, timeout=args.timeout)
            urls = media_urls(payload)
            attempt = {
                **feed_attempt_summary(attempt_candidate, token_source),
                "status": status,
                "candidate_media_urls": urls,
                "redacted_media_urls": [redact_url(url) for url in urls],
                "response": payload,
            }
            report["attempts"].append(attempt)
            write_report(report_path, report)
            if not urls:
                continue
            report["result"] = {
                "status": "found",
                "selected": {
                    "row_id": attempt_candidate.row_id,
                    "redacted_media_url": redact_url(urls[0]),
                    "source_db": safe_rel(Path(attempt_candidate.source_db)),
                    "token_candidate": token_fingerprint(token_source.token) if token_source else None,
                },
            }
            if args.list_only:
                write_report(report_path, report)
                print(f"Found media URL from Share Data feed state. Report: {report_path}")
                return 0
            convert(urls[0], output)
            report["result"]["status"] = "converted"
            report["result"]["output"] = str(output)
            write_report(report_path, report)
            print(f"Converted Share Data feed media -> {output}")
            return 0

    write_report(report_path, report)
    print(f"No playable media URL found from Share Data feed state. Report: {report_path}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
