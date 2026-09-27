#!/usr/bin/env python3
"""Watch new low-intrusion WeChat media/cache files and convert the first playable hit."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WECHAT_ROOTS = [
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium",
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files",
    Path.home() / "Library/Group Containers/5A4RE8SF68.com.tencent.xinWeChat/Library/Caches",
]
MEDIA_EXTS = {".mp4", ".mov", ".m4a", ".mp3", ".wav", ".webm", ".mkv", ".aac", ".ogg", ".opus", ".m3u8", ".ts"}
SKIP_PATH_PARTS = (
    "db_storage",
    "gpucache",
    "dawnwebgpucache",
    "dawngraphitecache",
    "shadercache",
    "grshadercache",
    "code cache",
)


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


def likely_candidate(path: Path, size: int, min_size: int) -> bool:
    if size < min_size:
        return False
    lower = str(path).lower()
    if any(part in lower for part in SKIP_PATH_PARTS) or lower.endswith((".db", ".db-wal", ".sqlite", ".plist", ".mmap")):
        return False
    return path.suffix.lower() in MEDIA_EXTS or any(
        part in lower for part in ("blob_storage", "cache", "video", "finder", "live")
    )


def allowed_by_scope(path: Path, radium_only: bool = False) -> bool:
    if not radium_only:
        return True
    lower = str(path).lower()
    return "/app_data/radium/" in lower


def duration_seconds(stream_info: str) -> float:
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stream_info)
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def playable(path: Path, min_duration: float) -> bool:
    ffmpeg = find_ffmpeg_tool("ffmpeg")
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-i",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=8)
    except Exception:
        return False
    stream_info = f"{proc.stdout}\n{proc.stderr}"
    return "Audio:" in stream_info and duration_seconds(stream_info) >= min_duration


def convert(path: Path, output: Path) -> None:
    converter = Path(__file__).with_name("media_url_to_mp3.py")
    subprocess.run([sys.executable, str(converter), str(path), "--output", str(output)], check=True)


def scan_newer_than(start_time: float, min_size: int, seen: set[str], radium_only: bool) -> list[Path]:
    hits: list[Path] = []
    for root in WECHAT_ROOTS:
        if not root.exists():
            continue
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                path = Path(dirpath) / name
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_mtime < start_time:
                    continue
                if not allowed_by_scope(path, radium_only=radium_only):
                    continue
                text = f"{path}:{stat.st_size}:{int(stat.st_mtime)}"
                if text in seen:
                    continue
                seen.add(text)
                if likely_candidate(path, stat.st_size, min_size):
                    hits.append(path)
    hits.sort(key=lambda p: p.stat().st_mtime)
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "outputs" / "weixin_video_channel.mp3"))
    parser.add_argument("--duration", type=float, default=300)
    parser.add_argument("--poll-interval", type=float, default=2)
    parser.add_argument("--min-size", type=int, default=50_000)
    parser.add_argument("--min-duration", type=float, default=30)
    parser.add_argument("--lookback-seconds", type=float, default=60)
    parser.add_argument("--radium-only", action="store_true")
    parser.add_argument("--list-only", action="store_true")
    args = parser.parse_args()

    start_time = time.time() - args.lookback_seconds
    deadline = time.time() + args.duration
    output = Path(args.output).expanduser().resolve()
    seen: set[str] = set()

    print("Open/play the target in logged-in desktop WeChat now.")
    print(f"Watching from {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_time))}")
    while time.time() < deadline:
        for path in scan_newer_than(start_time, args.min_size, seen, args.radium_only):
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            print(f"candidate {size} bytes: {path}", flush=True)
            if playable(path, args.min_duration):
                print(f"audio media found: {path}", flush=True)
                if args.list_only:
                    return 0
                try:
                    convert(path, output)
                except subprocess.CalledProcessError as exc:
                    print(f"conversion failed for candidate, continuing: {exc}", flush=True)
                    if output.exists() and output.stat().st_size == 0:
                        output.unlink()
                    continue
                if not playable(output, args.min_duration):
                    print(f"converted output is shorter than {args.min_duration}s, continuing", flush=True)
                    if output.exists():
                        output.unlink()
                    continue
                return 0
        time.sleep(args.poll_interval)
    print("No playable new WeChat media/cache file found.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
