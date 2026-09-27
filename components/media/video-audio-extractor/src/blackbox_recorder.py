from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional

from .ffmpeg_tools import atempo_chain, convert_to_mp3, find_binary, require_ffmpeg


ROOT = Path(__file__).resolve().parents[1]
SCK_SOURCE = ROOT / "native" / "sck_audio_recorder.swift"
SCK_BINARY = ROOT / "bin" / "sck_audio_recorder"


def screen_capture_kit_available() -> bool:
    return SCK_SOURCE.exists() and bool(shutil.which("swiftc") or SCK_BINARY.exists())


def build_sck_recorder() -> Path:
    if SCK_BINARY.exists() and SCK_BINARY.stat().st_mtime_ns >= SCK_SOURCE.stat().st_mtime_ns:
        return SCK_BINARY
    swiftc = shutil.which("swiftc")
    if not swiftc:
        raise RuntimeError("swiftc is required for ScreenCaptureKit system audio recording.")
    SCK_BINARY.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            swiftc,
            "-parse-as-library",
            str(SCK_SOURCE),
            "-o",
            str(SCK_BINARY),
            "-framework",
            "ScreenCaptureKit",
            "-framework",
            "AVFoundation",
            "-framework",
            "CoreMedia",
        ],
        text=True,
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-3000:] or proc.stdout[-3000:] or "ScreenCaptureKit recorder build failed")
    return SCK_BINARY


def list_avfoundation_devices() -> Dict:
    ffmpeg = require_ffmpeg()
    selected_system = platform.system()
    if selected_system == "Windows":
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            text=True,
            capture_output=True,
            timeout=15,
        )
        names: List[str] = []
        for line in proc.stderr.splitlines():
            match = re.search(r'"([^"]+)"\s*\(audio\)', line)
            if match and match.group(1) not in names:
                names.append(match.group(1))
        return {
            "platform": selected_system,
            "capture_backend": "dshow",
            "ffmpeg": ffmpeg,
            "raw": proc.stderr,
            "audio_devices": names,
            "system_audio_devices": [],
            "guidance": (
                "Select an explicit DirectShow loopback input such as Stereo Mix or a user-installed "
                "virtual audio cable. This tool never installs or enables an audio driver."
            ),
        }
    if selected_system != "Darwin":
        return {
            "platform": selected_system,
            "capture_backend": "unsupported",
            "ffmpeg": ffmpeg,
            "raw": "",
            "audio_devices": [],
            "system_audio_devices": [],
        }
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        text=True,
        capture_output=True,
        timeout=15,
    )
    stderr = proc.stderr
    audio_devices: List[str] = []
    in_audio = False
    for line in stderr.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if "AVFoundation video devices" in line:
            in_audio = False
        if in_audio and "] [" in line:
            audio_devices.append(line.strip())
    return {
        "platform": selected_system,
        "capture_backend": "avfoundation",
        "ffmpeg": ffmpeg,
        "raw": stderr,
        "audio_devices": audio_devices,
        "system_audio_devices": [
            {
                "value": "system",
                "label": "System audio via ScreenCaptureKit",
                "available": screen_capture_kit_available(),
            }
        ],
    }


def _ffmpeg_capture_input(audio_device: str, *, system: str | None = None) -> tuple[list[str], str]:
    selected_system = system or platform.system()
    if selected_system == "Windows":
        return ["-f", "dshow", "-i", f"audio={audio_device}"], "dshow"
    if selected_system == "Darwin":
        return ["-f", "avfoundation", "-i", audio_device], "avfoundation"
    raise RuntimeError("Blackbox audio recording is supported on macOS and Windows only.")


def parse_volumedetect(stderr: str) -> Dict:
    mean_match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", stderr or "")
    max_match = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", stderr or "")
    return {
        "mean_volume_db": float(mean_match.group(1)) if mean_match else None,
        "max_volume_db": float(max_match.group(1)) if max_match else None,
    }


def volume_summary(media_path: Path) -> Dict:
    ffmpeg = require_ffmpeg()
    proc = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(media_path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        text=True,
        capture_output=True,
        timeout=30,
    )
    payload = {
        "returncode": proc.returncode,
        "path": str(media_path),
        **parse_volumedetect(proc.stderr),
    }
    return payload


def capture_probe(audio_device: str, output: Path, seconds: float) -> Dict:
    selected_system = platform.system()
    use_system_audio = selected_system == "Darwin" and str(audio_device or "").lower() in {
        "system",
        "sck",
        "screencapturekit",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    if use_system_audio:
        recorder = build_sck_recorder()
        proc = subprocess.run(
            [
                str(recorder),
                "--output",
                str(output),
                "--duration",
                str(seconds),
            ],
            text=True,
            capture_output=True,
            timeout=max(10, int(seconds) + 10),
        )
    else:
        ffmpeg = require_ffmpeg()
        input_args, _backend = _ffmpeg_capture_input(audio_device, system=selected_system)
        proc = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-y",
                *input_args,
                "-ac",
                "2",
                "-ar",
                "48000",
                "-t",
                str(seconds),
                str(output),
            ],
            text=True,
            capture_output=True,
            timeout=max(10, int(seconds) + 10),
        )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-2000:] or proc.stdout[-2000:] or "Audio probe recording failed")
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("Audio probe did not create a readable file.")
    return {
        "path": str(output),
        "bytes": output.stat().st_size,
        "stdout_tail": proc.stdout[-1000:],
        "stderr_tail": proc.stderr[-1000:],
    }


def wait_for_audio_activity(
    audio_device: str,
    *,
    timeout: float,
    threshold_db: float = -60.0,
    probe_seconds: float = 1.5,
    work_dir: Path,
) -> Dict:
    if timeout <= 0:
        return {"enabled": False}
    deadline = time.monotonic() + timeout
    attempts: List[Dict] = []
    index = 0
    use_system_audio = platform.system() == "Darwin" and str(audio_device or "").lower() in {
        "system",
        "sck",
        "screencapturekit",
    }
    suffix = ".m4a" if use_system_audio else ".wav"
    print(
        f"Waiting for audible playback before recording "
        f"(timeout={timeout:g}s, threshold={threshold_db:g} dB)."
    )
    while time.monotonic() < deadline:
        index += 1
        probe = work_dir / f"audio-preflight-{index}{suffix}"
        try:
            capture = capture_probe(audio_device, probe, probe_seconds)
            volume = volume_summary(probe)
            attempt = {"attempt": index, "capture": capture, "volume": volume}
            attempts.append(attempt)
            max_volume = volume.get("max_volume_db")
            print(f"Audio preflight {index}: max_volume={max_volume} dB")
            if max_volume is not None and float(max_volume) > threshold_db:
                try:
                    probe.unlink()
                except OSError:
                    pass
                return {
                    "enabled": True,
                    "detected": True,
                    "attempts": attempts,
                    "threshold_db": threshold_db,
                    "probe_seconds": probe_seconds,
                }
        finally:
            try:
                probe.unlink()
            except OSError:
                pass
        time.sleep(0.5)
    return {
        "enabled": True,
        "detected": False,
        "attempts": attempts,
        "threshold_db": threshold_db,
        "probe_seconds": probe_seconds,
        "error": "No audible playback was detected before recording.",
    }


def run_blackbox_record(
    url: str,
    speed: float,
    out_path: str,
    duration: Optional[float] = None,
    audio_device: Optional[str] = None,
    open_url: bool = True,
    keep_fast: bool = False,
    raw_only: bool = False,
    wait_audio_timeout: float = 0,
    audio_threshold_db: float = -60.0,
    audio_probe_seconds: float = 1.5,
) -> Dict:
    if speed <= 0:
        raise ValueError("speed must be positive")
    output = Path(out_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    selected_system = platform.system()
    use_system_audio = selected_system == "Darwin" and str(audio_device or "").lower() in {
        "system",
        "sck",
        "screencapturekit",
    }
    fast_wav = output.with_suffix(".fast.m4a" if use_system_audio else ".fast.wav")
    log_path = output.with_suffix(".blackbox.json")
    ffmpeg = require_ffmpeg()
    devices = list_avfoundation_devices()

    print("Blackbox recording mode")
    print("Use only content you are allowed to save and transcribe.")
    print(f"URL: {url}")
    print(f"Output: {output}")
    print(f"Recorded playback speed: {speed:g}x")
    print(f"Tempo restore filter: {atempo_chain(1.0 / speed)}")
    if raw_only:
        print("Raw-only mode: capture fast audio now; restore tempo in a separate post-processing step.")
    if speed > 3:
        print(
            "Speed above 3x assumes the playback page was actually accelerated "
            "before recording. If the page stayed at 1x/3x, use that real speed instead."
        )
    if devices.get("system_audio_devices"):
        print("System audio capture:")
        for item in devices["system_audio_devices"]:
            availability = "available" if item.get("available") else "unavailable"
            print(f"  {item.get('value')} {item.get('label')} ({availability})")
    print("Audio input devices:")
    for item in devices["audio_devices"]:
        print(f"  {item}")
    if not audio_device:
        raise RuntimeError("No audio device selected. List devices first and pass one explicit input name/index.")

    if open_url:
        if selected_system == "Darwin":
            subprocess.run(["open", url], check=False)
        else:
            webbrowser.open(url)

    if duration is None:
        input("Confirm the page is logged in and playing, then press Enter to start recording.")

    preflight = wait_for_audio_activity(
        audio_device,
        timeout=wait_audio_timeout,
        threshold_db=audio_threshold_db,
        probe_seconds=audio_probe_seconds,
        work_dir=output.parent,
    )
    if preflight.get("enabled") and not preflight.get("detected"):
        log_path.write_text(
            json.dumps(
                {
                    "tool": "video-audio-extractor blackbox recorder",
                    "url": url,
                    "speed": speed,
                    "audio_device": audio_device,
                    "duration_seconds": duration,
                    "preflight": preflight,
                    "output": str(output),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        raise RuntimeError(
            "No audible playback was detected. Open the playback page, start the video, "
            "then retry blackbox recording."
        )
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    if use_system_audio:
        if duration is None:
            raise RuntimeError("ScreenCaptureKit system audio recording requires --duration.")
        recorder = build_sck_recorder()
        proc = subprocess.run(
            [
                str(recorder),
                "--output",
                str(fast_wav),
                "--duration",
                str(duration),
            ],
            text=True,
            capture_output=True,
        )
        stdout, stderr = proc.stdout, proc.stderr
        if proc.returncode != 0:
            raise RuntimeError(stderr[-3000:] or stdout[-3000:] or "ScreenCaptureKit recording failed")
        if not fast_wav.exists() or fast_wav.stat().st_size == 0:
            raise RuntimeError("System audio recording did not create a readable M4A file.")
        if raw_only:
            report = {
                "tool": "video-audio-extractor blackbox recorder",
                "capture_backend": "ScreenCaptureKit",
                "started_at": started,
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "url": url,
                "speed": speed,
                "audio_device": audio_device,
                "duration_seconds": duration,
                "ffmpeg": ffmpeg,
                "preflight": preflight,
                "raw_only": True,
                "fast_wav": str(fast_wav),
                "output": "",
                "conversion": {"status": "skipped_raw_only"},
                "stdout_tail": stdout[-3000:],
                "stderr_tail": stderr[-3000:],
            }
            log_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            report["log"] = str(log_path)
            print(f"Captured fast audio: {fast_wav}")
            print(f"Log: {log_path}")
            return report
        conversion = convert_to_mp3(str(fast_wav), output, tempo=1.0 / speed)
        if not keep_fast:
            try:
                fast_wav.unlink()
            except OSError:
                pass
        report = {
            "tool": "video-audio-extractor blackbox recorder",
            "capture_backend": "ScreenCaptureKit",
            "started_at": started,
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "url": url,
            "speed": speed,
            "audio_device": audio_device,
            "duration_seconds": duration,
            "ffmpeg": ffmpeg,
            "preflight": preflight,
            "fast_wav": str(fast_wav) if keep_fast else "",
            "output": str(output),
            "conversion": conversion,
            "stdout_tail": stdout[-3000:],
            "stderr_tail": stderr[-3000:],
        }
        log_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["log"] = str(log_path)
        print(f"Done: {output}")
        print(f"Log: {log_path}")
        return report

    input_args, capture_backend = _ffmpeg_capture_input(audio_device, system=selected_system)
    command = [
        ffmpeg,
        "-hide_banner",
        "-y",
        *input_args,
        "-ac",
        "2",
        "-ar",
        "48000",
    ]
    if duration is not None:
        command.extend(["-t", str(duration)])
    command.append(str(fast_wav))

    if duration is None:
        proc = subprocess.Popen(command, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        input("Recording. Press Enter to stop.")
        try:
            if proc.stdin:
                proc.stdin.write("q\n")
                proc.stdin.flush()
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
            stdout, stderr = proc.communicate(timeout=10)
    else:
        proc = subprocess.run(command, text=True, capture_output=True)
        stdout, stderr = proc.stdout, proc.stderr

    if not fast_wav.exists() or fast_wav.stat().st_size == 0:
        raise RuntimeError("Recording did not create a readable WAV file.")

    if raw_only:
        report = {
            "tool": "video-audio-extractor blackbox recorder",
            "capture_backend": capture_backend,
            "started_at": started,
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "url": url,
            "speed": speed,
            "audio_device": audio_device,
            "duration_seconds": duration,
            "ffmpeg": ffmpeg,
            "preflight": preflight,
            "raw_only": True,
            "fast_wav": str(fast_wav),
            "output": "",
            "conversion": {"status": "skipped_raw_only"},
            "stderr_tail": stderr[-3000:],
        }
        log_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["log"] = str(log_path)
        print(f"Captured fast audio: {fast_wav}")
        print(f"Log: {log_path}")
        return report

    conversion = convert_to_mp3(str(fast_wav), output, tempo=1.0 / speed)
    if not keep_fast:
        try:
            fast_wav.unlink()
        except OSError:
            pass

    report = {
        "tool": "video-audio-extractor blackbox recorder",
        "capture_backend": capture_backend,
        "started_at": started,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "url": url,
        "speed": speed,
        "audio_device": audio_device,
        "duration_seconds": duration,
        "ffmpeg": ffmpeg,
        "preflight": preflight,
        "fast_wav": str(fast_wav) if keep_fast else "",
        "output": str(output),
        "conversion": conversion,
        "stderr_tail": stderr[-3000:],
    }
    log_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["log"] = str(log_path)
    print(f"Done: {output}")
    print(f"Log: {log_path}")
    return report
