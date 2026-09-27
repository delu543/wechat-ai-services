from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT


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


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def slugify(value: str, fallback: str = "task") -> str:
    text = urllib.parse.urlparse(value).path or value
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")
    return (text[-48:] or fallback).lower()


def classify_url(url: str) -> str:
    lower = url.lower()
    if "xiaohongshu.com" in lower or "xhslink.com" in lower:
        return "xiaohongshu"
    if "weixin.qq.com/sph" in lower or "channels.weixin.qq.com" in lower or "视频号" in url:
        return "weixin"
    if "songy.info" in lower or "bandu-api.songy.info" in lower:
        return "third_party"
    return "other"


def parse_weixin_short_uri(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.endswith("weixin.qq.com") and parsed.path.startswith("/sph/"):
        short_uri = parsed.path.rsplit("/", 1)[-1].strip()
        if short_uri:
            return short_uri
    match = re.search(r"(?:weixin\.qq\.com/sph/|sph/)([A-Za-z0-9_-]+)", url)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{6,}", url.strip()):
        return url.strip()
    raise ValueError("Cannot identify Weixin Channels short URI.")


def is_media_url(url: str) -> bool:
    lower_url = url.lower()
    lower_path = lower_url.split("?", 1)[0]
    weixin_media_markers = ("stodownload", "snsvideodownload", "snscosdownload")
    return (
        lower_path.endswith(MEDIA_EXTS)
        or any(ext in lower_url for ext in (".m3u8", ".mp4"))
        or any(marker in lower_url for marker in weixin_media_markers)
    )


def parse_course_id(url: str, default: str = "784") -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if "course_id" in query and query["course_id"]:
        return query["course_id"][0]
    fragment = urllib.parse.urlparse(parsed.fragment)
    frag_query = urllib.parse.parse_qs(fragment.query)
    if "course_id" in frag_query and frag_query["course_id"]:
        return frag_query["course_id"][0]
    match = re.search(r"course[_/-]?id[=/](\d+)", url, re.I)
    return match.group(1) if match else default


def find_ffmpeg() -> str:
    env = os.environ.get("FFMPEG")
    if env and Path(env).exists():
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    candidates = sorted(
        [
            *(PROJECT_ROOT / "work" / "venv" / "lib").glob(
                "python*/site-packages/imageio_ffmpeg/binaries/ffmpeg-*"
            ),
            *(PROJECT_ROOT / "work" / "venv" / "Lib" / "site-packages").glob(
                "imageio_ffmpeg/binaries/ffmpeg-*"
            ),
        ]
    )
    if candidates:
        return str(candidates[0])
    raise RuntimeError("ffmpeg not found. Set FFMPEG=/path/to/ffmpeg.")


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("FFMPEG", find_ffmpeg())
    return env


def display_command_arg(value: str) -> str:
    parsed = urllib.parse.urlsplit(str(value))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return str(value)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"<auth>@{host}{port}" if parsed.username else parsed.netloc
    base = urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    return f"{base}?<redacted>" if parsed.query else base


def run_streaming(cmd: list[str], log, cwd: Path = PROJECT_ROOT) -> int:
    log("+ " + " ".join(display_command_arg(part) for part in cmd))
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        log(line.rstrip())
    return proc.wait()


def parse_ffmpeg_duration_seconds(output: str) -> float | None:
    match = re.search(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", output)
    if not match:
        return None
    hours = int(match.group(1))
    minutes = int(match.group(2))
    seconds = float(match.group(3))
    return round((hours * 3600) + (minutes * 60) + seconds, 3)


def verify_mp3(path: Path, log, min_duration_seconds: float = 0) -> dict[str, Any]:
    target = path.expanduser().resolve()
    if not target.exists():
        raise FileNotFoundError(f"Missing output: {target}")
    cmd = [find_ffmpeg(), "-hide_banner", "-nostats", "-i", str(target), "-f", "null", "-"]
    log("+ " + " ".join(cmd))
    proc = subprocess.run(cmd, text=True, capture_output=True, env=child_env())
    if proc.stdout:
        log(proc.stdout.rstrip())
    if proc.stderr:
        log(proc.stderr.rstrip())
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg verification failed for {target}")
    duration_seconds = parse_ffmpeg_duration_seconds(f"{proc.stdout}\n{proc.stderr}")
    if min_duration_seconds:
        if duration_seconds is None:
            raise RuntimeError(
                f"MP3 duration could not be read; required minimum is {min_duration_seconds:.2f}s for {target}"
            )
        if duration_seconds < min_duration_seconds:
            raise RuntimeError(
                f"MP3 output is shorter than required minimum: {duration_seconds:.2f}s < "
                f"{min_duration_seconds:.2f}s ({target})"
            )
    return {
        "ok": True,
        "path": str(target),
        "bytes": target.stat().st_size,
        "duration_seconds": duration_seconds,
        "min_duration_seconds": min_duration_seconds,
    }


def python_executable() -> str:
    candidates = (
        PROJECT_ROOT / "work" / "venv" / "bin" / "python",
        PROJECT_ROOT / "work" / "venv" / "Scripts" / "python.exe",
    )
    venv_python = next((path for path in candidates if path.exists()), Path(sys.executable))
    return str(venv_python)
