#!/usr/bin/env python3
"""High-frequency watcher for desktop WeChat seek/playback burst artifacts.

This is intentionally narrower than the general current-playback watcher:
it focuses on WeChat temp/media cache roots and open playback file descriptors,
so quick seek operations are less likely to be missed by a full directory walk.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from weixin_current_playback_delta_to_mp3 import (  # noqa: E402
    LsofRecord,
    duration_seconds,
    find_ffmpeg_tool,
    parse_lsof_field_output,
)


FAST_ROOTS = [
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/tmp",
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/net/cdncomm",
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium",
    Path.home() / "Library/Group Containers/5A4RE8SF68.com.tencent.xinWeChat/Library/Caches",
]

MEDIA_EXTS = {".mp4", ".mov", ".m4a", ".mp3", ".wav", ".webm", ".mkv", ".aac", ".ogg", ".opus", ".m3u8", ".ts"}
MEDIA_HINTS = (
    ".5a4re8sf68.com.tencent.xinwechat",
    "blob_storage",
    "cdncomm",
    "finder",
    "hls",
    "live",
    "media",
    "mmbiz",
    "playback",
    "stodownload",
    "video",
)
SKIP_PARTS = (
    "account web data",
    "cache/gpu",
    "code cache",
    "cookies",
    "dawngraphitecache",
    "dawnwebgpucache",
    "db_storage",
    "favicons",
    "gpucache",
    "grshadercache",
    "heavy_ad_intervention",
    "history",
    "indexeddb",
    "local storage",
    "quota",
    "shadercache",
    "shared_proto_db",
    "shortcuts",
    "service worker/database",
    "trust tokens",
    "visited links",
    "web data",
    "webstorage",
)
SKIP_SUFFIXES = (".db", ".db-journal", ".db-shm", ".db-wal", ".ldb", ".log", ".plist", ".sqlite", ".tmp")


def safe_roots() -> list[Path]:
    return [root for root in FAST_ROOTS if root.exists()]


def under_fast_root(path_text: str) -> bool:
    try:
        path = Path(path_text.split(" (", 1)[0]).expanduser()
    except Exception:
        return False
    text = str(path)
    return any(text == str(root) or text.startswith(str(root) + "/") for root in FAST_ROOTS)


def relative_label(path: Path | str) -> str:
    text = str(path).split(" (", 1)[0]
    for root in FAST_ROOTS:
        try:
            return str(Path(text).resolve().relative_to(root.resolve()))
        except Exception:
            continue
    return text


def likely_media(path: Path | str, size: int, min_size: int) -> bool:
    if size < min_size:
        return False
    lower = str(path).lower()
    if any(part in lower for part in SKIP_PARTS):
        return False
    if lower.endswith(SKIP_SUFFIXES):
        return False
    suffix = Path(str(path).split(" (", 1)[0]).suffix.lower()
    return suffix in MEDIA_EXTS or any(hint in lower for hint in MEDIA_HINTS)


def snapshot_visible(root: Path, min_size: int, max_depth: int, max_files: int) -> dict[str, tuple[int, int]]:
    found: dict[str, tuple[int, int]] = {}
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack and len(found) < max_files:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    lower = str(path).lower()
                    if any(part in lower for part in SKIP_PARTS):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if depth < max_depth:
                                stack.append((path, depth + 1))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if likely_media(path, stat.st_size, min_size):
                        found[str(path)] = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            continue
    return found


def visible_snapshot(min_size: int, max_depth: int, max_files_per_root: int) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for root in safe_roots():
        result.update(snapshot_visible(root, min_size, max_depth, max_files_per_root))
    return result


def parse_lsof_relevant(text: str, min_size: int) -> dict[str, LsofRecord]:
    records = {}
    for record in parse_lsof_field_output(text):
        if not under_fast_root(record.path):
            continue
        if not likely_media(record.path, record.size, min_size):
            continue
        records[f"{record.pid}:{record.fd}:{record.path}"] = record
    return records


def lsof_snapshot(min_size: int, timeout: float) -> dict[str, LsofRecord]:
    cmd = ["lsof", "-nP", "-F", "pcfnst", "-c", "WeChat", "-c", "WeChatAppEx", "-c", "wxplayer"]
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    except Exception:
        return {}
    return parse_lsof_relevant(proc.stdout, min_size)


def stream_info(path: Path, timeout: float = 8) -> str:
    ffmpeg = find_ffmpeg_tool("ffmpeg")
    proc = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], text=True, capture_output=True, timeout=timeout)
    return f"{proc.stdout}\n{proc.stderr}"


def playable_duration(path: Path, timeout: float = 8) -> float:
    try:
        info = stream_info(path, timeout=timeout)
    except Exception:
        return 0.0
    if "Audio:" not in info:
        return 0.0
    return duration_seconds(info)


def copy_candidate(path: Path, artifact_dir: Path) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", relative_label(path)).strip("-._") or "candidate.bin"
    if len(label) > 128:
        label = label[-128:]
    dest = artifact_dir / label
    if dest.exists():
        dest = artifact_dir / f"{time.time_ns()}-{label}"
    shutil.copy2(path, dest)
    return dest


def convert(path: Path, output: Path) -> None:
    converter = SCRIPT_DIR / "media_url_to_mp3.py"
    subprocess.run([sys.executable, str(converter), str(path), "--output", str(output)], check=True)


def attempt_candidate(path: Path, output: Path, artifact_dir: Path, min_duration: float, report: dict) -> bool:
    try:
        size = path.stat().st_size
    except OSError:
        return False
    print(f"candidate {size} bytes: {relative_label(path)}", flush=True)
    try:
        captured = copy_candidate(path, artifact_dir / "captured")
    except OSError as exc:
        report["attempts"].append({"path": str(path), "copy_error": str(exc)})
        return False
    duration = playable_duration(captured)
    attempt = {
        "path": str(path),
        "captured": str(captured),
        "bytes": captured.stat().st_size,
        "duration": duration,
    }
    report["attempts"].append(attempt)
    if duration < min_duration:
        return False
    try:
        convert(captured, output)
    except subprocess.CalledProcessError as exc:
        attempt["conversion_error"] = str(exc)
        if output.exists() and output.stat().st_size == 0:
            output.unlink()
        return False
    output_duration = playable_duration(output)
    attempt["output_duration"] = output_duration
    if output_duration < min_duration:
        if output.exists():
            output.unlink()
        return False
    report["result"] = {"source": str(captured), "output": str(output), "duration": output_duration}
    return True


def record_visible_event(path_text: str, stat: tuple[int, int], previous: tuple[int, int] | None) -> dict:
    return {
        "path": path_text,
        "relative_path": relative_label(path_text),
        "bytes": stat[0],
        "mtime_ns": stat[1],
        "size_delta": None if previous is None else stat[0] - previous[0],
    }


def record_lsof_event(record: LsofRecord, previous: LsofRecord | None) -> dict:
    data = asdict(record)
    data["relative_path"] = relative_label(record.path)
    data["exists_as_path"] = Path(record.path.split(" (", 1)[0]).exists()
    data["size_delta"] = None if previous is None else record.size - previous.size
    return data


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "outputs" / "weixin_seek_burst.mp3"))
    parser.add_argument("--duration", type=float, default=90)
    parser.add_argument("--poll-interval", type=float, default=0.2)
    parser.add_argument("--lsof-interval", type=float, default=1.0)
    parser.add_argument("--lsof-timeout", type=float, default=5)
    parser.add_argument("--min-size", type=int, default=4096)
    parser.add_argument("--min-duration", type=float, default=180)
    parser.add_argument("--max-depth", type=int, default=8)
    parser.add_argument("--max-files-per-root", type=int, default=5000)
    parser.add_argument("--artifact-dir", default=str(ROOT / "work" / "sensitive-artifacts" / "weixin-seek-burst"))
    parser.add_argument("--report", default="")
    parser.add_argument("--list-only", action="store_true")
    args = parser.parse_args()

    output = Path(args.output).expanduser().resolve()
    artifact_dir = Path(args.artifact_dir).expanduser().resolve()
    report_path = Path(args.report).expanduser().resolve() if args.report else artifact_dir / "report.json"
    report = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "roots": [str(root) for root in safe_roots()],
        "visible_events": [],
        "lsof_events": [],
        "unreadable_lsof": [],
        "attempts": [],
    }

    visible_known = visible_snapshot(args.min_size, args.max_depth, args.max_files_per_root)
    lsof_known = lsof_snapshot(args.min_size, args.lsof_timeout)
    report["baseline_visible"] = [
        record_visible_event(path, stat, None)
        for path, stat in sorted(visible_known.items(), key=lambda item: item[1][0], reverse=True)[:80]
    ]
    report["baseline_lsof"] = [
        record_lsof_event(record, None)
        for record in sorted(lsof_known.values(), key=lambda item: item.size, reverse=True)[:80]
    ]
    write_report(report_path, report)

    print("High-frequency WeChat playback watcher is running.", flush=True)
    print("During this window, seek/click the progress bar a few times if possible.", flush=True)
    deadline = time.time() + args.duration
    next_lsof = 0.0
    while time.time() < deadline:
        current_visible = visible_snapshot(args.min_size, args.max_depth, args.max_files_per_root)
        for path_text, stat in sorted(current_visible.items(), key=lambda item: item[1][1]):
            previous = visible_known.get(path_text)
            if previous and previous == stat:
                continue
            visible_known[path_text] = stat
            event = record_visible_event(path_text, stat, previous)
            report["visible_events"].append(event)
            if not args.list_only and attempt_candidate(Path(path_text), output, artifact_dir, args.min_duration, report):
                write_report(report_path, report)
                return 0

        now = time.time()
        if now >= next_lsof:
            current_lsof = lsof_snapshot(args.min_size, args.lsof_timeout)
            next_lsof = now + args.lsof_interval
            for key, record in sorted(current_lsof.items(), key=lambda item: item[1].path):
                previous = lsof_known.get(key)
                if previous and previous.size == record.size:
                    continue
                lsof_known[key] = record
                event = record_lsof_event(record, previous)
                report["lsof_events"].append(event)
                path = Path(record.path.split(" (", 1)[0])
                if path.exists():
                    if not args.list_only and attempt_candidate(path, output, artifact_dir, args.min_duration, report):
                        write_report(report_path, report)
                        return 0
                else:
                    report["unreadable_lsof"].append(event)
                    print(
                        "open playback fd is not available as a normal file: "
                        f"{record.command} pid={record.pid} fd={record.fd} size={record.size}",
                        flush=True,
                    )
        if len(report["visible_events"]) > 1000:
            report["visible_events"] = report["visible_events"][-1000:]
        if len(report["lsof_events"]) > 1000:
            report["lsof_events"] = report["lsof_events"][-1000:]
        if len(report["unreadable_lsof"]) > 1000:
            report["unreadable_lsof"] = report["unreadable_lsof"][-1000:]
        write_report(report_path, report)
        time.sleep(args.poll_interval)

    report["result"] = {"error": "no_playable_burst_media_file"}
    write_report(report_path, report)
    print("No playable burst media artifact found.", flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
