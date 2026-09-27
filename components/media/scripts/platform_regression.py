#!/usr/bin/env python3
"""Replay MP3 Studio platform regression smoke.

This verifies the website API routes for the four product buckets without
requiring private platform accounts. Real external YouTube download is optional
because local network/CDN reachability is not stable enough to make it a basic
product gate.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work" / "platform-regression"
REPORTS = ROOT / "reports"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request_json(url: str, *, timeout: int = 10) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(url: str, payload: dict[str, Any], *, timeout: int = 10) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
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


def start_process(cmd: list[str], log_path: Path, *, env: dict[str, str] | None = None) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w", encoding="utf-8")
    return subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
        env=env or os.environ.copy(),
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


def run(cmd: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=timeout)


def find_ffmpeg() -> str:
    from replay_mp3_studio.utils import find_ffmpeg as locate

    return locate()


def make_audio() -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    source = WORK / "source.wav"
    cmd = [
        find_ffmpeg(),
        "-hide_banner",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:duration=1.25",
        "-ac",
        "1",
        "-ar",
        "44100",
        str(source),
    ]
    subprocess.run(cmd, cwd=str(ROOT), check=True, timeout=30, text=True, capture_output=True)
    return source


def wait_job(studio_port: int, job_id: str, *, timeout: float = 45) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = request_json(f"http://127.0.0.1:{studio_port}/api/jobs/{job_id}")
        if current["state"] in {"completed", "failed"}:
            return current
        time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for job {job_id}")


def submit_job(studio_port: int, payload: dict[str, Any], *, timeout: float = 45) -> dict[str, Any]:
    job = post_json(f"http://127.0.0.1:{studio_port}/api/jobs", payload)
    return wait_job(studio_port, job["id"], timeout=timeout)


def summarize_completed_job(name: str, job: dict[str, Any]) -> dict[str, Any]:
    verify = job.get("verify") if isinstance(job.get("verify"), dict) else {}
    return {
        "name": name,
        "platform": job.get("platform"),
        "state": job.get("state"),
        "ok": job.get("state") == "completed" and bool(verify.get("ok")),
        "output_path": job.get("output_path"),
        "duration_seconds": verify.get("duration_seconds"),
        "bytes": verify.get("bytes"),
        "error": job.get("error") or "",
    }


def youtube_route_probe(youtube_url: str) -> dict[str, Any]:
    report_path = WORK / "youtube-list-only.json"
    proc = run(
        [
            sys.executable,
            "outputs/authorized_fetchers/other_link_to_mp3.py",
            youtube_url,
            "--output",
            str(WORK / "youtube-list-only.mp3"),
            "--report",
            str(report_path),
            "--list-only",
        ],
        timeout=30,
    )
    payload: dict[str, Any] = {}
    if report_path.exists():
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    return {
        "name": "youtube_route_probe",
        "ok": proc.returncode == 0 and payload.get("script_kind") == "youtube",
        "returncode": proc.returncode,
        "script_kind": payload.get("script_kind"),
        "status": payload.get("status"),
        "conversion_attempted": False,
        "meaning": "list-only route check; this confirms routing but does not download or convert MP3",
        "report_path": str(report_path),
        "stderr_tail": (proc.stderr or "")[-1000:],
    }


def youtube_live_sample(studio_port: int, youtube_url: str, *, sample_seconds: int, timeout: float) -> dict[str, Any]:
    job = submit_job(
        studio_port,
        {
            "platform": "other",
            "url": youtube_url,
            "sample_seconds": sample_seconds,
            "allow_short_output": True,
        },
        timeout=timeout,
    )
    summary = summarize_completed_job("youtube_live_sample", job)
    summary["conversion_attempted"] = True
    summary["diagnostic_ok"] = False
    if not summary["ok"]:
        report = Path(str(job.get("artifact_dir") or "")) / "other_link_report.json"
        if report.exists():
            try:
                payload = json.loads(report.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            summary["diagnostic_ok"] = payload.get("script_kind") == "youtube" and payload.get("status") == "failed"
            summary["failure_category"] = payload.get("failure_category")
            summary["diagnostic_report"] = str(report)
    return summary


def write_reports(result: dict[str, Any], label: str) -> tuple[Path, Path]:
    REPORTS.mkdir(parents=True, exist_ok=True)
    json_path = REPORTS / f"platform_regression_{label}.json"
    md_path = REPORTS / f"platform_regression_{label}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Platform Regression Report",
        "",
        f"- Started: `{result.get('started_at')}`",
        f"- Finished: `{result.get('finished_at')}`",
        f"- Studio URL: `{result.get('studio_url')}`",
        f"- Overall OK: `{result.get('ok')}`",
        "",
        "## Results",
        "",
    ]
    for item in result.get("checks", []):
        lines.append(f"- `{item.get('name')}`: ok=`{item.get('ok')}`, state=`{item.get('state', item.get('status', ''))}`")
        if item.get("output_path"):
            lines.append(f"  output: `{item.get('output_path')}`")
        if item.get("report_path"):
            lines.append(f"  report: `{item.get('report_path')}`")
        if item.get("diagnostic_report"):
            lines.append(f"  diagnostic_report: `{item.get('diagnostic_report')}`")
        if item.get("failure_category"):
            lines.append(f"  failure_category: `{item.get('failure_category')}`")
        if "conversion_attempted" in item:
            lines.append(f"  conversion_attempted: `{item.get('conversion_attempted')}`")
        if item.get("meaning"):
            lines.append(f"  meaning: `{item.get('meaning')}`")
        if item.get("error"):
            lines.append(f"  error: `{item.get('error')}`")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--youtube-url", default="https://www.youtube.com/watch?v=JN3KPFbWCy8")
    parser.add_argument("--youtube-live", action="store_true", help="Attempt a real YouTube sample download through the website.")
    parser.add_argument("--youtube-timeout", type=float, default=180)
    parser.add_argument("--sample-seconds", type=int, default=3)
    parser.add_argument("--keep-server", action="store_true", help="Leave the Studio server running and print the URL.")
    args = parser.parse_args()

    label = time.strftime("%Y%m%d_%H%M%S")
    source = make_audio()
    studio_port = free_port()
    media_port = free_port()
    library_root = WORK / f"library-{label}"
    env = os.environ.copy()
    env["REPLAY_MP3_LIBRARY"] = str(library_root)
    studio_proc: subprocess.Popen | None = None
    media_proc: subprocess.Popen | None = None
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    result: dict[str, Any] = {
        "started_at": started,
        "studio_url": f"http://127.0.0.1:{studio_port}",
        "library_root": str(library_root),
        "checks": [],
        "ok": False,
    }
    try:
        studio_proc = start_process(
            [sys.executable, "run_local_studio.py", "--host", "127.0.0.1", "--port", str(studio_port)],
            WORK / f"studio-{label}.log",
            env=env,
        )
        media_proc = start_process(
            [sys.executable, "-m", "http.server", str(media_port), "--directory", str(source.parent)],
            WORK / f"media-{label}.log",
        )
        wait_for(f"http://127.0.0.1:{studio_port}/api/state")
        wait_for(f"http://127.0.0.1:{media_port}/source.wav")
        media_url = f"http://127.0.0.1:{media_port}/source.wav"
        artifact_text = json.dumps({"raw_url": media_url})
        for name, platform in (
            ("xiaohongshu_artifact", "xiaohongshu"),
            ("weixin_artifact", "weixin"),
            ("third_party_artifact", "third_party"),
        ):
            job = submit_job(
                studio_port,
                {
                    "platform": platform,
                    "artifact_text": artifact_text,
                    "artifact_ext": ".json",
                    "allow_short_output": True,
                },
            )
            result["checks"].append(summarize_completed_job(name, job))
        other_job = submit_job(
            studio_port,
            {
                "platform": "other",
                "url": media_url,
                "allow_short_output": True,
            },
        )
        result["checks"].append(summarize_completed_job("other_direct_media", other_job))
        result["checks"].append(youtube_route_probe(args.youtube_url))
        if args.youtube_live:
            result["checks"].append(
                youtube_live_sample(
                    studio_port,
                    args.youtube_url,
                    sample_seconds=args.sample_seconds,
                    timeout=args.youtube_timeout,
                )
            )
        required = [
            item
            for item in result["checks"]
            if item.get("name") != "youtube_live_sample"
        ]
        result["ok"] = all(bool(item.get("ok")) for item in required)
    finally:
        result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        json_path, md_path = write_reports(result, label)
        result["json_report"] = str(json_path)
        result["markdown_report"] = str(md_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not args.keep_server:
            stop_process(media_proc)
            stop_process(studio_proc)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
