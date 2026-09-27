from __future__ import annotations

import json
import shutil
import threading
import time
import traceback
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from .config import LIBRARY_ROOT, PLATFORMS, WORK_ROOT, ensure_layout, platform_folder
from .extractors import (
    convert_media,
    open_songy_login,
    open_url,
    open_weixin_target,
    post_open_source_artifact_wait_seconds,
    run_blackbox_record,
    run_cache_audit,
    run_artifact,
    run_artifact_text,
    run_network_probe,
    run_other_site,
    run_songy,
    run_weixin_cache,
    run_weixin_current_delta,
    run_weixin_link,
    run_xiaohongshu,
)
from .fast_pipeline import (
    AUTO_BLACKBOX_SEGMENT_MIN_SOURCE_SECONDS,
    AutoPipelineOptions,
    run_auto_pipeline,
    source_artifact_roots_from_env,
)
from .utils import classify_url, is_media_url, now_iso, slugify, timestamp_slug, verify_mp3
from .weixin_pipeline_state import (
    load_or_create_pipeline_state,
    mark_pipeline_phase_complete,
    pipeline_state_path,
)


WEIXIN_OFFICIAL_BLACKBOX_SPEED_MAX = 3.0
WINDOWS_REPLACE_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.12, 0.18)
DIAGNOSTIC_ONLY_ACTIONS = {"audit-cache"}
MP3_REQUIRED_ACTIONS = {"convert", "blackbox-record", "health-check"}
TITLE_QUERY_KEYS = {
    "title",
    "name",
    "video_title",
    "videotitle",
    "video_name",
    "videoname",
    "desc",
    "description",
    "subject",
    "filename",
}


def _replace_with_bounded_retry(source: Path, target: Path) -> None:
    for delay in (*WINDOWS_REPLACE_RETRY_DELAYS, None):
        try:
            source.replace(target)
            return
        except PermissionError:
            if delay is None:
                raise
            time.sleep(delay)


def _clean_url_title(value: object) -> str:
    text = urllib.parse.unquote_plus(str(value or "")).strip()
    text = " ".join(text.split())
    if not text or text.lower().startswith(("http://", "https://")):
        return ""
    return text[:120]


def extract_title_from_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urllib.parse.urlparse(raw)
    except Exception:
        return ""

    query_sources = [parsed.query]
    if parsed.fragment:
        fragment_query = parsed.fragment.split("?", 1)[1] if "?" in parsed.fragment else parsed.fragment
        if "=" in fragment_query:
            query_sources.append(fragment_query)

    for query in query_sources:
        for key, values in urllib.parse.parse_qs(query, keep_blank_values=False).items():
            if key.lower() not in TITLE_QUERY_KEYS:
                continue
            for value in values:
                title = _clean_url_title(value)
                if title:
                    return title
    return ""


def minimum_output_duration_seconds(platform: str, payload: dict[str, Any]) -> float:
    if payload.get("allow_short_output"):
        return 0
    override = payload.get("min_duration_seconds", payload.get("min_duration"))
    if override not in (None, ""):
        return float(override)
    if platform == "weixin":
        return 180
    if platform == "third_party" and "songy.info" in str(payload.get("url") or "").lower():
        return 180
    return 0


def effective_blackbox_speed(platform: str, requested_speed: object) -> float:
    try:
        speed = float(requested_speed)
    except (TypeError, ValueError):
        speed = 3.0
    if speed <= 0:
        raise ValueError("Blackbox speed must be positive.")
    if platform == "weixin" and speed > WEIXIN_OFFICIAL_BLACKBOX_SPEED_MAX:
        return WEIXIN_OFFICIAL_BLACKBOX_SPEED_MAX
    return speed


def _payload_float(payload: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = payload.get(key)
    if value in (None, ""):
        return default
    return float(value)


def _payload_int(payload: dict[str, Any], key: str, default: int) -> int:
    value = payload.get(key)
    if value in (None, ""):
        return default
    return int(float(value))


def _payload_path(value: object) -> Path | None:
    raw = str(value or "").strip()
    return Path(raw).expanduser() if raw else None


def _payload_source_artifact_roots(payload: dict[str, Any]) -> tuple[Path, ...]:
    raw = payload.get("source_artifact_roots", payload.get("source_artifact_root"))
    if isinstance(raw, list):
        roots = tuple(Path(str(item)).expanduser() for item in raw if str(item).strip())
        return roots or source_artifact_roots_from_env()
    if isinstance(raw, str) and raw.strip():
        roots = tuple(Path(line.strip()).expanduser() for line in raw.splitlines() if line.strip())
        return roots or source_artifact_roots_from_env()
    return source_artifact_roots_from_env()


def should_use_auto_weixin_blackbox_pipeline(platform: str, payload: dict[str, Any], duration: float) -> bool:
    if platform != "weixin":
        return False
    if _payload_float(payload, "segment_seconds", 0.0) > 0:
        return True
    return float(duration or 0.0) >= AUTO_BLACKBOX_SEGMENT_MIN_SOURCE_SECONDS


def _status_auto_pipeline_summary(run: dict[str, Any]) -> dict[str, str]:
    segmented = next(
        (
            route
            for route in run.get("routes", [])
            if isinstance(route, dict) and route.get("name") == "segmented_blackbox"
        ),
        {},
    )
    return {
        "auto_pipeline_selected_route": str(run.get("selected_route") or ""),
        "auto_pipeline_highest_stable_speed": str(run.get("highest_stable_speed") or ""),
        "auto_pipeline_limit_point": str(run.get("limit_point") or ""),
        "auto_pipeline_report_path": str(run.get("report_path") or run.get("report") or ""),
        "auto_pipeline_json_report_path": str(run.get("json_report_path") or run.get("json_report") or ""),
        "auto_pipeline_segment_manifest_path": str(segmented.get("manifest") or "") if isinstance(segmented, dict) else "",
    }


def run_weixin_auto_blackbox_fallback(
    url: str,
    output: Path,
    artifacts: Path,
    log,
    *,
    payload: dict[str, Any],
    duration: float,
    requested_speed: float,
    effective_speed: float,
    min_duration_seconds: float,
) -> dict[str, Any]:
    report = artifacts / "weixin_auto_blackbox_report.md"
    work_dir = artifacts / "weixin-auto-blackbox"
    source_artifact = _payload_path(payload.get("source_artifact") or payload.get("source_artifact_path"))
    source_wait_raw = payload.get("source_artifact_wait_seconds")
    source_wait = post_open_source_artifact_wait_seconds(str(source_wait_raw)) if source_wait_raw not in (None, "") else post_open_source_artifact_wait_seconds()
    command_timeout = max(1, _payload_int(payload, "command_timeout_seconds", 300))
    if effective_speed != requested_speed:
        log(f"Auto blackbox playback speed capped: requested {requested_speed:g}x, effective {effective_speed:g}x.")
    log(
        "Using Weixin auto pipeline for long blackbox job: "
        "authorized source checks first, segmented blackbox fallback second."
    )
    run = run_auto_pipeline(
        AutoPipelineOptions(
            url=url,
            output=output,
            report=report,
            mode="auto",
            work_dir=work_dir,
            source_artifact=source_artifact,
            source_artifact_roots=_payload_source_artifact_roots(payload),
            allow_wechat_ui=True,
            allow_blackbox=True,
            duration=float(duration or 0.0),
            audio_device=str(payload.get("audio_device") or "").strip(),
            blackbox_speed=float(requested_speed),
            segment_seconds=_payload_float(payload, "segment_seconds", 0.0),
            min_duration_seconds=float(min_duration_seconds or 0.0),
            command_timeout_seconds=command_timeout,
            source_artifact_wait_seconds=source_wait,
            current_delta_watch_seconds=_payload_float(payload, "current_delta_watch_seconds", 0.0),
        )
    )
    log(
        "Weixin auto pipeline finished: "
        f"route={run.get('selected_route') or 'none'}, speed={run.get('highest_stable_speed') or 'unverified'}."
    )
    if not run.get("mp3_complete"):
        reason = run.get("limit_point") or run.get("speed_reason") or "unknown"
        raise RuntimeError(f"Weixin auto blackbox fallback did not produce a complete MP3: {reason}")
    return run


def action_label(action: str) -> str:
    return {
        "convert": "转 MP3",
        "audit-cache": "缓存审计",
        "probe-url": "网络探测",
        "blackbox-record": "黑箱录制",
        "health-check": "健康检查",
    }.get(action, action or "转 MP3")


def action_expects_mp3_output(action: str) -> bool:
    return str(action or "convert") not in DIAGNOSTIC_ONLY_ACTIONS


def action_requires_mp3_output(action: str) -> bool:
    return str(action or "convert") in MP3_REQUIRED_ACTIONS


def output_status_for_action(action: str, state: str, output_exists: bool) -> str:
    if output_exists:
        return "ready"
    if not action_expects_mp3_output(action):
        return "not_applicable"
    if state in {"queued", "running", "paused"}:
        return "pending"
    if state == "completed" and action_requires_mp3_output(action):
        return "missing"
    if state == "completed":
        return "optional_missing"
    return "failed_missing" if state == "failed" else "missing"


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _weixin_source_proof(artifacts_dir: Path | None) -> dict[str, Any] | None:
    if not artifacts_dir or not artifacts_dir.exists():
        return None
    proofs: list[dict[str, Any]] = []
    for report in sorted(artifacts_dir.glob("weixin_vendor_source_artifact*.json")):
        payload = _load_json_object(report)
        body = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        if not isinstance(body, dict):
            continue
        conversion = body.get("numeric_key_conversion") if isinstance(body.get("numeric_key_conversion"), dict) else {}
        if not conversion and isinstance(body.get("decode_key_conversion"), dict):
            conversion = body.get("decode_key_conversion")
        verification = body.get("verification") if isinstance(body.get("verification"), dict) else {}
        scan = body.get("scan") if isinstance(body.get("scan"), dict) else {}
        pair_summary = body.get("numeric_key_pair_summary")
        if not isinstance(pair_summary, list):
            pair_summary = body.get("decode_key_pair_summary") if isinstance(body.get("decode_key_pair_summary"), list) else []
        first_pair = pair_summary[0] if pair_summary and isinstance(pair_summary[0], dict) else {}
        proof = {
            "report_path": str(report),
            "source_kind": str(body.get("source_kind") or ""),
            "encrypted_bytes": int(conversion.get("encrypted_bytes") or 0),
            "expected_bytes": int(first_pair.get("expected_bytes") or 0),
            "duration_seconds": float(verification.get("duration_seconds") or 0),
            "candidate_count": int(
                scan.get("numeric_key_pair_count")
                or scan.get("decode_key_pair_count")
                or len(pair_summary)
                or 0
            ),
        }
        if proof["encrypted_bytes"] or proof["expected_bytes"] or proof["duration_seconds"]:
            proofs.append(proof)
    if not proofs:
        return None
    return max(
        proofs,
        key=lambda proof: (int(proof.get("encrypted_bytes") or 0), float(proof.get("duration_seconds") or 0)),
    )


def _artifact_path(status: dict[str, Any], name: str) -> str:
    for artifact in status.get("artifacts") or []:
        if artifact.get("name") == name:
            return str(artifact.get("path") or "")
    return ""


def _artifact_json(status: dict[str, Any], name: str) -> dict[str, Any]:
    path = _artifact_path(status, name)
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _diagnostic_stage(diagnostics: dict[str, Any], name: str) -> dict[str, Any]:
    stages = diagnostics.get("stages")
    if not isinstance(stages, list):
        return {}
    for stage in stages:
        if isinstance(stage, dict) and stage.get("name") == name:
            return stage
    return {}


def _uploaded_artifact_json(status: dict[str, Any]) -> dict[str, Any]:
    artifacts = status.get("artifacts") if isinstance(status.get("artifacts"), list) else []
    newest_path = ""
    newest_mtime = -1.0
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        name = str(artifact.get("name") or "")
        path_value = str(artifact.get("path") or "")
        if not name.startswith("uploaded/") or not name.lower().endswith((".json", ".har", ".txt")):
            continue
        try:
            path = Path(path_value)
            mtime = path.stat().st_mtime
        except Exception:
            continue
        if mtime > newest_mtime:
            newest_mtime = mtime
            newest_path = path_value
    if not newest_path:
        return {}
    try:
        payload = json.loads(Path(newest_path).read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    if isinstance(payload, dict):
        payload["_artifact_path"] = newest_path
        return payload
    return {"_artifact_path": newest_path}


def _weixin_bridge_artifact_action(status: dict[str, Any]) -> dict[str, str] | None:
    if status.get("platform") != "weixin" or status.get("state") != "failed":
        return None
    error = str(status.get("error") or "")
    if "Artifact conversion failed" not in error:
        return None
    payload = _uploaded_artifact_json(status)
    if payload.get("source") != "weixin_bridge_autopost":
        return None
    bridge_error = str(payload.get("error") or "")
    detail_payload = payload.get("detail") if isinstance(payload.get("detail"), dict) else {}
    err_msg = str(detail_payload.get("err_msg") or "")
    artifact_path = str(payload.get("_artifact_path") or "")
    if err_msg == "system:access_denied" or "No liveId" in bridge_error:
        return {
            "kind": "weixin_bridge_wrong_context",
            "label": "Bridge 未在真实播放页上下文执行",
            "detail": (
                "Bridge 已回传，但未拿到 h5AuthToken/liveId。当前证据更像是在本地 Bridge 页面或非视频号播放页执行，"
                "需要在授权的视频号播放页上下文执行 Bridge JS，或提交包含 renderReplayUrl/renderReplayHlsUrl 的授权响应。"
            ),
            "artifact_path": artifact_path,
        }
    return {
        "kind": "weixin_bridge_artifact_no_media",
        "label": "Bridge 响应未包含媒体 URL",
        "detail": "已收到 Bridge artifact，但其中没有可转换的 renderReplayUrl/renderReplayHlsUrl 或媒体 URL。",
        "artifact_path": artifact_path,
    }


def _bridge_query(status: dict[str, Any]) -> str:
    packet_path = _artifact_path(status, "weixin_open_packet/packet.json")
    if not packet_path:
        return ""
    try:
        packet = json.loads(Path(packet_path).read_text(encoding="utf-8"))
    except Exception:
        return ""
    short_uri = packet.get("short_uri") if isinstance(packet, dict) else ""
    if short_uri:
        return urllib.parse.urlencode({"noprompt": "1", "short_uri": str(short_uri)})
    scene = packet.get("scene_info") if isinstance(packet, dict) else {}
    export_id = scene.get("dynamicExportId") if isinstance(scene, dict) else ""
    if not export_id:
        return ""
    return urllib.parse.urlencode({"noprompt": "1", "eid": str(export_id)})


def _bridge_snippet_url(status: dict[str, Any]) -> str:
    query = _bridge_query(status)
    return f"/api/weixin/bridge-autopost-snippet?{query}" if query else ""


def _bridge_page_url(status: dict[str, Any]) -> str:
    query = _bridge_query(status)
    return f"/weixin-bridge-autopost?autorun=1&{query}" if query else ""


def _bridge_launcher_url(status: dict[str, Any]) -> str:
    query = _bridge_query(status)
    return f"/weixin-bridge-launcher?autorun=1&{query}" if query else ""


def _bridge_payload_packet_path(status: dict[str, Any]) -> str:
    return _artifact_path(status, "weixin_bridge_payload_packet.json")


def diagnose_next_action(status: dict[str, Any]) -> dict[str, str] | None:
    if status.get("state") != "failed":
        return None
    bridge_artifact_action = _weixin_bridge_artifact_action(status)
    if bridge_artifact_action:
        return bridge_artifact_action
    platform = status.get("platform")
    url = str(status.get("url") or "")
    error = str(status.get("error") or "")
    if platform == "third_party" and "songy.info" in url.lower() and "Songy browser capture failed" in error:
        return {
            "kind": "songy_login_play_retry",
            "label": "需要登录/播放后重试",
            "detail": "在打开的学升手机浏览器里完成登录，进入课程并播放一小段，再重新开始任务或提交捕获 artifact。",
            "artifact_path": _artifact_path(status, "songy_browser_capture.json"),
        }
    if platform == "weixin" and "Weixin link-to-MP3 failed" in error:
        diagnostics = _artifact_json(status, "weixin_link_diagnostics.json")
        identity = diagnostics.get("target_identity") if isinstance(diagnostics.get("target_identity"), dict) else {}
        delta_stage = _diagnostic_stage(diagnostics, "current_playback_delta_watch")
        delta_diagnostics = (
            delta_stage.get("diagnostics") if isinstance(delta_stage.get("diagnostics"), dict) else {}
        )
        direct_stage = _diagnostic_stage(diagnostics, "direct_link_provider_probe")
        provider_keys = direct_stage.get("provider_keys") if isinstance(direct_stage.get("provider_keys"), dict) else {}
        bridge_url = _bridge_snippet_url(status)
        bridge_page_url = _bridge_page_url(status)
        bridge_launcher_url = _bridge_launcher_url(status)
        if delta_diagnostics.get("diagnosis") == "playback_fd_unlinked":
            detail = (
                "短链已归一到底层视频，但当前播放数据落在微信未命名临时 fd，脚本无法像普通文件一样复制；"
                "下一步需要在播放页执行 Bridge JS 回传授权响应，或配置授权 provider key 后重试。"
                "如果仍不能取得媒体源，可以切到“黑箱录制”模式，填写本机音频采集设备后显式启动兜底录制。"
            )
            if identity.get("dynamic_export_id_sha256_12"):
                detail += f" 当前底层标识哈希：{identity['dynamic_export_id_sha256_12']}。"
            return {
                "kind": "weixin_bridge_or_provider_required",
                "label": "需要 Bridge/授权响应",
                "detail": detail,
                "diagnostics_path": _artifact_path(status, "weixin_link_diagnostics.json"),
                "open_packet_path": _artifact_path(status, "weixin_open_packet/open_packet.html"),
                "bridge_snippet_url": bridge_url,
                "bridge_page_url": bridge_page_url,
                "bridge_launcher_url": bridge_launcher_url,
                "bridge_payload_packet_path": _bridge_payload_packet_path(status),
                "identity_hash": str(identity.get("dynamic_export_id_sha256_12") or ""),
                "provider_keys_configured": "yes" if any(provider_keys.values()) else "no",
            }
        return {
            "kind": "weixin_playback_bridge",
            "label": "需要视频号播放页授权桥接",
            "detail": "把目标视频号播放页重新打开到前台，保持播放，再运行 bridge 片段或当前播放监听。",
            "diagnostics_path": _artifact_path(status, "weixin_link_diagnostics.json"),
            "open_packet_path": _artifact_path(status, "weixin_open_packet/open_packet.html"),
            "bridge_snippet_url": bridge_url,
            "bridge_page_url": bridge_page_url,
            "bridge_launcher_url": bridge_launcher_url,
            "bridge_payload_packet_path": _bridge_payload_packet_path(status),
        }
    return None


class JobStore:
    def __init__(self) -> None:
        ensure_layout()
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def list_jobs(self) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        deleted_ids = self._read_id_registry("deleted")
        paused_ids = self._read_id_registry("paused")
        for platform in PLATFORMS:
            root = platform_folder(platform)
            for status_path in root.glob("*/status.json"):
                try:
                    status = json.loads(status_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if status.get("id") in deleted_ids or status.get("deleted"):
                    continue
                if status.get("id") in paused_ids:
                    status["state"] = "paused"
                    status["pause_requested"] = True
                jobs.append(self._hydrate_status(status))
        jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return jobs

    def get_job(self, job_id: str) -> dict[str, Any]:
        status = self._find_status(job_id)
        if not status or job_id in self._read_id_registry("deleted") or status.get("deleted"):
            raise FileNotFoundError(job_id)
        if job_id in self._read_id_registry("paused"):
            status["state"] = "paused"
            status["pause_requested"] = True
        return self._hydrate_status(status)

    def read_log(self, job_id: str) -> str:
        status = self.get_job(job_id)
        path = Path(status["log_path"])
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = str(payload.get("url", "")).strip()
        artifact_path = str(payload.get("artifact_path") or "").strip()
        artifact_text = str(payload.get("artifact_text") or "").strip()
        platform = str(payload.get("platform") or "auto")
        if platform == "auto":
            platform = classify_url(url)
        if platform not in PLATFORMS:
            raise ValueError(f"Unknown platform: {platform}")
        if not url and not artifact_path and not artifact_text and platform != "weixin":
            raise ValueError("A URL, local artifact path, or pasted artifact text is required.")

        action = str(payload.get("action") or "convert")
        job_id = uuid.uuid4().hex[:12]
        run_name = f"{timestamp_slug()}-{slugify(url or action, platform)}-{job_id[:6]}"
        run_dir = platform_folder(platform) / run_name
        artifacts_dir = run_dir / "artifacts"
        run_dir.mkdir(parents=True, exist_ok=True)
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        output = run_dir / "output.mp3"
        log_path = run_dir / "job.log"
        status = {
            "id": job_id,
            "platform": platform,
            "platform_label": PLATFORMS[platform]["label"],
            "action": action,
            "action_label": action_label(action),
            "url": url,
            "display_title": extract_title_from_url(url),
            "state": "queued",
            "created_at": now_iso(),
            "started_at": None,
            "finished_at": None,
            "run_dir": str(run_dir),
            "artifact_dir": str(artifacts_dir),
            "output_path": str(output),
            "log_path": str(log_path),
            "error": "",
            "verify": None,
        }
        (run_dir / "input.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._write_status(status)
        thread = threading.Thread(target=self._run_job, args=(job_id, payload), daemon=True)
        with self._lock:
            self._threads[job_id] = thread
        thread.start()
        return status

    def pause_jobs(self, job_ids: list[str]) -> dict[str, Any]:
        ids = self._normalize_job_ids(job_ids)
        paused_ids = self._read_id_registry("paused")
        deleted_ids = self._read_id_registry("deleted")
        result: dict[str, Any] = {"paused": [], "skipped": [], "missing": []}
        for job_id in ids:
            status = self._find_status(job_id)
            if not status or job_id in deleted_ids or status.get("deleted"):
                result["missing"].append(job_id)
                continue
            if status.get("state") not in {"queued", "running"}:
                result["skipped"].append(job_id)
                continue
            status["state"] = "paused"
            status["pause_requested"] = True
            status["pause_reason"] = "Paused from the Studio task list."
            status["finished_at"] = status.get("finished_at") or now_iso()
            self._append_log(status, "Paused from the Studio task list.")
            self._write_status(status)
            paused_ids.add(job_id)
            result["paused"].append(job_id)
        self._write_id_registry("paused", paused_ids)
        return result

    def delete_jobs(self, job_ids: list[str]) -> dict[str, Any]:
        ids = self._normalize_job_ids(job_ids)
        deleted_ids = self._read_id_registry("deleted")
        paused_ids = self._read_id_registry("paused")
        result: dict[str, Any] = {"deleted": [], "archived": {}, "hidden_running": [], "missing": []}
        for job_id in ids:
            status = self._find_status(job_id)
            if not status:
                if job_id in deleted_ids:
                    result["deleted"].append(job_id)
                else:
                    result["missing"].append(job_id)
                continue
            status["deleted"] = True
            status["deleted_at"] = now_iso()
            deleted_ids.add(job_id)
            paused_ids.discard(job_id)
            if status.get("state") in {"queued", "running"}:
                status["state"] = "paused"
                status["pause_requested"] = True
                status["pause_reason"] = "Deleted from the Studio task list."
                status["finished_at"] = status.get("finished_at") or now_iso()
                self._append_log(status, "Deleted from the Studio task list; hidden from the UI.")
                self._write_status(status)
                result["hidden_running"].append(job_id)
            else:
                self._write_status(status)
                archived_path = self._archive_finished_job(status)
                if archived_path:
                    result["archived"][job_id] = archived_path
            result["deleted"].append(job_id)
        self._write_id_registry("deleted", deleted_ids)
        self._write_id_registry("paused", paused_ids)
        return result

    def open_target(self, url: str, platform: str = "auto") -> dict[str, Any]:
        if platform == "auto":
            platform = classify_url(url)
        if platform == "weixin":
            return {"ok": True, **open_weixin_target(url)}
        if platform == "third_party" and ("songy.info" in url.lower() or not url):
            return {"ok": True, **open_songy_login(url)}
        open_url(url)
        return {"ok": True, "opened": url, "method": "default"}

    def _run_job(self, job_id: str, payload: dict[str, Any]) -> None:
        status = self.get_job(job_id)
        log_path = Path(status["log_path"])

        def log(message: str) -> None:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(message + "\n")

        try:
            status["state"] = "running"
            status["started_at"] = now_iso()
            self._write_status(status)
            log(f"Started {status['platform_label']} job {job_id}.")

            output = Path(status["output_path"])
            artifacts = Path(status["artifact_dir"])
            url = status["url"]
            mode = str(payload.get("mode") or "auto")
            action = str(status.get("action") or payload.get("action") or "convert")
            artifact_path = str(payload.get("artifact_path") or "").strip()
            artifact_text = str(payload.get("artifact_text") or "").strip()
            artifact_ext = str(payload.get("artifact_ext") or ".json")
            duration = int(payload.get("duration") or 300)

            if action == "audit-cache":
                cache_dirs_raw = payload.get("cache_dirs") or []
                cache_dirs = (
                    [str(item).strip() for item in cache_dirs_raw if str(item).strip()]
                    if isinstance(cache_dirs_raw, list)
                    else [line.strip() for line in str(cache_dirs_raw).splitlines() if line.strip()]
                )
                report_path = run_cache_audit(
                    status["platform"],
                    url,
                    artifacts,
                    log,
                    duration=duration,
                    dirs=cache_dirs,
                )
                log(f"Cache audit report: {report_path}")
                status["verify"] = None
                status["state"] = "completed"
                log("Diagnostic cache audit completed.")
                return

            if action == "probe-url":
                produced = run_network_probe(url, output, artifacts, log, duration=duration, convert=True)
                if produced:
                    min_duration_seconds = minimum_output_duration_seconds(status["platform"], payload)
                    status["verify"] = verify_mp3(output, log, min_duration_seconds=min_duration_seconds)
                    log("Network probe converted a candidate media URL to MP3.")
                else:
                    status["verify"] = None
                    log("Network probe completed without a convertible audio candidate.")
                status["state"] = "completed"
                return

            if action == "blackbox-record":
                requested_speed = float(payload.get("blackbox_speed") or payload.get("speed") or 3)
                speed = effective_blackbox_speed(status["platform"], requested_speed)
                status["blackbox_requested_speed"] = requested_speed
                status["blackbox_effective_speed"] = speed
                if speed != requested_speed:
                    log(
                        "Blackbox speed capped for verified playback: "
                        f"requested {requested_speed:g}x, effective {speed:g}x."
                    )
                min_duration_seconds = minimum_output_duration_seconds(
                    status["platform"],
                    {**payload, "allow_short_output": bool(payload.get("allow_short_output", True))},
                )
                if should_use_auto_weixin_blackbox_pipeline(status["platform"], payload, duration):
                    run = run_weixin_auto_blackbox_fallback(
                        url,
                        output,
                        artifacts,
                        log,
                        payload=payload,
                        duration=duration,
                        requested_speed=requested_speed,
                        effective_speed=speed,
                        min_duration_seconds=min_duration_seconds,
                    )
                    status.update(_status_auto_pipeline_summary(run))
                    status["verify"] = verify_mp3(output, log, min_duration_seconds=min_duration_seconds)
                    status["state"] = "completed"
                    log("Long Weixin blackbox job completed through the auto segmented pipeline.")
                    return
                run_blackbox_record(
                    url,
                    output,
                    artifacts,
                    log,
                    duration=duration,
                    speed=speed,
                    audio_device=str(payload.get("audio_device") or "").strip(),
                    wait_audio_timeout=float(payload.get("wait_audio_timeout") or 0),
                )
                status["verify"] = verify_mp3(output, log, min_duration_seconds=min_duration_seconds)
                status["state"] = "completed"
                log("Blackbox recording completed and MP3 verification passed.")
                return

            if artifact_text:
                log("Processing pasted authorized artifact text.")
                run_artifact_text(status["platform"], url, artifact_text, artifact_ext, output, artifacts, log)
            elif artifact_path:
                log("Processing a local authorized artifact path.")
                run_artifact(status["platform"], url, artifact_path, output, artifacts, log)
            elif status["platform"] == "xiaohongshu":
                run_xiaohongshu(url, output, artifacts, log)
            elif status["platform"] == "weixin":
                watch_current_only = mode == "watch-current" or bool(payload.get("watch_current"))
                manual_playback = mode == "manual-playback" or bool(payload.get("weixin_manual_playback"))
                min_duration_seconds = minimum_output_duration_seconds(status["platform"], payload)
                if url:
                    run_weixin_link(
                        url,
                        output,
                        artifacts,
                        log,
                        duration=duration,
                        watch_current_only=watch_current_only,
                        manual_playback=manual_playback,
                        min_duration=min_duration_seconds,
                    )
                else:
                    if watch_current_only:
                        log("Watching the current logged-in WeChat playback without reopening a link.")
                        run_weixin_current_delta(output, artifacts, log, duration=duration)
                    else:
                        log("Use the current logged-in WeChat account and keep the replay playing.")
                        run_weixin_cache(output, log, duration=duration)
            elif status["platform"] == "other":
                run_other_site(
                    url,
                    output,
                    artifacts,
                    log,
                    sample_seconds=int(payload.get("sample_seconds") or 0),
                )
            else:
                if "songy.info" in url.lower() or mode == "songy-login":
                    wait_seconds = int(payload.get("wait_seconds") or 180)
                    fast_record = bool(payload.get("fast_record"))
                    run_songy(url, output, artifacts, log, wait_seconds=wait_seconds, fast_record=fast_record)
                elif is_media_url(url):
                    convert_media(url, output, log)
                else:
                    raise RuntimeError("Unsupported third-party link. Use Songy login capture or a direct media URL.")

            min_duration_seconds = minimum_output_duration_seconds(status["platform"], payload)
            status["verify"] = verify_mp3(output, log, min_duration_seconds=min_duration_seconds)
            if status["platform"] == "weixin" and url and pipeline_state_path(artifacts).is_file():
                pipeline_mode = (
                    "manual_playback"
                    if mode == "manual-playback" or bool(payload.get("weixin_manual_playback"))
                    else "watch_current"
                    if mode == "watch-current" or bool(payload.get("watch_current"))
                    else "open_then_watch"
                )
                try:
                    pipeline_path, pipeline_state = load_or_create_pipeline_state(
                        artifacts,
                        url=url,
                        mode=pipeline_mode,
                    )
                    mark_pipeline_phase_complete(
                        pipeline_path,
                        pipeline_state,
                        "output_verified",
                        details={
                            "duration_seconds": status["verify"].get("duration_seconds"),
                            "bytes": int(status["verify"].get("bytes") or 0),
                        },
                    )
                except Exception as exc:
                    log(f"Weixin pipeline state finalization warning: {exc}")
            status["state"] = "completed"
            log("Job completed and MP3 verification passed.")
        except Exception as exc:
            status["state"] = "failed"
            status["error"] = str(exc)
            log("ERROR: " + str(exc))
            log(traceback.format_exc())
        finally:
            if job_id in self._read_id_registry("paused"):
                status["state"] = "paused"
                status["pause_requested"] = True
                status["pause_reason"] = status.get("pause_reason") or "Paused from the Studio task list."
            status["finished_at"] = now_iso()
            self._write_status(status)

    def _find_status(self, job_id: str) -> dict[str, Any] | None:
        for platform in PLATFORMS:
            for status_path in platform_folder(platform).glob("*/status.json"):
                try:
                    status = json.loads(status_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if status.get("id") == job_id:
                    return status
        return None

    def _write_status(self, status: dict[str, Any]) -> None:
        path = Path(status["run_dir"]) / "status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
            _replace_with_bounded_retry(tmp_path, path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def _append_log(self, status: dict[str, Any], message: str) -> None:
        try:
            path = Path(str(status.get("log_path") or ""))
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(message + "\n")
        except Exception:
            return

    def _registry_path(self, name: str) -> Path:
        return WORK_ROOT / f"studio-{name}-jobs.json"

    def _read_id_registry(self, name: str) -> set[str]:
        path = self._registry_path(name)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return set()
        if isinstance(payload, list):
            return {str(item) for item in payload if str(item).strip()}
        if isinstance(payload, dict):
            values = payload.get("ids") if isinstance(payload.get("ids"), list) else []
            return {str(item) for item in values if str(item).strip()}
        return set()

    def _write_id_registry(self, name: str, ids: set[str]) -> None:
        path = self._registry_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        payload = {"ids": sorted(ids), "updated_at": now_iso()}
        try:
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            _replace_with_bounded_retry(tmp_path, path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def _archive_finished_job(self, status: dict[str, Any]) -> str:
        run_dir_value = str(status.get("run_dir") or "")
        if not run_dir_value:
            return ""
        run_dir = Path(run_dir_value)
        if not run_dir.exists():
            return ""
        archive_root = WORK_ROOT / "deleted-jobs"
        archive_root.mkdir(parents=True, exist_ok=True)
        archive_name = f"{timestamp_slug()}-{slugify(status.get('url') or status.get('action') or status.get('id'), status.get('platform') or 'job')}-{status.get('id')}"
        target = archive_root / archive_name
        if target.exists():
            target = archive_root / f"{archive_name}-{uuid.uuid4().hex[:6]}"
        shutil.move(str(run_dir), str(target))
        return str(target)

    def _normalize_job_ids(self, job_ids: list[str]) -> list[str]:
        seen: set[str] = set()
        normalized: list[str] = []
        for value in job_ids:
            job_id = str(value or "").strip()
            if not job_id or job_id in seen:
                continue
            seen.add(job_id)
            normalized.append(job_id)
        return normalized

    def _hydrate_status(self, status: dict[str, Any]) -> dict[str, Any]:
        copy = dict(status)
        copy.setdefault("action", "convert")
        copy.setdefault("action_label", action_label(str(copy.get("action") or "convert")))
        copy.setdefault("display_title", extract_title_from_url(str(copy.get("url") or "")))
        action = str(copy.get("action") or "convert")
        artifacts = []
        artifact_dir_value = copy.get("artifact_dir")
        artifacts_dir = Path(artifact_dir_value) if artifact_dir_value else None
        if artifacts_dir and artifacts_dir.exists():
            for path in sorted(item for item in artifacts_dir.rglob("*") if item.is_file()):
                artifacts.append(
                    {
                        "name": path.relative_to(artifacts_dir).as_posix(),
                        "path": str(path),
                        "bytes": path.stat().st_size,
                    }
                )
        copy["artifacts"] = artifacts
        output_path_value = copy.get("output_path")
        output_path = Path(output_path_value) if output_path_value else None
        copy["output_exists"] = bool(output_path and output_path.exists())
        if output_path and output_path.exists():
            copy["output_bytes"] = output_path.stat().st_size
        copy["expects_mp3_output"] = action_expects_mp3_output(action)
        copy["output_required"] = action_requires_mp3_output(action)
        copy["diagnostic_only"] = not copy["expects_mp3_output"]
        copy["output_status"] = output_status_for_action(action, str(copy.get("state") or ""), copy["output_exists"])
        if copy.get("platform") == "weixin":
            proof = _weixin_source_proof(artifacts_dir)
            if proof:
                copy["weixin_source_proof"] = proof
        copy["is_health_check"] = copy.get("action") == "health-check"
        copy["next_action"] = diagnose_next_action(copy)
        return copy


def state_payload(store: JobStore) -> dict[str, Any]:
    return {
        "library_root": str(LIBRARY_ROOT),
        "platforms": PLATFORMS,
        "jobs": store.list_jobs(),
    }
