#!/usr/bin/env python3
"""Run a website-level regression for Weixin Channels links.

The script starts the local Studio API on an ephemeral localhost port, submits
several Weixin links as normal Studio jobs, waits for completion, and writes a
JSON/Markdown report with evidence. It intentionally does not treat diagnostics
or placeholder files as success: a link passes only when the Studio job produces
a verified MP3.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from replay_mp3_studio.jobs import JobStore
from replay_mp3_studio.server import StudioHandler
from replay_mp3_studio.utils import find_ffmpeg


DEFAULT_LINKS = [
    "https://weixin.qq.com/sph/Aa0UXW05IP",
    "https://weixin.qq.com/sph/AHCIZNAGQb",
    "https://weixin.qq.com/sph/AFfTIp5Ywj",
]


def request_json(url: str, payload: dict[str, Any] | None = None, timeout: int = 15) -> dict[str, Any]:
    data = None
    method = "GET"
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = "POST"
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def start_server() -> tuple[ThreadingHTTPServer, str]:
    handler = StudioHandler
    handler.store = JobStore()
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def recommended_timeout_seconds(duration: int, open_wechat: bool, action: str = "convert") -> int:
    """Return a timeout that covers the staged Weixin pipeline.

    A single job can run direct/provider probing, optional WeChat open, Radium
    source scan, Share Data probing, current-playback delta watching, and the
    final cache fallback. The two watch stages each consume `duration`, while
    provider/network stages need fixed overhead.
    """

    if action == "blackbox-record":
        return max(90, int(duration) + 90)
    watch_budget = max(0, int(duration)) * 3
    open_budget = 45 if open_wechat else 0
    return max(120, watch_budget + open_budget + 180)


def should_open_wechat(open_wechat: bool, watch_current: bool) -> bool:
    return bool(open_wechat or not watch_current)


def artifact_path(job: dict[str, Any], name: str) -> str:
    for artifact in job.get("artifacts") or []:
        if artifact.get("name") == name:
            return str(artifact.get("path") or "")
    return ""


def read_json_file(path: str) -> dict[str, Any]:
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def stage_summary(diagnostics: dict[str, Any]) -> list[dict[str, Any]]:
    stages = diagnostics.get("stages") if isinstance(diagnostics.get("stages"), list) else []
    rows: list[dict[str, Any]] = []
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        rows.append(
            {
                "name": stage.get("name"),
                "attempted": stage.get("attempted"),
                "success": stage.get("success"),
                "exit_code": stage.get("exit_code"),
                "skipped_reason": stage.get("skipped_reason"),
                "error": stage.get("error"),
                "diagnosis": (
                    stage.get("diagnostics", {}).get("diagnosis")
                    if isinstance(stage.get("diagnostics"), dict)
                    else ""
                ),
            }
        )
    return rows


def volume_summary(path: str) -> dict[str, Any]:
    if not path or not Path(path).exists():
        return {}
    proc = subprocess.run(
        [find_ffmpeg(), "-hide_banner", "-nostats", "-i", path, "-af", "volumedetect", "-f", "null", "-"],
        text=True,
        capture_output=True,
        timeout=60,
    )
    stderr = proc.stderr or ""
    mean_match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", stderr)
    max_match = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", stderr)
    payload: dict[str, Any] = {
        "returncode": proc.returncode,
        "mean_volume_db": float(mean_match.group(1)) if mean_match else None,
        "max_volume_db": float(max_match.group(1)) if max_match else None,
    }
    payload["not_silent"] = payload["max_volume_db"] is not None and float(payload["max_volume_db"]) > -60.0
    return payload


def summarize_job(job: dict[str, Any]) -> dict[str, Any]:
    diagnostics = read_json_file(artifact_path(job, "weixin_link_diagnostics.json"))
    direct_probe = read_json_file(artifact_path(job, "weixin_direct_link_probe.json"))
    current_delta = read_json_file(artifact_path(job, "weixin_current_playback_delta.json"))
    direct_stage = {}
    for stage in diagnostics.get("stages") or []:
        if isinstance(stage, dict) and stage.get("name") == "direct_link_provider_probe":
            direct_stage = stage
            break
    provider_keys = (
        direct_stage.get("provider_keys")
        if isinstance(direct_stage.get("provider_keys"), dict)
        else {}
    )
    output_path = str(job.get("output_path") or "")
    output_exists = bool(job.get("output_exists"))
    volume = volume_summary(output_path) if output_exists and job.get("action") == "blackbox-record" else {}
    verified = job.get("state") == "completed" and output_exists and bool(job.get("verify"))
    if job.get("action") == "blackbox-record":
        verified = verified and bool(volume.get("not_silent"))
    direct_attempts = []
    for attempt in direct_probe.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        direct_attempts.append(
            {
                "name": attempt.get("name"),
                "status": attempt.get("status"),
                "configured": attempt.get("configured"),
                "candidate_media_url_count": (
                    attempt.get("summary", {}).get("candidate_media_url_count")
                    if isinstance(attempt.get("summary"), dict)
                    else None
                ),
            }
        )
    return {
        "job_id": job.get("id"),
        "url": job.get("url"),
        "state": job.get("state"),
        "original_state": job.get("original_state"),
        "regression_timeout": bool(job.get("regression_timeout")),
        "success": verified,
        "output_path": output_path if output_exists else "",
        "output_bytes": job.get("output_bytes", 0) if output_exists else 0,
        "verify": job.get("verify"),
        "volume": volume,
        "error": job.get("error"),
        "next_action": job.get("next_action"),
        "diagnostics_path": artifact_path(job, "weixin_link_diagnostics.json"),
        "direct_probe_path": artifact_path(job, "weixin_direct_link_probe.json"),
        "current_delta_path": artifact_path(job, "weixin_current_playback_delta.json"),
        "current_delta_diagnosis": current_delta.get("diagnosis") if isinstance(current_delta, dict) else "",
        "current_delta_unreadable_fd_count": (
            current_delta.get("baseline_unreadable_media_fd_count")
            if isinstance(current_delta, dict)
            else None
        ),
        "current_delta_largest_unreadable_fd_bytes": (
            current_delta.get("largest_unreadable_fd_bytes")
            if isinstance(current_delta, dict)
            else None
        ),
        "direct_probe_candidate_count": len(direct_probe.get("candidate_media_urls") or []),
        "provider_keys_configured": {
            name: bool(configured) for name, configured in provider_keys.items()
        },
        "direct_attempts": direct_attempts,
        "stages": stage_summary(diagnostics),
        "summary": diagnostics.get("summary") if isinstance(diagnostics, dict) else "",
    }


def wait_for_job(base_url: str, job_id: str, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {"id": job_id, "state": "unknown"}
    last_error = ""
    while time.monotonic() < deadline:
        try:
            last = request_json(f"{base_url}/api/jobs/{job_id}", timeout=15)
            last_error = ""
            if last.get("state") in {"completed", "failed"}:
                return last
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
        time.sleep(2)
    timed_out = timeout_job_summary(last, timeout)
    if last_error:
        timed_out["last_poll_error"] = last_error
    return timed_out


def timeout_job_summary(job: dict[str, Any], timeout: int) -> dict[str, Any]:
    timed_out = dict(job)
    timed_out["original_state"] = job.get("state") or ""
    timed_out["state"] = "timeout"
    timed_out["regression_timeout"] = True
    timed_out["error"] = f"Regression timed out after {timeout}s while job state was `{job.get('state') or 'unknown'}`."
    return timed_out


def write_reports(report_base: Path, report: dict[str, Any]) -> None:
    report_base.parent.mkdir(parents=True, exist_ok=True)
    json_path = report_base.with_suffix(".json")
    md_path = report_base.with_suffix(".md")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Weixin Link Regression",
        "",
        f"- Started at: `{report['started_at']}`",
        f"- Action: `{report.get('action', 'convert')}`",
        f"- Duration per job: `{report['job_duration_seconds']}s`",
        f"- Blackbox speed: `{report.get('speed', '')}`",
        f"- Audio device: `{report.get('audio_device', '')}`",
        f"- Timeout per job: `{report['timeout_seconds']}s`",
        f"- Watch-current mode: `{report['watch_current']}`",
        f"- Passed: `{report['passed']}/{report['total']}`",
        "",
        "| Link | State | Success | Output | Failure / Evidence |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in report["results"]:
        evidence = ""
        if item.get("current_delta_diagnosis"):
            evidence = (
                f"current delta: {item.get('current_delta_diagnosis')}; "
                f"unreadable fd count: {item.get('current_delta_unreadable_fd_count')}"
            )
        if not evidence:
            evidence = item.get("summary") or item.get("error") or ""
        volume = item.get("volume") if isinstance(item.get("volume"), dict) else {}
        if volume:
            volume_text = f"volume max: {volume.get('max_volume_db')} dB; not_silent: {volume.get('not_silent')}"
            evidence = f"{evidence}; {volume_text}" if evidence else volume_text
        if not evidence and item.get("stages"):
            last = item["stages"][-1]
            evidence = last.get("error") or last.get("diagnosis") or ""
        lines.append(
            "| {link} | {state} | {success} | {output} | {evidence} |".format(
                link=item.get("url", ""),
                state=item.get("state", ""),
                success="yes" if item.get("success") else "no",
                output=item.get("output_path") or "",
                evidence=str(evidence).replace("|", "\\|"),
            )
        )
    lines.extend(
        [
            "",
            "## Next Actions",
            "",
        ]
    )
    for item in report["results"]:
        next_action = item.get("next_action") if isinstance(item.get("next_action"), dict) else {}
        if not next_action:
            continue
        lines.extend(
            [
                f"### {item.get('url', '')}",
                "",
                f"- Kind: `{next_action.get('kind', '')}`",
                f"- Label: {next_action.get('label', '')}",
                f"- Detail: {next_action.get('detail', '')}",
            ]
        )
        for key, label in (
            ("bridge_launcher_url", "Bridge 入口"),
            ("bridge_page_url", "Bridge 页面"),
            ("bridge_snippet_url", "Bridge JS"),
            ("bridge_payload_packet_path", "Bridge payload"),
            ("diagnostics_path", "诊断"),
            ("open_packet_path", "桥接包"),
        ):
            value = next_action.get(key)
            if value:
                lines.append(f"- {label}: `{value}`")
        lines.append("")
    lines.extend(
        [
            "",
            "## Conclusion",
            "",
            report.get("conclusion", ""),
        ]
    )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("links", nargs="*", default=DEFAULT_LINKS)
    parser.add_argument("--duration", type=int, default=3, help="Playback/cache watch or blackbox recording duration per job.")
    parser.add_argument(
        "--timeout",
        type=int,
        default=0,
        help="Maximum wait time per job. Use 0 for an automatic timeout based on --duration and mode.",
    )
    parser.add_argument("--out", default="reports/weixin_link_regression_latest")
    parser.add_argument(
        "--open-wechat",
        action="store_true",
        help="Let the Studio open each Weixin link. This is the default link-to-MP3 regression mode.",
    )
    parser.add_argument(
        "--watch-current",
        action="store_true",
        help="Do not reopen links; diagnose whatever WeChat replay is already playing.",
    )
    parser.add_argument(
        "--blackbox",
        action="store_true",
        help="Run explicit blackbox recording jobs instead of the direct/cache convert pipeline.",
    )
    parser.add_argument("--audio-device", default="system", help="Audio device for --blackbox, usually `system`.")
    parser.add_argument("--speed", type=float, default=1.0, help="Actual playback speed for --blackbox restoration.")
    args = parser.parse_args()

    open_wechat = should_open_wechat(args.open_wechat, args.watch_current)
    action = "blackbox-record" if args.blackbox else "convert"
    per_job_timeout = args.timeout or recommended_timeout_seconds(args.duration, open_wechat, action=action)
    server, base_url = start_server()
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    results: list[dict[str, Any]] = []
    try:
        for link in args.links:
            payload = {
                "platform": "weixin",
                "action": action,
                "url": link,
                "duration": args.duration,
                "watch_current": not open_wechat,
                "mode": "open-then-watch" if open_wechat else "watch-current",
            }
            if args.blackbox:
                payload.update(
                    {
                        "audio_device": args.audio_device,
                        "blackbox_speed": args.speed,
                        "allow_short_output": True,
                    }
                )
            try:
                created = request_json(f"{base_url}/api/jobs", payload=payload, timeout=15)
                job = wait_for_job(base_url, str(created["id"]), per_job_timeout)
                results.append(summarize_job(job))
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
                results.append(
                    {
                        "job_id": "",
                        "url": link,
                        "state": "request_error",
                        "success": False,
                        "output_path": "",
                        "output_bytes": 0,
                        "verify": None,
                        "error": str(exc),
                        "next_action": None,
                        "stages": [],
                    }
                )
    finally:
        server.shutdown()

    passed = sum(1 for item in results if item.get("success"))
    report = {
        "started_at": started_at,
        "base_url": base_url,
        "job_duration_seconds": args.duration,
        "action": action,
        "audio_device": args.audio_device if args.blackbox else "",
        "speed": args.speed if args.blackbox else None,
        "timeout_seconds": per_job_timeout,
        "watch_current": not open_wechat,
        "total": len(results),
        "passed": passed,
        "results": results,
        "conclusion": (
            "All tested Weixin links produced verified MP3 files."
            if passed == len(results)
            else "Regression failed: at least one Weixin link did not produce a verified MP3. "
            "This is not ready as the final video-channel delivery."
        ),
    }
    write_reports(Path(args.out).expanduser().resolve(), report)
    print(json.dumps({"passed": passed, "total": len(results), "out": str(Path(args.out).resolve())}, ensure_ascii=False))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
