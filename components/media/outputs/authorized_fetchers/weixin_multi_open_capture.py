#!/usr/bin/env python3
"""Repeat desktop WeChat open attempts and summarize local source evidence.

The goal is regression evidence, not broad scraping: open the authorized link
through File Transfer Assistant, then scan recent playback-side files for
media markers, stodownload candidates, decode-key markers, and open temp FDs.
Full signed URLs remain only inside the child reports under sensitive-artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from replay_mp3_studio.config import WORK_ROOT  # noqa: E402
from replay_mp3_studio.weixin_filehelper import (  # noqa: E402
    open_weixin_filehelper,
    reopen_verified_filehelper_link,
)
from replay_mp3_studio.weixin_source_pairs import (  # noqa: E402
    decode_key_marker_inventory_from_file,
    extract_decode_key_pairs_from_file,
    extract_numeric_key_pairs_from_file,
    merge_decode_key_marker_inventories,
    redacted_numeric_key_pair_summary,
    redacted_pair_summary,
    write_sensitive_pair_artifact,
)


SCAN_ROOTS = [
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium",
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/net/cdncomm",
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/net/kvcomm",
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/log/radium",
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/log/player",
    Path.home() / "Library/Group Containers/5A4RE8SF68.com.tencent.xinWeChat/Library/Caches",
]
DELTA_SCAN_ROOTS = [
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data",
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/tmp",
    Path.home() / "Library/Group Containers/5A4RE8SF68.com.tencent.xinWeChat/Library/Caches",
]
MARKERS = (
    "finder.video.qq.com",
    "stodownload",
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
SKIP_PARTS = (
    "account web data",
    "cookies",
    "favicons",
    "history",
    "visited links",
    "web data",
)
DELTA_SKIP_PARTS = (
    *SKIP_PARTS,
    "db_storage",
    "/message/",
    "/contact/",
    "/chat/",
    "chat_msg",
    "wcdb",
    ".sqlite",
    ".db",
    ".db-wal",
    ".db-shm",
    ".db-journal",
    ".ldb",
)
DELTA_TEXT_SUFFIXES = {".json", ".har", ".txt", ".log", ".html", ".htm", ".xml", ".js"}
RAW_URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]{20,}", re.I)
ENCODED_URL_RE = re.compile(r"https?%3A%2F%2F[A-Za-z0-9._~%:/?#\[\]@!$&()*+,;=%-]{20,}", re.I)
SENSITIVE_PAIR_ROOT = WORK_ROOT / "sensitive-artifacts" / "weixin-fast-mp3" / "decode-pairs"
SOURCE_SNAPSHOT_ROOT = WORK_ROOT / "sensitive-artifacts" / "weixin-fast-mp3" / "source-snapshots"
CHILD_REPORT_NAMES = {
    "marker-scan.json",
    "radium-source.json",
    "profile-state.json",
    "seek-burst.json",
    "source-snapshots.json",
}


def safe_rel(path: Path) -> str:
    text = str(path)
    home = str(Path.home())
    if text.startswith(home + "/"):
        return "~/" + text[len(home) + 1 :]
    return text


def should_skip(path: Path) -> bool:
    lower = str(path).lower()
    return any(part in lower for part in SKIP_PARTS)


def clean_url(value: str) -> str:
    value = urllib.parse.unquote(value)
    value = (
        value.replace("\\/", "/")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\u003A", ":")
        .replace("\\u003a", ":")
        .replace("\\u0026", "&")
        .replace("&amp;", "&")
    )
    for sep in ("\x00", "\x01", "\x02", "\x03", "\x04", "\n", "\r", "\t"):
        if sep in value:
            value = value.split(sep, 1)[0]
    return value.strip().strip("\"'<>").rstrip(").,;\"'")


def redact_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except Exception:
        return "<unparseable-url>"
    if (
        "wxapp.tc.qq.com" in parsed.netloc
        or "wximg.wxs.qq.com" in parsed.netloc
        or "snscosdownload" in parsed.path
    ):
        return f"{parsed.scheme}://{parsed.netloc}/<redacted-media-path>"
    path = parsed.path
    if len(path) > 90:
        path = path[:45] + "..." + path[-30:]
    return f"{parsed.scheme}://{parsed.netloc}{path}?<redacted>" if parsed.query else url


def sanitize_text(text: str) -> str:
    sanitized = RAW_URL_RE.sub(lambda match: redact_url(clean_url(match.group(0))), text)
    return ENCODED_URL_RE.sub(lambda match: redact_url(clean_url(match.group(0))), sanitized)


def sanitize_child_report_payload(value):
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            key_text = str(key)
            if re.search(r"(?i)(decode[_-]?key|decrypt[_-]?key|media[_-]?key|encfilekey|token|cookie|authorization)", key_text):
                sanitized[key_text] = "<redacted>"
            elif re.search(r"(?i)(^|[^A-Za-z0-9_])key($|[^A-Za-z0-9_])", key_text) and isinstance(item, (int, str)):
                sanitized[key_text] = "<redacted>"
            else:
                sanitized[key_text] = sanitize_child_report_payload(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_child_report_payload(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def sanitize_child_report_file(path: Path) -> None:
    if not path.exists() or not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        path.write_text(sanitize_text(path.read_text(encoding="utf-8", errors="ignore")), encoding="utf-8")
        return
    path.write_text(json.dumps(sanitize_child_report_payload(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def should_skip_delta_path(path: Path) -> bool:
    lower = str(path).replace("\\", "/").lower()
    return any(part in lower for part in DELTA_SKIP_PARTS)


def safe_delta_roots() -> list[Path]:
    return [root for root in DELTA_SCAN_ROOTS if root.exists()]


def snapshot_safe_file_state(
    roots: list[Path] | tuple[Path, ...] | None = None,
    *,
    max_file_bytes: int,
    max_files: int,
) -> dict[str, dict]:
    files: list[tuple[int, Path, os.stat_result]] = []
    for root in roots if roots is not None else safe_delta_roots():
        root = root.expanduser()
        if not root.exists():
            continue
        walk_items = [(root.parent, [], [root.name])] if root.is_file() else os.walk(root)
        for dirpath, dirnames, filenames in walk_items:
            dirnames[:] = [name for name in dirnames if not should_skip_delta_path(Path(dirpath) / name)]
            for filename in filenames:
                path = Path(dirpath) / filename
                if should_skip_delta_path(path):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_size <= 0 or stat.st_size > max_file_bytes:
                    continue
                files.append((stat.st_mtime_ns, path, stat))
    files.sort(key=lambda item: (item[0], str(item[1])), reverse=True)
    result: dict[str, dict] = {}
    for _mtime, path, stat in files[:max_files]:
        resolved = path.resolve()
        result[str(resolved)] = {
            "path": str(resolved),
            "relative_path": safe_rel(resolved),
            "bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "suffix": path.suffix.lower(),
        }
    return result


def _delta_changed_paths(before: dict[str, dict], after: dict[str, dict]) -> list[dict]:
    changes: list[dict] = []
    for path_text, current in after.items():
        previous = before.get(path_text)
        if previous and previous.get("bytes") == current.get("bytes") and previous.get("mtime_ns") == current.get("mtime_ns"):
            continue
        changes.append({**current, "change_type": "new" if previous is None else "modified"})
    changes.sort(key=lambda item: (int(item.get("mtime_ns") or 0), str(item.get("path") or "")), reverse=True)
    return changes


def _scan_delta_text_file(path: Path, max_read_bytes: int) -> dict:
    data = path.read_bytes()[:max_read_bytes]
    text = data.decode("utf-8", errors="ignore")
    lower = text.lower()
    hits = [marker for marker in MARKERS if marker.lower() in lower]
    urls: list[str] = []
    for regex in (RAW_URL_RE, ENCODED_URL_RE):
        for match in regex.finditer(text):
            url = clean_url(match.group(0))
            if "finder.video.qq.com" in url.lower() or "stodownload" in url.lower():
                urls.append(url)
    unique_urls: list[str] = []
    seen_urls: set[str] = set()
    for url in urls:
        if url in seen_urls:
            continue
        seen_urls.add(url)
        unique_urls.append(url)
    pairs = extract_decode_key_pairs_from_file(path, max_read_bytes=max_read_bytes)
    numeric_pairs = extract_numeric_key_pairs_from_file(path, max_read_bytes=max_read_bytes)
    inventory = decode_key_marker_inventory_from_file(path, max_read_bytes=max_read_bytes)
    return {
        "hits": hits,
        "url_count": len(unique_urls),
        "redacted_urls": [redact_url(url) for url in unique_urls[:10]],
        "pairs": pairs,
        "numeric_pairs": numeric_pairs,
        "decode_key_marker_inventory": {
            key: inventory.get(key)
            for key in ("marker_count", "near_media_count", "field_counts", "markers")
            if key in inventory
        },
    }


def scan_safe_file_delta(
    before: dict[str, dict],
    after: dict[str, dict],
    *,
    max_read_bytes: int,
    max_items: int = 120,
) -> dict:
    changes = _delta_changed_paths(before, after)
    items: list[dict] = []
    raw_pairs: list[dict] = []
    raw_numeric_pairs: list[dict] = []
    text_scanned = 0
    files_with_markers = 0
    candidate_url_count = 0
    marker_inventories: list[dict] = []
    for change in changes[:max_items]:
        path = Path(str(change.get("path") or ""))
        suffix = str(change.get("suffix") or path.suffix.lower())
        item = {
            "relative_path": change.get("relative_path") or safe_rel(path),
            "bytes": int(change.get("bytes") or 0),
            "change_type": change.get("change_type") or "",
            "suffix": suffix,
        }
        if suffix in DELTA_TEXT_SUFFIXES or int(change.get("bytes") or 0) <= max_read_bytes:
            try:
                scanned = _scan_delta_text_file(path, max_read_bytes)
            except OSError:
                scanned = {}
            text_scanned += 1
            hits = scanned.get("hits") or []
            url_count = int(scanned.get("url_count") or 0)
            if hits or url_count:
                files_with_markers += 1
            candidate_url_count += url_count
            pairs = [pair for pair in scanned.get("pairs") or [] if isinstance(pair, dict)]
            numeric_pairs = [pair for pair in scanned.get("numeric_pairs") or [] if isinstance(pair, dict)]
            raw_pairs.extend(pairs)
            raw_numeric_pairs.extend(numeric_pairs)
            marker_inventory = scanned.get("decode_key_marker_inventory")
            if isinstance(marker_inventory, dict):
                marker_inventories.append(marker_inventory)
            item.update(
                {
                    "hits": hits,
                    "url_count": url_count,
                    "redacted_urls": scanned.get("redacted_urls") or [],
                    "decode_key_pair_count": len(pairs),
                    "decode_key_pair_summary": redacted_pair_summary(pairs),
                    "numeric_key_pair_count": len(numeric_pairs),
                    "numeric_key_pair_summary": redacted_numeric_key_pair_summary(numeric_pairs),
                }
            )
        items.append(item)
    unique_pairs: dict[tuple[str, str, str], dict] = {}
    for pair in raw_pairs:
        unique_pairs.setdefault(
            (str(pair.get("url") or ""), str(pair.get("decode_key") or ""), str(pair.get("path") or "")),
            pair,
        )
    unique_numeric_pairs: dict[tuple[str, int, str], dict] = {}
    for pair in raw_numeric_pairs:
        try:
            key = int(pair.get("key") or 0)
        except (TypeError, ValueError):
            continue
        unique_numeric_pairs.setdefault((str(pair.get("url") or ""), key, str(pair.get("path") or "")), pair)
    pairs = list(unique_pairs.values())
    numeric_pairs = list(unique_numeric_pairs.values())
    return {
        "delta_roots": [str(root) for root in safe_delta_roots()],
        "before_file_count": len(before),
        "after_file_count": len(after),
        "changed_file_count": len(changes),
        "scanned_text_file_count": text_scanned,
        "files_with_media_markers": files_with_markers,
        "candidate_url_count": candidate_url_count,
        "decode_key_pair_count": len(pairs),
        "decode_key_pair_summary": redacted_pair_summary(pairs),
        "numeric_key_pair_count": len(numeric_pairs),
        "numeric_key_pair_summary": redacted_numeric_key_pair_summary(numeric_pairs),
        "decode_key_marker_inventory": merge_decode_key_marker_inventories(marker_inventories),
        "items": items,
        "pairs": pairs,
        "numeric_pairs": numeric_pairs,
    }


def iter_recent_files(since: float, max_file_bytes: int, max_files: int) -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if not should_skip(Path(dirpath) / name)]
            for filename in filenames:
                path = Path(dirpath) / filename
                if should_skip(path):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_size <= 0 or stat.st_size > max_file_bytes or stat.st_mtime < since:
                    continue
                files.append(path)
    return sorted(files, key=lambda item: item.stat().st_mtime_ns, reverse=True)[:max_files]


def scan_recent_markers(since: float, max_file_bytes: int, max_read_bytes: int, max_files: int) -> dict:
    results = []
    for path in iter_recent_files(since, max_file_bytes, max_files):
        try:
            stat = path.stat()
            data = path.read_bytes()[:max_read_bytes]
        except OSError:
            continue
        text = data.decode("utf-8", errors="ignore")
        lower = text.lower()
        hits = [marker for marker in MARKERS if marker.lower() in lower]
        urls: list[str] = []
        for regex in (RAW_URL_RE, ENCODED_URL_RE):
            for match in regex.finditer(text):
                url = clean_url(match.group(0))
                if "finder.video.qq.com" in url.lower() or "stodownload" in url.lower():
                    urls.append(url)
        unique_urls = []
        seen = set()
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            unique_urls.append(url)
        if hits or unique_urls:
            results.append(
                {
                    "relative_path": safe_rel(path),
                    "bytes": stat.st_size,
                    "mtime": stat.st_mtime,
                    "hits": hits,
                    "url_count": len(unique_urls),
                    "redacted_urls": [redact_url(url) for url in unique_urls[:10]],
                }
            )
    return {
        "files_with_hits": len(results),
        "candidate_url_count": sum(item["url_count"] for item in results),
        "decode_marker_files": sum(
            1
            for item in results
            if any(hit.lower() in {"decodekey", "decode_key", "decodekey"} for hit in item["hits"])
        ),
        "items": results[:80],
    }


def run_child(name: str, command: list[str], timeout: float) -> dict:
    started = time.time()
    try:
        proc = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    except Exception as exc:
        return {"name": name, "error": str(exc), "elapsed_seconds": round(time.time() - started, 2)}
    return {
        "name": name,
        "returncode": proc.returncode,
        "elapsed_seconds": round(time.time() - started, 2),
        "stdout_tail": sanitize_text((proc.stdout or "")[-1800:]),
        "stderr_tail": sanitize_text((proc.stderr or "")[-1800:]),
    }


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": str(exc)}


def _path_from_report_value(value: str) -> Path | None:
    if not value:
        return None
    text = str(value)
    if text.startswith("~/"):
        return (Path.home() / text[2:]).resolve()
    path = Path(text)
    if path.is_absolute():
        return path.expanduser().resolve()
    return None


def _report_items_with_paths(data: dict) -> list[dict]:
    items: list[dict] = []
    for key in ("files_with_hits", "items", "candidates", "probes", "redacted_candidates", "snapshots"):
        value = data.get(key)
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
    result = data.get("result")
    if isinstance(result, dict):
        for key in ("files_with_hits", "items", "candidates", "probes"):
            value = result.get(key)
            if isinstance(value, list):
                items.extend(item for item in value if isinstance(item, dict))
    return items


def source_path_inventory_from_child_reports(paths: list[Path]) -> dict:
    existing: dict[str, Path] = {}
    missing: dict[str, Path] = {}
    for report_path in paths:
        data = read_json(report_path)
        if not isinstance(data, dict):
            continue
        for item in _report_items_with_paths(data):
            for key in ("path", "source_path", "relative_path", "source_snapshot_path"):
                path = _path_from_report_value(str(item.get(key) or ""))
                if not path:
                    continue
                if path.exists() and path.is_file():
                    existing[str(path)] = path
                else:
                    missing[str(path)] = path
    return {
        "existing": list(existing.values()),
        "missing": list(missing.values()),
        "reference_count": len(existing) + len(missing),
    }


def snapshot_dir_for_run(run_dir: Path) -> Path:
    resolved = str(run_dir.expanduser().resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]
    return SOURCE_SNAPSHOT_ROOT / f"{run_dir.name}-{digest}"


def snapshot_source_files_from_reports(
    report_paths: list[Path],
    snapshot_dir: Path,
    *,
    max_read_bytes: int,
    max_files: int = 80,
) -> dict:
    inventory = source_path_inventory_from_child_reports(report_paths)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshots: list[dict] = []
    for index, source_path in enumerate(list(inventory["existing"])[:max_files], start=1):
        try:
            stat = source_path.stat()
            data = source_path.read_bytes()[:max_read_bytes]
        except OSError:
            continue
        digest = hashlib.sha256(data).hexdigest()[:16]
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", source_path.name)[:80] or "source"
        target = snapshot_dir / f"{index:03d}-{digest}-{safe_name}"
        if not target.exists():
            target.write_bytes(data)
        snapshots.append(
            {
                "source_path_redacted": safe_rel(source_path),
                "source_snapshot_path": str(target),
                "source_size": stat.st_size,
                "bytes_copied": len(data),
                "sha256_16": digest,
                "truncated": stat.st_size > len(data),
            }
        )
    return {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "snapshot_dir": str(snapshot_dir),
        "source_file_reference_count": int(inventory["reference_count"]),
        "source_file_count": len(inventory["existing"]),
        "missing_source_file_count": len(inventory["missing"]),
        "snapshot_count": len(snapshots),
        "snapshots": snapshots,
    }


def source_paths_from_child_reports(paths: list[Path]) -> list[Path]:
    inventory = source_path_inventory_from_child_reports(paths)
    return list(inventory["existing"])


def scan_decode_pairs_from_reports(
    report_paths: list[Path],
    *,
    max_read_bytes: int,
    extra_source_paths: list[Path] | None = None,
) -> dict:
    inventory = source_path_inventory_from_child_reports(report_paths)
    source_paths = list(inventory["existing"])
    if extra_source_paths:
        seen_sources = {str(path) for path in source_paths}
        for path in extra_source_paths:
            if not path.exists() or not path.is_file():
                continue
            if str(path) in seen_sources:
                continue
            seen_sources.add(str(path))
            source_paths.append(path)
    pairs: list[dict[str, str]] = []
    marker_inventories: list[dict] = []
    report_files_scanned = 0
    report_files_with_pairs = 0
    for path in report_paths:
        try:
            file_pairs = extract_decode_key_pairs_from_file(
                path,
                max_read_bytes=max_read_bytes,
                allow_key_aliases=False,
            )
        except OSError:
            continue
        report_files_scanned += 1
        if file_pairs:
            report_files_with_pairs += 1
            pairs.extend(file_pairs)
        try:
            marker_inventories.append(decode_key_marker_inventory_from_file(path, max_read_bytes=max_read_bytes))
        except OSError:
            pass
    files_scanned = 0
    files_with_pairs = 0
    for path in source_paths:
        try:
            file_pairs = extract_decode_key_pairs_from_file(path, max_read_bytes=max_read_bytes)
        except OSError:
            continue
        files_scanned += 1
        if file_pairs:
            files_with_pairs += 1
            pairs.extend(file_pairs)
        try:
            marker_inventories.append(decode_key_marker_inventory_from_file(path, max_read_bytes=max_read_bytes))
        except OSError:
            pass
    unique: dict[tuple[str, str, str], dict[str, str]] = {}
    for pair in pairs:
        unique.setdefault(
            (
                str(pair.get("url") or ""),
                str(pair.get("decode_key") or ""),
                str(pair.get("path") or ""),
            ),
            pair,
        )
    unique_pairs = list(unique.values())
    marker_inventory = merge_decode_key_marker_inventories(marker_inventories)
    return {
        "source_file_reference_count": int(inventory["reference_count"]),
        "source_file_count": len(source_paths),
        "missing_source_file_count": len(inventory["missing"]),
        "missing_source_files": [safe_rel(path) for path in list(inventory["missing"])[:20]],
        "files_scanned": files_scanned,
        "files_with_pairs": files_with_pairs,
        "report_files_scanned": report_files_scanned,
        "report_files_with_pairs": report_files_with_pairs,
        "pair_count": len(unique_pairs),
        "pairs": unique_pairs,
        "redacted_pair_summary": redacted_pair_summary(unique_pairs),
        "decode_key_marker_inventory": marker_inventory,
    }


def collect_child_report_paths(run_dir: Path) -> list[Path]:
    run_dir = run_dir.expanduser().resolve()
    if not run_dir.exists():
        return []
    paths = [
        path
        for path in run_dir.rglob("*.json")
        if path.is_file() and path.name in CHILD_REPORT_NAMES
    ]
    return sorted(paths)


def collect_source_snapshot_files(roots: list[Path]) -> list[Path]:
    files: dict[str, Path] = {}
    for root in roots:
        root = root.expanduser().resolve()
        if not root.exists():
            continue
        if root.is_file():
            if root.name != "source-snapshots.json":
                files[str(root)] = root
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.name == "source-snapshots.json":
                continue
            files[str(path)] = path
    return sorted(files.values())


def rescan_decode_pairs_in_run_dir(run_dir: Path, *, max_read_bytes: int) -> dict:
    result = rescan_decode_pairs_in_run_dirs([run_dir], max_read_bytes=max_read_bytes)
    result["run_dir"] = str(run_dir.expanduser().resolve())
    return result


def rescan_decode_pairs_in_run_dirs(
    run_dirs: list[Path],
    *,
    max_read_bytes: int,
    source_snapshot_roots: list[Path] | None = None,
) -> dict:
    report_paths: dict[str, Path] = {}
    resolved_run_dirs: list[Path] = []
    for run_dir in run_dirs:
        resolved = run_dir.expanduser().resolve()
        resolved_run_dirs.append(resolved)
        for path in collect_child_report_paths(resolved):
            report_paths[str(path)] = path
    source_snapshot_files = collect_source_snapshot_files(source_snapshot_roots or [])
    result = scan_decode_pairs_from_reports(
        list(report_paths.values()),
        max_read_bytes=max_read_bytes,
        extra_source_paths=source_snapshot_files,
    )
    result["run_dirs"] = [str(path) for path in resolved_run_dirs]
    result["run_dir_count"] = len(resolved_run_dirs)
    result["child_report_count"] = len(report_paths)
    result["child_reports"] = [str(path) for path in report_paths.values()]
    result["source_snapshot_roots"] = [str(path.expanduser().resolve()) for path in (source_snapshot_roots or [])]
    result["source_snapshot_file_count"] = len(source_snapshot_files)
    result["source_snapshot_files"] = [safe_rel(path) for path in source_snapshot_files[:80]]
    return result


def write_decode_pair_rescan_report(
    run_dir: Path,
    *,
    max_read_bytes: int,
    extra_run_dirs: list[Path] | None = None,
    source_snapshot_roots: list[Path] | None = None,
) -> tuple[Path, dict]:
    report_path = run_dir.expanduser().resolve() / "decode-pair-rescan.json"
    result = rescan_decode_pairs_in_run_dirs(
        [run_dir, *(extra_run_dirs or [])],
        max_read_bytes=max_read_bytes,
        source_snapshot_roots=source_snapshot_roots,
    )
    raw_pairs = result.pop("pairs", [])
    report: dict = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "run_dir": str(run_dir.expanduser().resolve()),
        "result": "decode_key_pair_found" if raw_pairs else "decode_key_pair_missing_after_rescan",
        "decode_key_pair_count": int(result.get("pair_count") or 0),
        "decode_key_pair_summary": result.get("redacted_pair_summary", []),
        "rescan": result,
    }
    if raw_pairs:
        artifact = write_sensitive_pair_artifact(raw_pairs, SENSITIVE_PAIR_ROOT, label="rescan")
        report["decode_key_pair_artifact"] = str(artifact)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path, report


def summarize_probe(probe: dict) -> dict:
    http_range = probe.get("http_range") or {}
    return {
        "redacted_url": probe.get("redacted_url") or probe.get("redacted_value") or "",
        "audio": bool(probe.get("audio")),
        "video": bool(probe.get("video")),
        "duration": probe.get("duration", 0),
        "diagnosis": probe.get("diagnosis", ""),
        "http_range": {
            key: http_range.get(key)
            for key in (
                "status",
                "content_type",
                "content_range",
                "content_length",
                "first16_hex",
                "container_signature",
                "encrypted_or_obfuscated",
                "error",
            )
            if key in http_range
        },
    }


def summarize_child_report(kind: str, path: Path) -> dict:
    data = read_json(path)
    if kind == "marker":
        return {
            "candidate_url_count": data.get("candidate_url_count", 0),
            "redacted_candidate_urls": data.get("redacted_candidate_urls", [])[:12],
            "files_with_hits": [
                {
                    "relative_path": item.get("relative_path"),
                    "bytes": item.get("bytes"),
                    "hits": item.get("hits", []),
                    "url_count": item.get("url_count", 0),
                    "redacted_urls": item.get("redacted_urls", [])[:4],
                }
                for item in data.get("files_with_hits", [])[:20]
            ],
        }
    if kind in {"radium", "profile"}:
        return {
            "candidate_count": data.get("candidate_count", 0),
            "redacted_candidates": data.get("redacted_candidates", [])[:12],
            "probes": [summarize_probe(item) for item in data.get("probes", [])[:12]],
            "result": data.get("result", {}),
        }
    if kind == "burst":
        return {
            "visible_events": len(data.get("visible_events", [])),
            "lsof_events": len(data.get("lsof_events", [])),
            "unreadable_lsof": len(data.get("unreadable_lsof", [])),
            "baseline_lsof": [
                {
                    "command": item.get("command"),
                    "pid": item.get("pid"),
                    "fd": item.get("fd"),
                    "size": item.get("size"),
                    "relative_path": item.get("relative_path"),
                    "exists_as_path": item.get("exists_as_path"),
                }
                for item in data.get("baseline_lsof", [])[:12]
            ],
        }
    return data


def aggregate_result(rounds: list[dict]) -> str:
    encrypted = False
    source_urls = False
    decode_markers = False
    playable_audio = False
    for item in rounds:
        if int(item.get("decode_key_pair_count") or 0) > 0:
            return "decode_key_pair_found"
        if int(item.get("numeric_key_pair_count") or 0) > 0:
            return "numeric_key_pair_found"
        marker = item.get("inline_marker_scan", {})
        decode_markers = decode_markers or marker.get("decode_marker_files", 0) > 0
        source_urls = source_urls or marker.get("candidate_url_count", 0) > 0
        delta = item.get("filesystem_delta_scan", {})
        if isinstance(delta, dict):
            source_urls = source_urls or int(delta.get("candidate_url_count") or 0) > 0
            decode_markers = decode_markers or int(
                (delta.get("decode_key_marker_inventory") or {}).get("marker_count") or 0
            ) > 0
        for key in ("marker_summary", "radium_summary", "profile_summary"):
            summary = item.get(key, {})
            source_urls = source_urls or summary.get("candidate_url_count", 0) > 0 or summary.get("candidate_count", 0) > 0
            for probe in summary.get("probes", []):
                playable_audio = playable_audio or bool(probe.get("audio"))
                encrypted = encrypted or bool((probe.get("http_range") or {}).get("encrypted_or_obfuscated"))
    if playable_audio:
        return "playable_audio_source_found"
    if encrypted:
        return "encrypted_stodownload_found_decode_key_missing"
    if decode_markers:
        return "decode_key_marker_found_but_no_playable_source"
    if source_urls:
        return "source_url_marker_found_but_not_playable"
    return "no_source_markers_after_auto_open"


def open_authorized_link_for_round(url: str, index: int, *, timeout: int) -> dict:
    """Send once, then reuse the exact verified message on later retries."""

    if index <= 1:
        return open_weixin_filehelper(url, click_after_send=True, timeout=timeout)
    return reopen_verified_filehelper_link(url, timeout=timeout)


def run_round(args: argparse.Namespace, run_dir: Path, index: int) -> dict:
    round_dir = run_dir / f"round-{index:02d}"
    round_dir.mkdir(parents=True, exist_ok=True)
    round_started = time.time()
    item: dict = {"round": index, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    delta_before = snapshot_safe_file_state(
        max_file_bytes=args.delta_max_file_bytes,
        max_files=args.delta_max_files,
    )
    try:
        item["open"] = open_authorized_link_for_round(
            args.url,
            index,
            timeout=args.open_timeout,
        )
    except Exception as exc:
        item["open_error"] = str(exc)
    time.sleep(args.settle_seconds)

    since = time.time() - args.since_minutes * 60
    item["inline_marker_scan"] = scan_recent_markers(
        since,
        args.max_marker_file_bytes,
        args.max_marker_read_bytes,
        args.max_marker_files,
    )

    marker_report = round_dir / "marker-scan.json"
    radium_report = round_dir / "radium-source.json"
    profile_report = round_dir / "profile-state.json"
    burst_report = round_dir / "seek-burst.json"
    delta_report = round_dir / "filesystem-delta.json"
    child_specs = [
        (
            "marker",
            [
                sys.executable,
                str(SCRIPT_DIR / "weixin_recent_media_marker_scan.py"),
                "--since-minutes",
                str(args.since_minutes),
                "--output",
                str(marker_report),
            ],
            args.child_timeout,
            marker_report,
        ),
        (
            "radium",
            [
                sys.executable,
                str(SCRIPT_DIR / "weixin_radium_source_to_mp3.py"),
                "--duration",
                str(args.scan_duration),
                "--poll-interval",
                "1",
                "--since-minutes",
                str(args.since_minutes),
                "--report",
                str(radium_report),
                "--list-only",
                "--probe-timeout",
                str(args.probe_timeout),
                "--max-candidates",
                str(args.max_candidates),
                "--min-duration",
                "1",
                "--source-snapshot-dir",
                str(snapshot_dir_for_run(run_dir) / f"round-{index:02d}" / "radium-source"),
                "--source-snapshot-max-read-bytes",
                str(args.max_marker_read_bytes),
            ],
            args.child_timeout,
            radium_report,
        ),
        (
            "profile",
            [
                sys.executable,
                str(SCRIPT_DIR / "weixin_profile_state_to_mp3.py"),
                "--duration",
                str(args.scan_duration),
                "--poll-interval",
                "1",
                "--since-minutes",
                str(args.profile_since_minutes),
                "--report",
                str(profile_report),
                "--list-only",
                "--probe-timeout",
                str(args.probe_timeout),
                "--max-candidates",
                str(args.max_candidates),
                "--include-cache-data",
                "--min-duration",
                "1",
            ],
            args.child_timeout,
            profile_report,
        ),
        (
            "burst",
            [
                sys.executable,
                str(SCRIPT_DIR / "weixin_seek_burst_watch.py"),
                "--duration",
                str(args.burst_duration),
                "--list-only",
                "--report",
                str(burst_report),
                "--artifact-dir",
                str(round_dir / "burst-artifacts"),
                "--min-size",
                "1024",
            ],
            args.child_timeout,
            burst_report,
        ),
    ]
    child_runs = []
    report_paths = []
    for name, command, timeout, report_path in child_specs:
        child_runs.append(run_child(name, command, timeout))
        item[f"{name}_report"] = str(report_path)
        item[f"{name}_summary"] = summarize_child_report(name, report_path)
        report_paths.append(report_path)
    delta_after = snapshot_safe_file_state(
        max_file_bytes=args.delta_max_file_bytes,
        max_files=args.delta_max_files,
    )
    delta_scan = scan_safe_file_delta(
        delta_before,
        delta_after,
        max_read_bytes=args.delta_max_read_bytes,
    )
    delta_raw_pairs = delta_scan.pop("pairs", [])
    delta_raw_numeric_pairs = delta_scan.pop("numeric_pairs", [])
    delta_report.write_text(json.dumps(delta_scan, ensure_ascii=False, indent=2), encoding="utf-8")
    item["filesystem_delta_report"] = str(delta_report)
    item["filesystem_delta_scan"] = {
        key: delta_scan.get(key)
        for key in (
            "before_file_count",
            "after_file_count",
            "changed_file_count",
            "scanned_text_file_count",
            "files_with_media_markers",
            "candidate_url_count",
            "decode_key_pair_count",
            "decode_key_pair_summary",
            "numeric_key_pair_count",
            "numeric_key_pair_summary",
            "decode_key_marker_inventory",
        )
        if key in delta_scan
    }
    snapshot_report_path = round_dir / "source-snapshots.json"
    snapshot_report = snapshot_source_files_from_reports(
        report_paths,
        snapshot_dir_for_run(run_dir) / f"round-{index:02d}",
        max_read_bytes=args.max_marker_read_bytes,
    )
    snapshot_report_path.write_text(json.dumps(snapshot_report, ensure_ascii=False, indent=2), encoding="utf-8")
    item["source_snapshot_report"] = str(snapshot_report_path)
    item["source_snapshot_summary"] = {
        "snapshot_count": int(snapshot_report.get("snapshot_count") or 0),
        "source_file_reference_count": int(snapshot_report.get("source_file_reference_count") or 0),
        "source_file_count": int(snapshot_report.get("source_file_count") or 0),
        "missing_source_file_count": int(snapshot_report.get("missing_source_file_count") or 0),
    }
    pair_scan = scan_decode_pairs_from_reports(
        [*report_paths, snapshot_report_path],
        max_read_bytes=args.max_marker_read_bytes,
    )
    raw_pairs = [*delta_raw_pairs, *pair_scan.pop("pairs", [])]
    item["decode_pair_scan"] = pair_scan
    delta_pair_summary = delta_scan.get("decode_key_pair_summary") if isinstance(delta_scan.get("decode_key_pair_summary"), list) else []
    rescan_pair_summary = pair_scan.get("redacted_pair_summary") if isinstance(pair_scan.get("redacted_pair_summary"), list) else []
    item["decode_key_pair_count"] = len(raw_pairs) if raw_pairs else int(pair_scan.get("pair_count") or 0)
    item["decode_key_pair_summary"] = [*delta_pair_summary, *rescan_pair_summary]
    if raw_pairs:
        artifact = write_sensitive_pair_artifact(raw_pairs, SENSITIVE_PAIR_ROOT, label=f"round-{index:02d}")
        item["decode_key_pair_artifact"] = str(artifact)
    item["numeric_key_pair_count"] = len(delta_raw_numeric_pairs)
    item["numeric_key_pair_summary"] = delta_scan.get("numeric_key_pair_summary", [])
    if delta_raw_numeric_pairs:
        artifact = write_sensitive_pair_artifact(
            delta_raw_numeric_pairs,
            SENSITIVE_PAIR_ROOT,
            label=f"round-{index:02d}-numeric",
        )
        item["numeric_key_pair_artifact"] = str(artifact)
    for report_path in report_paths:
        sanitize_child_report_file(report_path)
    item["child_runs"] = child_runs
    item["elapsed_seconds"] = round(time.time() - round_started, 2)
    return item


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Authorized Weixin Channels short link.")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--settle-seconds", type=float, default=5)
    parser.add_argument("--open-timeout", type=int, default=35)
    parser.add_argument("--since-minutes", type=float, default=8)
    parser.add_argument("--profile-since-minutes", type=float, default=30)
    parser.add_argument("--scan-duration", type=float, default=5)
    parser.add_argument("--burst-duration", type=float, default=7)
    parser.add_argument("--probe-timeout", type=float, default=8)
    parser.add_argument("--child-timeout", type=float, default=45)
    parser.add_argument("--max-candidates", type=int, default=8)
    parser.add_argument("--max-marker-files", type=int, default=400)
    parser.add_argument("--max-marker-file-bytes", type=int, default=80_000_000)
    parser.add_argument("--max-marker-read-bytes", type=int, default=40_000_000)
    parser.add_argument("--delta-max-files", type=int, default=2500)
    parser.add_argument("--delta-max-file-bytes", type=int, default=80_000_000)
    parser.add_argument("--delta-max-read-bytes", type=int, default=40_000_000)
    parser.add_argument(
        "--rescan-only",
        action="store_true",
        help="Do not open WeChat; rescan existing child reports under --run-dir for URL+decode_key pairs.",
    )
    parser.add_argument(
        "--extra-run-dir",
        action="append",
        default=[],
        help="Additional existing multi-open run directory to include during --rescan-only.",
    )
    parser.add_argument(
        "--source-snapshot-root",
        action="append",
        default=[],
        help="Sensitive source-snapshot root to include during --rescan-only.",
    )
    parser.add_argument(
        "--run-dir",
        default=str(
            ROOT
            / "work"
            / "sensitive-artifacts"
            / "weixin-fast-mp3"
            / f"multi-open-{time.strftime('%Y%m%d-%H%M%S')}"
        ),
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir).expanduser().resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    if args.rescan_only:
        report_path, report = write_decode_pair_rescan_report(
            run_dir,
            max_read_bytes=args.max_marker_read_bytes,
            extra_run_dirs=[Path(value) for value in args.extra_run_dir],
            source_snapshot_roots=[Path(value) for value in args.source_snapshot_root],
        )
        print(
            f"rescan_result={report['result']} "
            f"decode_pairs={report['decode_key_pair_count']} "
            f"report={report_path}",
            flush=True,
        )
        return 0 if report["decode_key_pair_count"] else 2

    report_path = run_dir / "report.json"
    report = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "url_redacted": redact_url(args.url),
        "run_dir": str(run_dir),
        "rounds": [],
    }
    for index in range(1, args.rounds + 1):
        item = run_round(args, run_dir, index)
        report["rounds"].append(item)
        report["result"] = aggregate_result(report["rounds"])
        artifacts = [str(row.get("decode_key_pair_artifact")) for row in report["rounds"] if row.get("decode_key_pair_artifact")]
        numeric_artifacts = [
            str(row.get("numeric_key_pair_artifact"))
            for row in report["rounds"]
            if row.get("numeric_key_pair_artifact")
        ]
        summaries = []
        numeric_summaries = []
        for row in report["rounds"]:
            summaries.extend(row.get("decode_key_pair_summary") or [])
            numeric_summaries.extend(row.get("numeric_key_pair_summary") or [])
        report["decode_key_pair_count"] = sum(int(row.get("decode_key_pair_count") or 0) for row in report["rounds"])
        report["decode_key_pair_summary"] = summaries
        report["numeric_key_pair_count"] = sum(int(row.get("numeric_key_pair_count") or 0) for row in report["rounds"])
        report["numeric_key_pair_summary"] = numeric_summaries
        if artifacts:
            report["decode_key_pair_artifact"] = artifacts[-1]
        if numeric_artifacts:
            report["numeric_key_pair_artifact"] = numeric_artifacts[-1]
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            f"round={index} result={report['result']} "
            f"inline_urls={item.get('inline_marker_scan', {}).get('candidate_url_count', 0)} "
            f"decode_pairs={item.get('decode_key_pair_count', 0)} "
            f"numeric_pairs={item.get('numeric_key_pair_count', 0)} "
            f"report={report_path}",
            flush=True,
        )
    report["result"] = aggregate_result(report["rounds"])
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"multi_open_result={report['result']} report={report_path}")
    return 0 if report["result"] in {
        "decode_key_pair_found",
        "numeric_key_pair_found",
        "playable_audio_source_found",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
