from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .config import WORK_ROOT
from .utils import find_ffmpeg, now_iso, verify_mp3
from .weixin_decode_key import decode_weixin_numeric_key_pair_to_mp3, decode_weixin_pair_to_mp3
from .weixin_vendor_sources import convert_vendor_source_to_mp3
from .weixin_source_pairs import load_sensitive_pair_artifact, redacted_numeric_key_pair_summary


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTHORIZED_FETCHERS = PROJECT_ROOT / "outputs" / "authorized_fetchers"
VIDEO_AUDIO_EXTRACTOR_ROOT = PROJECT_ROOT / "video-audio-extractor"
DIRECT_LINK_PROBE_REPORT = WORK_ROOT / "direct-link-probes" / "weixin_direct_link_probe.json"
DEFAULT_AUTO_BLACKBOX_SEGMENT_SECONDS = 600.0
AUTO_BLACKBOX_SEGMENT_MIN_SOURCE_SECONDS = 1800.0
WEIXIN_OFFICIAL_BLACKBOX_SPEED_MAX = 3.0
SOURCE_ARTIFACT_TEXT_SUFFIXES = {".json", ".txt", ".log", ".md", ".html", ".htm", ".xml", ".har"}
SOURCE_ARTIFACT_MEDIA_SUFFIXES = {".mp4", ".flv", ".m4a", ".mp3", ".mov", ".webm", ".m3u8", ".aac", ".wav"}
DEFAULT_SOURCE_ARTIFACT_ROOTS = (
    WORK_ROOT / "authorized-source-vault" / "sources",
    WORK_ROOT / "authorized-source-vault" / "snapshots",
    WORK_ROOT / "source-listener-inbox",
)
WEIXIN_DECRYPT_PROBE_SUCCESS_ROOT = (
    WORK_ROOT / "sensitive-artifacts" / "weixin-fast-mp3" / "decrypt-probe-successes"
)


def _video_audio_extractor_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "src.main", *args]


REQUIRED_REPORT_QUESTIONS = [
    "1. 当前最高稳定可用倍速是多少？",
    "2. 为什么能达到这个倍速？",
    "3. 如果不能超过 3 倍速，限制点在哪里？",
    "4. 本次尝试过哪些方案？",
    "5. 哪些方案成功，哪些失败？",
    "6. 最终 MP3 是否完整？",
    "7. 最终耗时是多少？",
    "8. 相比原来的 3 倍速方案，节省了多少时间？",
    "9. 推荐后续继续优化的方向是什么？",
]


@dataclass(frozen=True)
class RoutePlan:
    name: str
    purpose: str
    expected_bottleneck: str
    expected_min_speedup_over_3x: float
    expected_max_speedup_over_3x: float
    invasive: bool = False


@dataclass(frozen=True)
class AutoPipelineOptions:
    url: str
    output: Path
    report: Path
    mode: str = "auto"
    work_dir: Path = WORK_ROOT / "fast-pipeline-auto"
    source_artifact: Path | None = None
    source_artifact_roots: tuple[Path, ...] = ()
    allow_wechat_ui: bool = False
    allow_blackbox: bool = False
    duration: float = 0.0
    audio_device: str = ""
    blackbox_speed: float = 3.0
    segment_seconds: float = 0.0
    min_duration_seconds: float = 0.0
    command_timeout_seconds: int = 300
    source_artifact_wait_seconds: float = 0.0
    current_delta_watch_seconds: float = 0.0


def plan_auto_routes(url: str) -> list[RoutePlan]:
    lower = url.lower()
    if "weixin.qq.com/sph" not in lower and "channels.weixin.qq.com" not in lower:
        return [
            RoutePlan(
                "existing_direct_or_artifact",
                "Try existing direct media/artifact conversion before recording.",
                "provider_or_artifact_availability",
                1.0,
                10.0,
            )
        ]
    return [
        RoutePlan(
            "existing_direct_or_artifact",
            "Use already supported authorized direct/artifact routes if a media source is available.",
            "provider_or_artifact_availability",
            1.0,
            10.0,
        ),
        RoutePlan(
            "wx_channels_source_download",
            "Capture media URL and decode key, then download/decrypt/extract MP3 without full playback.",
            "network_download_decode_ffmpeg",
            1.5,
            30.0,
            invasive=True,
        ),
        RoutePlan(
            "wx_channels_current_delta_watch",
            "After opening playback, watch low-intrusion local media/cache deltas for a complete readable file.",
            "visible_playback_cache_availability",
            1.0,
            20.0,
        ),
        RoutePlan(
            "html_media_speed_probe",
            "Check whether the actual media element can be forced above the official UI speed.",
            "wechat_webview_control",
            0.8,
            4.0,
        ),
        RoutePlan(
            "timeline_seek_probe",
            "Check whether seek bursts or timeline slicing can reduce capture wall-clock without losing audio continuity.",
            "audio_continuity_after_seek",
            0.0,
            3.0,
        ),
        RoutePlan(
            "segmented_blackbox",
            "Record shorter verified chunks and run restoration/transcode while later chunks record.",
            "playback_wall_time",
            1.0,
            1.3,
        ),
        RoutePlan(
            "blackbox_3x_fallback",
            "Preserve the current stable File Transfer Assistant plus 3x blackbox recording path.",
            "official_player_speed_limit",
            1.0,
            1.0,
        ),
    ]


def effective_auto_blackbox_speed(requested_speed: float) -> float:
    if requested_speed <= 0:
        raise ValueError("blackbox_speed must be positive")
    return min(float(requested_speed), WEIXIN_OFFICIAL_BLACKBOX_SPEED_MAX)


def _redact_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    cleaned = raw.strip("\"'")
    parsed = urllib.parse.urlparse(cleaned)
    if not parsed.scheme or not parsed.netloc:
        return raw
    if parsed.query:
        cleaned = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "<redacted>", ""))
    return cleaned


def redact_sensitive_text(text: str) -> str:
    redacted = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        "<redacted-private-key>",
        text,
        flags=re.S,
    )
    redacted = re.sub(r"https?://[^\s\"'<>]+", _redact_url, redacted)
    redacted = re.sub(r"(--key\s+)[^\s]+", r"\1<redacted>", redacted)
    redacted = re.sub(r"((?:token|cookie|auth|sign|encfilekey)=)[^&\s]+", r"\1<redacted>", redacted, flags=re.I)
    return redacted


def _decoded_text(value: str) -> str:
    return (
        urllib.parse.unquote(value)
        .replace("\\/", "/")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\u003A", ":")
        .replace("\\u003a", ":")
        .replace("\\u0026", "&")
    )


def _media_urls_from_text(text: str) -> list[str]:
    decoded = _decoded_text(text)
    pattern = re.compile(
        r"https?://[^\s\"'<>\\]+?(?:stodownload|snsvideodownload|snscosdownload|\.m3u8|\.mp4)[^\s\"'<>\\]*",
        re.I,
    )
    urls: list[str] = []
    seen: set[str] = set()
    for match in pattern.finditer(decoded):
        url = match.group(0).strip().strip('",')
        lower = url.lower()
        if "<redacted>" in lower:
            continue
        if ("cover" in lower or "thumb" in lower) and not any(marker in lower for marker in ("stodownload", "snsvideodownload")):
            continue
        if url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


def _decode_key_values_from_dict(value: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for key, item in value.items():
        normalized = str(key).replace("-", "_").lower()
        if normalized in {"decode_key", "decodekey", "decode_key_v2", "decodekey_v2"}:
            if isinstance(item, str) and item.strip():
                keys.append(item.strip())
    return keys


def _media_urls_from_dict_values(value: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for item in value.values():
        if isinstance(item, str):
            urls.extend(_media_urls_from_text(item))
    unique: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url not in seen:
            unique.append(url)
            seen.add(url)
    return unique


def extract_weixin_decode_key_pairs(value: Any, *, path: str = "$") -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    if isinstance(value, dict):
        keys = _decode_key_values_from_dict(value)
        urls = _media_urls_from_dict_values(value)
        for decode_key in keys:
            for url in urls:
                pairs.append({"url": url, "decode_key": decode_key, "path": path})
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            pairs.extend(extract_weixin_decode_key_pairs(item, path=child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            pairs.extend(extract_weixin_decode_key_pairs(item, path=f"{path}[{index}]"))
    return pairs


def redacted_decode_key_pair_summary(pairs: list[dict[str, str]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for pair in pairs:
        decode_key = str(pair.get("decode_key") or "")
        summary.append(
            {
                "url": redact_sensitive_text(str(pair.get("url") or "")),
                "decode_key_sha256_12": hashlib.sha256(decode_key.encode("utf-8")).hexdigest()[:12]
                if decode_key
                else "",
                "decode_key_length": len(decode_key),
                "path": str(pair.get("path") or ""),
            }
        )
    return summary


def _artifact_paths_from_source_payload(
    payload: dict[str, Any],
    artifact_keys: tuple[str, ...] = ("decode_key_pair_artifact", "decode_key_pair_artifacts"),
) -> list[Path]:
    values: list[str] = []
    for key in artifact_keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            values.append(value)
        elif isinstance(value, list):
            values.extend(str(item) for item in value if item)
    rounds = payload.get("rounds") if isinstance(payload.get("rounds"), list) else []
    for item in rounds:
        if not isinstance(item, dict):
            continue
        for key in artifact_keys:
            value = item.get(key)
            if isinstance(value, str) and value:
                values.append(value)
            elif isinstance(value, list):
                values.extend(str(child) for child in value if child)
    paths: list[Path] = []
    seen: set[str] = set()
    for value in values:
        path = Path(value).expanduser()
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def _decode_key_pairs_from_source_payload(payload: dict[str, Any]) -> list[dict[str, str]]:
    pairs = extract_weixin_decode_key_pairs(payload)
    for artifact_path in _artifact_paths_from_source_payload(payload):
        pairs.extend(load_sensitive_pair_artifact(artifact_path))
    unique: dict[tuple[str, str, str], dict[str, str]] = {}
    for pair in pairs:
        unique.setdefault(
            (
                str(pair.get("url") or ""),
                str(pair.get("decode_key") or ""),
                str(pair.get("path") or ""),
            ),
            pair,
        )
    return list(unique.values())


def _numeric_key_pairs_from_source_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for artifact_path in _artifact_paths_from_source_payload(
        payload,
        ("numeric_key_pair_artifact", "numeric_key_pair_artifacts"),
    ):
        pairs.extend(load_sensitive_pair_artifact(artifact_path))
    unique: dict[tuple[str, int, str], dict[str, Any]] = {}
    for pair in pairs:
        try:
            key = int(pair.get("key") or 0)
        except (TypeError, ValueError):
            continue
        if key <= 0:
            continue
        unique.setdefault((str(pair.get("url") or ""), key, str(pair.get("path") or "")), pair)

    def expected_bytes(pair: dict[str, Any]) -> int:
        for field in ("expected_bytes", "encrypted_bytes", "content_length", "file_size", "fileSize", "bytes"):
            try:
                value = int(pair.get(field) or 0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        content_range = str(pair.get("content_range") or "")
        match = re.search(r"/(\d+)\s*$", content_range)
        return int(match.group(1)) if match else 0

    return sorted(unique.values(), key=expected_bytes, reverse=True)


def _find_direct_provider_decode_key_success(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        media_source = str(value.get("media_source") or "")
        if media_source in {"resolver_decode_key_pair", "yuanbao_decode_key_pair"}:
            return {
                "media_source": media_source,
                "decode_key_pair_count": int(value.get("decode_key_pair_count") or 0),
                "decode_key_pair_summary": value.get("decode_key_pair_summary")
                if isinstance(value.get("decode_key_pair_summary"), list)
                else [],
            }
        for item in value.values():
            found = _find_direct_provider_decode_key_success(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_direct_provider_decode_key_success(item)
            if found:
                return found
    return {}


def _source_snapshot_summary_from_payload(payload: dict[str, Any]) -> dict[str, int]:
    rounds = payload.get("rounds") if isinstance(payload.get("rounds"), list) else []
    totals = {
        "snapshot_count": 0,
        "source_file_reference_count": 0,
        "source_file_count": 0,
        "missing_source_file_count": 0,
    }
    for item in rounds:
        if not isinstance(item, dict):
            continue
        summary = item.get("source_snapshot_summary")
        if not isinstance(summary, dict):
            continue
        for key in totals:
            try:
                totals[key] += int(summary.get(key) or 0)
            except (TypeError, ValueError):
                continue
    return {key: value for key, value in totals.items() if value}


def _parse_content_range_total(value: Any) -> int:
    text = str(value or "")
    if "/" not in text:
        return 0
    tail = text.rsplit("/", 1)[-1]
    try:
        return int(tail)
    except ValueError:
        return 0


def _candidate_url_classification_summary(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload.get("probe_results") if isinstance(payload.get("probe_results"), list) else []
    status_counts: dict[str, int] = {}
    class_counts: dict[str, int] = {}
    content_type_counts: dict[str, int] = {}
    range_totals: dict[str, int] = {}
    direct_playable = 0
    encrypted_video = 0
    http_success = 0
    http_error = 0
    video_mp4 = 0
    image_candidates = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("range_status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        first_class = str(row.get("first_bytes_class") or "unknown")
        class_counts[first_class] = class_counts.get(first_class, 0) + 1
        content_type = str(row.get("content_type") or "unknown")
        content_type_counts[content_type] = content_type_counts.get(content_type, 0) + 1
        total = _parse_content_range_total(row.get("content_range"))
        if total > 0:
            total_key = str(total)
            range_totals[total_key] = range_totals.get(total_key, 0) + 1
        try:
            status_code = int(row.get("range_status") or 0)
        except (TypeError, ValueError):
            status_code = 0
        if 200 <= status_code < 300:
            http_success += 1
        elif status_code >= 400:
            http_error += 1
        if first_class in {"mp4_container", "mp3_audio", "hls_playlist"}:
            direct_playable += 1
        if content_type.startswith("video/mp4"):
            video_mp4 += 1
        if content_type.startswith("image/"):
            image_candidates += 1
        if content_type.startswith("video/") and first_class == "binary_unknown_or_encrypted":
            encrypted_video += 1
    range_total_bytes_top = [
        {"bytes": int(total), "count": count}
        for total, count in sorted(
            range_totals.items(),
            key=lambda item: (item[1], int(item[0])),
            reverse=True,
        )[:8]
    ]
    return {
        "unique_candidate_url_count": int(payload.get("unique_candidate_url_count") or len(rows)),
        "probe_enabled": bool(payload.get("probe_enabled")),
        "probed_count": len(rows),
        "http_success_candidate_count": http_success,
        "http_error_candidate_count": http_error,
        "video_mp4_candidate_count": video_mp4,
        "image_candidate_count": image_candidates,
        "encrypted_video_candidate_count": encrypted_video,
        "direct_playable_candidate_count": direct_playable,
        "status_counts": dict(sorted(status_counts.items())),
        "first_bytes_class_counts": dict(sorted(class_counts.items())),
        "content_type_counts": dict(sorted(content_type_counts.items())),
        "range_total_bytes_top": range_total_bytes_top,
    }


def _encrypted_candidate_probe_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: payload.get(key)
        for key in (
            "source_file_count",
            "candidate_url_count",
            "decode_key_pair_count",
            "numeric_key_pair_count",
            "heuristic_string_key_count",
            "heuristic_numeric_key_count",
            "successful_numeric_pair_count",
            "result",
            "raw_values_in_report",
        )
        if key in payload
    }


def safe_wx_channels_download_config(
    cert_file: str,
    key_file: str,
    cert_name: str,
    port: int = 2023,
    system_proxy: bool = True,
    upstream_proxy: str = "",
    skip_install_root_cert: bool = False,
) -> str:
    system_value = "true" if system_proxy else "false"
    skip_value = "true" if skip_install_root_cert else "false"
    upstream_value = json.dumps(upstream_proxy)
    return f"""proxy:
  system: {system_value}
  hostname: 127.0.0.1
  port: {int(port)}
  tun: false
  skipInstallRootCert: {skip_value}
  upstreamProxy: {upstream_value}
cert:
  file: {cert_file}
  key: {key_file}
  name: {cert_name}
pagespy:
  enabled: false
  protocol: https
  api: ""
download:
  remoteServer:
    enabled: false
    protocol: http
    hostname: 127.0.0.1
    port: 80
mp:
  remoteServer:
    protocol: http
    hostname: 127.0.0.1
    port: 80
cloudflare:
  enabled: false
"""


def _answer_for(question: str, run: dict[str, Any]) -> str:
    routes = run.get("routes") if isinstance(run.get("routes"), list) else []
    time_model = run.get("time_model") if isinstance(run.get("time_model"), dict) else {}
    if question.startswith("1."):
        return str(run.get("highest_stable_speed") or "unverified")
    if question.startswith("2."):
        return str(run.get("speed_reason") or "尚未完成实时实验；当前报告只记录计划或阶段性结果。")
    if question.startswith("3."):
        return str(run.get("limit_point") or "尚未验证。")
    if question.startswith("4."):
        return ", ".join(str(route.get("name")) for route in routes if isinstance(route, dict)) or "none"
    if question.startswith("5."):
        return "; ".join(
            f"{route.get('name')}: {route.get('status', 'unknown')}" for route in routes if isinstance(route, dict)
        ) or "none"
    if question.startswith("6."):
        return "yes" if run.get("mp3_complete") else "no"
    if question.startswith("7."):
        return f"{float(run.get('wall_seconds') or 0):.2f}s"
    if question.startswith("8."):
        value = run.get("saved_vs_3x_seconds")
        if value is None:
            return "unverified"
        detail = time_model.get("saved_explanation")
        suffix = f" ({detail})" if detail else ""
        return f"{float(value):.2f}s{suffix}"
    if question.startswith("9."):
        return str(run.get("next_optimization") or "优先验证 source download/decrypt 路线，再优化 3x 分段回退。")
    return ""


def _route_evidence_level(route: dict[str, Any]) -> str:
    status = str(route.get("status") or "")
    if status == "success" or route.get("verification"):
        return "verified_mp3"
    if status in {"running", "completed", "evidence_only", "failed", "no_pair"}:
        if route.get("probe"):
            return "diagnostic_probe"
        return "diagnostic"
    if status in {"skipped", "not_run", ""}:
        return "not_attempted"
    if status in {"replaced"}:
        return "replaced"
    return status


def build_route_timing_ledger(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ledger: list[dict[str, Any]] = []
    for route in routes:
        if not isinstance(route, dict):
            continue
        row: dict[str, Any] = {
            "name": str(route.get("name") or ""),
            "status": str(route.get("status") or "unknown"),
            "evidence_level": _route_evidence_level(route),
        }
        if "elapsed_seconds" in route:
            try:
                row["elapsed_seconds"] = round(float(route.get("elapsed_seconds") or 0.0), 3)
            except (TypeError, ValueError):
                row["elapsed_seconds"] = 0.0
        elif "capture_elapsed_seconds" in route:
            try:
                row["elapsed_seconds"] = round(float(route.get("capture_elapsed_seconds") or 0.0), 3)
            except (TypeError, ValueError):
                row["elapsed_seconds"] = 0.0
        else:
            row["elapsed_seconds"] = 0.0
        for key in (
            "summary",
            "limit_point",
            "source_duration_seconds",
            "record_duration_seconds",
            "planned_segment_count",
        ):
            if key in route:
                row[key] = route.get(key)
        probe = route.get("probe")
        if isinstance(probe, dict) and probe.get("limit_point"):
            row["limit_point"] = probe.get("limit_point")
        verification = route.get("verification")
        if isinstance(verification, dict) and "duration_seconds" in verification:
            row["verification_duration_seconds"] = verification.get("duration_seconds")
        segments = route.get("segments")
        if isinstance(segments, list):
            row["actual_segment_count"] = len(segments)
        planned_segments = route.get("planned_segments")
        if isinstance(planned_segments, list):
            row["planned_segment_count"] = len(planned_segments)
        ledger.append(row)
    return ledger


def render_report_markdown(run: dict[str, Any]) -> str:
    safe_run = json.loads(redact_sensitive_text(json.dumps(run, ensure_ascii=False)))
    lines = [
        "# Weixin Fast MP3 Report",
        "",
        f"- URL: `{redact_sensitive_text(str(safe_run.get('url') or ''))}`",
        f"- Output: `{safe_run.get('output') or ''}`",
        f"- Mode: `{safe_run.get('mode') or 'auto'}`",
        f"- Started: `{safe_run.get('started_at') or ''}`",
        f"- Finished: `{safe_run.get('finished_at') or ''}`",
        f"- Wall seconds: `{float(safe_run.get('wall_seconds') or 0):.2f}`",
        "",
        "## Required Answers",
        "",
    ]
    for question in REQUIRED_REPORT_QUESTIONS:
        lines.append(f"### {question}")
        lines.append(redact_sensitive_text(_answer_for(question, safe_run)))
        lines.append("")
    time_model = safe_run.get("time_model")
    if isinstance(time_model, dict) and time_model:
        lines.extend(["## Time Model", ""])
        for key in (
            "source_duration_seconds",
            "confirmed_playback_speed",
            "playback_wall_seconds",
            "serial_segmented_wall_seconds",
            "pipelined_segmented_wall_seconds",
            "saved_vs_serial_segmented_seconds",
            "hard_lower_bound_without_source_seconds",
            "source_decode_seconds",
            "source_decode_saved_vs_3x_seconds",
        ):
            if key in time_model:
                value = time_model[key]
                if isinstance(value, (int, float)):
                    lines.append(f"- {key}: `{float(value):.2f}`")
                else:
                    lines.append(f"- {key}: `{redact_sensitive_text(str(value))}`")
        if time_model.get("saved_explanation"):
            lines.append(f"- saved_explanation: `{redact_sensitive_text(str(time_model['saved_explanation']))}`")
        lines.append("")
    lines.append("## Route Evidence")
    lines.append("")
    routes = safe_run.get("routes") if isinstance(safe_run.get("routes"), list) else []
    if not routes:
        lines.append("- No routes recorded.")
    for route in routes:
        if not isinstance(route, dict):
            continue
        lines.append(
            f"- `{route.get('name')}`: `{route.get('status', 'unknown')}` - "
            f"{redact_sensitive_text(str(route.get('summary') or route.get('purpose') or ''))}"
        )
    ledger = safe_run.get("route_timing_ledger")
    if isinstance(ledger, list) and ledger:
        lines.extend(["", "## Route Timing Ledger", ""])
        for item in ledger:
            if not isinstance(item, dict):
                continue
            detail = [
                f"status `{item.get('status', 'unknown')}`",
                f"elapsed `{float(item.get('elapsed_seconds') or 0):.3f}s`",
                f"evidence `{redact_sensitive_text(str(item.get('evidence_level') or ''))}`",
            ]
            if item.get("limit_point"):
                detail.append(f"limit `{redact_sensitive_text(str(item.get('limit_point') or ''))}`")
            lines.append(f"- `{redact_sensitive_text(str(item.get('name') or ''))}`: " + ", ".join(detail))
    discovery_routes = [
        route
        for route in routes
        if isinstance(route, dict) and isinstance(route.get("source_artifact_discovery"), dict)
    ]
    if discovery_routes:
        lines.extend(["", "## Source Artifact Discovery", ""])
        for route in discovery_routes:
            discovery = route.get("source_artifact_discovery")
            if not isinstance(discovery, dict):
                continue
            lines.append(f"- Route: `{redact_sensitive_text(str(route.get('name') or ''))}`")
            for key in (
                "status",
                "matched_path",
                "match_reason",
                "token_count",
                "candidates_checked",
            ):
                if key in discovery:
                    lines.append(f"- {key}: `{redact_sensitive_text(str(discovery.get(key)))}`")
            roots = discovery.get("roots")
            if isinstance(roots, list) and roots:
                rendered_roots = ", ".join(redact_sensitive_text(str(item)) for item in roots[:5])
                lines.append(f"- roots: `{rendered_roots}`")
    post_open_discovery_routes = [
        route
        for route in routes
        if isinstance(route, dict) and isinstance(route.get("post_open_source_artifact_discovery"), dict)
    ]
    if post_open_discovery_routes:
        lines.extend(["", "## Post-Open Source Artifact Discovery", ""])
        for route in post_open_discovery_routes:
            discovery = route.get("post_open_source_artifact_discovery")
            if not isinstance(discovery, dict):
                continue
            lines.append(f"- Route: `{redact_sensitive_text(str(route.get('name') or ''))}`")
            for key in (
                "status",
                "matched_path",
                "match_reason",
                "token_count",
                "candidates_checked",
                "attempts",
                "wait_seconds",
                "elapsed_seconds",
            ):
                if key in discovery:
                    lines.append(f"- {key}: `{redact_sensitive_text(str(discovery.get(key)))}`")
    delta_routes = [
        route
        for route in routes
        if isinstance(route, dict)
        and route.get("name") == "wx_channels_current_delta_watch"
        and route.get("status") not in {"not_run", "skipped"}
    ]
    if delta_routes:
        lines.extend(["", "## Current Playback Delta Watch", ""])
        for route in delta_routes:
            lines.append(f"- Route: `{redact_sensitive_text(str(route.get('name') or ''))}`")
            for key in (
                "status",
                "diagnosis",
                "report",
                "watch_seconds",
                "visible_media_event_count",
                "baseline_unreadable_media_fd_count",
                "unreadable_media_fd_count",
                "unreadable_media_fd_event_count",
                "largest_unreadable_fd_bytes",
                "attempt_count",
                "sample_unreadable_fds",
                "unreadable_fd_access_probe",
            ):
                if key in route:
                    lines.append(f"- {key}: `{redact_sensitive_text(str(route.get(key)))}`")
            archive = route.get("source_vault_archive")
            if isinstance(archive, dict) and archive:
                for key in ("status", "artifact_path", "manifest_path", "media_file", "source_kind"):
                    if key in archive:
                        lines.append(f"- source_vault_archive.{key}: `{redact_sensitive_text(str(archive.get(key)))}`")
    speed_routes = [
        route
        for route in routes
        if isinstance(route, dict)
        and route.get("name") == "html_media_speed_probe"
        and isinstance(route.get("probe"), dict)
    ]
    if speed_routes:
        lines.extend(["", "## Speed Control Probe", ""])
        for route in speed_routes:
            probe = route.get("probe")
            if not isinstance(probe, dict):
                continue
            for key in ("player_stack", "safe_control_channel", "libvlc_set_rate_symbol", "limit_point"):
                if key in probe:
                    lines.append(f"- {key}: `{redact_sensitive_text(str(probe.get(key)))}`")
            control_probe = probe.get("control_probe")
            if isinstance(control_probe, dict):
                flags = control_probe.get("remote_debugging_flags")
                if isinstance(flags, list):
                    rendered = ", ".join(redact_sensitive_text(str(item)) for item in flags) or "none"
                    lines.append(f"- remote_debugging_flags: `{rendered}`")
                ports = control_probe.get("candidate_debug_ports")
                if isinstance(ports, list):
                    rendered_ports = ", ".join(str(int(port)) for port in ports if isinstance(port, (int, float)))
                    lines.append(f"- candidate_debug_ports: `{rendered_ports or 'none'}`")
                if "cdp_version_ok" in control_probe:
                    lines.append(f"- cdp_version_ok: `{bool(control_probe.get('cdp_version_ok'))}`")
                if control_probe.get("safe_webview_control_channel"):
                    lines.append(
                        "- safe_webview_control_channel: "
                        f"`{redact_sensitive_text(str(control_probe.get('safe_webview_control_channel')))}`"
                    )
                if control_probe.get("limit_point"):
                    lines.append(
                        "- control_limit_point: "
                        f"`{redact_sensitive_text(str(control_probe.get('limit_point')))}`"
                    )
            actual_timeline_probe = probe.get("actual_timeline_probe")
            if isinstance(actual_timeline_probe, dict):
                for key in ("status", "reason", "observed_speed", "requested_speed", "limit_point"):
                    if key in actual_timeline_probe:
                        lines.append(
                            f"- actual_timeline_probe.{key}: "
                            f"`{redact_sensitive_text(str(actual_timeline_probe.get(key)))}`"
                        )
    timeline_routes = [
        route
        for route in routes
        if isinstance(route, dict)
        and route.get("name") == "timeline_seek_probe"
        and isinstance(route.get("probe"), dict)
    ]
    if timeline_routes:
        lines.extend(["", "## Timeline Seek Probe", ""])
        for route in timeline_routes:
            probe = route.get("probe")
            if not isinstance(probe, dict):
                continue
            for key in (
                "complete_mp3_possible",
                "planned_seek_count",
                "segment_seconds",
                "capture_window_seconds",
                "sampled_source_seconds",
                "source_coverage_ratio",
                "hard_lower_bound_without_source_seconds",
                "limit_point",
            ):
                if key in probe:
                    lines.append(f"- {key}: `{redact_sensitive_text(str(probe.get(key)))}`")
    snapshot_routes = [
        route
        for route in routes
        if isinstance(route, dict) and isinstance(route.get("source_snapshot_summary"), dict)
    ]
    if snapshot_routes:
        lines.extend(["", "## Source Snapshot Evidence", ""])
        for route in snapshot_routes:
            lines.append(f"- Route: `{redact_sensitive_text(str(route.get('name') or ''))}`")
            summary = route.get("source_snapshot_summary")
            if isinstance(summary, dict):
                for key in (
                    "snapshot_count",
                    "source_file_reference_count",
                    "source_file_count",
                    "missing_source_file_count",
                ):
                    if key in summary:
                        lines.append(f"- source_snapshot_summary.{key}: `{summary.get(key)}`")
    classification_routes = [
        route
        for route in routes
        if isinstance(route, dict) and isinstance(route.get("candidate_url_classification_summary"), dict)
    ]
    if classification_routes:
        lines.extend(["", "## Candidate URL Classification", ""])
        for route in classification_routes:
            lines.append(f"- Route: `{redact_sensitive_text(str(route.get('name') or ''))}`")
            if route.get("candidate_url_classification_report"):
                lines.append(
                    "- Report: "
                    f"`{redact_sensitive_text(str(route.get('candidate_url_classification_report') or ''))}`"
                )
            summary = route.get("candidate_url_classification_summary")
            if isinstance(summary, dict):
                for key in (
                    "unique_candidate_url_count",
                    "probed_count",
                    "http_success_candidate_count",
                    "http_error_candidate_count",
                    "video_mp4_candidate_count",
                    "encrypted_video_candidate_count",
                    "direct_playable_candidate_count",
                    "image_candidate_count",
                ):
                    if key in summary:
                        lines.append(f"- {key}: `{summary.get(key)}`")
                for counts_key in ("status_counts", "first_bytes_class_counts", "content_type_counts"):
                    counts = summary.get(counts_key)
                    if isinstance(counts, dict) and counts:
                        rendered = ", ".join(
                            f"{redact_sensitive_text(str(name))}={count}"
                            for name, count in sorted(counts.items())
                        )
                        lines.append(f"- {counts_key}: `{rendered}`")
                totals = summary.get("range_total_bytes_top")
                if isinstance(totals, list) and totals:
                    rendered_totals = ", ".join(
                        f"{int(item.get('bytes') or 0)}B x{int(item.get('count') or 0)}"
                        for item in totals
                        if isinstance(item, dict)
                    )
                    if rendered_totals:
                        lines.append(f"- range_total_bytes_top: `{rendered_totals}`")
    encrypted_probe_routes = [
        route
        for route in routes
        if isinstance(route, dict) and isinstance(route.get("encrypted_candidate_probe_summary"), dict)
    ]
    if encrypted_probe_routes:
        lines.extend(["", "## Encrypted Candidate Probe", ""])
        for route in encrypted_probe_routes:
            lines.append(f"- Route: `{redact_sensitive_text(str(route.get('name') or ''))}`")
            if route.get("encrypted_candidate_probe_report"):
                lines.append(
                    "- Report: "
                    f"`{redact_sensitive_text(str(route.get('encrypted_candidate_probe_report') or ''))}`"
                )
            summary = route.get("encrypted_candidate_probe_summary")
            if isinstance(summary, dict):
                for key in (
                    "result",
                    "candidate_url_count",
                    "heuristic_numeric_key_count",
                    "successful_numeric_pair_count",
                    "raw_values_in_report",
                ):
                    if key in summary:
                        lines.append(f"- {key}: `{redact_sensitive_text(str(summary.get(key)))}`")
    rescan_routes = [
        route
        for route in routes
        if isinstance(route, dict) and route.get("post_capture_rescan_report")
    ]
    if rescan_routes:
        lines.extend(["", "## Post-Capture Rescan Evidence", ""])
        for route in rescan_routes:
            lines.append(f"- Route: `{redact_sensitive_text(str(route.get('name') or ''))}`")
            lines.append(
                f"- Report: `{redact_sensitive_text(str(route.get('post_capture_rescan_report') or ''))}`"
            )
            if route.get("post_capture_rescan_result"):
                lines.append(
                    "- Result: "
                    f"`{redact_sensitive_text(str(route.get('post_capture_rescan_result') or ''))}`"
                )
            if "post_capture_rescan_pair_count" in route:
                lines.append(f"- Pair count: `{int(route.get('post_capture_rescan_pair_count') or 0)}`")
            stats = route.get("post_capture_rescan_stats")
            if isinstance(stats, dict) and stats:
                for key in (
                    "child_report_count",
                    "source_file_reference_count",
                    "source_file_count",
                    "missing_source_file_count",
                    "files_scanned",
                    "report_files_scanned",
                    "pair_count",
                ):
                    if key in stats:
                        lines.append(f"- {key}: `{stats.get(key)}`")
                marker_inventory = stats.get("decode_key_marker_inventory")
                if isinstance(marker_inventory, dict) and marker_inventory:
                    lines.append("- decode_key_marker_inventory:")
                    for key in ("marker_count", "near_media_count"):
                        if key in marker_inventory:
                            lines.append(f"- {key}: `{marker_inventory.get(key)}`")
                    field_counts = marker_inventory.get("field_counts")
                    if isinstance(field_counts, dict) and field_counts:
                        rendered_fields = ", ".join(
                            f"{redact_sensitive_text(str(field))}={count}"
                            for field, count in sorted(field_counts.items())
                        )
                        lines.append(f"- field_counts: `{rendered_fields}`")
    segmented = next(
        (route for route in routes if isinstance(route, dict) and route.get("name") == "segmented_blackbox"),
        None,
    )
    if isinstance(segmented, dict) and (segmented.get("segments") or segmented.get("planned_segments")):
        lines.extend(["", "## Segmented Blackbox Evidence", ""])
        if segmented.get("manifest"):
            lines.append(f"- Manifest: `{redact_sensitive_text(str(segmented.get('manifest')))}`")
        if segmented.get("planned_segments") and not segmented.get("segments"):
            lines.append(f"- Planned segments: `{len(segmented.get('planned_segments') or [])}`")
        pipeline = segmented.get("postprocess_pipeline")
        if isinstance(pipeline, dict):
            lines.append(
                "- Postprocess pipeline: "
                f"`{pipeline.get('mode')}`, estimated saved tail "
                f"`{float(pipeline.get('estimated_saved_vs_serial_segmented_seconds') or 0):.2f}s`"
            )
        resume_plan = segmented.get("resume_plan")
        if isinstance(resume_plan, dict):
            lines.append("- Resume plan:")
            lines.append(
                "- first incomplete segment: "
                f"`{resume_plan.get('first_incomplete_segment_index')}`"
            )
            if isinstance(resume_plan.get("reuse_ready_segment_indices"), list):
                lines.append(
                    "- reuse-ready segments: "
                    f"`{','.join(str(item) for item in resume_plan.get('reuse_ready_segment_indices') or [])}`"
                )
            if isinstance(resume_plan.get("retry_segment_indices"), list):
                lines.append(
                    "- retry segments: "
                    f"`{','.join(str(item) for item in resume_plan.get('retry_segment_indices') or [])}`"
                )
            if resume_plan.get("same_work_dir_required"):
                lines.append(
                    "- same work dir required: "
                    f"`{redact_sensitive_text(str(resume_plan.get('same_work_dir_required') or ''))}`"
                )
            if resume_plan.get("same_output_required"):
                lines.append(
                    "- same output required: "
                    f"`{redact_sensitive_text(str(resume_plan.get('same_output_required') or ''))}`"
                )
            command_template = resume_plan.get("command_template")
            if isinstance(command_template, list):
                lines.append(
                    "- command template: "
                    f"`{' '.join(redact_sensitive_text(str(part)) for part in command_template)}`"
                )
        for segment in segmented.get("segments", []):
            if not isinstance(segment, dict):
                continue
            index = int(segment.get("index") or 0)
            source_duration = float(segment.get("source_duration_seconds") or segment.get("duration_seconds") or 0)
            record_duration = segment.get("record_duration_seconds")
            status = str(segment.get("status") or "unknown")
            output = redact_sensitive_text(str(segment.get("output") or ""))
            if record_duration is None:
                lines.append(f"- part {index}: `{status}`, source `{source_duration:.2f}s`, `{output}`")
            else:
                lines.append(
                    f"- part {index}: `{status}`, source `{source_duration:.2f}s`, "
                    f"record `{float(record_duration):.2f}s`, `{output}`"
                )
    lines.append("")
    return "\n".join(lines)


def _tail(value: str, limit: int = 3000) -> str:
    return redact_sensitive_text((value or "")[-limit:])


def _route_record(plan: RoutePlan) -> dict[str, Any]:
    return {**asdict(plan), "status": "not_run", "summary": ""}


def _start_route_timer(route: dict[str, Any]) -> None:
    if "_timer_started_monotonic" not in route:
        route["_timer_started_monotonic"] = time.monotonic()


def _finish_route_timer(route: dict[str, Any]) -> None:
    started = route.pop("_timer_started_monotonic", None)
    if started is None:
        route.setdefault("elapsed_seconds", 0.0)
        return
    route["elapsed_seconds"] = round(time.monotonic() - float(started), 3)


def _finish_all_route_timers(routes: list[dict[str, Any]]) -> None:
    for route in routes:
        if isinstance(route, dict):
            _finish_route_timer(route)


def _route_by_name(routes: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for route in routes:
        if route.get("name") == name:
            return route
    raise KeyError(name)


def _source_artifact_match_tokens(url: str) -> list[str]:
    parsed = urllib.parse.urlparse(url)
    candidates: list[str] = []
    for part in re.split(r"[^A-Za-z0-9_-]+", f"{parsed.netloc} {parsed.path} {parsed.query}"):
        if len(part) >= 6 and part.lower() not in {"weixin", "channels", "qq", "https", "http"}:
            candidates.append(part)
    seen: set[str] = set()
    unique: list[str] = []
    for item in candidates:
        lowered = item.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        unique.append(item)
    return unique


def _source_artifact_text_files(candidate: Path, *, limit: int = 30) -> list[Path]:
    if candidate.is_file():
        return [candidate] if candidate.suffix.lower() in SOURCE_ARTIFACT_TEXT_SUFFIXES else []
    files: list[Path] = []
    try:
        iterator = candidate.rglob("*")
        for item in iterator:
            if len(files) >= limit:
                break
            if item.is_file() and item.suffix.lower() in SOURCE_ARTIFACT_TEXT_SUFFIXES and item.stat().st_size <= 1024 * 1024:
                files.append(item)
    except OSError:
        return files
    return files


def _source_artifact_matches_url(candidate: Path, url: str, tokens: list[str]) -> tuple[bool, str]:
    haystack = str(candidate).lower()
    for token in tokens:
        if token.lower() in haystack:
            return True, "path_token"
    for text_file in _source_artifact_text_files(candidate):
        try:
            text = text_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lowered = text.lower()
        if url.lower() in lowered:
            return True, "text_url"
        for token in tokens:
            if token.lower() in lowered:
                return True, "text_token"
    return False, ""


def source_artifact_roots_from_env(value: str | None = None) -> tuple[Path, ...]:
    raw = value if value is not None else os.environ.get("WEIXIN_SOURCE_ARTIFACT_ROOTS", "")
    roots: list[Path] = []
    if raw.strip():
        for part in raw.split(os.pathsep):
            item = part.strip()
            if item:
                roots.append(Path(item).expanduser())
    roots.extend(DEFAULT_SOURCE_ARTIFACT_ROOTS)
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return tuple(unique)


def discover_source_artifact_for_url(
    url: str,
    roots: tuple[Path, ...] | list[Path],
    *,
    max_candidates: int = 80,
) -> tuple[Path | None, dict[str, Any]]:
    tokens = _source_artifact_match_tokens(url)
    report: dict[str, Any] = {
        "status": "not_found",
        "roots": [str(root.expanduser()) for root in roots],
        "token_count": len(tokens),
        "candidates_checked": 0,
        "matched_path": "",
        "match_reason": "",
    }
    if not tokens:
        report["status"] = "skipped_no_url_token"
        return None, report
    candidates: list[Path] = []
    for root in roots:
        expanded = root.expanduser()
        if not expanded.exists():
            continue
        if expanded.is_file():
            candidates.append(expanded)
            continue
        try:
            children = [item for item in expanded.iterdir() if item.is_dir() or item.is_file()]
        except OSError:
            continue
        children.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)
        candidates.extend(children)
    for candidate in candidates[:max_candidates]:
        report["candidates_checked"] += 1
        matched, reason = _source_artifact_matches_url(candidate, url, tokens)
        if not matched:
            continue
        report.update(
            {
                "status": "matched",
                "matched_path": str(candidate.resolve()),
                "match_reason": reason,
            }
        )
        try:
            report["matched_mtime"] = round(candidate.stat().st_mtime, 3)
        except OSError:
            pass
        return candidate.resolve(), report
    return None, report


def wait_for_source_artifact_for_url(
    url: str,
    roots: tuple[Path, ...] | list[Path],
    *,
    wait_seconds: float = 0.0,
    poll_seconds: float = 0.5,
) -> tuple[Path | None, dict[str, Any]]:
    started = time.monotonic()
    attempts = 0
    last_report: dict[str, Any] = {}
    while True:
        attempts += 1
        discovered, report = discover_source_artifact_for_url(url, roots)
        last_report = report
        last_report["attempts"] = attempts
        last_report["wait_seconds"] = round(max(0.0, float(wait_seconds)), 3)
        last_report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if discovered is not None:
            return discovered, last_report
        if time.monotonic() - started >= max(0.0, float(wait_seconds)):
            return None, last_report
        time.sleep(max(0.05, min(float(poll_seconds), max(0.0, float(wait_seconds)))))


def plan_blackbox_segments(total_duration: float, segment_seconds: float) -> list[dict[str, float | int]]:
    if total_duration <= 0:
        raise ValueError("total_duration must be positive")
    if segment_seconds <= 0:
        raise ValueError("segment_seconds must be positive")
    segments: list[dict[str, float | int]] = []
    start = 0.0
    index = 1
    while start < total_duration - 0.0001:
        duration = min(segment_seconds, total_duration - start)
        segments.append(
            {
                "index": index,
                "start_seconds": round(start, 3),
                "duration_seconds": round(duration, 3),
            }
        )
        start += duration
        index += 1
    return segments


def blackbox_segment_selection(options: AutoPipelineOptions) -> dict[str, Any]:
    explicit = float(options.segment_seconds or 0.0)
    duration = float(options.duration or 0.0)
    if explicit > 0:
        return {
            "source": "explicit_cli",
            "segment_seconds": round(explicit, 3),
            "auto": False,
            "reason": "user supplied --segment-seconds",
        }
    if duration >= AUTO_BLACKBOX_SEGMENT_MIN_SOURCE_SECONDS:
        return {
            "source": "auto_long_blackbox_default",
            "segment_seconds": DEFAULT_AUTO_BLACKBOX_SEGMENT_SECONDS,
            "auto": True,
            "reason": (
                f"source duration >= {AUTO_BLACKBOX_SEGMENT_MIN_SOURCE_SECONDS:g}s; "
                "auto mode uses 600s source segments for resumability and pipelined post-processing"
            ),
        }
    return {
        "source": "none",
        "segment_seconds": 0.0,
        "auto": False,
        "reason": "source duration below auto segmentation threshold and --segment-seconds was not supplied",
    }


def complete_min_duration_seconds(options: AutoPipelineOptions) -> float:
    explicit = max(0.0, float(options.min_duration_seconds or 0.0))
    if explicit:
        return explicit
    declared_source = max(0.0, float(options.duration or 0.0))
    if declared_source <= 0:
        return 0.0
    return max(0.0, max(declared_source - 2.0, declared_source * 0.99))


def estimate_wall_clock_model(
    *,
    source_duration_seconds: float,
    confirmed_playback_speed: float,
    segment_seconds: float = 0.0,
    postprocess_seconds: list[float] | None = None,
    source_decode_seconds: float | None = None,
) -> dict[str, Any]:
    if source_duration_seconds <= 0:
        return {
            "source_duration_seconds": 0.0,
            "confirmed_playback_speed": confirmed_playback_speed,
            "playback_wall_seconds": None,
            "hard_lower_bound_without_source_seconds": None,
            "saved_explanation": "missing_source_duration",
        }
    if confirmed_playback_speed <= 0:
        raise ValueError("confirmed_playback_speed must be positive")
    post = [max(0.0, float(value)) for value in (postprocess_seconds or [])]
    playback_wall = source_duration_seconds / confirmed_playback_speed
    serial = playback_wall + sum(post)
    if segment_seconds > 0 and post:
        first_segment_wall = min(segment_seconds, source_duration_seconds) / confirmed_playback_speed
        overlap_budget = max(0.0, playback_wall - first_segment_wall)
        saved = min(sum(post), overlap_budget)
        pipelined = max(playback_wall, serial - saved)
    else:
        saved = 0.0
        pipelined = serial
    model: dict[str, Any] = {
        "source_duration_seconds": round(source_duration_seconds, 3),
        "confirmed_playback_speed": round(confirmed_playback_speed, 3),
        "playback_wall_seconds": round(playback_wall, 3),
        "segment_seconds": round(segment_seconds, 3) if segment_seconds else 0.0,
        "serialized_postprocess_seconds": round(sum(post), 3),
        "serial_segmented_wall_seconds": round(serial, 3),
        "pipelined_segmented_wall_seconds": round(pipelined, 3),
        "saved_vs_serial_segmented_seconds": round(saved, 3),
        "hard_lower_bound_without_source_seconds": round(playback_wall, 3),
        "saved_explanation": (
            "Pipeline overlaps post-processing with later recording, but cannot reduce the playback wall-clock lower bound."
        ),
    }
    if source_decode_seconds is not None:
        model["source_decode_seconds"] = round(max(0.0, float(source_decode_seconds)), 3)
        model["source_decode_saved_vs_3x_seconds"] = round(max(0.0, playback_wall - float(source_decode_seconds)), 3)
        model["saved_explanation"] = (
            "Source artifact/decode conversion bypasses playback; saved time is the 3x playback lower bound "
            "minus source conversion wall time."
        )
    return model


def evaluate_timeline_seek_strategy(
    *,
    source_duration_seconds: float,
    confirmed_playback_speed: float,
    segment_seconds: float,
    capture_window_seconds: float,
    source_media_access: bool = False,
    safe_fast_seek_control: bool = False,
) -> dict[str, Any]:
    if source_duration_seconds <= 0:
        return {
            "complete_mp3_possible": False,
            "planned_seek_count": 0,
            "sampled_source_seconds": 0.0,
            "source_coverage_ratio": 0.0,
            "hard_lower_bound_without_source_seconds": 0.0,
            "limit_point": "source_duration_unknown",
        }
    speed = max(confirmed_playback_speed, 0.001)
    segment = segment_seconds if segment_seconds > 0 else source_duration_seconds
    window = max(0.0, capture_window_seconds)
    planned_seek_count = int((source_duration_seconds + segment - 0.000001) // segment)
    sampled = min(source_duration_seconds, planned_seek_count * window)
    coverage = sampled / source_duration_seconds if source_duration_seconds else 0.0
    complete_by_sampling = sampled >= source_duration_seconds
    if source_media_access:
        complete = True
        limit_point = "source_media_access_makes_seek_recording_unnecessary"
    elif complete_by_sampling and safe_fast_seek_control:
        complete = True
        limit_point = "seek_windows_cover_full_source_with_verified_continuity"
    elif safe_fast_seek_control:
        complete = False
        limit_point = "seek_windows_do_not_cover_full_source_audio"
    else:
        complete = False
        limit_point = "seek_burst_captures_discontinuous_audio"
    return {
        "complete_mp3_possible": complete,
        "planned_seek_count": planned_seek_count,
        "segment_seconds": round(segment, 3),
        "capture_window_seconds": round(window, 3),
        "sampled_source_seconds": round(sampled, 3),
        "source_coverage_ratio": round(coverage, 6),
        "hard_lower_bound_without_source_seconds": round(source_duration_seconds / speed, 3),
        "source_media_access": bool(source_media_access),
        "safe_fast_seek_control": bool(safe_fast_seek_control),
        "limit_point": limit_point,
    }


def _run_command(
    command: list[str],
    *,
    timeout: int,
    cwd: Path = PROJECT_ROOT,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def _record_process(route: dict[str, Any], proc: Any) -> None:
    route["exit_code"] = int(getattr(proc, "returncode", 0))
    stdout = str(getattr(proc, "stdout", "") or "")
    stderr = str(getattr(proc, "stderr", "") or "")
    if stdout:
        route["stdout_tail"] = _tail(stdout)
    if stderr:
        route["stderr_tail"] = _tail(stderr)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _remote_debugging_flags(ps_output: str) -> tuple[list[str], list[int]]:
    flags: list[str] = []
    ports: list[int] = []
    seen_flags: set[str] = set()
    for match in re.finditer(
        r"--remote-debugging-(port|socket-name|pipe)(?:[=\s]+([^\s]+))?",
        ps_output,
        flags=re.IGNORECASE,
    ):
        name = match.group(1).lower()
        value = match.group(2) or ""
        if name == "port" and value:
            try:
                port = int(value)
            except ValueError:
                normalized = "--remote-debugging-port=<invalid>"
            else:
                if 0 < port <= 65535:
                    ports.append(port)
                    normalized = f"--remote-debugging-port={port}"
                else:
                    normalized = "--remote-debugging-port=<invalid>"
        elif name == "pipe":
            normalized = "--remote-debugging-pipe"
        else:
            normalized = "--remote-debugging-socket-name=<redacted>"
        if normalized not in seen_flags:
            flags.append(normalized)
            seen_flags.add(normalized)
    return flags, sorted(set(ports))


def _wechat_listener_ports(lsof_output: str) -> list[int]:
    ports: list[int] = []
    for line in lsof_output.splitlines():
        lower = line.lower()
        if "listen" not in lower:
            continue
        if not any(marker in lower for marker in ("wechat", "wechatappex", "wxplayer")):
            continue
        for match in re.finditer(r"(?:127\.0\.0\.1|localhost|\*)[:.](\d+)", line, flags=re.IGNORECASE):
            port = int(match.group(1))
            if 0 < port <= 65535:
                ports.append(port)
    return sorted(set(ports))


def _cdp_version_looks_safe(raw: str) -> bool:
    try:
        payload = json.loads(raw)
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    browser = str(payload.get("Browser") or payload.get("browser") or "")
    protocol = str(payload.get("Protocol-Version") or payload.get("protocolVersion") or "")
    websocket = str(payload.get("webSocketDebuggerUrl") or "")
    return bool(browser or protocol or websocket)


def summarize_webview_control_channels(
    *,
    ps_output: str,
    lsof_output: str = "",
    cdp_versions: dict[int, str] | None = None,
) -> dict[str, Any]:
    flags, flag_ports = _remote_debugging_flags(ps_output)
    lsof_ports = _wechat_listener_ports(lsof_output)
    candidate_ports = sorted(set(flag_ports + lsof_ports))
    cdp_versions = cdp_versions or {}
    cdp_ok_ports = [port for port in candidate_ports if _cdp_version_looks_safe(str(cdp_versions.get(port) or ""))]
    safe_channel = "cdp" if cdp_ok_ports else "none_verified"
    if safe_channel == "cdp":
        limit_point = "safe_wechat_webview_cdp_channel_verified"
    elif candidate_ports:
        limit_point = "wechat_webview_cdp_candidates_unverified"
    else:
        limit_point = "wechat_webview_cdp_not_exposed"
    return {
        "remote_debugging_flags": flags,
        "candidate_debug_ports": candidate_ports,
        "cdp_version_ok": bool(cdp_ok_ports),
        "cdp_ok_ports": cdp_ok_ports,
        "safe_webview_control_channel": safe_channel,
        "limit_point": limit_point,
    }


def summarize_speed_capability_probe(
    *,
    ps_output: str,
    lsof_output: str = "",
    nm_output: str = "",
    cdp_versions: dict[int, str] | None = None,
) -> dict[str, Any]:
    ps_lower = ps_output.lower()
    has_wxplayer = "wxplayer" in ps_lower
    has_wechat_renderer = "wechatappex helper" in ps_lower or "wechatappex" in ps_lower
    libvlc_paths = sorted(set(re.findall(r"\S*libvlc[^\s]*\.dylib", lsof_output)))
    has_libvlc = bool(libvlc_paths) or "libvlc" in lsof_output.lower()
    has_set_rate = "_libvlc_media_player_set_rate" in nm_output or "libvlc_media_player_set_rate" in nm_output
    control_probe = summarize_webview_control_channels(
        ps_output=ps_output,
        lsof_output=lsof_output,
        cdp_versions=cdp_versions,
    )
    safe_control_channel = str(control_probe["safe_webview_control_channel"])
    if has_wxplayer and has_libvlc:
        player_stack = "desktop_wechat_wxplayer_libvlc"
        limit_point = "not_html_media_control_channel; desktop_wechat_wxplayer_libvlc_safe_rate_control_unverified"
    elif has_wechat_renderer:
        player_stack = "desktop_wechat_webview_renderer"
        limit_point = str(control_probe["limit_point"])
    else:
        player_stack = "unverified"
        limit_point = "playback_stack_not_observed"
    if safe_control_channel != "none_verified":
        limit_point = str(control_probe["limit_point"])
    if safe_control_channel == "none_verified":
        actual_timeline_probe = {
            "status": "not_run",
            "reason": "no_safe_page_context_control_channel",
        }
    else:
        actual_timeline_probe = {
            "status": "not_run",
            "reason": "safe_control_channel_detected_but_probe_execution_not_wired",
            "safe_control_channel": safe_control_channel,
        }
    return {
        "player_stack": player_stack,
        "wxplayer_process": has_wxplayer,
        "wechat_renderer_process": has_wechat_renderer,
        "libvlc_loaded": has_libvlc,
        "libvlc_paths": libvlc_paths,
        "libvlc_set_rate_symbol": has_set_rate,
        "safe_control_channel": safe_control_channel,
        "control_probe": control_probe,
        "actual_timeline_probe": actual_timeline_probe,
        "limit_point": limit_point,
    }


def run_speed_capability_probe(
    *,
    runner: Callable[..., Any] = _run_command,
    timeout: int = 20,
) -> dict[str, Any]:
    ps_proc = runner(["ps", "-ax", "-o", "pid=,command="], timeout=timeout, cwd=PROJECT_ROOT)
    ps_output = f"{getattr(ps_proc, 'stdout', '') or ''}\n{getattr(ps_proc, 'stderr', '') or ''}"
    lsof_proc = runner(["lsof", "-nP", "-c", "wxplayer"], timeout=timeout, cwd=PROJECT_ROOT)
    lsof_output = f"{getattr(lsof_proc, 'stdout', '') or ''}\n{getattr(lsof_proc, 'stderr', '') or ''}"
    listen_proc = runner(["lsof", "-nP", "-a", "-c", "WeChatAppEx", "-iTCP", "-sTCP:LISTEN"], timeout=timeout, cwd=PROJECT_ROOT)
    listen_output = f"{getattr(listen_proc, 'stdout', '') or ''}\n{getattr(listen_proc, 'stderr', '') or ''}"
    combined_lsof_output = f"{lsof_output}\n{listen_output}"
    libvlc_paths = sorted(set(re.findall(r"\S*libvlc[^\s]*\.dylib", lsof_output)))
    nm_output = ""
    if libvlc_paths:
        nm_proc = runner(["nm", "-gU", libvlc_paths[0]], timeout=timeout, cwd=PROJECT_ROOT)
        nm_output = f"{getattr(nm_proc, 'stdout', '') or ''}\n{getattr(nm_proc, 'stderr', '') or ''}"
    cdp_versions: dict[int, str] = {}
    candidate_ports = summarize_webview_control_channels(
        ps_output=ps_output,
        lsof_output=combined_lsof_output,
    )["candidate_debug_ports"]
    for port in candidate_ports:
        curl_proc = runner(
            ["curl", "-fsS", "--max-time", "1", f"http://127.0.0.1:{port}/json/version"],
            timeout=timeout,
            cwd=PROJECT_ROOT,
        )
        cdp_versions[int(port)] = f"{getattr(curl_proc, 'stdout', '') or ''}\n{getattr(curl_proc, 'stderr', '') or ''}"
    summary = summarize_speed_capability_probe(
        ps_output=ps_output,
        lsof_output=combined_lsof_output,
        nm_output=nm_output,
        cdp_versions=cdp_versions,
    )
    summary["ps_exit_code"] = int(getattr(ps_proc, "returncode", 0))
    summary["lsof_exit_code"] = int(getattr(lsof_proc, "returncode", 0))
    summary["listen_lsof_exit_code"] = int(getattr(listen_proc, "returncode", 0))
    return summary


def _apply_source_capture_result(
    run: dict[str, Any],
    route: dict[str, Any],
    report_path: Path,
    *,
    selected_route: str = "wx_channels_source_download",
    success_summary: str = "Captured a same-response Weixin media URL plus decode_key and converted it to a verified MP3.",
    next_optimization: str = "把可授权稳定返回 URL+decode_key 的 source capture 提升为默认最快路径。",
    output: Path | None = None,
    work_dir: Path | None = None,
    runner: Callable[..., Any] | None = None,
    verifier: Callable[..., dict[str, Any]] | None = None,
    min_duration_seconds: float = 0.0,
    timeout: int = 300,
) -> bool:
    payload = _load_json(report_path)
    pairs = _decode_key_pairs_from_source_payload(payload)
    numeric_pairs = _numeric_key_pairs_from_source_payload(payload)
    if pairs:
        route["decode_key_pair_count"] = len(pairs)
        route["decode_key_pair_summary"] = redacted_decode_key_pair_summary(pairs)
        if output is not None and work_dir is not None and runner is not None and verifier is not None:
            try:
                conversion = decode_weixin_pair_to_mp3(
                    pairs[0],
                    output,
                    work_dir=work_dir
                    / (
                        "decode-key-source-"
                        + hashlib.sha256(str(pairs[0].get("url") or "").encode("utf-8")).hexdigest()[:12]
                    ),
                    runner=runner,
                    timeout=timeout,
                )
                route["decode_key_conversion"] = conversion
                route["verification"] = verifier(output, lambda _message: None, min_duration_seconds=min_duration_seconds)
            except Exception as exc:
                route["decode_key_conversion"] = {"status": "failed", "error": _tail(str(exc))}
            else:
                route["status"] = "success"
                route["summary"] = success_summary
                run.update(
                    {
                        "mp3_complete": True,
                        "selected_route": selected_route,
                        "highest_stable_speed": "non-realtime_source_decode_key",
                        "speed_reason": (
                            "同一响应里拿到媒体 URL 与 decode_key 后，直接下载、解密前 128KB 并转 MP3；"
                            "该路径不受播放器 3x UI 限制。"
                        ),
                        "limit_point": "not_playback_limited",
                        "next_optimization": next_optimization,
                    }
                )
                return True
    if numeric_pairs:
        route["numeric_key_pair_count"] = len(numeric_pairs)
        route["numeric_key_pair_summary"] = redacted_numeric_key_pair_summary(numeric_pairs)
        if output is not None and work_dir is not None and runner is not None and verifier is not None:
            try:
                conversion = decode_weixin_numeric_key_pair_to_mp3(
                    numeric_pairs[0],
                    output,
                    work_dir=work_dir
                    / (
                        "numeric-key-source-"
                        + hashlib.sha256(str(numeric_pairs[0].get("url") or "").encode("utf-8")).hexdigest()[:12]
                    ),
                    runner=runner,
                    timeout=timeout,
                )
                route["numeric_key_conversion"] = conversion
                route["verification"] = verifier(output, lambda _message: None, min_duration_seconds=min_duration_seconds)
            except Exception as exc:
                route["numeric_key_conversion"] = {"status": "failed", "error": _tail(str(exc))}
            else:
                route["status"] = "success"
                route["summary"] = "Captured a Weixin media URL plus numeric key and converted it to a verified MP3."
                run.update(
                    {
                        "mp3_complete": True,
                        "selected_route": selected_route,
                        "highest_stable_speed": "non-realtime_source_numeric_key",
                        "speed_reason": (
                            "同一安全抓取上下文里拿到媒体 URL 与 numeric key 后，直接下载、"
                            "解密并转 MP3；该路径不受播放器 3x UI 限制。"
                        ),
                        "limit_point": "not_playback_limited",
                        "next_optimization": next_optimization,
                    }
                )
                return True
    if not pairs and not numeric_pairs:
        route["decode_key_pair_count"] = int(payload.get("decode_key_pair_count") or 0)
        if isinstance(payload.get("decode_key_pair_summary"), list):
            route["decode_key_pair_summary"] = payload.get("decode_key_pair_summary")
        route["numeric_key_pair_count"] = int(payload.get("numeric_key_pair_count") or 0)
        if isinstance(payload.get("numeric_key_pair_summary"), list):
            route["numeric_key_pair_summary"] = payload.get("numeric_key_pair_summary")
    result = str(payload.get("result") or "")
    if result:
        route["capture_result"] = result
    rounds = payload.get("rounds") if isinstance(payload.get("rounds"), list) else []
    route["capture_rounds"] = len(rounds)
    snapshot_summary = _source_snapshot_summary_from_payload(payload)
    if snapshot_summary:
        route["source_snapshot_summary"] = snapshot_summary
    if result == "encrypted_stodownload_found_decode_key_missing":
        run["limit_point"] = "encrypted_stodownload_decode_key_missing"
        run["speed_reason"] = (
            "已通过受控微信打开抓到加密/混淆 stodownload 媒体线索，但没有可用 decode key；"
            "因此不能绕过完整播放直接生成 MP3。"
        )
        run["next_optimization"] = "继续寻找合法可复现的 decode key 来源，或改做显式 3x 录制兜底优化。"
    elif result == "playable_audio_source_found":
        run["limit_point"] = "source_capture_found_playable_audio_but_converter_not_wired"
        run["speed_reason"] = "源抓取发现可播放音频候选，但当前 auto 编排还未把该证据转换为最终 MP3。"
    elif result:
        run["limit_point"] = result
        run["speed_reason"] = f"受控微信打开后的源抓取结果为 {result}，未生成可验证 MP3。"
    return False


def _apply_vendor_source_artifact(
    run: dict[str, Any],
    route: dict[str, Any],
    source_artifact: Path,
    *,
    output: Path,
    work_dir: Path,
    runner: Callable[..., Any],
    verifier: Callable[..., dict[str, Any]],
    min_duration_seconds: float = 0.0,
    source_duration_seconds: float = 0.0,
    selected_route: str = "existing_direct_or_artifact",
    success_summary: str = "Used an authorized source-listener artifact/local media export and converted it without playback recording.",
    next_optimization: str = (
        "把可授权稳定产出 source-listener artifact 的抓取器接入默认最快路径，"
        "继续减少对黑箱录制的依赖。"
    ),
    timeout: int = 300,
) -> bool:
    vendor_report = work_dir / "source-artifact-vendor.json"
    started = time.monotonic()

    def vendor_runner(command: list[str], **kwargs: Any) -> Any:
        return runner(
            command,
            timeout=int(kwargs.get("timeout") or timeout),
            cwd=PROJECT_ROOT,
        )

    try:
        conversion = convert_vendor_source_to_mp3(
            source_artifact,
            output,
            report_path=vendor_report,
            work_dir=work_dir / "source-artifact-vendor-work",
            runner=vendor_runner,
            verifier=verifier,
            decoder=decode_weixin_pair_to_mp3,
            timeout=timeout,
            min_duration_seconds=min_duration_seconds,
        )
    except Exception as exc:
        route["vendor_source_status"] = "not_matched"
        route["vendor_source_error"] = _tail(str(exc))
        if vendor_report.exists():
            route["vendor_source_report"] = str(vendor_report)
        return False

    elapsed = round(time.monotonic() - started, 3)
    route["status"] = "success"
    route["summary"] = success_summary
    route["vendor_source_status"] = "converted"
    route["vendor_source_report"] = str(vendor_report)
    route["vendor_source_kind"] = str(conversion.get("source_kind") or "")
    route["vendor_source_elapsed_seconds"] = elapsed
    if isinstance(conversion.get("scan"), dict):
        scan = conversion["scan"]
        route["vendor_source_scan"] = {
            key: scan.get(key)
            for key in (
                "source_is_directory",
                "file_count",
                "text_file_scanned_count",
                "skipped_executable_count",
                "local_media_candidate_count",
                "decode_key_pair_count",
                "numeric_key_pair_count",
                "raw_values_in_report",
            )
            if key in scan
        }
    if isinstance(conversion.get("selected_media"), dict):
        route["selected_media"] = conversion["selected_media"]
    if isinstance(conversion.get("decode_key_pair_summary"), list):
        route["decode_key_pair_summary"] = conversion["decode_key_pair_summary"]
        route["decode_key_pair_count"] = len(conversion["decode_key_pair_summary"])
    if isinstance(conversion.get("verification"), dict):
        route["verification"] = conversion["verification"]
    source_kind = str(conversion.get("source_kind") or "")
    if source_kind == "decode_key_pair":
        speed_label = "non-realtime_source_decode_key"
        speed_reason = (
            "使用本地 source-listener artifact 中的媒体 URL 与 decode_key 直接下载解密转 MP3；"
            "该路径不依赖播放器实时播放，因此不受视频号 3x UI 限制。"
        )
    elif source_kind == "numeric_key_pair":
        speed_label = "non-realtime_source_numeric_key"
        speed_reason = (
            "使用本地 source-listener artifact 中的媒体 URL 与 numeric key 直接下载解密转 MP3；"
            "该路径不依赖播放器实时播放，因此不受视频号 3x UI 限制。"
        )
    else:
        speed_label = "non-realtime_vendor_source"
        speed_reason = (
            "使用本地 source-listener artifact 或已下载媒体直接转 MP3；"
            "该路径不依赖播放器实时播放，因此不受视频号 3x UI 限制。"
        )
    run.update(
        {
            "mp3_complete": True,
            "selected_route": selected_route,
            "highest_stable_speed": speed_label,
            "speed_reason": speed_reason,
            "limit_point": "not_playback_limited",
            "next_optimization": next_optimization,
        }
    )
    if source_duration_seconds > 0:
        run["time_model"] = estimate_wall_clock_model(
            source_duration_seconds=float(source_duration_seconds),
            confirmed_playback_speed=WEIXIN_OFFICIAL_BLACKBOX_SPEED_MAX,
            source_decode_seconds=elapsed,
        )
        run["saved_vs_3x_seconds"] = run["time_model"].get("source_decode_saved_vs_3x_seconds")
    return True


def _summarize_current_delta_report(report_path: Path) -> dict[str, Any]:
    if not report_path.exists():
        return {}
    payload = _load_json(report_path)
    summary: dict[str, Any] = {}
    for key in (
        "diagnosis",
        "baseline_unreadable_media_fd_count",
        "unreadable_media_fd_event_count",
        "largest_unreadable_fd_bytes",
        "sample_unreadable_fds",
        "unreadable_fd_access_probe",
    ):
        if key in payload:
            summary[key] = payload.get(key)
    visible_events = payload.get("visible_events")
    if isinstance(visible_events, list):
        summary["visible_media_event_count"] = len(
            [item for item in visible_events if isinstance(item, dict) and item.get("media_candidate")]
        )
    unreadable = payload.get("unreadable_lsof")
    if isinstance(unreadable, list):
        summary["unreadable_media_fd_count"] = len(
            [item for item in unreadable if isinstance(item, dict) and item.get("media_candidate")]
        )
    attempts = payload.get("attempts")
    if isinstance(attempts, list):
        summary["attempt_count"] = len(attempts)
        if attempts:
            last_attempt = attempts[-1]
            if isinstance(last_attempt, dict):
                for key in ("duration", "output_duration", "bytes"):
                    if key in last_attempt:
                        summary[f"last_attempt_{key}"] = last_attempt.get(key)
    result = payload.get("result")
    if isinstance(result, dict):
        for key in ("duration", "output", "error"):
            if key in result:
                summary[f"result_{key}"] = result.get(key)
    return summary


def _hash_file_12(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def _current_delta_source_candidates(delta_report: Path, output: Path) -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    payload = _load_json(delta_report) if delta_report.exists() else {}
    result = payload.get("result")
    if isinstance(result, dict) and result.get("source"):
        candidates.append(("result_source", Path(str(result["source"])).expanduser()))
    attempts = payload.get("attempts")
    if isinstance(attempts, list):
        for attempt in reversed(attempts):
            if isinstance(attempt, dict) and attempt.get("captured"):
                candidates.append(("attempt_captured", Path(str(attempt["captured"])).expanduser()))
                break
    candidates.append(("final_output_mp3", output.expanduser()))
    unique: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for kind, path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append((kind, path))
    return unique


def archive_current_delta_source_artifact(
    *,
    url: str,
    delta_report: Path,
    output: Path,
    roots: tuple[Path, ...] | list[Path],
) -> dict[str, Any]:
    root = (tuple(roots) or (DEFAULT_SOURCE_ARTIFACT_ROOTS[0],))[0].expanduser()
    tokens = _source_artifact_match_tokens(url)
    token = tokens[0] if tokens else hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    safe_token = re.sub(r"[^A-Za-z0-9_-]+", "-", token).strip("-_") or "weixin-source"
    base_name = f"{safe_token}-current-delta"
    artifact_dir = root / base_name
    if artifact_dir.exists():
        suffix = 2
        while (root / f"{base_name}-{suffix}").exists():
            suffix += 1
        artifact_dir = root / f"{base_name}-{suffix}"
    artifact_dir.mkdir(parents=True, exist_ok=False)

    selected_kind = ""
    selected_path: Path | None = None
    for kind, candidate in _current_delta_source_candidates(delta_report, output):
        if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0:
            selected_kind = kind
            selected_path = candidate
            break
    if selected_path is None:
        try:
            artifact_dir.rmdir()
        except OSError:
            pass
        return {"status": "not_archived", "reason": "no_existing_source_or_output"}

    suffix = selected_path.suffix.lower()
    if suffix not in SOURCE_ARTIFACT_MEDIA_SUFFIXES:
        suffix = ".mp3" if selected_kind == "final_output_mp3" else ".bin"
    media_name = f"downloaded{suffix}"
    media_path = artifact_dir / media_name
    shutil.copy2(selected_path, media_path)
    media_sha = _hash_file_12(media_path)
    manifest = {
        "source": "current_delta_watch",
        "url_token": safe_token,
        "url_sha256_12": hashlib.sha256(url.encode("utf-8")).hexdigest()[:12],
        "created_at": now_iso(),
        "media_file": media_name,
        "selected_source_kind": selected_kind,
        "media_bytes": media_path.stat().st_size,
        "media_sha256_12": media_sha,
        "delta_report": str(delta_report),
    }
    manifest_path = artifact_dir / "manifest.json"
    manifest_path.write_text(redact_sensitive_text(json.dumps(manifest, ensure_ascii=False, indent=2)), encoding="utf-8")
    return {
        "status": "archived",
        "artifact_path": str(artifact_dir.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "media_file": str(media_path.resolve()),
        "source_kind": selected_kind,
        "media_bytes": media_path.stat().st_size,
        "media_sha256_12": media_sha,
    }


def _run_current_delta_watch(
    run: dict[str, Any],
    route: dict[str, Any],
    *,
    options: AutoPipelineOptions,
    output: Path,
    work_dir: Path,
    runner: Callable[..., Any],
    verifier: Callable[..., dict[str, Any]],
) -> bool:
    if not options.allow_wechat_ui:
        route["status"] = "skipped"
        route["summary"] = "Skipped because --allow-wechat-ui was not set; no WeChat playback was opened."
        return False
    if options.current_delta_watch_seconds <= 0:
        route["status"] = "skipped"
        route["summary"] = "Skipped because --current-delta-watch-seconds was not set."
        return False

    artifact_dir = work_dir / "current-playback-delta"
    delta_report = artifact_dir / "weixin_current_playback_delta.json"
    watch_seconds = max(0.1, float(options.current_delta_watch_seconds))
    min_duration = complete_min_duration_seconds(options)
    command = [
        sys.executable,
        str(AUTHORIZED_FETCHERS / "weixin_current_playback_delta_to_mp3.py"),
        "--output",
        str(output),
        "--duration",
        str(watch_seconds),
        "--min-size",
        "50000",
        "--min-duration",
        str(min_duration),
        "--artifact-dir",
        str(artifact_dir),
        "--report",
        str(delta_report),
    ]
    route["status"] = "running"
    _start_route_timer(route)
    route["command"] = [redact_sensitive_text(part) for part in command]
    route["watch_seconds"] = round(watch_seconds, 3)
    route["report"] = str(delta_report)
    started = time.monotonic()
    try:
        proc = runner(
            command,
            timeout=max(options.command_timeout_seconds, int(watch_seconds) + 60),
            cwd=PROJECT_ROOT,
        )
        _record_process(route, proc)
    except Exception as exc:
        route["status"] = "failed"
        route["error"] = _tail(str(exc))
        route.update(_summarize_current_delta_report(delta_report))
        route["summary"] = "Current playback delta watcher failed before producing a verified MP3."
        _finish_route_timer(route)
        return False

    route.update(_summarize_current_delta_report(delta_report))
    if int(getattr(proc, "returncode", 0)) == 0 and output.exists() and output.stat().st_size > 0:
        route["verification"] = verifier(
            output,
            lambda _message: None,
            min_duration_seconds=min_duration,
        )
        elapsed = round(time.monotonic() - started, 3)
        route["status"] = "success"
        route["summary"] = (
            "Current playback delta watcher found a readable complete local media/cache candidate "
            "and converted it without blackbox recording."
        )
        route["delta_watch_elapsed_seconds"] = elapsed
        route["source_vault_archive"] = archive_current_delta_source_artifact(
            url=options.url,
            delta_report=delta_report,
            output=output,
            roots=options.source_artifact_roots,
        )
        run.update(
            {
                "mp3_complete": True,
                "selected_route": "wx_channels_current_delta_watch",
                "highest_stable_speed": "non-realtime_current_delta_source",
                "speed_reason": (
                    "打开微信播放后，本地可读媒体/cache 文件变化提供了完整源材料；"
                    "转换不依赖完整实时录制，因此不受 3x 播放上限约束。"
                ),
                "limit_point": "not_playback_limited_if_cache_materialized",
                "next_optimization": (
                    "继续扩大安全可读缓存/source-listener 归档覆盖率；若只暴露不可读 fd，"
                    "再回退到显式 3x 分段录制。"
                ),
            }
        )
        source_duration = float(options.duration or 0)
        if source_duration <= 0:
            verification = route.get("verification")
            if isinstance(verification, dict):
                try:
                    source_duration = float(verification.get("duration_seconds") or 0)
                except (TypeError, ValueError):
                    source_duration = 0.0
        if source_duration > 0:
            run["time_model"] = estimate_wall_clock_model(
                source_duration_seconds=source_duration,
                confirmed_playback_speed=WEIXIN_OFFICIAL_BLACKBOX_SPEED_MAX,
                source_decode_seconds=elapsed,
            )
            run["saved_vs_3x_seconds"] = run["time_model"].get("source_decode_saved_vs_3x_seconds")
        _finish_route_timer(route)
        return True

    route["status"] = "evidence_only" if int(getattr(proc, "returncode", 0)) == 0 else "failed"
    route["summary"] = "Current playback delta watcher did not create a verified MP3."
    _finish_route_timer(route)
    return False


def _finalize_run(run: dict[str, Any], report: Path, started_monotonic: float) -> dict[str, Any]:
    run["finished_at"] = now_iso()
    run["wall_seconds"] = round(time.monotonic() - started_monotonic, 3)
    routes = run.get("routes") if isinstance(run.get("routes"), list) else []
    _finish_all_route_timers(routes)
    run["route_timing_ledger"] = build_route_timing_ledger(routes)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render_report_markdown(run), encoding="utf-8")
    json_path = report.with_suffix(".json")
    json_path.write_text(redact_sensitive_text(json.dumps(run, ensure_ascii=False, indent=2)), encoding="utf-8")
    run["report_path"] = str(report)
    run["json_report_path"] = str(json_path)
    return run


def _write_concat_file(paths: list[Path], concat_file: Path) -> None:
    concat_file.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for path in paths:
        escaped = str(path).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _merge_mp3_segments(
    segment_paths: list[Path],
    output: Path,
    *,
    work_dir: Path,
    runner: Callable[..., Any],
    timeout: int,
) -> dict[str, Any]:
    concat_file = work_dir / "concat-list.txt"
    _write_concat_file(segment_paths, concat_file)
    command = [
        find_ffmpeg(),
        "-hide_banner",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-c",
        "copy",
        str(output),
    ]
    proc = runner(command, timeout=timeout, cwd=PROJECT_ROOT)
    payload: dict[str, Any] = {
        "command": [redact_sensitive_text(part) for part in command],
        "concat_file": str(concat_file),
        "exit_code": int(getattr(proc, "returncode", 0)),
    }
    _record_process(payload, proc)
    if payload["exit_code"] != 0:
        raise RuntimeError("Segment MP3 merge failed.")
    if not output.exists() or output.stat().st_size <= 0:
        raise RuntimeError("Segment MP3 merge did not create output.")
    return payload


def _fast_segment_path(part_path: Path, audio_device: str) -> Path:
    use_system_audio = str(audio_device or "").lower() in {"system", "sck", "screencapturekit"}
    return part_path.with_suffix(".fast.m4a" if use_system_audio else ".fast.wav")


def _segment_status_counts(segment_records: list[dict[str, Any]]) -> dict[str, int]:
    completed_statuses = {"success", "reused"}
    failed = 0
    completed = 0
    recoverable_partial = 0
    for record in segment_records:
        status = str(record.get("status") or "")
        if status in completed_statuses:
            completed += 1
        elif status == "failed":
            failed += 1
        elif status in {"captured", "fast_reused"}:
            recoverable_partial += 1
    return {
        "completed_segment_count": completed,
        "failed_segment_count": failed,
        "recoverable_partial_segment_count": recoverable_partial,
        "pending_segment_count": max(0, len(segment_records) - completed - failed - recoverable_partial),
    }


def _segment_resume_plan(
    *,
    options: AutoPipelineOptions,
    output: Path,
    segment_seconds: float,
    segment_records: list[dict[str, Any]],
) -> dict[str, Any]:
    reuse_ready: list[int] = []
    recoverable_fast: list[int] = []
    retry: list[int] = []
    for record in segment_records:
        index = int(record.get("index") or 0)
        if index <= 0:
            continue
        status = str(record.get("status") or "")
        if status in {"success", "reused"}:
            reuse_ready.append(index)
        elif status in {"captured", "fast_reused"}:
            recoverable_fast.append(index)
        elif status == "failed":
            retry.append(index)

    planned_count = len(plan_blackbox_segments(float(options.duration or 0), segment_seconds)) if segment_seconds > 0 and options.duration > 0 else len(segment_records)
    seen = {int(record.get("index") or 0) for record in segment_records if int(record.get("index") or 0) > 0}
    pending = [index for index in range(1, planned_count + 1) if index not in seen]
    retry.extend(pending)
    first_incomplete = min(recoverable_fast + retry) if recoverable_fast or retry else None
    work_dir = options.work_dir.expanduser().resolve()
    output_path = output.expanduser().resolve()
    command_template = [
        sys.executable,
        "main.py",
        "--url",
        "<same-weixin-url>",
        "--output",
        str(output_path),
        "--mode",
        "auto",
        "--work-dir",
        str(work_dir),
        "--allow-blackbox",
        "--duration",
        str(float(options.duration)),
        "--audio-device",
        options.audio_device,
        "--blackbox-speed",
        str(float(options.blackbox_speed)),
        "--segment-seconds",
        str(float(segment_seconds)),
    ]
    minimum_complete_duration = complete_min_duration_seconds(options)
    if minimum_complete_duration:
        command_template.extend(["--min-duration-seconds", str(float(minimum_complete_duration))])
    return {
        "strategy": "rerun_same_command_with_same_work_dir",
        "first_incomplete_segment_index": first_incomplete,
        "reuse_ready_segment_indices": reuse_ready,
        "recoverable_fast_segment_indices": recoverable_fast,
        "retry_segment_indices": retry,
        "pending_segment_indices": pending,
        "same_work_dir_required": str(work_dir),
        "same_output_required": str(output_path),
        "command_template": command_template,
        "note": (
            "Rerun with the same output and work-dir. Existing verified part MP3 files are reused; "
            "existing fast raw recordings are converted before recording missing segments."
        ),
    }


def _write_segmented_blackbox_manifest(
    *,
    manifest_path: Path,
    options: AutoPipelineOptions,
    output: Path,
    requested_speed: float,
    effective_speed: float,
    segment_seconds: float,
    segment_selection: dict[str, Any],
    segment_records: list[dict[str, Any]],
    postprocess_pipeline: dict[str, Any] | None = None,
    time_model: dict[str, Any] | None = None,
    merge: dict[str, Any] | None = None,
    final_verification: dict[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    counts = _segment_status_counts(segment_records)
    resume_plan = _segment_resume_plan(
        options=options,
        output=output,
        segment_seconds=segment_seconds,
        segment_records=segment_records,
    )
    manifest = {
        "url": redact_sensitive_text(options.url),
        "output": str(output),
        "speed": effective_speed,
        "requested_speed": requested_speed,
        "effective_speed": effective_speed,
        "source_duration_seconds": options.duration,
        "source_segment_seconds": segment_seconds,
        "segment_selection": segment_selection,
        "audio_device": options.audio_device,
        "segments": segment_records,
        **counts,
        "recoverable": counts["completed_segment_count"] > 0 or counts["recoverable_partial_segment_count"] > 0,
        "complete": bool(final_verification and final_verification.get("ok")),
        "resume_plan": resume_plan,
    }
    if postprocess_pipeline is not None:
        manifest["postprocess_pipeline"] = postprocess_pipeline
    if time_model is not None:
        manifest["time_model"] = time_model
    if merge is not None:
        manifest["merge"] = merge
    if final_verification is not None:
        manifest["final_verification"] = final_verification
    if error:
        manifest["error"] = _tail(error)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(redact_sensitive_text(json.dumps(manifest, ensure_ascii=False, indent=2)), encoding="utf-8")
    return manifest


def _convert_fast_segment(
    *,
    fast_path: Path,
    part_path: Path,
    speed: float,
    runner: Callable[..., Any],
    verifier: Callable[..., dict[str, Any]],
    timeout: int,
) -> dict[str, Any]:
    command = [
        *_video_audio_extractor_command(
            "convert-file",
            "--input",
            str(fast_path),
            "--out",
            str(part_path),
            "--recorded-speed",
            str(speed),
        ),
    ]
    started = time.monotonic()
    proc = runner(command, timeout=timeout, cwd=VIDEO_AUDIO_EXTRACTOR_ROOT)
    elapsed = round(time.monotonic() - started, 3)
    payload: dict[str, Any] = {
        "command": [redact_sensitive_text(part) for part in command],
        "exit_code": int(getattr(proc, "returncode", 0)),
        "elapsed_seconds": elapsed,
    }
    _record_process(payload, proc)
    if payload["exit_code"] != 0:
        raise RuntimeError(f"Segment post-processing failed for {part_path.name}.")
    if not part_path.exists() or part_path.stat().st_size <= 0:
        raise RuntimeError(f"Segment post-processing did not create {part_path.name}.")
    payload["verification"] = verifier(part_path, lambda _message: None, min_duration_seconds=0)
    return payload


def _run_segmented_blackbox(
    *,
    options: AutoPipelineOptions,
    output: Path,
    route: dict[str, Any],
    runner: Callable[..., Any],
    verifier: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    requested_speed = float(options.blackbox_speed)
    effective_speed = effective_auto_blackbox_speed(requested_speed)
    route["requested_speed"] = requested_speed
    route["effective_speed"] = effective_speed
    segment_root = options.work_dir.expanduser().resolve() / "blackbox-segments"
    segment_root.mkdir(parents=True, exist_ok=True)
    segment_selection = blackbox_segment_selection(options)
    segment_seconds = float(segment_selection.get("segment_seconds") or 0.0)
    if segment_seconds <= 0:
        raise ValueError("segmented blackbox requires positive segment_seconds")
    route["segment_selection"] = segment_selection
    planned = plan_blackbox_segments(options.duration, segment_seconds)
    segment_records: list[dict[str, Any]] = []
    conversion_futures: list[tuple[dict[str, Any], Path, Future[dict[str, Any]]]] = []
    manifest_path = segment_root / "manifest.json"
    route["segments"] = segment_records
    route["manifest"] = str(manifest_path)
    max_source_segment = max(float(segment["duration_seconds"]) for segment in planned) if planned else 0.0
    max_record_segment = max_source_segment / effective_speed if max_source_segment else 0.0
    postprocess_timeout = max(options.command_timeout_seconds, int(max(max_source_segment, max_record_segment)) + 120)

    def write_checkpoint(error: str = "") -> dict[str, Any]:
        manifest = _write_segmented_blackbox_manifest(
            manifest_path=manifest_path,
            options=options,
            output=output,
            requested_speed=requested_speed,
            effective_speed=effective_speed,
            segment_seconds=segment_seconds,
            segment_selection=segment_selection,
            segment_records=segment_records,
            error=error,
        )
        route["segments"] = segment_records
        route["manifest"] = str(manifest_path)
        return manifest

    def finish_pending_conversions() -> None:
        for record, _part_path, future in conversion_futures:
            if str(record.get("status") or "") in {"success", "failed"}:
                continue
            try:
                conversion = future.result(timeout=postprocess_timeout)
            except Exception as exc:
                record["status"] = "failed"
                record["postprocess_error"] = _tail(str(exc))
            else:
                record["postprocess"] = conversion
                record["verification"] = conversion.get("verification", {})
                record["status"] = "success"
            write_checkpoint()

    write_checkpoint()
    with ThreadPoolExecutor(max_workers=1) as executor:
        for segment in planned:
            index = int(segment["index"])
            source_duration = float(segment["duration_seconds"])
            record_duration = round(source_duration / effective_speed, 3)
            part_path = segment_root / f"{output.stem}.part{index:03d}.mp3"
            fast_path = _fast_segment_path(part_path, options.audio_device)
            record: dict[str, Any] = {
                **segment,
                "source_duration_seconds": round(source_duration, 3),
                "record_duration_seconds": record_duration,
                "output": str(part_path),
                "fast_output": str(fast_path),
                "postprocess_mode": "pipeline_raw_capture_then_convert",
                "status": "running",
            }
            segment_records.append(record)
            if part_path.exists() and part_path.stat().st_size > 0:
                try:
                    record["verification"] = verifier(part_path, lambda _message: None, min_duration_seconds=0)
                except Exception as exc:
                    record["reuse_error"] = _tail(str(exc))
                else:
                    record["status"] = "reused"
                    write_checkpoint()
                    continue
            if fast_path.exists() and fast_path.stat().st_size > 0:
                record["status"] = "fast_reused"
                write_checkpoint()
            else:
                command = [
                    *_video_audio_extractor_command(
                        "blackbox-record",
                        "--url",
                        options.url,
                        "--speed",
                        str(effective_speed),
                        "--out",
                        str(part_path),
                        "--duration",
                        str(record_duration),
                        "--audio-device",
                        options.audio_device,
                        "--no-open",
                        "--wait-audio-timeout",
                        "20",
                        "--keep-fast",
                        "--raw-only",
                    ),
                ]
                record["command"] = [redact_sensitive_text(part) for part in command]
                capture_started = time.monotonic()
                proc = runner(command, timeout=max(options.command_timeout_seconds, int(record_duration) + 120), cwd=VIDEO_AUDIO_EXTRACTOR_ROOT)
                record["capture_elapsed_seconds"] = round(time.monotonic() - capture_started, 3)
                _record_process(record, proc)
                if int(getattr(proc, "returncode", 0)) != 0:
                    record["status"] = "failed"
                    finish_pending_conversions()
                    write_checkpoint(f"Segment {index} recording failed.")
                    raise RuntimeError(f"Segment {index} recording failed.")
                if not fast_path.exists() or fast_path.stat().st_size <= 0:
                    record["status"] = "failed"
                    finish_pending_conversions()
                    write_checkpoint(f"Segment {index} did not create a fast recording.")
                    raise RuntimeError(f"Segment {index} did not create a fast recording.")
                record["status"] = "captured"
                write_checkpoint()
            future = executor.submit(
                _convert_fast_segment,
                fast_path=fast_path,
                part_path=part_path,
                speed=effective_speed,
                runner=runner,
                verifier=verifier,
                timeout=postprocess_timeout,
            )
            conversion_futures.append((record, part_path, future))

        for record, _part_path, future in conversion_futures:
            try:
                conversion = future.result(timeout=postprocess_timeout)
            except Exception as exc:
                record["status"] = "failed"
                record["postprocess_error"] = _tail(str(exc))
                write_checkpoint(str(exc))
                raise
            record["postprocess"] = conversion
            record["verification"] = conversion.get("verification", {})
            record["status"] = "success"
            write_checkpoint()

    conversion_elapsed = [
        float((record.get("postprocess") or {}).get("elapsed_seconds") or 0)
        for record in segment_records
        if isinstance(record.get("postprocess"), dict)
    ]
    first_source_segment_duration = float(planned[0]["duration_seconds"]) if planned else 0.0
    following_capture_wall = max(0.0, (float(options.duration) - first_source_segment_duration) / effective_speed)
    serialized_postprocess = sum(conversion_elapsed)
    estimated_saved = min(
        max(0.0, serialized_postprocess),
        following_capture_wall,
    )
    pipeline_summary = {
        "enabled": True,
        "mode": "raw_capture_then_background_convert",
        "worker_count": 1,
        "serialized_postprocess_seconds": round(serialized_postprocess, 3),
        "estimated_saved_vs_serial_segmented_seconds": round(estimated_saved, 3),
    }
    time_model = estimate_wall_clock_model(
        source_duration_seconds=options.duration,
        confirmed_playback_speed=effective_speed,
        segment_seconds=segment_seconds,
        postprocess_seconds=conversion_elapsed,
    )
    segment_paths = [Path(str(record["output"])) for record in segment_records]

    merge = _merge_mp3_segments(
        segment_paths,
        output,
        work_dir=segment_root,
        runner=runner,
        timeout=max(options.command_timeout_seconds, 120),
    )
    final_verification = verifier(output, lambda _message: None, min_duration_seconds=complete_min_duration_seconds(options))
    manifest = _write_segmented_blackbox_manifest(
        manifest_path=manifest_path,
        options=options,
        output=output,
        requested_speed=requested_speed,
        effective_speed=effective_speed,
        segment_seconds=segment_seconds,
        segment_selection=segment_selection,
        segment_records=segment_records,
        postprocess_pipeline=pipeline_summary,
        time_model=time_model,
        merge=merge,
        final_verification=final_verification,
    )
    route["segments"] = segment_records
    route["manifest"] = str(manifest_path)
    route["postprocess_pipeline"] = pipeline_summary
    route["time_model"] = time_model
    route["merge"] = merge
    route["verification"] = final_verification
    return manifest


def _attach_fallback_eta(run: dict[str, Any], segmented: dict[str, Any], options: AutoPipelineOptions) -> None:
    if options.duration <= 0:
        return
    try:
        effective_speed = effective_auto_blackbox_speed(float(options.blackbox_speed))
    except ValueError:
        return
    segment_selection = blackbox_segment_selection(options)
    segment_seconds = float(segment_selection.get("segment_seconds") or 0.0)
    if segment_seconds <= 0:
        segment_seconds = options.duration
    try:
        planned = plan_blackbox_segments(options.duration, segment_seconds)
    except ValueError:
        planned = []
    for segment in planned:
        source_duration = float(segment.get("duration_seconds") or 0)
        segment["source_duration_seconds"] = round(source_duration, 3)
        segment["record_duration_seconds"] = round(source_duration / effective_speed, 3)
    model = estimate_wall_clock_model(
        source_duration_seconds=options.duration,
        confirmed_playback_speed=effective_speed,
        segment_seconds=segment_seconds,
        postprocess_seconds=[],
    )
    model["requested_playback_speed"] = round(float(options.blackbox_speed), 3)
    model["planning_only"] = True
    model["planned_segment_count"] = len(planned)
    segmented["segment_selection"] = segment_selection
    segmented["requested_speed"] = float(options.blackbox_speed)
    segmented["effective_speed"] = effective_speed
    segmented["planned_segments"] = planned
    segmented["time_model"] = model
    run["time_model"] = model
    run["saved_vs_3x_seconds"] = model.get("saved_vs_serial_segmented_seconds")


def run_auto_pipeline(
    options: AutoPipelineOptions,
    *,
    runner: Callable[..., Any] = _run_command,
    verifier: Callable[..., dict[str, Any]] = verify_mp3,
) -> dict[str, Any]:
    started_monotonic = time.monotonic()
    output = options.output.expanduser().resolve()
    report = options.report.expanduser().resolve()
    work_dir = options.work_dir.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    minimum_complete_duration = complete_min_duration_seconds(options)
    routes = [_route_record(route) for route in plan_auto_routes(options.url)]
    run: dict[str, Any] = {
        "url": options.url,
        "output": str(output),
        "mode": options.mode,
        "started_at": now_iso(),
        "finished_at": "",
        "wall_seconds": 0.0,
        "mp3_complete": False,
        "highest_stable_speed": "unverified",
        "speed_reason": "",
        "limit_point": "",
        "selected_route": "",
        "saved_vs_3x_seconds": None,
        "routes": routes,
        "next_optimization": "继续优先寻找可下载/可解密源；录制只作为显式兜底。",
    }

    direct = _route_by_name(routes, "existing_direct_or_artifact")
    direct["status"] = "running"
    _start_route_timer(direct)
    source_artifact = options.source_artifact.expanduser().resolve() if options.source_artifact else None
    auto_discovered_source_artifact = False
    if source_artifact is None:
        roots = options.source_artifact_roots
        if roots:
            discovered, discovery_report = discover_source_artifact_for_url(options.url, roots)
            direct["source_artifact_discovery"] = discovery_report
            if discovered is not None:
                source_artifact = discovered
                auto_discovered_source_artifact = True
    if source_artifact is not None:
        direct["source_artifact"] = str(source_artifact)
        if not source_artifact.exists():
            direct["source_artifact_status"] = "missing"
        else:
            direct["source_artifact_status"] = "auto_discovered" if auto_discovered_source_artifact else "present"
            if _apply_source_capture_result(
                run,
                direct,
                source_artifact,
                selected_route="existing_direct_or_artifact",
                success_summary=(
                    "Used an auto-discovered source artifact with media URL plus decode_key and converted it to a verified MP3."
                    if auto_discovered_source_artifact
                    else "Used an authorized resolver artifact with media URL plus decode_key and converted it to a verified MP3."
                ),
                next_optimization="把授权 resolver artifact/source provider 接入默认最快路径，减少对播放器录制的依赖。",
                output=output,
                work_dir=work_dir,
                runner=runner,
                verifier=verifier,
                min_duration_seconds=minimum_complete_duration,
                timeout=options.command_timeout_seconds,
            ):
                return _finalize_run(run, report, started_monotonic)
            if _apply_vendor_source_artifact(
                run,
                direct,
                source_artifact,
                output=output,
                work_dir=work_dir,
                runner=runner,
                verifier=verifier,
                min_duration_seconds=minimum_complete_duration,
                source_duration_seconds=options.duration,
                timeout=options.command_timeout_seconds,
            ):
                if auto_discovered_source_artifact:
                    direct["summary"] = (
                        "Used an auto-discovered source artifact/local media export and converted it without playback recording."
                    )
                return _finalize_run(run, report, started_monotonic)

    direct_command = [
        sys.executable,
        str(AUTHORIZED_FETCHERS / "direct_links_to_mp3.py"),
        "--only",
        "weixin",
        "--weixin-link",
        options.url,
        "--weixin-output",
        str(output),
    ]
    direct["command"] = [redact_sensitive_text(part) for part in direct_command]
    try:
        proc = runner(direct_command, timeout=options.command_timeout_seconds, cwd=PROJECT_ROOT)
        _record_process(direct, proc)
    except Exception as exc:
        direct["status"] = "failed"
        direct["error"] = _tail(str(exc))
    else:
        if int(getattr(proc, "returncode", 0)) == 0 and output.exists() and output.stat().st_size > 0:
            direct["status"] = "success"
            direct["summary"] = "Direct/provider route created a local media output; ffmpeg verification passed."
            direct["verification"] = verifier(output, lambda _message: None, min_duration_seconds=minimum_complete_duration)
            highest_stable_speed = "non-realtime_source"
            speed_reason = "使用直接媒体/授权解析路径生成 MP3，不受播放器 3x UI 限制。"
            if DIRECT_LINK_PROBE_REPORT.exists():
                direct["direct_probe_report"] = str(DIRECT_LINK_PROBE_REPORT)
                provider_payload = _load_json(DIRECT_LINK_PROBE_REPORT)
                decode_provider = _find_direct_provider_decode_key_success(provider_payload)
                if decode_provider:
                    direct["direct_provider_media_source"] = decode_provider.get("media_source")
                    direct["decode_key_pair_count"] = int(decode_provider.get("decode_key_pair_count") or 0)
                    if isinstance(decode_provider.get("decode_key_pair_summary"), list):
                        direct["decode_key_pair_summary"] = decode_provider.get("decode_key_pair_summary")
                    highest_stable_speed = "non-realtime_source_decode_key"
                    speed_reason = (
                        "授权 provider/resolver 返回了同一响应里的媒体 URL 与 decode_key，"
                        "因此直接下载解密转 MP3，不受播放器 3x UI 限制。"
                    )
            run.update(
                {
                    "mp3_complete": True,
                    "selected_route": "existing_direct_or_artifact",
                    "highest_stable_speed": highest_stable_speed,
                    "speed_reason": speed_reason,
                    "limit_point": "not_playback_limited",
                }
            )
            return _finalize_run(run, report, started_monotonic)
        direct["status"] = "failed"
        direct["summary"] = "Direct/provider route did not create a verified MP3."
    _finish_route_timer(direct)

    source = _route_by_name(routes, "wx_channels_source_download")
    if not options.allow_wechat_ui:
        source["status"] = "skipped"
        source["summary"] = "Skipped because --allow-wechat-ui was not set; no File Transfer Assistant send/open action was performed."
    else:
        source["status"] = "running"
        _start_route_timer(source)
        source_run_dir = work_dir / "multi-open-capture"
        source_command = [
            sys.executable,
            str(AUTHORIZED_FETCHERS / "weixin_multi_open_capture.py"),
            options.url,
            "--rounds",
            "3",
            "--settle-seconds",
            "8",
            "--scan-duration",
            "8",
            "--child-timeout",
            "75",
            "--run-dir",
            str(source_run_dir),
        ]
        source["command"] = [redact_sensitive_text(part) for part in source_command]
        try:
            proc = runner(source_command, timeout=options.command_timeout_seconds, cwd=PROJECT_ROOT)
            _record_process(source, proc)
            source_report = source_run_dir / "report.json"
            source["report"] = str(source_report)
            source["status"] = "evidence_only" if int(getattr(proc, "returncode", 0)) == 0 else "failed"
            source["summary"] = (
                "Captured source evidence; auto conversion runs only when a same-response "
                "media URL plus decode_key pair is present."
            )
            if source_report.exists():
                if _apply_source_capture_result(
                    run,
                    source,
                    source_report,
                    output=output,
                    work_dir=work_dir,
                    runner=runner,
                    verifier=verifier,
                    min_duration_seconds=minimum_complete_duration,
                    timeout=options.command_timeout_seconds,
                ):
                    return _finalize_run(run, report, started_monotonic)
                classification_report = source_run_dir / "candidate-url-classification.json"
                classification_command = [
                    sys.executable,
                    str(AUTHORIZED_FETCHERS / "weixin_candidate_url_classifier.py"),
                    str(source_run_dir),
                    "--output",
                    str(classification_report),
                    "--timeout",
                    "10",
                ]
                source["candidate_url_classification_command"] = [
                    redact_sensitive_text(part) for part in classification_command
                ]
                classification_record: dict[str, Any] = {
                    "status": "running",
                    "report": str(classification_report),
                }
                try:
                    classification_proc = runner(
                        classification_command,
                        timeout=min(options.command_timeout_seconds, 180),
                        cwd=PROJECT_ROOT,
                    )
                    _record_process(classification_record, classification_proc)
                    classification_record["status"] = (
                        "completed" if int(getattr(classification_proc, "returncode", 0)) == 0 else "failed"
                    )
                except Exception as exc:
                    classification_record["status"] = "failed"
                    classification_record["error"] = _tail(str(exc))
                source["candidate_url_classification"] = classification_record
                source["candidate_url_classification_report"] = str(classification_report)
                if classification_report.exists():
                    classification_payload = _load_json(classification_report)
                    source["candidate_url_classification_summary"] = _candidate_url_classification_summary(
                        classification_payload
                    )
                encrypted_probe_report = source_run_dir / "encrypted-candidate-probe.json"
                encrypted_probe_command = [
                    sys.executable,
                    str(AUTHORIZED_FETCHERS / "weixin_encrypted_candidate_probe.py"),
                    str(source_run_dir),
                    "--output",
                    str(encrypted_probe_report),
                    "--work-dir",
                    str(work_dir / "encrypted-candidate-probe-work"),
                    "--sensitive-artifact-dir",
                    str(WEIXIN_DECRYPT_PROBE_SUCCESS_ROOT),
                    "--max-urls",
                    "12",
                    "--max-heuristic-keys",
                    "12",
                    "--max-heuristic-numeric-keys",
                    "60",
                    "--timeout",
                    "20",
                ]
                source["encrypted_candidate_probe_command"] = [
                    redact_sensitive_text(part) for part in encrypted_probe_command
                ]
                encrypted_probe_record: dict[str, Any] = {
                    "status": "running",
                    "report": str(encrypted_probe_report),
                }
                try:
                    encrypted_probe_proc = runner(
                        encrypted_probe_command,
                        timeout=min(options.command_timeout_seconds, 240),
                        cwd=PROJECT_ROOT,
                    )
                    _record_process(encrypted_probe_record, encrypted_probe_proc)
                    encrypted_probe_record["status"] = (
                        "completed" if int(getattr(encrypted_probe_proc, "returncode", 0)) == 0 else "no_key"
                    )
                except Exception as exc:
                    encrypted_probe_record["status"] = "failed"
                    encrypted_probe_record["error"] = _tail(str(exc))
                source["encrypted_candidate_probe"] = encrypted_probe_record
                source["encrypted_candidate_probe_report"] = str(encrypted_probe_report)
                if encrypted_probe_report.exists():
                    encrypted_probe_payload = _load_json(encrypted_probe_report)
                    source["encrypted_candidate_probe_summary"] = _encrypted_candidate_probe_summary(
                        encrypted_probe_payload
                    )
                    numeric_artifact = encrypted_probe_payload.get("numeric_key_pair_artifact")
                    if isinstance(numeric_artifact, str) and numeric_artifact:
                        source["encrypted_candidate_numeric_key_artifact"] = numeric_artifact
                        if _apply_vendor_source_artifact(
                            run,
                            source,
                            Path(numeric_artifact),
                            output=output,
                            work_dir=work_dir,
                            runner=runner,
                            verifier=verifier,
                            min_duration_seconds=minimum_complete_duration,
                            source_duration_seconds=options.duration,
                            selected_route="wx_channels_source_download",
                            success_summary=(
                                "Decrypted an encrypted Weixin media candidate using a numeric key recovered "
                                "from the same local playback-source context, then converted it without recording."
                            ),
                            next_optimization=(
                                "把 encrypted-candidate numeric-key probe 保持在默认 source-download 路径中，"
                                "并继续扩大可解释 key 字段识别。"
                            ),
                            timeout=options.command_timeout_seconds,
                        ):
                            return _finalize_run(run, report, started_monotonic)
                rescan_report = source_run_dir / "decode-pair-rescan.json"
                rescan_command = [
                    sys.executable,
                    str(AUTHORIZED_FETCHERS / "weixin_multi_open_capture.py"),
                    options.url,
                    "--rescan-only",
                    "--run-dir",
                    str(source_run_dir),
                ]
                source["post_capture_rescan_command"] = [
                    redact_sensitive_text(part) for part in rescan_command
                ]
                rescan_record: dict[str, Any] = {
                    "status": "running",
                    "report": str(rescan_report),
                }
                try:
                    rescan_proc = runner(
                        rescan_command,
                        timeout=min(options.command_timeout_seconds, 120),
                        cwd=PROJECT_ROOT,
                    )
                    _record_process(rescan_record, rescan_proc)
                    rescan_record["status"] = (
                        "completed" if int(getattr(rescan_proc, "returncode", 0)) == 0 else "no_pair"
                    )
                except Exception as exc:
                    rescan_record["status"] = "failed"
                    rescan_record["error"] = _tail(str(exc))
                source["post_capture_rescan"] = rescan_record
                source["post_capture_rescan_report"] = str(rescan_report)
                if rescan_report.exists():
                    rescan_payload = _load_json(rescan_report)
                    rescan_pairs = _decode_key_pairs_from_source_payload(rescan_payload)
                    source["post_capture_rescan_result"] = str(rescan_payload.get("result") or "")
                    source["post_capture_rescan_pair_count"] = (
                        len(rescan_pairs) if rescan_pairs else int(rescan_payload.get("decode_key_pair_count") or 0)
                    )
                    if isinstance(rescan_payload.get("decode_key_pair_summary"), list):
                        source["post_capture_rescan_pair_summary"] = rescan_payload.get("decode_key_pair_summary")
                    if isinstance(rescan_payload.get("rescan"), dict):
                        rescan_stats = rescan_payload["rescan"]
                        source["post_capture_rescan_stats"] = {
                            key: rescan_stats.get(key)
                            for key in (
                                "child_report_count",
                                "source_file_reference_count",
                                "source_file_count",
                                "missing_source_file_count",
                                "files_scanned",
                                "report_files_scanned",
                                "pair_count",
                                "decode_key_marker_inventory",
                            )
                            if key in rescan_stats
                        }
                    if rescan_pairs:
                        if _apply_source_capture_result(
                            run,
                            source,
                            rescan_report,
                            output=output,
                            work_dir=work_dir,
                            runner=runner,
                            verifier=verifier,
                            min_duration_seconds=minimum_complete_duration,
                            timeout=options.command_timeout_seconds,
                        ):
                            return _finalize_run(run, report, started_monotonic)
        except Exception as exc:
            source["status"] = "failed"
            source["error"] = _tail(str(exc))

    if options.allow_wechat_ui and options.source_artifact_roots and not run.get("mp3_complete"):
        discovered_after_open, post_open_discovery = wait_for_source_artifact_for_url(
            options.url,
            options.source_artifact_roots,
            wait_seconds=options.source_artifact_wait_seconds,
        )
        source["post_open_source_artifact_discovery"] = post_open_discovery
        if discovered_after_open is not None:
            source["source_artifact"] = str(discovered_after_open)
            source["source_artifact_status"] = "post_open_auto_discovered"
            if _apply_vendor_source_artifact(
                run,
                source,
                discovered_after_open,
                output=output,
                work_dir=work_dir,
                runner=runner,
                verifier=verifier,
                min_duration_seconds=minimum_complete_duration,
                source_duration_seconds=options.duration,
                selected_route="wx_channels_source_download",
                success_summary=(
                    "Used a post-open Source Vault/source-listener artifact and converted it without playback recording."
                ),
                next_optimization=(
                    "把安全本地 source-listener 的落盘时机接到微信打开流程，"
                    "让裸链接在出现授权源材料后直接绕过 3x 录制。"
                ),
                timeout=options.command_timeout_seconds,
            ):
                return _finalize_run(run, report, started_monotonic)
    _finish_route_timer(source)

    delta_watch = _route_by_name(routes, "wx_channels_current_delta_watch")
    if _run_current_delta_watch(
        run,
        delta_watch,
        options=options,
        output=output,
        work_dir=work_dir,
        runner=runner,
        verifier=verifier,
    ):
        return _finalize_run(run, report, started_monotonic)

    speed_probe = _route_by_name(routes, "html_media_speed_probe")
    _start_route_timer(speed_probe)
    try:
        speed_summary = run_speed_capability_probe(runner=runner, timeout=min(20, options.command_timeout_seconds))
        speed_probe["status"] = "completed"
        speed_probe["probe"] = speed_summary
        speed_probe["summary"] = (
            "Observed playback stack "
            f"{speed_summary.get('player_stack')}; safe >3x control channel remains unverified."
        )
        if not run.get("limit_point") and speed_summary.get("limit_point"):
            run["limit_point"] = str(speed_summary["limit_point"])
    except Exception as exc:
        speed_probe["status"] = "failed"
        speed_probe["error"] = _tail(str(exc))
        speed_probe["summary"] = "Could not complete local speed capability probe."
    _finish_route_timer(speed_probe)

    timeline_probe = _route_by_name(routes, "timeline_seek_probe")
    _start_route_timer(timeline_probe)
    if options.duration > 0:
        try:
            effective_blackbox_speed = effective_auto_blackbox_speed(float(options.blackbox_speed))
        except ValueError:
            effective_blackbox_speed = 0.0
        segment_selection = blackbox_segment_selection(options)
        timeline_segment_seconds = float(segment_selection.get("segment_seconds") or 0.0) or options.duration
        capture_window_seconds = min(30.0, timeline_segment_seconds)
        timeline_summary = evaluate_timeline_seek_strategy(
            source_duration_seconds=options.duration,
            confirmed_playback_speed=effective_blackbox_speed or WEIXIN_OFFICIAL_BLACKBOX_SPEED_MAX,
            segment_seconds=timeline_segment_seconds,
            capture_window_seconds=capture_window_seconds,
            source_media_access=False,
            safe_fast_seek_control=False,
        )
        timeline_probe["status"] = "completed"
        timeline_probe["probe"] = timeline_summary
        if timeline_summary.get("complete_mp3_possible"):
            timeline_probe["summary"] = "Timeline slicing can cover the full source audio under the current assumptions."
        else:
            timeline_probe["summary"] = (
                "Seek-burst sampling is diagnostic only: it does not cover continuous source audio, "
                "so it cannot replace full playback recording."
            )
        if not run.get("limit_point"):
            run["limit_point"] = str(timeline_summary.get("limit_point") or "")
    else:
        timeline_probe["status"] = "skipped"
        timeline_probe["summary"] = "Skipped because source duration is unknown; seek slicing cannot be evaluated."
    _finish_route_timer(timeline_probe)

    segmented = _route_by_name(routes, "segmented_blackbox")
    segmented["status"] = "skipped"
    segmented["summary"] = "Segmented recording can reduce post-processing latency but cannot beat the player wall-clock limit by itself."

    blackbox = _route_by_name(routes, "blackbox_3x_fallback")
    if not options.allow_blackbox:
        blackbox["status"] = "skipped"
        blackbox["summary"] = "Skipped because --allow-blackbox was not set; no recording was started."
        _attach_fallback_eta(run, segmented, options)
        if not run.get("limit_point"):
            run["limit_point"] = "direct_source_unavailable_and_ui_or_recording_not_allowed"
        if not run.get("speed_reason"):
            run["speed_reason"] = "Only the non-UI direct route was attempted in this run."
        if source.get("status") == "skipped":
            run["next_optimization"] = (
                "Use --allow-wechat-ui for guarded source evidence capture, or --allow-blackbox "
                "with duration/audio-device for explicit fallback recording."
            )
        return _finalize_run(run, report, started_monotonic)

    if options.duration <= 0 or not options.audio_device:
        blackbox["status"] = "skipped"
        blackbox["summary"] = "Skipped because blackbox fallback requires --duration and --audio-device."
        _attach_fallback_eta(run, segmented, options)
        run["limit_point"] = "blackbox_missing_duration_or_audio_device"
        run["speed_reason"] = "Recording fallback was explicitly allowed but lacked required capture parameters."
        return _finalize_run(run, report, started_monotonic)

    segmented = _route_by_name(routes, "segmented_blackbox")
    segment_selection = blackbox_segment_selection(options)
    effective_segment_seconds = float(segment_selection.get("segment_seconds") or 0.0)
    segmented["segment_selection"] = segment_selection
    if effective_segment_seconds > 0 and options.duration > effective_segment_seconds:
        segmented["status"] = "running"
        _start_route_timer(segmented)
        try:
            _run_segmented_blackbox(
                options=options,
                output=output,
                route=segmented,
                runner=runner,
                verifier=verifier,
            )
        except Exception as exc:
            segmented["status"] = "failed"
            segmented["error"] = _tail(str(exc))
            run["limit_point"] = "segmented_blackbox_failed"
            run["speed_reason"] = "Explicit segmented blackbox fallback failed before producing a verified final MP3."
            _finish_route_timer(segmented)
        else:
            segmented["status"] = "success"
            selection_source = str(segment_selection.get("source") or "")
            requested_speed = float(options.blackbox_speed)
            effective_speed = effective_auto_blackbox_speed(requested_speed)
            segmented["summary"] = (
                "Auto-selected blackbox segmentation recorded verified segments and merged them into the final MP3."
                if selection_source == "auto_long_blackbox_default"
                else "Explicit blackbox fallback recorded verified segments and merged them into the final MP3."
            )
            blackbox["status"] = "replaced"
            blackbox["summary"] = "Skipped because segmented_blackbox produced the final MP3."
            run.update(
                {
                    "mp3_complete": True,
                    "selected_route": "segmented_blackbox",
                    "highest_stable_speed": f"{effective_speed:g}x_requested_segmented",
                    "speed_reason": (
                        "视频号黑箱录制按已验证的官方播放器速度执行；请求值高于 3x 时仅作为诊断记录，"
                        "不会缩短必须录到的墙钟播放时间。"
                        if requested_speed > effective_speed
                        else "分段录制按已验证播放器速度执行；它提高了失败恢复能力和后处理可并行化空间。"
                    ),
                    "limit_point": "playback_wall_time_segmented_fallback",
                    "saved_vs_3x_seconds": segmented.get("time_model", {}).get("saved_vs_serial_segmented_seconds"),
                    "time_model": segmented.get("time_model", {}),
                }
            )
            return _finalize_run(run, report, started_monotonic)

    if options.blackbox_speed <= 0:
        blackbox["status"] = "failed"
        blackbox["summary"] = "Explicit blackbox fallback requires a positive playback speed."
        run["limit_point"] = "blackbox_invalid_speed"
        run["speed_reason"] = "Recording fallback was explicitly allowed but playback speed was not positive."
        return _finalize_run(run, report, started_monotonic)

    blackbox["status"] = "running"
    _start_route_timer(blackbox)
    requested_speed = float(options.blackbox_speed)
    effective_speed = effective_auto_blackbox_speed(requested_speed)
    blackbox["requested_speed"] = requested_speed
    blackbox["effective_speed"] = effective_speed
    record_duration = round(options.duration / effective_speed, 3)
    blackbox_command = [
        *_video_audio_extractor_command(
            "blackbox-record",
            "--url",
            options.url,
            "--speed",
            str(effective_speed),
            "--out",
            str(output),
            "--duration",
            str(record_duration),
            "--audio-device",
            options.audio_device,
            "--no-open",
            "--wait-audio-timeout",
            "20",
        ),
    ]
    blackbox["command"] = [redact_sensitive_text(part) for part in blackbox_command]
    blackbox["source_duration_seconds"] = options.duration
    blackbox["record_duration_seconds"] = record_duration
    try:
        proc = runner(blackbox_command, timeout=max(options.command_timeout_seconds, int(record_duration) + 120), cwd=VIDEO_AUDIO_EXTRACTOR_ROOT)
        _record_process(blackbox, proc)
    except Exception as exc:
        blackbox["status"] = "failed"
        blackbox["error"] = _tail(str(exc))
    else:
        if int(getattr(proc, "returncode", 0)) == 0 and output.exists() and output.stat().st_size > 0:
            blackbox["status"] = "success"
            blackbox["summary"] = "Explicit blackbox fallback produced a verified MP3."
            blackbox["verification"] = verifier(output, lambda _message: None, min_duration_seconds=minimum_complete_duration)
            run.update(
                {
                    "mp3_complete": True,
                    "selected_route": "blackbox_3x_fallback",
                    "highest_stable_speed": f"{effective_speed:g}x_requested",
                    "speed_reason": (
                        "视频号官方播放器当前只验证到 3x；用户请求更高倍速时，黑箱录制仍按 3x 计算完整时长。"
                        if requested_speed > effective_speed
                        else "录制速度来自已验证播放器速度；只有真实播放器也按该速度播放时才代表墙钟提速。"
                    ),
                    "limit_point": "playback_wall_time",
                }
            )
            return _finalize_run(run, report, started_monotonic)
        blackbox["status"] = "failed"
        blackbox["summary"] = "Explicit blackbox fallback did not create a verified MP3."
    _finish_route_timer(blackbox)

    run["limit_point"] = "all_allowed_routes_failed"
    run["speed_reason"] = "No attempted route produced a verified MP3."
    return _finalize_run(run, report, started_monotonic)


def build_dry_run(url: str, output: Path, mode: str) -> dict[str, Any]:
    started = now_iso()
    routes = [
        {
            **asdict(route),
            "status": "not_run",
            "summary": "planned",
        }
        for route in plan_auto_routes(url)
    ]
    finished = now_iso()
    return {
        "url": url,
        "output": str(output),
        "mode": mode,
        "started_at": started,
        "finished_at": finished,
        "wall_seconds": 0.0,
        "mp3_complete": False,
        "highest_stable_speed": "unverified",
        "speed_reason": "dry-run only; no playback, proxy, download, or recording action was executed.",
        "limit_point": "dry-run",
        "routes": routes,
        "next_optimization": "Run source-download experiment with isolated local certificate and proxy cleanup.",
    }


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weixin link to MP3 fast pipeline")
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", default="auto", choices=["auto"])
    parser.add_argument("--report", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--work-dir", default="")
    parser.add_argument(
        "--source-artifact",
        default="",
        help="Explicit authorized local JSON/report containing a same-context Weixin media URL plus decode_key.",
    )
    parser.add_argument(
        "--source-artifact-root",
        action="append",
        default=[],
        help="Authorized local Source Vault/source-listener root to search before opening WeChat or recording.",
    )
    parser.add_argument(
        "--source-artifact-wait-seconds",
        type=float,
        default=0.0,
        help="After guarded WeChat open/source capture, wait this many seconds for a matching local source artifact.",
    )
    parser.add_argument(
        "--current-delta-watch-seconds",
        type=float,
        default=0.0,
        help="After guarded WeChat open/source capture, watch current playback cache/media file deltas for this many seconds.",
    )
    parser.add_argument("--allow-wechat-ui", action="store_true")
    parser.add_argument("--allow-blackbox", action="store_true")
    parser.add_argument("--duration", type=float, default=0.0)
    parser.add_argument("--audio-device", default="")
    parser.add_argument("--blackbox-speed", type=float, default=3.0)
    parser.add_argument("--segment-seconds", type=float, default=0.0)
    parser.add_argument("--min-duration-seconds", type=float, default=0.0)
    args = parser.parse_args(argv)

    output = Path(args.output).expanduser()
    report = Path(args.report).expanduser() if args.report else output.with_suffix(".report.md")
    start = time.monotonic()
    if args.dry_run:
        run = build_dry_run(args.url, output, args.mode)
        run["wall_seconds"] = round(time.monotonic() - start, 3)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(render_report_markdown(run), encoding="utf-8")
        print(json.dumps({"ok": True, "dry_run": True, "report_path": str(report)}, ensure_ascii=False))
        return 0

    cli_source_artifact_roots = (
        tuple(Path(item).expanduser() for item in args.source_artifact_root)
        if args.source_artifact_root
        else source_artifact_roots_from_env()
    )
    run = run_auto_pipeline(
        AutoPipelineOptions(
            url=args.url,
            output=output,
            report=report,
            mode=args.mode,
            work_dir=Path(args.work_dir).expanduser() if args.work_dir else WORK_ROOT / "fast-pipeline-auto",
            source_artifact=Path(args.source_artifact).expanduser() if args.source_artifact else None,
            source_artifact_roots=cli_source_artifact_roots,
            allow_wechat_ui=args.allow_wechat_ui,
            allow_blackbox=args.allow_blackbox,
            duration=args.duration,
            audio_device=args.audio_device,
            blackbox_speed=args.blackbox_speed,
            segment_seconds=args.segment_seconds,
            min_duration_seconds=args.min_duration_seconds,
            source_artifact_wait_seconds=args.source_artifact_wait_seconds,
            current_delta_watch_seconds=args.current_delta_watch_seconds,
        )
    )
    print(
        json.dumps(
            {
                "ok": bool(run.get("mp3_complete")),
                "output": run.get("output"),
                "report_path": run.get("report_path"),
                "selected_route": run.get("selected_route"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if run.get("mp3_complete") else 2
