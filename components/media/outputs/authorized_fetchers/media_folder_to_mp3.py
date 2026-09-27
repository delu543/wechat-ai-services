#!/usr/bin/env python3
"""Convert a folder of authorized media files into one MP3."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


MEDIA_EXTS = {
    ".mp4",
    ".mov",
    ".m4a",
    ".mp3",
    ".wav",
    ".webm",
    ".mkv",
    ".aac",
    ".ogg",
    ".opus",
    ".ts",
    ".m4v",
    ".flv",
}


def natural_key(path: Path) -> list[object]:
    parts = re.split(r"(\d+)", path.name.lower())
    return [int(part) if part.isdigit() else part for part in parts]


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


def find_media_files(folder: Path, recursive: bool) -> list[Path]:
    iterator = folder.rglob("*") if recursive else folder.iterdir()
    files = [p for p in iterator if p.is_file() and p.suffix.lower() in MEDIA_EXTS]
    return sorted(files, key=natural_key)


def run(cmd: list[str]) -> None:
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def normalize(ffmpeg: str, source: Path, wav: Path) -> None:
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-codec:a",
            "pcm_s16le",
            str(wav),
        ]
    )


def concat_wavs(ffmpeg: str, wavs: list[Path], output: Path, bitrate: str) -> None:
    if len(wavs) == 1:
        run(
            [
                ffmpeg,
                "-hide_banner",
                "-y",
                "-i",
                str(wavs[0]),
                "-codec:a",
                "libmp3lame",
                "-b:a",
                bitrate,
                str(output),
            ]
        )
        return

    inputs: list[str] = []
    for wav in wavs:
        inputs.extend(["-i", str(wav)])
    concat_filter = "".join(f"[{idx}:a]" for idx in range(len(wavs)))
    concat_filter += f"concat=n={len(wavs)}:v=0:a=1[a]"
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-y",
            *inputs,
            "-filter_complex",
            concat_filter,
            "-map",
            "[a]",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            bitrate,
            str(output),
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", help="Folder exported from an authorized downloader/test device")
    parser.add_argument("--output", required=True, help="Output MP3 path")
    parser.add_argument("--bitrate", default="128k")
    parser.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-verify", action="store_true")
    args = parser.parse_args()

    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        raise SystemExit(f"Input folder not found: {folder}")
    files = find_media_files(folder, args.recursive)
    if not files:
        raise SystemExit(f"No supported media files found in {folder}")

    print("Media files, in final order:")
    for file in files:
        print(file)
    if args.dry_run:
        return 0

    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg()

    with tempfile.TemporaryDirectory(prefix="media-folder-to-mp3-") as tmp:
        wavs: list[Path] = []
        for idx, source in enumerate(files, 1):
            wav = Path(tmp) / f"part-{idx:04d}.wav"
            normalize(ffmpeg, source, wav)
            wavs.append(wav)
        concat_wavs(ffmpeg, wavs, output, args.bitrate)

    if not args.no_verify:
        run([ffmpeg, "-hide_banner", "-i", str(output), "-f", "null", "-"])
    print(f"Created: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
