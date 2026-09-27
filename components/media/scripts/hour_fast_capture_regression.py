#!/usr/bin/env python3
"""Run a one-hour fast-capture regression on a controllable HTML media element."""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from replay_mp3_studio.utils import find_ffmpeg, now_iso, python_executable, verify_mp3


WEB_FAST_CAPTURE = ROOT / "outputs" / "capture_accelerator" / "web_fast_capture.py"


def build_time_model(source_duration_seconds: float, speed: float) -> dict[str, float]:
    if source_duration_seconds <= 0:
        raise ValueError("source_duration_seconds must be positive")
    if speed <= 0:
        raise ValueError("speed must be positive")
    expected_wall = source_duration_seconds / speed
    official_3x_wall = source_duration_seconds / 3.0
    return {
        "source_duration_seconds": round(source_duration_seconds, 3),
        "requested_speed": round(speed, 3),
        "expected_record_wall_seconds": round(expected_wall, 3),
        "official_3x_wall_seconds": round(official_3x_wall, 3),
        "expected_saved_vs_3x_seconds": round(max(0.0, official_3x_wall - expected_wall), 3),
        "expected_speedup_vs_realtime": round(speed, 3),
        "expected_speedup_vs_3x": round(official_3x_wall / expected_wall, 3),
    }


def local_audio_page_html(source_name: str) -> str:
    safe_name = source_name.replace('"', "%22").replace("<", "%3C").replace(">", "%3E")
    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <title>Codex Fast Capture Regression</title>
  </head>
  <body>
    <audio id="source" controls autoplay src="{safe_name}"></audio>
  </body>
</html>
"""


def build_capture_command(
    *,
    python_exe: str,
    url: str,
    output: Path,
    raw_output: Path,
    profile_dir: Path,
    speed: float,
    max_wall_seconds: float,
) -> list[str]:
    return [
        python_exe,
        str(WEB_FAST_CAPTURE),
        url,
        "--rate",
        f"{speed:g}",
        "--output",
        str(output),
        "--raw-output",
        str(raw_output),
        "--profile-dir",
        str(profile_dir),
        "--max-wall-seconds",
        f"{max_wall_seconds:g}",
        "--restart-media",
        "--headless",
    ]


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    print("+ " + " ".join(command), flush=True)
    return subprocess.run(command, text=True, capture_output=True)


def generate_source_audio(ffmpeg: str, output: Path, duration_seconds: float) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=880:duration={duration_seconds:g}",
        "-vn",
        "-ar",
        "22050",
        "-ac",
        "1",
        "-codec:a",
        "libmp3lame",
        "-b:a",
        "32k",
        str(output),
    ]
    proc = run(command)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "source audio generation failed")[-2000:])


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class QuietStaticHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        return


class StaticServer:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.port = free_local_port()
        handler = functools.partial(QuietStaticHandler, directory=str(root))
        self.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", self.port), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "StaticServer":
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    @property
    def page_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/page.html"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-duration", type=float, default=3600.0)
    parser.add_argument("--speed", type=float, default=8.0)
    parser.add_argument("--work-dir", default="")
    parser.add_argument("--max-wall-seconds", type=float, default=0.0)
    parser.add_argument("--min-duration-ratio", type=float, default=0.95)
    args = parser.parse_args()

    if args.source_duration <= 0:
        raise SystemExit("--source-duration must be positive")
    if args.speed <= 1:
        raise SystemExit("--speed must be greater than 1 for a fast-capture regression")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    work_dir = Path(args.work_dir).expanduser().resolve() if args.work_dir else (
        ROOT / "work" / "hour-fast-capture-regression" / stamp
    )
    work_dir.mkdir(parents=True, exist_ok=True)

    started_at = now_iso()
    source = work_dir / "source-hour.mp3"
    page = work_dir / "page.html"
    output = work_dir / "output.normal-speed.mp3"
    raw_output = work_dir / "output.fast.webm"
    profile_dir = work_dir / "chrome-profile"
    report = work_dir / "report.json"

    model = build_time_model(args.source_duration, args.speed)
    max_wall = args.max_wall_seconds or (model["expected_record_wall_seconds"] + 90.0)

    ffmpeg = find_ffmpeg()
    started = time.monotonic()
    generate_source_audio(ffmpeg, source, args.source_duration)
    page.write_text(local_audio_page_html(source.name), encoding="utf-8")

    capture_started = time.monotonic()
    with StaticServer(work_dir) as server:
        command = build_capture_command(
            python_exe=python_executable(),
            url=server.page_url,
            output=output,
            raw_output=raw_output,
            profile_dir=profile_dir,
            speed=args.speed,
            max_wall_seconds=max_wall,
        )
        proc = run(command)
    capture_wall = time.monotonic() - capture_started
    finished = time.monotonic()

    verification: dict[str, Any] | None = None
    error = ""
    ok = proc.returncode == 0 and output.exists()
    if ok:
        try:
            verification = verify_mp3(
                output,
                print,
                min_duration_seconds=args.source_duration * args.min_duration_ratio,
            )
        except Exception as exc:  # pragma: no cover - exercised by CLI failures
            ok = False
            error = str(exc)
    else:
        error = (proc.stderr or proc.stdout or "fast capture command failed")[-4000:]

    payload: dict[str, Any] = {
        "ok": ok,
        "started_at": started_at,
        "finished_at": now_iso(),
        "total_wall_seconds": round(finished - started, 3),
        "capture_command_wall_seconds": round(capture_wall, 3),
        "time_model": model,
        "source_path": str(source),
        "page_path": str(page),
        "raw_output_path": str(raw_output),
        "output_path": str(output),
        "report_path": str(report),
        "capture_returncode": proc.returncode,
        "capture_stdout_tail": (proc.stdout or "")[-4000:],
        "capture_stderr_tail": (proc.stderr or "")[-4000:],
        "verification": verification,
        "error": error,
    }
    if verification and verification.get("duration_seconds"):
        payload["restored_duration_seconds"] = verification["duration_seconds"]
        payload["restored_duration_ratio"] = round(
            float(verification["duration_seconds"]) / float(args.source_duration),
            4,
        )
    write_report(report, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
