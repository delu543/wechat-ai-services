from __future__ import annotations

import hashlib
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .ffmpeg_tools import probe_media
from .media_classifier import classify_header, header_hex, likely_artifact_role, read_header
from .report_writer import write_audit_reports


MEDIA_SUFFIXES = {".mp4", ".m4a", ".m4s", ".ts", ".aac", ".mp3", ".webm", ".mov", ".flv"}


@dataclass
class FileSnapshot:
    path: str
    size: int
    mtime: float
    ctime: float
    mtime_ns: int
    ctime_ns: int
    inode: int
    device: int
    sha256: str
    header_hex: str
    header_bytes_read: int
    classification: Dict
    artifact_role: str


def sha256_for_file(path: Path, max_bytes: int) -> str:
    try:
        if path.stat().st_size > max_bytes:
            return ""
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def snapshot_file(path: Path, hash_max_bytes: int, header_bytes: int) -> Optional[FileSnapshot]:
    try:
        stat = path.stat()
        if not path.is_file():
            return None
    except OSError:
        return None

    header = read_header(path, header_bytes)
    classification = classify_header(header, path)
    return FileSnapshot(
        path=str(path),
        size=stat.st_size,
        mtime=stat.st_mtime,
        ctime=stat.st_ctime,
        mtime_ns=stat.st_mtime_ns,
        ctime_ns=stat.st_ctime_ns,
        inode=stat.st_ino,
        device=stat.st_dev,
        sha256=sha256_for_file(path, hash_max_bytes),
        header_hex=header_hex(header, header_bytes),
        header_bytes_read=len(header),
        classification=asdict(classification),
        artifact_role=likely_artifact_role(classification, path),
    )


def iter_files(dirs: Iterable[Path]) -> Iterable[Path]:
    for root in dirs:
        if not root.exists():
            continue
        if root.is_file():
            yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in {".git", "__pycache__"}]
            for name in filenames:
                yield Path(dirpath) / name


def build_snapshot(dirs: Iterable[Path], hash_max_bytes: int, header_bytes: int) -> Dict[str, FileSnapshot]:
    snapshot: Dict[str, FileSnapshot] = {}
    for path in iter_files(dirs):
        item = snapshot_file(path, hash_max_bytes, header_bytes)
        if item:
            snapshot[item.path] = item
    return snapshot


def media_probe_needed(snapshot: FileSnapshot, probe_min_bytes: int) -> bool:
    suffix = Path(snapshot.path).suffix.lower()
    if snapshot.size < probe_min_bytes:
        return False
    if snapshot.classification.get("media_candidate"):
        return True
    return suffix in MEDIA_SUFFIXES or "media" in snapshot.path.lower() or "video" in snapshot.path.lower()


def diff_snapshots(
    previous: Dict[str, FileSnapshot],
    current: Dict[str, FileSnapshot],
    probe_min_bytes: int,
) -> List[Dict]:
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    events: List[Dict] = []
    previous_paths = set(previous)
    current_paths = set(current)

    for path in sorted(current_paths - previous_paths):
        item = current[path]
        event = {"time": now, "event": "created", "path": path, "current": asdict(item)}
        if media_probe_needed(item, probe_min_bytes):
            event["ffprobe"] = probe_media(path)
        events.append(event)

    for path in sorted(previous_paths - current_paths):
        events.append({"time": now, "event": "deleted", "path": path, "previous": asdict(previous[path])})

    for path in sorted(previous_paths & current_paths):
        old = previous[path]
        new = current[path]
        changes = []
        if old.size != new.size:
            changes.append("size")
        if old.mtime_ns != new.mtime_ns:
            changes.append("mtime")
        if old.ctime_ns != new.ctime_ns:
            changes.append("ctime")
        if not changes:
            continue
        event = {
            "time": now,
            "event": "changed",
            "path": path,
            "changes": changes,
            "previous": asdict(old),
            "current": asdict(new),
        }
        if media_probe_needed(new, probe_min_bytes):
            event["ffprobe"] = probe_media(path)
        events.append(event)
    return events


def run_audit(
    dirs: List[str],
    duration: float,
    out_prefix: str,
    interval: float = 1.0,
    hash_max_mb: float = 50.0,
    header_bytes: int = 256,
    probe_min_kb: float = 32.0,
) -> Dict:
    roots = [Path(item).expanduser().resolve() for item in dirs]
    hash_max_bytes = int(hash_max_mb * 1024 * 1024)
    probe_min_bytes = int(probe_min_kb * 1024)
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    baseline = build_snapshot(roots, hash_max_bytes, header_bytes)
    known = dict(baseline)
    events: List[Dict] = []
    deadline = time.time() + duration

    while time.time() < deadline:
        time.sleep(interval)
        current = build_snapshot(roots, hash_max_bytes, header_bytes)
        new_events = diff_snapshots(known, current, probe_min_bytes)
        events.extend(new_events)
        known = current

    report = {
        "tool": "video-audio-extractor cache auditor",
        "started_at": started,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "duration_seconds": duration,
        "interval_seconds": interval,
        "directories": [str(root) for root in roots],
        "hash_max_mb": hash_max_mb,
        "header_bytes": max(64, min(header_bytes, 256)),
        "probe_min_kb": probe_min_kb,
        "baseline_count": len(baseline),
        "events": events,
    }
    report["outputs"] = write_audit_reports(Path(out_prefix).expanduser().resolve(), report)
    return report
