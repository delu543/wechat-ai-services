#!/usr/bin/env python3
"""Convert an authorized media URL or local media file to MP3."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import urllib.parse
from pathlib import Path


def find_ffmpeg() -> str:
    env = os.environ.get("FFMPEG")
    if env and Path(env).exists():
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    root = Path(__file__).resolve().parents[2]
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
    raise SystemExit("ffmpeg not found. Set FFMPEG=/path/to/ffmpeg.")


def display_arg(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?<redacted>" if parsed.query else value
    return value


def redact_text(text: str, sensitive_values: list[str]) -> str:
    redacted = text
    for value in sensitive_values:
        if value:
            redacted = redacted.replace(value, display_arg(value))
    return redacted


def run(cmd: list[str]) -> None:
    print("+ " + " ".join(display_arg(part) for part in cmd), flush=True)
    proc = subprocess.run(cmd, text=True, capture_output=True)
    sensitive_values = [part for part in cmd if part.startswith(("http://", "https://"))]
    stdout = redact_text(proc.stdout or "", sensitive_values)
    stderr = redact_text(proc.stderr or "", sensitive_values)
    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="")
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=stdout, stderr=stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Authorized media URL or local file")
    parser.add_argument("--output", required=True, help="Output MP3 path")
    parser.add_argument("--bitrate", default="128k")
    parser.add_argument("--headers", default="", help="Extra ffmpeg HTTP headers, CRLF-separated")
    args = parser.parse_args()

    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    cmd = [find_ffmpeg(), "-hide_banner", "-y"]
    if args.headers:
        cmd.extend(["-headers", args.headers])
    cmd.extend(
        [
            "-i",
            args.input,
            "-vn",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            args.bitrate,
            str(output),
        ]
    )
    run(cmd)
    run([find_ffmpeg(), "-hide_banner", "-i", str(output), "-f", "null", "-"])
    print(f"Created: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
