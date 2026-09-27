from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent


def find_binary(name: str) -> Optional[str]:
    env_name = name.upper()
    env_value = os.environ.get(env_name)
    if env_value and Path(env_value).exists():
        return env_value

    found = shutil.which(name)
    if found:
        return found

    search_roots = [PROJECT_ROOT, WORKSPACE_ROOT]
    for root in search_roots:
        if name == "ffmpeg":
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
        candidates = sorted(root.glob(f"**/{name}"))
        for candidate in candidates[:20]:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def require_ffmpeg() -> str:
    ffmpeg = find_binary("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found. Install ffmpeg or set FFMPEG=/absolute/path/to/ffmpeg.")
    return ffmpeg


def duration_seconds(text: str) -> float:
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _stream_summary_from_ffprobe(payload: Dict) -> Dict:
    streams = payload.get("streams") or []
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    fmt = payload.get("format") or {}
    duration = fmt.get("duration") or 0
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "probe_method": "ffprobe",
        "recognized": bool(streams or fmt),
        "container": fmt.get("format_name", ""),
        "duration": duration,
        "bit_rate": fmt.get("bit_rate", ""),
        "has_audio": bool(audio_streams),
        "has_video": bool(video_streams),
        "audio_streams": [
            {
                "codec_name": stream.get("codec_name", ""),
                "sample_rate": stream.get("sample_rate", ""),
                "bit_rate": stream.get("bit_rate", ""),
                "channels": stream.get("channels", ""),
            }
            for stream in audio_streams
        ],
        "video_streams": [
            {
                "codec_name": stream.get("codec_name", ""),
                "width": stream.get("width", ""),
                "height": stream.get("height", ""),
                "bit_rate": stream.get("bit_rate", ""),
            }
            for stream in video_streams
        ],
    }


def _stream_summary_from_ffmpeg(stderr: str) -> Dict:
    audio_streams: List[Dict] = []
    video_streams: List[Dict] = []
    for line in stderr.splitlines():
        if " Audio: " in line:
            codec = line.split("Audio:", 1)[1].split(",", 1)[0].strip()
            sample_match = re.search(r"(\d+)\s*Hz", line)
            bitrate_match = re.search(r"(\d+)\s*kb/s", line)
            audio_streams.append(
                {
                    "codec_name": codec,
                    "sample_rate": sample_match.group(1) if sample_match else "",
                    "bit_rate": f"{bitrate_match.group(1)} kb/s" if bitrate_match else "",
                    "channels": "",
                }
            )
        if " Video: " in line:
            codec = line.split("Video:", 1)[1].split(",", 1)[0].strip()
            size_match = re.search(r"(\d{2,5})x(\d{2,5})", line)
            video_streams.append(
                {
                    "codec_name": codec,
                    "width": size_match.group(1) if size_match else "",
                    "height": size_match.group(2) if size_match else "",
                    "bit_rate": "",
                }
            )
    return {
        "probe_method": "ffmpeg-fallback",
        "recognized": bool(audio_streams or video_streams or "Duration:" in stderr),
        "container": "",
        "duration": duration_seconds(stderr),
        "bit_rate": "",
        "has_audio": bool(audio_streams),
        "has_video": bool(video_streams),
        "audio_streams": audio_streams,
        "video_streams": video_streams,
    }


def probe_media(input_ref: str, timeout: float = 20) -> Dict:
    ffprobe = find_binary("ffprobe")
    if ffprobe:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                input_ref,
            ],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if proc.returncode == 0:
            try:
                return {**_stream_summary_from_ffprobe(json.loads(proc.stdout)), "returncode": proc.returncode}
            except json.JSONDecodeError:
                pass
        return {
            "probe_method": "ffprobe",
            "recognized": False,
            "returncode": proc.returncode,
            "error": proc.stderr[-2000:],
            "has_audio": False,
            "has_video": False,
            "duration": 0.0,
        }

    ffmpeg = require_ffmpeg()
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", input_ref],
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    summary = _stream_summary_from_ffmpeg(proc.stderr)
    summary["returncode"] = proc.returncode
    if not summary["recognized"]:
        summary["error"] = proc.stderr[-2000:]
    return summary


def atempo_chain(tempo: float) -> str:
    if tempo <= 0:
        raise ValueError("tempo must be positive")
    filters: List[float] = []
    remaining = tempo
    while remaining < 0.5:
        filters.append(0.5)
        remaining = remaining / 0.5
    while remaining > 2.0:
        filters.append(2.0)
        remaining = remaining / 2.0
    filters.append(remaining)
    return ",".join(f"atempo={value:.7g}" for value in filters)


def convert_to_mp3(
    input_ref: str,
    output: Path,
    tempo: float = 1.0,
    bitrate: str = "128k",
    timeout: float = 7200,
) -> Dict:
    ffmpeg = require_ffmpeg()
    output.parent.mkdir(parents=True, exist_ok=True)
    command: List[str] = [ffmpeg, "-hide_banner", "-y", "-i", input_ref, "-vn"]
    if abs(tempo - 1.0) > 0.0001:
        command.extend(["-filter:a", atempo_chain(tempo)])
    command.extend(["-codec:a", "libmp3lame", "-b:a", bitrate, str(output)])
    proc = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-3000:])
    return {
        "output": str(output),
        "bytes": output.stat().st_size if output.exists() else 0,
        "tempo": tempo,
        "bitrate": bitrate,
        "verify": probe_media(str(output), timeout=30),
    }


def run_command(command: Sequence[str], timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    return subprocess.run(list(command), text=True, capture_output=True, timeout=timeout)
