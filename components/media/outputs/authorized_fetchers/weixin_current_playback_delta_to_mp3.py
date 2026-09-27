#!/usr/bin/env python3
"""Watch the current desktop WeChat playback session for new or changed media files."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WATCH_ROOTS = [
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium",
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/net/cdncomm",
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/tmp",
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files",
    Path.home() / "Library/Group Containers/5A4RE8SF68.com.tencent.xinWeChat/Library/Caches",
]
MEDIA_EXTS = {".mp4", ".mov", ".m4a", ".mp3", ".wav", ".webm", ".mkv", ".aac", ".ogg", ".opus", ".m3u8", ".ts"}
MEDIA_HINTS = (
    "blob_storage",
    "media",
    "video",
    "finder",
    "live",
    "playback",
    "stodownload",
    "hls",
    "m3u8",
    ".5a4re8sf68.com.tencent.xinwechat",
)
SKIP_PATH_PARTS = (
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
DIAGNOSTIC_SKIP_PATH_PARTS = (
    "account web data",
    "cookies",
    "favicons",
    "history",
    "local storage",
    "quota",
    "shortcuts",
    "trust tokens",
    "visited links",
    "web data",
)
SKIP_SUFFIXES = (
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".ldb",
    ".log",
    ".mmap",
    ".plist",
    ".sqlite",
    ".tmp",
)


@dataclass
class FileStat:
    size: int
    mtime_ns: int


@dataclass
class LsofRecord:
    pid: str
    command: str
    fd: str
    kind: str
    size: int
    path: str


def find_ffmpeg_tool(name: str) -> str:
    env = os.environ.get(name.upper())
    if env and Path(env).exists():
        return env
    found = shutil.which(name)
    if found:
        return found
    candidates = sorted(
        [
            *(ROOT / "work" / "venv" / "lib").glob(
                f"python*/site-packages/imageio_ffmpeg/binaries/{name}-*"
            ),
            *(ROOT / "work" / "venv" / "Lib" / "site-packages").glob(
                f"imageio_ffmpeg/binaries/{name}-*"
            ),
        ]
    )
    if candidates:
        return str(candidates[0])
    if name == "ffprobe":
        ffmpeg = find_ffmpeg_tool("ffmpeg")
        probe = Path(ffmpeg).with_name(Path(ffmpeg).name.replace("ffmpeg", "ffprobe", 1))
        if probe.exists():
            return str(probe)
    raise SystemExit(f"{name} not found.")


def duration_seconds(stream_info: str) -> float:
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stream_info)
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def safe_roots() -> list[Path]:
    return [root for root in WATCH_ROOTS if root.exists()]


def relative_to_watch_root(path: Path | str) -> str:
    text = str(path)
    for root in WATCH_ROOTS:
        try:
            return str(Path(text).resolve().relative_to(root.resolve()))
        except Exception:
            continue
    return text


def under_watch_root(path: str) -> bool:
    try:
        target = Path(path).expanduser()
    except Exception:
        return False
    text = str(target).replace("\\", "/")
    if "com.tencent.xinWeChat/Data/tmp" in text:
        return True
    for root in WATCH_ROOTS:
        root_text = str(root).replace("\\", "/").rstrip("/")
        if text == root_text or text.startswith(root_text + "/"):
            return True
    return False


def likely_media_candidate(path: Path | str, size: int, min_size: int) -> bool:
    if size < min_size:
        return False
    lower = str(path).lower()
    if any(part in lower for part in SKIP_PATH_PARTS):
        return False
    if lower.endswith(SKIP_SUFFIXES):
        return False
    suffix = Path(str(path).split(" (", 1)[0]).suffix.lower()
    return suffix in MEDIA_EXTS or any(hint in lower for hint in MEDIA_HINTS)


def diagnostic_lsof_candidate(path: Path | str) -> bool:
    lower = str(path).lower()
    if any(part in lower for part in SKIP_PATH_PARTS):
        return False
    if lower.endswith(SKIP_SUFFIXES):
        return False
    return True


def diagnostic_visible_candidate(path: Path | str) -> bool:
    lower = str(path).lower()
    if any(part in lower for part in SKIP_PATH_PARTS):
        return False
    if lower.endswith(SKIP_SUFFIXES):
        return False
    return True


def scan_visible_files(min_size: int) -> dict[str, FileStat]:
    stats: dict[str, FileStat] = {}
    for root in safe_roots():
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                name
                for name in dirnames
                if not any(part in str(Path(dirpath, name)).lower() for part in SKIP_PATH_PARTS)
            ]
            for name in filenames:
                path = Path(dirpath) / name
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_size < min_size and not likely_media_candidate(path, stat.st_size, 0):
                    continue
                stats[str(path)] = FileStat(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
    return stats


def scan_recent_visible_files(min_size: int, since: float) -> dict[str, FileStat]:
    stats: dict[str, FileStat] = {}
    since_ns = int(since * 1_000_000_000)
    for root in safe_roots():
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                name
                for name in dirnames
                if diagnostic_visible_candidate(Path(dirpath, name))
            ]
            for name in filenames:
                path = Path(dirpath) / name
                if not diagnostic_visible_candidate(path):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_mtime_ns < since_ns or stat.st_size < min_size:
                    continue
                stats[str(path)] = FileStat(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
    return stats


def parse_lsof_field_output(text: str) -> list[LsofRecord]:
    records: list[LsofRecord] = []
    current = {"pid": "", "command": "", "fd": "", "kind": "", "size": 0}
    for raw in text.splitlines():
        if not raw:
            continue
        prefix, value = raw[0], raw[1:]
        if prefix == "p":
            current = {"pid": value, "command": current.get("command", ""), "fd": "", "kind": "", "size": 0}
        elif prefix == "c":
            current["command"] = value
        elif prefix == "f":
            current["fd"] = value
            current["kind"] = ""
            current["size"] = 0
        elif prefix == "t":
            current["kind"] = value
        elif prefix == "s":
            try:
                current["size"] = int(value)
            except ValueError:
                current["size"] = 0
        elif prefix == "n":
            if under_watch_root(value):
                records.append(
                    LsofRecord(
                        pid=str(current.get("pid", "")),
                        command=str(current.get("command", "")),
                        fd=str(current.get("fd", "")),
                        kind=str(current.get("kind", "")),
                        size=int(current.get("size", 0)),
                        path=value,
                    )
                )
    return records


def scan_lsof(media_only: bool = True) -> dict[str, LsofRecord]:
    cmd = ["lsof", "-nP", "-F", "pcfnst", "-c", "WeChat", "-c", "WeChatAppEx", "-c", "wxplayer"]
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=8)
    except Exception:
        return {}
    records = parse_lsof_field_output(proc.stdout)
    filtered = {
        f"{record.pid}:{record.fd}:{record.path}": record
        for record in records
        if (
            likely_media_candidate(record.path, record.size, min_size=50_000)
            if media_only
            else diagnostic_lsof_candidate(record.path)
        )
    }
    return filtered


def report_file_event(path_text: str, stat: FileStat, previous: FileStat | None) -> dict:
    size_delta = None if previous is None else stat.size - previous.size
    return {
        "path": path_text,
        "relative_path": relative_to_watch_root(path_text),
        "bytes": stat.size,
        "size_delta": size_delta,
        "changed": bool(previous),
        "mtime_ns": stat.mtime_ns,
        "media_candidate": likely_media_candidate(path_text, stat.size, min_size=50_000),
    }


def report_lsof_record(record: LsofRecord, previous: LsofRecord | None = None) -> dict:
    payload = asdict(record)
    payload["relative_path"] = relative_to_watch_root(record.path)
    payload["exists_as_path"] = Path(record.path.split(" (", 1)[0]).exists()
    payload["size_delta"] = None if previous is None else record.size - previous.size
    payload["media_candidate"] = likely_media_candidate(record.path, record.size, min_size=50_000)
    return payload


def stream_info(path: Path, timeout: float = 10) -> str:
    ffmpeg = find_ffmpeg_tool("ffmpeg")
    proc = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], text=True, capture_output=True, timeout=timeout)
    return f"{proc.stdout}\n{proc.stderr}"


def playable_duration(path: Path) -> float:
    try:
        info = stream_info(path)
    except Exception:
        return 0.0
    if "Audio:" not in info:
        return 0.0
    return duration_seconds(info)


def wait_until_stable(path: Path, stable_seconds: float, poll_interval: float) -> bool:
    try:
        previous = path.stat().st_size
    except OSError:
        return False
    stable_since = time.time()
    deadline = time.time() + max(stable_seconds * 4, stable_seconds + 2)
    while time.time() < deadline:
        time.sleep(min(poll_interval, 1.0))
        try:
            current = path.stat().st_size
        except OSError:
            return False
        if current != previous:
            previous = current
            stable_since = time.time()
        elif time.time() - stable_since >= stable_seconds:
            return True
    return False


def copy_candidate(path: Path, artifact_dir: Path) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", relative_to_watch_root(path)).strip("-._")
    if len(safe_name) > 120:
        safe_name = safe_name[-120:]
    dest = artifact_dir / (safe_name or "candidate.bin")
    if dest.exists():
        dest = artifact_dir / f"{int(time.time())}-{dest.name}"
    shutil.copy2(path, dest)
    return dest


def convert(path: Path, output: Path) -> None:
    converter = Path(__file__).with_name("media_url_to_mp3.py")
    subprocess.run([sys.executable, str(converter), str(path), "--output", str(output)], check=True)


def attempt_visible_candidate(
    path: Path,
    output: Path,
    artifacts: Path,
    min_duration: float,
    stable_seconds: float,
    poll_interval: float,
    report: dict,
) -> bool:
    print(f"candidate changed {path.stat().st_size} bytes: {relative_to_watch_root(path)}", flush=True)
    if stable_seconds > 0 and not wait_until_stable(path, stable_seconds, poll_interval):
        report["attempts"].append({"path": str(path), "result": "not_stable"})
        return False
    captured = copy_candidate(path, artifacts / "visible-candidates")
    duration = playable_duration(captured)
    report["attempts"].append(
        {
            "path": str(path),
            "captured": str(captured),
            "bytes": captured.stat().st_size,
            "duration": duration,
        }
    )
    if duration < min_duration:
        return False
    try:
        convert(captured, output)
    except subprocess.CalledProcessError as exc:
        report["attempts"][-1]["conversion_error"] = str(exc)
        if output.exists() and output.stat().st_size == 0:
            output.unlink()
        return False
    output_duration = playable_duration(output)
    report["attempts"][-1]["output_duration"] = output_duration
    if output_duration < min_duration:
        if output.exists():
            output.unlink()
        return False
    report["result"] = {"source": str(captured), "duration": output_duration, "output": str(output)}
    return True


def unreadable_media_fds(report: dict) -> list[dict]:
    rows: list[dict] = []
    for key in ("baseline_lsof", "unreadable_lsof"):
        values = report.get(key)
        if not isinstance(values, list):
            continue
        for row in values:
            if (
                isinstance(row, dict)
                and row.get("media_candidate")
                and not row.get("exists_as_path")
            ):
                rows.append(row)
    return sorted(rows, key=lambda row: int(row.get("size") or row.get("bytes") or 0), reverse=True)


def _fd_digits(value: object) -> str:
    match = re.match(r"(\d+)", str(value or ""))
    return match.group(1) if match else ""


def probe_unreadable_fd_access(rows: list[dict], path_exists=None, sample_limit: int = 5) -> dict:
    exists = path_exists or (lambda path: Path(str(path)).exists())
    samples: list[dict] = []
    safe_copy_possible = False
    for row in rows[:sample_limit]:
        pid = str(row.get("pid") or "")
        fd = _fd_digits(row.get("fd"))
        original = str(row.get("path") or "")
        if not original and row.get("relative_path"):
            original = str(row.get("relative_path") or "")
        original_path = original.split(" (", 1)[0]
        proc_fd = f"/proc/{pid}/fd/{fd}" if pid and fd else ""
        dev_fd_pid_scoped = f"/dev/fd/{pid}/{fd}" if pid and fd else ""
        original_exists = bool(original_path and exists(original_path))
        proc_exists = bool(proc_fd and exists(proc_fd))
        dev_pid_exists = bool(dev_fd_pid_scoped and exists(dev_fd_pid_scoped))
        safe_copy_possible = safe_copy_possible or original_exists or proc_exists or dev_pid_exists
        samples.append(
            {
                "pid": pid,
                "fd": fd,
                "size": int(row.get("size") or row.get("bytes") or 0),
                "relative_path": row.get("relative_path"),
                "original_path_exists": original_exists,
                "proc_pid_fd_exists": proc_exists,
                "dev_fd_pid_scoped_exists": dev_pid_exists,
                "raw_dev_fd_probe": "not_attempted_not_pid_scoped",
            }
        )
    if not rows:
        limit_point = "no_unreadable_media_fd"
    elif safe_copy_possible:
        limit_point = "safe_filesystem_alias_available"
    else:
        limit_point = "renderer_fd_has_no_safe_filesystem_alias"
    return {
        "checked_count": len(samples),
        "safe_copy_possible": safe_copy_possible,
        "limit_point": limit_point,
        "samples": samples,
    }


def scrub_report_for_storage(report: dict) -> None:
    for key in ("baseline_all_lsof", "all_lsof_events"):
        values = report.get(key)
        if not isinstance(values, list):
            continue
        report[key] = [
            row
            for row in values
            if not isinstance(row, dict)
            or not row.get("path")
            or diagnostic_lsof_candidate(str(row.get("path") or ""))
        ]
    for key in ("baseline_recent_visible", "recent_visible_changes"):
        values = report.get(key)
        if not isinstance(values, list):
            continue
        report[key] = [
            row
            for row in values
            if not isinstance(row, dict)
            or not row.get("path")
            or diagnostic_visible_candidate(str(row.get("path") or ""))
        ]


def refresh_report_diagnostics(report: dict) -> None:
    visible_media_events = [
        row
        for row in report.get("visible_events", [])
        if isinstance(row, dict) and row.get("media_candidate")
    ]
    unreadable = unreadable_media_fds(report)
    result = report.get("result") if isinstance(report.get("result"), dict) else {}
    if unreadable and not visible_media_events:
        diagnosis = "playback_fd_unlinked"
    elif result.get("error") == "no_playable_changed_media_file":
        diagnosis = "no_playable_changed_media_file"
    elif visible_media_events:
        diagnosis = "visible_media_changed_but_not_converted"
    else:
        diagnosis = "watching_no_media_signal_yet"
    report["diagnosis"] = diagnosis
    report["baseline_unreadable_media_fd_count"] = len(
        [
            row
            for row in report.get("baseline_lsof", [])
            if isinstance(row, dict) and row.get("media_candidate") and not row.get("exists_as_path")
        ]
    )
    report["unreadable_media_fd_event_count"] = len(
        [
            row
            for row in report.get("unreadable_lsof", [])
            if isinstance(row, dict) and row.get("media_candidate") and not row.get("exists_as_path")
        ]
    )
    report["largest_unreadable_fd_bytes"] = int(unreadable[0].get("size") or 0) if unreadable else 0
    report["sample_unreadable_fds"] = [
        {
            "pid": row.get("pid"),
            "command": row.get("command"),
            "fd": row.get("fd"),
            "size": row.get("size"),
            "relative_path": row.get("relative_path"),
        }
        for row in unreadable[:5]
    ]
    report["unreadable_fd_access_probe"] = probe_unreadable_fd_access(unreadable)


def write_report(report_path: Path | None, report: dict) -> None:
    if not report_path:
        return
    refresh_report_diagnostics(report)
    scrub_report_for_storage(report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "outputs" / "weixin_video_channel.mp3"))
    parser.add_argument("--duration", type=float, default=300)
    parser.add_argument("--baseline-seconds", type=float, default=2)
    parser.add_argument("--poll-interval", type=float, default=1)
    parser.add_argument("--min-size", type=int, default=50_000)
    parser.add_argument("--min-duration", type=float, default=180)
    parser.add_argument("--stable-seconds", type=float, default=2)
    parser.add_argument("--artifact-dir", default=str(ROOT / "work" / "sensitive-artifacts" / "weixin-current-delta"))
    parser.add_argument("--report", default="")
    parser.add_argument("--list-only", action="store_true")
    args = parser.parse_args()

    output = Path(args.output).expanduser().resolve()
    artifacts = Path(args.artifact_dir).expanduser().resolve()
    report_path = Path(args.report).expanduser().resolve() if args.report else artifacts / "weixin_current_playback_delta.json"
    report: dict = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "watch_roots": [str(root) for root in safe_roots()],
        "baseline_lsof": [],
        "baseline_all_lsof": [],
        "baseline_visible_candidates": [],
        "baseline_recent_visible": [],
        "visible_events": [],
        "recent_visible_changes": [],
        "lsof_events": [],
        "all_lsof_events": [],
        "unreadable_lsof": [],
        "attempts": [],
    }

    print("Watching current logged-in desktop WeChat playback without reopening the target.")
    print(f"Taking baseline for {args.baseline_seconds:g}s.", flush=True)
    diagnostic_since = time.time() - max(args.baseline_seconds, 60)
    visible_baseline = scan_visible_files(args.min_size)
    lsof_baseline = scan_lsof()
    all_lsof_baseline = scan_lsof(media_only=False)
    recent_visible_baseline = scan_recent_visible_files(max(1024, min(args.min_size, 50_000)), diagnostic_since)
    time.sleep(max(0, args.baseline_seconds))
    visible_known = dict(visible_baseline)
    visible_known.update(scan_visible_files(args.min_size))
    lsof_known = dict(lsof_baseline)
    lsof_known.update(scan_lsof())
    all_lsof_known = dict(all_lsof_baseline)
    all_lsof_known.update(scan_lsof(media_only=False))
    recent_visible_known = dict(recent_visible_baseline)
    recent_visible_known.update(scan_recent_visible_files(max(1024, min(args.min_size, 50_000)), diagnostic_since))
    report["baseline_visible_candidates"] = [
        report_file_event(path, stat, previous=None)
        for path, stat in sorted(visible_known.items(), key=lambda item: item[1].size, reverse=True)
        if likely_media_candidate(path, stat.size, args.min_size)
    ][:80]
    report["baseline_recent_visible"] = [
        report_file_event(path, stat, previous=None)
        for path, stat in sorted(recent_visible_known.items(), key=lambda item: item[1].mtime_ns, reverse=True)
    ][:120]
    report["baseline_lsof"] = [
        report_lsof_record(record)
        for record in sorted(lsof_known.values(), key=lambda item: item.size, reverse=True)
    ][:120]
    report["baseline_all_lsof"] = [
        report_lsof_record(record)
        for record in sorted(all_lsof_known.values(), key=lambda item: item.size, reverse=True)
    ][:200]
    for record in report["baseline_lsof"][:20]:
        if not record["exists_as_path"]:
            print(
                "baseline open playback/temp fd is not available as a normal file: "
                f"{record['command']} pid={record['pid']} fd={record['fd']} size={record['size']}",
                flush=True,
            )
    write_report(report_path, report)
    deadline = time.time() + args.duration

    while time.time() < deadline:
        current_visible = scan_visible_files(args.min_size)
        for path_text, stat in sorted(current_visible.items(), key=lambda item: item[1].mtime_ns):
            previous = visible_known.get(path_text)
            if previous and previous.size == stat.size and previous.mtime_ns == stat.mtime_ns:
                continue
            visible_known[path_text] = stat
            path = Path(path_text)
            if not likely_media_candidate(path, stat.size, args.min_size):
                continue
            event = report_file_event(path_text, stat, previous)
            report["visible_events"].append(event)
            if args.list_only:
                continue
            if attempt_visible_candidate(
                path,
                output,
                artifacts,
                args.min_duration,
                args.stable_seconds,
                args.poll_interval,
                report,
            ):
                    write_report(report_path, report)
                    return 0

        current_recent_visible = scan_recent_visible_files(max(1024, min(args.min_size, 50_000)), diagnostic_since)
        for path_text, stat in sorted(current_recent_visible.items(), key=lambda item: item[1].mtime_ns):
            previous = recent_visible_known.get(path_text)
            if previous and previous.size == stat.size and previous.mtime_ns == stat.mtime_ns:
                continue
            recent_visible_known[path_text] = stat
            report["recent_visible_changes"].append(report_file_event(path_text, stat, previous))
        if len(report["recent_visible_changes"]) > 500:
            report["recent_visible_changes"] = report["recent_visible_changes"][-500:]

        current_lsof = scan_lsof()
        for key, record in sorted(current_lsof.items(), key=lambda item: item[1].path):
            previous = lsof_known.get(key)
            if previous and previous.size == record.size:
                continue
            lsof_known[key] = record
            event = report_lsof_record(record, previous)
            report["lsof_events"].append(event)
            path = Path(record.path.split(" (", 1)[0])
            if path.exists() and not args.list_only:
                if attempt_visible_candidate(
                    path,
                    output,
                    artifacts,
                    args.min_duration,
                    args.stable_seconds,
                    args.poll_interval,
                    report,
                ):
                    write_report(report_path, report)
                    return 0
            else:
                report["unreadable_lsof"].append(event)
                print(
                    "open playback fd is not available as a normal file: "
                    f"{record.command} pid={record.pid} fd={record.fd} size={record.size}",
                    flush=True,
                )

        current_all_lsof = scan_lsof(media_only=False)
        for key, record in sorted(current_all_lsof.items(), key=lambda item: item[1].path):
            previous = all_lsof_known.get(key)
            if previous and previous.size == record.size:
                continue
            all_lsof_known[key] = record
            report["all_lsof_events"].append(report_lsof_record(record, previous))
        if len(report["all_lsof_events"]) > 500:
            report["all_lsof_events"] = report["all_lsof_events"][-500:]

        write_report(report_path, report)
        time.sleep(args.poll_interval)

    report["result"] = {"error": "no_playable_changed_media_file"}
    write_report(report_path, report)
    print("No playable changed WeChat playback media file found.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
