#!/usr/bin/env python3
"""Optional ScreenCaptureKit blackbox recording smoke test.

This uses only a locally generated sine wave. It is intentionally not part of
the default health check because it records system audio and may require macOS
Screen Recording/System Audio permissions.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work" / "blackbox-system-audio-smoke"
SOURCE = WORK / "source.wav"
OUTPUT = ROOT / "video-audio-extractor" / "outputs" / "system_audio_smoke.mp3"


def find_ffmpeg() -> str:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from replay_mp3_studio.utils import find_ffmpeg as locate

    return locate()


def run(cmd: list[str], *, cwd: Path = ROOT, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True, timeout=timeout)


def ensure_source() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    proc = run(
        [
            find_ffmpeg(),
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=990:duration=2",
            "-ac",
            "2",
            "-ar",
            "48000",
            str(SOURCE),
        ],
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-2000:])


def probe_output() -> dict:
    proc = run(
        [
            sys.executable,
            "-m",
            "src.main",
            "probe-file",
            "--input",
            str(OUTPUT),
        ],
        cwd=ROOT / "video-audio-extractor",
        timeout=30,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"error": proc.stderr[-2000:] or proc.stdout[-2000:]}
    payload["returncode"] = proc.returncode
    return payload


def main() -> int:
    ensure_source()
    if OUTPUT.exists():
        OUTPUT.unlink()
    player = subprocess.Popen(["afplay", str(SOURCE)])
    try:
        proc = run(
            [
                sys.executable,
                "-m",
                "src.main",
                "blackbox-record",
                "--url",
                "about:blank",
                "--speed",
                "1",
                "--audio-device",
                "system",
                "--duration",
                "2",
                "--no-open",
                "--out",
                str(OUTPUT),
            ],
            cwd=ROOT / "video-audio-extractor",
            timeout=45,
        )
    finally:
        if player.poll() is None:
            player.terminate()
            try:
                player.wait(timeout=2)
            except subprocess.TimeoutExpired:
                player.kill()
                player.wait(timeout=2)

    result = {
        "ok": False,
        "output": str(OUTPUT),
        "record_returncode": proc.returncode,
        "record_stdout_tail": proc.stdout[-2000:],
        "record_stderr_tail": proc.stderr[-3000:],
        "output_exists": OUTPUT.exists(),
        "output_bytes": OUTPUT.stat().st_size if OUTPUT.exists() else 0,
        "probe": probe_output() if OUTPUT.exists() else None,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    probe = result.get("probe") or {}
    result["ok"] = bool(
        proc.returncode == 0
        and result["output_exists"]
        and result["output_bytes"]
        and probe.get("recognized")
        and probe.get("has_audio")
        and float(probe.get("duration") or 0) > 1
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
