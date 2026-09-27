#!/usr/bin/env python3
"""End-to-end health check for Replay MP3 Studio."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work" / "studio-health-check"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run(cmd: list[str], *, timeout: int = 60) -> None:
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(ROOT), check=True, timeout=timeout)


def request_json(url: str, *, timeout: int = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for(url: str, *, timeout: float = 10) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def find_ffmpeg() -> str:
    from replay_mp3_studio.utils import find_ffmpeg as locate

    return locate()


def make_audio() -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    source = WORK / "source.wav"
    run(
        [
            find_ffmpeg(),
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=990:duration=1",
            "-ac",
            "1",
            "-ar",
            "44100",
            str(source),
        ],
        timeout=30,
    )
    shutil_target = WORK / "stodownload"
    shutil_target.write_bytes(source.read_bytes())
    return source


def start_process(cmd: list[str], log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w", encoding="utf-8")
    return subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
    )


def stop_process(proc: subprocess.Popen | None) -> None:
    if not proc or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def run_job_smoke(studio_port: int, media_port: int) -> dict:
    artifact_text = json.dumps({"raw_url": f"http://127.0.0.1:{media_port}/source.wav"})
    payload = json.dumps(
        {
            "action": "health-check",
            "platform": "third_party",
            "artifact_text": artifact_text,
            "artifact_ext": ".json",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{studio_port}/api/jobs",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        job = json.loads(response.read().decode("utf-8"))

    deadline = time.time() + 30
    while time.time() < deadline:
        current = request_json(f"http://127.0.0.1:{studio_port}/api/jobs/{job['id']}")
        if current["state"] in {"completed", "failed"}:
            if current["state"] != "completed":
                raise RuntimeError(f"Health job failed: {current.get('error')}")
            if not current.get("verify", {}).get("ok"):
                raise RuntimeError("Health job completed without MP3 verification.")
            if not current.get("artifacts"):
                raise RuntimeError("Health job did not expose uploaded artifact.")
            return current
        time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for health job {job['id']}.")


def run_receiver_smoke(studio_port: int, media_port: int) -> dict:
    payload = json.dumps(
        {
            "action": "health-check",
            "platform": "weixin",
            "artifact_text": json.dumps({"raw_url": f"http://127.0.0.1:{media_port}/source.wav"}),
            "artifact_ext": ".json",
            "allow_short_output": True,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{studio_port}/api/receive-artifact",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        job = json.loads(response.read().decode("utf-8"))

    deadline = time.time() + 30
    while time.time() < deadline:
        current = request_json(f"http://127.0.0.1:{studio_port}/api/jobs/{job['id']}")
        if current["state"] in {"completed", "failed"}:
            if current["state"] != "completed":
                raise RuntimeError(f"Receiver job failed: {current.get('error')}")
            if current.get("platform") != "weixin":
                raise RuntimeError("Receiver job was not routed to Weixin.")
            if not current.get("verify", {}).get("ok"):
                raise RuntimeError("Receiver job completed without MP3 verification.")
            return current
        time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for receiver job {job['id']}.")


def run_direct_stodownload_smoke(studio_port: int, media_port: int) -> dict:
    payload = json.dumps(
        {
            "platform": "other",
            "url": f"http://127.0.0.1:{media_port}/stodownload?token=synthetic",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{studio_port}/api/jobs",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        job = json.loads(response.read().decode("utf-8"))

    deadline = time.time() + 30
    while time.time() < deadline:
        current = request_json(f"http://127.0.0.1:{studio_port}/api/jobs/{job['id']}")
        if current["state"] in {"completed", "failed"}:
            if current["state"] != "completed":
                raise RuntimeError(f"Direct stodownload-like job failed: {current.get('error')}")
            if current.get("platform") != "other":
                raise RuntimeError("Direct stodownload-like job was not routed to other.")
            if not current.get("verify", {}).get("ok"):
                raise RuntimeError("Direct stodownload-like job completed without MP3 verification.")
            return current
        time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for direct stodownload-like job {job['id']}.")


def main() -> int:
    run([sys.executable, "-m", "unittest", "discover", "-s", "tests"])
    run([sys.executable, "-m", "py_compile", *[str(path) for path in (ROOT / "replay_mp3_studio").glob("*.py")], "run_local_studio.py"])
    run(["node", "--check", "replay_mp3_studio/static/app.js"])

    source = make_audio()
    studio_port = free_port()
    media_port = free_port()
    studio_proc: subprocess.Popen | None = None
    media_proc: subprocess.Popen | None = None
    try:
        studio_proc = start_process(
            [sys.executable, "run_local_studio.py", "--host", "127.0.0.1", "--port", str(studio_port)],
            WORK / "studio.log",
        )
        media_proc = start_process(
            [sys.executable, "-m", "http.server", str(media_port), "--directory", str(source.parent)],
            WORK / "media.log",
        )
        wait_for(f"http://127.0.0.1:{studio_port}/api/state")
        wait_for(f"http://127.0.0.1:{media_port}/source.wav")

        state = request_json(f"http://127.0.0.1:{studio_port}/api/state")
        expected_dirs = {"xiaohongshu", "weixin", "third_party", "other"}
        actual_dirs = set(state.get("platforms", {}))
        if expected_dirs != actual_dirs:
            raise RuntimeError(f"Unexpected platform keys: {actual_dirs}")
        for folder in ("xiaohongshu", "weixin", "third_party", "other"):
            if not (ROOT / "library" / folder).is_dir():
                raise RuntimeError(f"Missing library folder: {folder}")

        with urllib.request.urlopen(f"http://127.0.0.1:{studio_port}/api/weixin/bridge-snippet", timeout=10) as response:
            snippet = response.read().decode("utf-8")
        if "finderH5ExtTransfer" not in snippet:
            raise RuntimeError("Bridge snippet endpoint did not return expected content.")
        with urllib.request.urlopen(f"http://127.0.0.1:{studio_port}/api/weixin/bridge-autopost-snippet", timeout=10) as response:
            autopost = response.read().decode("utf-8")
        if "/api/receive-artifact" not in autopost or "finderH5ExtTransfer" not in autopost:
            raise RuntimeError("Bridge auto-post snippet endpoint did not return expected content.")
        with urllib.request.urlopen(f"http://127.0.0.1:{studio_port}/api/weixin/runtime-capture-snippet", timeout=10) as response:
            runtime_capture = response.read().decode("utf-8")
        if "weixin_runtime_profile_capture" not in runtime_capture or "document.cookie" in runtime_capture:
            raise RuntimeError("Runtime capture snippet endpoint did not return expected safe content.")
        devices = request_json(f"http://127.0.0.1:{studio_port}/api/audio-devices")
        if "audio_devices" not in devices:
            raise RuntimeError("Audio device endpoint did not return audio_devices.")
        speed = request_json(f"http://127.0.0.1:{studio_port}/api/speed-snippet?speed=8")
        if speed.get("speed") != 8.0 or "playbackRate" not in str(speed.get("snippet") or ""):
            raise RuntimeError("Speed snippet endpoint did not return the expected playbackRate snippet.")
        if "currentTime" not in str(speed.get("timeline_probe_snippet") or ""):
            raise RuntimeError("Speed snippet endpoint did not return the expected timeline probe snippet.")

        completed = run_job_smoke(studio_port, media_port)
        received = run_receiver_smoke(studio_port, media_port)
        direct_stodownload = run_direct_stodownload_smoke(studio_port, media_port)
        print(
            json.dumps(
                {
                    "ok": True,
                    "library_root": state["library_root"],
                    "health_job": completed["id"],
                    "receiver_job": received["id"],
                    "direct_stodownload_job": direct_stodownload["id"],
                    "output": completed["output_path"],
                    "receiver_output": received["output_path"],
                    "direct_stodownload_output": direct_stodownload["output_path"],
                    "artifacts": [item["name"] for item in completed.get("artifacts", [])],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        stop_process(media_proc)
        stop_process(studio_proc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
