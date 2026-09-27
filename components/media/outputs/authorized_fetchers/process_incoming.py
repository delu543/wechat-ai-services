#!/usr/bin/env python3
"""Process authorized incoming artifacts/recordings into target MP3 outputs."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INCOMING = ROOT / "incoming"

WEIXIN_HINTS = ("weixin", "wechat", "wx", "sph", "shipinhao", "video_channel", "视频号")
SONGY_HINTS = ("songy", "bandu", "course_784", "784", "松一")
MEDIA_EXTS = {".mp4", ".mov", ".m4a", ".mp3", ".wav", ".webm", ".mkv", ".aac", ".ogg", ".opus", ".m3u8"}
ARTIFACT_EXTS = {".har", ".json", ".txt", ".log", ".html", ".htm", ".xml"}
SPEED_RE = re.compile(r"(?:^|[_@\\-\\s])(?:speed)?([0-9]+(?:\\.[0-9]+)?)x(?:[_@\\-\\s.]|$)", re.I)


def target_for(path: Path, outputs: Path) -> Path | None:
    name = path.name.lower()
    if any(hint in name for hint in WEIXIN_HINTS):
        return outputs / "weixin_video_channel.mp3"
    if any(hint in name for hint in SONGY_HINTS):
        return outputs / "songy_course_784.mp3"
    return None


def run(cmd: list[str]) -> None:
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def speed_for(path: Path) -> float | None:
    match = SPEED_RE.search(path.stem)
    if not match:
        return None
    speed = float(match.group(1))
    return speed if speed > 0 and speed != 1 else None


def folder_media_files(path: Path) -> list[Path]:
    media_exts = MEDIA_EXTS | {".ts", ".m4v", ".flv"}
    return sorted(
        (p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in media_exts),
        key=lambda p: p.name.lower(),
    )


def process(path: Path, outputs: Path, dry_run: bool) -> str:
    target = target_for(path, outputs)
    if not target:
        return f"skip {path.name}: filename does not identify weixin/songy"
    ext = path.suffix.lower()
    target.parent.mkdir(parents=True, exist_ok=True)
    converter = Path(__file__).with_name("media_url_to_mp3.py")
    extractor = Path(__file__).with_name("extract_media_from_artifact.py")
    folder_converter = Path(__file__).with_name("media_folder_to_mp3.py")
    songy_artifact = Path(__file__).with_name("songy_artifact_to_mp3.py")
    weixin_object_artifact = Path(__file__).with_name("weixin_object_artifact_to_mp3.py")
    recover = ROOT / "outputs" / "capture_accelerator" / "recover_audio.py"

    if path.is_dir():
        speed = speed_for(path)
        if speed:
            files = folder_media_files(path)
            if not files:
                return f"skip {path.name}: folder contains no supported media files"
            cmd = [
                sys.executable,
                str(recover),
                *(str(file) for file in files),
                "--speed",
                str(speed),
                "--output",
                str(target),
            ]
        else:
            cmd = [sys.executable, str(folder_converter), str(path), "--output", str(target)]
    elif ext in MEDIA_EXTS:
        speed = speed_for(path)
        if speed:
            cmd = [sys.executable, str(recover), str(path), "--speed", str(speed), "--output", str(target)]
        else:
            cmd = [sys.executable, str(converter), str(path), "--output", str(target)]
    elif ext in ARTIFACT_EXTS:
        if target.name == "songy_course_784.mp3":
            cmd = [sys.executable, str(songy_artifact), str(path), "--output", str(target)]
        else:
            cmd = [sys.executable, str(weixin_object_artifact), str(path), "--output", str(target)]
    else:
        return f"skip {path.name}: unsupported extension {ext}"

    if dry_run:
        return "dry-run " + " ".join(cmd)
    run(cmd)
    return f"created {target}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incoming", default=str(DEFAULT_INCOMING))
    parser.add_argument("--outputs", default=str(ROOT / "outputs"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    args = parser.parse_args()

    incoming = Path(args.incoming).expanduser().resolve()
    outputs = Path(args.outputs).expanduser().resolve()
    incoming.mkdir(parents=True, exist_ok=True)
    candidates = sorted(p for p in incoming.iterdir() if not p.name.startswith("."))
    if not candidates:
        print(f"No files in {incoming}")
        return 0
    failed = False
    for path in candidates:
        try:
            print(process(path, outputs, args.dry_run))
        except Exception as exc:
            failed = True
            print(f"failed {path.name}: {exc}")
            if not args.continue_on_error:
                raise
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
