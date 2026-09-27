from __future__ import annotations

import csv
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlsplit

from .ffmpeg_tools import convert_to_mp3, probe_media
from .report_writer import write_json


MEDIA_EXT_RE = re.compile(r"\.(m3u8|mpd|m4s|ts|aac|m4a|mp4|flv|webm)(?:[?#]|$)", re.I)
MEDIA_CONTENT_HINTS = (
    "audio/",
    "video/",
    "application/vnd.apple.mpegurl",
    "application/dash+xml",
    "application/x-mpegurl",
)


def redact_url(url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path
    if len(path) > 100:
        path = path[:60] + "..." + path[-30:]
    return f"{parsed.scheme}://{parsed.netloc}{path}?<redacted>" if parsed.query else f"{parsed.scheme}://{parsed.netloc}{path}"


def is_media_candidate(url: str, content_type: str = "") -> bool:
    lower_type = content_type.lower()
    return bool(MEDIA_EXT_RE.search(url)) or any(hint in lower_type for hint in MEDIA_CONTENT_HINTS)


def write_network_reports(out_prefix: Path, report: Dict) -> Dict[str, str]:
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = out_prefix.with_suffix(".json")
    csv_path = out_prefix.with_suffix(".csv")
    md_path = out_prefix.with_suffix(".md")
    write_json(json_path, report)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        fields = ["time_offset", "status", "method", "resource_type", "content_type", "redacted_url", "recognized", "has_audio", "has_video", "duration"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in report.get("candidates", []):
            probe = item.get("ffprobe") or {}
            writer.writerow(
                {
                    "time_offset": item.get("time_offset", ""),
                    "status": item.get("status", ""),
                    "method": item.get("method", ""),
                    "resource_type": item.get("resource_type", ""),
                    "content_type": item.get("content_type", ""),
                    "redacted_url": item.get("redacted_url", ""),
                    "recognized": probe.get("recognized", ""),
                    "has_audio": probe.get("has_audio", ""),
                    "has_video": probe.get("has_video", ""),
                    "duration": probe.get("duration", ""),
                }
            )
    audio = [item for item in report.get("candidates", []) if (item.get("ffprobe") or {}).get("has_audio")]
    lines = [
        "# Network Probe Report",
        "",
        f"- URL: `{report.get('redacted_input_url', '')}`",
        f"- Duration: `{report.get('duration_seconds', '')}` seconds",
        f"- Candidate requests: `{len(report.get('candidates', []))}`",
        f"- Candidates with audio stream: `{len(audio)}`",
        "",
        "| 方法 | 是否成功 | 证据 | 输出文件 | 风险 | 建议 |",
        "| --- | --- | --- | --- | --- | --- |",
        f"| 网络媒体流 | {'成功' if audio else '失败'} | {'ffprobe found audio stream' if audio else 'no audio stream in observed candidate URLs'} | {report.get('converted_output', '-') or '-'} | 低/中 | {'优先' if audio else '备用'} |",
        "| 缓存文件 | 未运行 | 使用 `audit-cache` 验证 | - | 低 | 备用 |",
        "| 黑箱录制 | 未运行 | 需用户显式启动 | - | 中 | 兜底 |",
        "",
    ]
    if report.get("error"):
        lines.extend(["## Error", "", f"`{report['error']}`", ""])
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return {"json": str(json_path), "csv": str(csv_path), "markdown": str(md_path)}


def run_probe(
    url: str,
    duration: float,
    out_prefix: str,
    headless: bool = False,
    profile_dir: Optional[str] = None,
    save_sensitive_urls: bool = False,
    max_probes: int = 20,
    convert_out: Optional[str] = None,
) -> Dict:
    started_at = time.time()
    report: Dict = {
        "tool": "video-audio-extractor network probe",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "duration_seconds": duration,
        "redacted_input_url": redact_url(url),
        "passive_observation_only": True,
        "saves_sensitive_urls": save_sensitive_urls,
        "candidates": [],
        "converted_output": "",
    }
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        report["error"] = f"Playwright is not installed or unavailable: {exc}"
        report["outputs"] = write_network_reports(Path(out_prefix).expanduser().resolve(), report)
        return report

    seen = set()
    raw_candidates: List[str] = []

    def record_candidate(request, response=None) -> None:
        content_type = ""
        status = ""
        if response is not None:
            status = response.status
            content_type = response.headers.get("content-type", "")
        candidate_url = request.url
        if not is_media_candidate(candidate_url, content_type):
            return
        if candidate_url in seen:
            return
        seen.add(candidate_url)
        item = {
            "time_offset": round(time.time() - started_at, 3),
            "method": request.method,
            "resource_type": request.resource_type,
            "status": status,
            "content_type": content_type,
            "redacted_url": redact_url(candidate_url),
        }
        if save_sensitive_urls:
            item["url"] = candidate_url
        if len(raw_candidates) < max_probes:
            raw_candidates.append(candidate_url)
            item["ffprobe"] = probe_media(candidate_url, timeout=18)
        report["candidates"].append(item)

    with sync_playwright() as playwright:
        browser = None
        context = None
        try:
            if profile_dir:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(Path(profile_dir).expanduser().resolve()),
                    headless=headless,
                )
            else:
                browser = playwright.chromium.launch(headless=headless)
                context = browser.new_context()
            page = context.new_page()
            page.on("request", lambda request: record_candidate(request, None))
            page.on("response", lambda response: record_candidate(response.request, response))
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(int(duration * 1000))
        except Exception as exc:
            report["error"] = str(exc)
        finally:
            if context:
                context.close()
            if browser:
                browser.close()

    if convert_out:
        for candidate_url in raw_candidates:
            probe = probe_media(candidate_url, timeout=18)
            if probe.get("has_audio"):
                result = convert_to_mp3(candidate_url, Path(convert_out).expanduser().resolve())
                report["converted_output"] = result.get("output", "")
                report["conversion"] = result
                break

    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    report["outputs"] = write_network_reports(Path(out_prefix).expanduser().resolve(), report)
    return report
