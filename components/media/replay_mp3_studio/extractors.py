from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import AUTHORIZED_FETCHERS, LIBRARY_ROOT, PROJECT_ROOT, WORK_ROOT
from .fast_pipeline import discover_source_artifact_for_url, source_artifact_roots_from_env, wait_for_source_artifact_for_url
from .utils import (
    child_env,
    is_media_url,
    parse_course_id,
    parse_weixin_short_uri,
    python_executable,
    run_streaming,
    timestamp_slug,
    verify_mp3,
)
from .weixin_filehelper import (
    WeixinWindowCaptureUnavailable,
    open_weixin_filehelper,
    trigger_weixin_video_playback,
)
from .weixin_pipeline_state import (
    load_or_create_pipeline_state,
    mark_existing_pipeline_phase,
    mark_pipeline_phase_complete,
    mark_pipeline_phase_failure,
    pipeline_phase_completed,
    pipeline_resume_action,
)
from .platform_support import weixin_cache_audit_roots, weixin_recent_source_roots


XHS_CLIP_API = "https://www.xiaohongshu.com/api/sns/v1/live/dynamic/clip_detail_web"
REPLAY_TO_MP3 = Path.home() / ".codex" / "skills" / "replay-to-mp3" / "scripts" / "replay_to_mp3.py"
VIDEO_AUDIO_EXTRACTOR = PROJECT_ROOT / "video-audio-extractor"
DEFAULT_POST_OPEN_SOURCE_ARTIFACT_WAIT_SECONDS = 0.0
WEIXIN_CAUSAL_CAPTURE_CHECKPOINT_FILENAME = "weixin_causal_capture_checkpoint.json"
WEIXIN_CAUSAL_CAPTURE_CHECKPOINT_SCHEMA = 1


def default_cache_audit_dirs(platform: str, url: str = "") -> list[str]:
    if platform == "weixin":
        return [str(path) for path in weixin_cache_audit_roots()]
    if platform == "third_party" and "songy.info" in url.lower():
        return [str(WORK_ROOT / "studio-profiles" / "songy-mobile")]
    return []


def parse_xhs_ids(url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    clip_id = ""
    if query.get("share_source_id"):
        clip_id = query["share_source_id"][0]
    elif query.get("clip_id"):
        clip_id = query["clip_id"][0]
    elif query.get("id"):
        clip_id = query["id"][0]
    match = re.search(r"/live_replay/(\d+)", parsed.path)
    if not clip_id and match:
        clip_id = match.group(1)
    host_id = query.get("host_id", [""])[0]
    if not clip_id:
        raise RuntimeError("Cannot identify Xiaohongshu replay clip id.")
    if not host_id:
        raise RuntimeError("Cannot identify Xiaohongshu host_id.")
    return clip_id, host_id


def resolve_xhs_share_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if host != "xhslink.com" and not host.endswith(".xhslink.com"):
        return url
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; WeixinReplayToMP3/1.0)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            resolved = response.geturl()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Xiaohongshu share-link resolution failed: {exc}") from exc
    return resolved or url


def urls_from_string(text: str) -> list[str]:
    decoded = (
        urllib.parse.unquote(text)
        .replace("\\/", "/")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\u003A", ":")
        .replace("\\u003a", ":")
        .replace("\\u0026", "&")
    )
    pattern = re.compile(
        r"https?://[^\s\"'<>\\]+?(?:\.m3u8|\.mp4|\.mp3|\.m4a|\.aac|\.wav|\.ogg|\.opus|\.webm|stodownload)[^\s\"'<>\\]*",
        re.I,
    )
    return [match.group(0) for match in pattern.finditer(decoded)]


def walk_media(value: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).replace("-", "_").lower()
            if isinstance(item, str) and any(part in normalized for part in ("url", "media", "audio", "video", "hls", "raw")):
                urls.extend(urls_from_string(item))
            urls.extend(walk_media(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(walk_media(item))
    elif isinstance(value, str):
        urls.extend(urls_from_string(value))
    unique = []
    seen = set()
    for url in urls:
        cleaned = url.strip().strip('",')
        if cleaned and cleaned not in seen and is_media_url(cleaned):
            unique.append(cleaned)
            seen.add(cleaned)
    return unique


def media_score(url: str) -> int:
    lower = url.lower().split("?", 1)[0]
    if lower.endswith((".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus")):
        return 0
    if lower.endswith(".m3u8"):
        return 1
    if lower.endswith((".mp4", ".mov", ".webm")) or "stodownload" in lower:
        return 2
    return 3


def convert_media(input_value: str, output: Path, log) -> None:
    script = AUTHORIZED_FETCHERS / "media_url_to_mp3.py"
    code = run_streaming([sys.executable, str(script), input_value, "--output", str(output)], log)
    if code != 0:
        raise RuntimeError(f"media conversion failed with exit code {code}")


def import_artifact(source: str, artifacts: Path) -> Path:
    src = Path(source).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"Artifact not found: {src}")
    if not src.is_file():
        raise RuntimeError("Artifact path must be a file for this version.")
    upload_dir = artifacts / "uploaded"
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_dir / src.name
    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        index = 2
        while dest.exists():
            dest = upload_dir / f"{stem}-{index}{suffix}"
            index += 1
    shutil.copy2(src, dest)
    return dest


def import_artifact_text(text: str, artifacts: Path, extension: str = ".json", name_hint: str = "pasted") -> Path:
    if not text.strip():
        raise RuntimeError("Artifact text is empty.")
    ext = extension if extension.startswith(".") else "." + extension
    if ext.lower() not in {".json", ".har", ".txt", ".log", ".html", ".htm", ".xml"}:
        ext = ".txt"
    safe_hint = re.sub(r"[^A-Za-z0-9._-]+", "-", name_hint).strip("-._") or "pasted"
    upload_dir = artifacts / "uploaded"
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / f"{safe_hint}-{time.strftime('%Y%m%d-%H%M%S')}{ext}"
    path.write_text(text, encoding="utf-8")
    return path


def run_imported_artifact(platform: str, url: str, imported: Path, output: Path, log) -> None:
    if platform == "weixin":
        script = AUTHORIZED_FETCHERS / "weixin_object_artifact_to_mp3.py"
        cmd = [sys.executable, str(script), str(imported), "--output", str(output)]
    elif platform == "third_party":
        if "songy.info" in url.lower() or "songy" in imported.name.lower() or "bandu" in imported.name.lower():
            script = REPLAY_TO_MP3
            cmd = [
                sys.executable,
                str(script),
                "songy-artifact",
                str(imported),
                "--course-id",
                parse_course_id(url, default="784"),
                "--output",
                str(output),
            ]
        else:
            script = AUTHORIZED_FETCHERS / "extract_media_from_artifact.py"
            cmd = [sys.executable, str(script), str(imported), "--output", str(output)]
    else:
        script = AUTHORIZED_FETCHERS / "extract_media_from_artifact.py"
        cmd = [sys.executable, str(script), str(imported), "--output", str(output)]
    code = run_streaming(cmd, log)
    if code != 0:
        raise RuntimeError(f"Artifact conversion failed with exit code {code}")


def run_weixin_vendor_source_artifact(
    source: Path,
    output: Path,
    artifacts: Path,
    log,
    *,
    min_duration: float = 0.0,
) -> None:
    script = AUTHORIZED_FETCHERS / "weixin_vendor_artifact_to_mp3.py"
    report = artifacts / "weixin_vendor_source_artifact.json"
    work_dir = artifacts / "weixin-vendor-source-work"
    cmd = [
        sys.executable,
        str(script),
        str(source),
        "--output",
        str(output),
        "--report",
        str(report),
        "--work-dir",
        str(work_dir),
    ]
    if min_duration and min_duration > 0:
        cmd.extend(["--min-duration", str(min_duration)])
    code = run_streaming(cmd, log)
    if code != 0:
        raise RuntimeError(f"Weixin vendor source artifact conversion failed with exit code {code}")


def run_artifact(platform: str, url: str, artifact_path: str, output: Path, artifacts: Path, log) -> None:
    source = Path(artifact_path).expanduser().resolve()
    if platform == "weixin":
        try:
            log("Trying safe Weixin vendor/source-listener artifact adapter first.")
            run_weixin_vendor_source_artifact(source, output, artifacts, log)
            return
        except Exception as exc:
            if source.is_dir() or source.suffix.lower() in {".mp4", ".flv", ".m4a", ".mp3", ".mov", ".webm"}:
                raise
            log(f"Weixin vendor/source-listener adapter did not match this artifact; falling back: {exc}")
    imported = import_artifact(artifact_path, artifacts)
    log(f"Imported artifact: {imported}")
    run_imported_artifact(platform, url, imported, output, log)


def run_artifact_text(
    platform: str,
    url: str,
    artifact_text: str,
    artifact_ext: str,
    output: Path,
    artifacts: Path,
    log,
) -> None:
    imported = import_artifact_text(artifact_text, artifacts, artifact_ext, f"{platform}-pasted")
    log(f"Saved pasted artifact: {imported}")
    if platform == "weixin":
        try:
            log("Trying safe Weixin vendor/source-listener pasted artifact adapter first.")
            run_weixin_vendor_source_artifact(imported, output, artifacts, log)
            return
        except Exception as exc:
            log(f"Weixin vendor/source-listener pasted artifact adapter did not match; falling back: {exc}")
    run_imported_artifact(platform, url, imported, output, log)


def find_reusable_songy_artifact(
    url: str,
    library_root: Path = LIBRARY_ROOT,
) -> Path | None:
    course_id = parse_course_id(url, default="")
    if not course_id:
        return None
    root = library_root / "third_party"
    matches: list[Path] = []
    if root.exists():
        for path in root.glob("*/artifacts/uploaded/songy_browser_capture*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            artifact_url = str(payload.get("url") or "")
            if parse_course_id(artifact_url, default="") != course_id:
                continue
            media_urls = payload.get("media_urls")
            if isinstance(media_urls, list) and media_urls:
                matches.append(path)
    if not matches:
        return None
    return max(matches, key=lambda item: (item.stat().st_mtime_ns, str(item)))


def find_reusable_songy_mp3(url: str, project_root: Path = PROJECT_ROOT) -> Path | None:
    course_id = parse_course_id(url, default="")
    if not course_id:
        return None
    output = project_root / "outputs" / f"songy_course_{course_id}.mp3"
    return output if output.exists() else None


def run_xiaohongshu(url: str, output: Path, artifacts: Path, log) -> None:
    resolved_url = resolve_xhs_share_url(url)
    clip_id, host_id = parse_xhs_ids(resolved_url)
    params = urllib.parse.urlencode({"clip_id": clip_id, "host_id": host_id})
    api_url = f"{XHS_CLIP_API}?{params}"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.xiaohongshu.com",
        "Referer": resolved_url,
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
        ),
    }
    log(f"Requesting Xiaohongshu replay metadata for clip {clip_id}.")
    req = urllib.request.Request(api_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Xiaohongshu metadata request failed: {exc}") from exc
    urls = sorted(walk_media(payload), key=media_score)
    report = {
        "api_url": api_url,
        "clip_id": clip_id,
        "host_id": host_id,
        "resolved_host": urllib.parse.urlparse(resolved_url).netloc,
        "media_urls": urls,
        "response": payload,
    }
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "xiaohongshu_metadata.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not urls:
        raise RuntimeError("No Xiaohongshu media URL found.")
    log(f"Found {len(urls)} Xiaohongshu media candidate(s).")
    convert_media(urls[0], output, log)


def run_songy_direct_link(url: str, output: Path, artifacts: Path, log) -> None:
    script = AUTHORIZED_FETCHERS / "direct_links_to_mp3.py"
    cmd = [
        python_executable(),
        str(script),
        "--only",
        "songy",
        "--songy-link",
        url,
        "--songy-output",
        str(output),
    ]
    code = run_streaming(cmd, log)
    if code != 0 or not output.is_file():
        raise RuntimeError(
            "Songy direct link did not expose authorized media. Use a user-authorized "
            "artifact or local media file."
        )


def run_songy(
    url: str,
    output: Path,
    artifacts: Path,
    log,
    wait_seconds: int = 180,
    fast_record: bool = False,
    reuse_latest_artifact: bool = True,
) -> None:
    course_id = parse_course_id(url, default="784")
    script = REPLAY_TO_MP3
    profile_dir = WORK_ROOT / "studio-profiles" / "songy-mobile"
    artifact = artifacts / "songy_browser_capture.json"
    cmd = [
        python_executable(),
        str(script),
        "songy-browser",
        "--url",
        url,
        "--course-id",
        course_id,
        "--output",
        str(output),
        "--artifact",
        str(artifact),
        "--profile-dir",
        str(profile_dir),
        "--wait-seconds",
        str(wait_seconds),
        "--mobile",
    ]
    if fast_record:
        cmd.extend(["--fast-record", "--rate", "12", "--max-wall-seconds", "600"])
    code = run_streaming(cmd, log)
    if code != 0:
        if reuse_latest_artifact:
            cached_mp3 = find_reusable_songy_mp3(url)
            if cached_mp3:
                log(f"Songy browser capture failed; reusing local verified MP3 cache: {cached_mp3}")
                shutil.copy2(cached_mp3, output)
                return
            reusable = find_reusable_songy_artifact(url)
            if reusable:
                log(f"Songy browser capture failed; retrying latest local authorized artifact: {reusable}")
                imported = import_artifact(str(reusable), artifacts)
                log(f"Imported reusable Songy artifact: {imported}")
                run_imported_artifact("third_party", url, imported, output, log)
                return
        raise RuntimeError(f"Songy browser capture failed with exit code {code}")


def other_script_kind(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        return ""
    if is_media_url(url):
        return "direct_media"
    if host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"):
        return "youtube"
    if host == "x.com" or host == "twitter.com" or host.endswith(".twitter.com"):
        return "x"
    return "yt_dlp"


def missing_other_script_message(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc or "unknown"
    return f"无法处理 {host}：只接受 http/https 媒体或网页链接。"


def run_other_site(url: str, output: Path, artifacts: Path, log, sample_seconds: int = 0) -> None:
    if not url:
        raise RuntimeError("其他平台需要提供链接。")
    kind = other_script_kind(url)
    if not kind:
        raise RuntimeError(missing_other_script_message(url))
    script = AUTHORIZED_FETCHERS / "other_link_to_mp3.py"
    if not script.exists():
        raise RuntimeError("缺少该脚本：outputs/authorized_fetchers/other_link_to_mp3.py")
    report = artifacts / "other_link_report.json"
    cmd = [
        python_executable(),
        str(script),
        url,
        "--output",
        str(output),
        "--report",
        str(report),
    ]
    if sample_seconds > 0:
        cmd.extend(["--sample-seconds", str(sample_seconds)])
    code = run_streaming(cmd, log)
    if code != 0:
        raise RuntimeError(f"其他链接脚本执行失败：{kind}，exit code {code}")


def open_songy_login(url: str) -> dict[str, str]:
    target = url or "https://webapp.songy.info/#/courses/details?course_id=784"
    profile_dir = WORK_ROOT / "studio-profiles" / "songy-mobile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "open",
        "-na",
        "Google Chrome",
        "--args",
        f"--user-data-dir={profile_dir}",
        "--window-size=430,900",
        target,
    ]
    try:
        subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True)
        return {"opened": target, "profile_dir": str(profile_dir), "browser": "Google Chrome"}
    except Exception:
        open_url(target)
        return {"opened": target, "profile_dir": str(profile_dir), "browser": "default"}


def generate_weixin_open_packet(url: str, output_dir: Path, log) -> dict[str, Any]:
    short_uri = parse_weixin_short_uri(url)
    script = PROJECT_ROOT / "outputs" / "capture_accelerator" / "weixin_test_device_open_packet.py"
    packet_dir = output_dir / "weixin_open_packet"
    cmd = [
        sys.executable,
        str(script),
        "--short-uri",
        short_uri,
        "--output",
        str(packet_dir),
    ]
    log("+ " + " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=child_env(),
            text=True,
            capture_output=True,
            timeout=45,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Weixin open packet generation timed out after 45s") from exc
    if proc.stdout:
        for line in proc.stdout.splitlines():
            log(line)
    if proc.stderr:
        for line in proc.stderr.splitlines():
            log(line)
    if proc.returncode != 0:
        raise RuntimeError(f"Weixin open packet generation failed with exit code {proc.returncode}")
    packet_path = packet_dir / "packet.json"
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    write_weixin_bridge_payload_packet(output_dir, short_uri, packet, log)
    return {"short_uri": short_uri, "packet_dir": str(packet_dir), "packet": packet}


def write_weixin_bridge_payload_packet(output_dir: Path, short_uri: str, packet: dict[str, Any], log) -> Path:
    scene = packet.get("scene_info") if isinstance(packet.get("scene_info"), dict) else {}
    export_id = str(scene.get("dynamicExportId") or "")
    if not export_id:
        return output_dir / "weixin_bridge_payload_packet.json"
    payload = {
        "short_uri": short_uri,
        "generated_at": int(time.time()),
        "scene_info": scene,
        "boundary": "Use only in an authorized WeChat WebView/playback context.",
        "h5_auth": {
            "bridge": "WeixinJSBridge.invoke",
            "method": "finderH5Auth",
            "params": {"h5Version": 3774873601, "scope": "finderLive"},
            "returns": "h5AuthToken for finderH5ExtTransfer",
        },
        "step_1_finder_get_comment_detail": {
            "bridge": "WeixinJSBridge.invoke",
            "method": "finderH5ExtTransfer",
            "name": "FinderGetCommentDetail",
            "params": {
                "req_json": json.dumps(
                    {
                        "finder_basereq": {"expt_flag": 1, "request_id": str(int(time.time() * 1000))},
                        "platform_scene": 2,
                        "encrypted_objectid": export_id,
                        "need_object": 1,
                        "scene": 141,
                        "direction": 2,
                        "identity_scene": 2,
                        "pull_scene": 1,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "url": "/cgi-bin/micromsg-bin/pc_findergetcommentdetail",
                "cgi_cmdid": 5259,
                "h5AuthToken": "",
                "is_security_check": False,
                "scope": "finderLive",
            },
        },
        "step_2_finder_get_live_info_template": {
            "bridge": "WeixinJSBridge.invoke",
            "method": "finderH5ExtTransfer",
            "name": "FinderGetLiveInfo",
            "params": {
                "req_json": json.dumps(
                    {"finder_basereq": {}, "live_id": "<fill from FinderGetCommentDetail response>"},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "url": "/cgi-bin/micromsg-bin/pc_findergetliveinfo",
                "cgi_cmdid": 10064,
                "h5AuthToken": "",
                "is_security_check": False,
                "scope": "finderLive",
            },
        },
        "target_media_fields": [
            "data.liveInfo.replayInfo.renderReplayUrl",
            "data.liveInfo.replayInfo.renderReplayHlsUrl",
            "liveInfo.replayInfo.renderReplayUrl",
            "liveInfo.replayInfo.renderReplayHlsUrl",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "weixin_bridge_payload_packet.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Wrote Weixin bridge payload packet: {path}")
    return path


def open_weixin_target(
    url: str,
    output_dir: Path | None = None,
    log=lambda message: None,
    method: str = "",
) -> dict[str, Any]:
    if not url:
        subprocess.run(["open", "-a", "WeChat"], cwd=str(PROJECT_ROOT), check=False)
        return {"opened": "WeChat", "method": "app"}
    selected_method = (method or os.environ.get("WEIXIN_OPEN_METHOD") or "auto").strip().lower()
    if selected_method in {"auto", "filehelper", "file-transfer", "file_transfer_assistant"}:
        try:
            log("Opening Weixin link through File Transfer Assistant.")
            info = open_weixin_filehelper(url, click_after_send=True)
            info.setdefault("short_uri", parse_weixin_short_uri(url))
            return info
        except WeixinWindowCaptureUnavailable as exc:
            if selected_method == "auto":
                raise RuntimeError(
                    "Weixin protected-window target verification did not complete; "
                    "no scheme or browser fallback was used because it would bypass the "
                    "File Transfer Assistant target gate."
                ) from exc
            raise
        except Exception as exc:
            if selected_method == "auto":
                raise RuntimeError(
                    "Weixin File Transfer Assistant open failed; no scheme/default-browser fallback was used. "
                    "Set WEIXIN_OPEN_METHOD=scheme only for an explicit non-File-Transfer-Assistant open. "
                    f"Original error: {exc}"
                ) from exc
            raise
    if selected_method not in {"scheme", "weixin_scheme", "packet", "open_packet"}:
        raise RuntimeError(f"Unsupported Weixin open method: {selected_method}")
    if output_dir is None:
        output_dir = LIBRARY_ROOT / "weixin" / "_open_packets" / f"{timestamp_slug()}-{parse_weixin_short_uri(url)}"
    packet_info = generate_weixin_open_packet(url, output_dir, log)
    scheme = packet_info["packet"].get("weixin_scheme") or url
    subprocess.run(
        ["open", "-b", "com.tencent.xinWeChat", scheme],
        cwd=str(PROJECT_ROOT),
        check=False,
    )
    return {
        "opened": scheme,
        "method": "weixin_scheme",
        "short_uri": packet_info["short_uri"],
        "packet_dir": packet_info["packet_dir"],
    }


def _write_artifact_json(artifacts: Path, name: str, payload: dict[str, Any]) -> Path:
    artifacts.mkdir(parents=True, exist_ok=True)
    path = artifacts / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def redacted_weixin_packet_identity(packet: dict[str, Any]) -> dict[str, Any]:
    scene = packet.get("scene_info") if isinstance(packet, dict) else {}
    scene = scene if isinstance(scene, dict) else {}
    dynamic_export_id = str(scene.get("dynamicExportId") or "")
    identity: dict[str, Any] = {
        "short_uri": str(packet.get("short_uri") or ""),
        "scene_keys": sorted(scene.keys()),
        "expired_time": scene.get("expiredTime"),
        "request_scene": scene.get("requestScene"),
        "entry_scene": scene.get("entryScene"),
        "comment_scene": scene.get("commentScene"),
        "entry_card_type": scene.get("entryCardType"),
    }
    if dynamic_export_id:
        identity.update(
            {
                "dynamic_export_id_sha256_12": hashlib.sha256(dynamic_export_id.encode("utf-8")).hexdigest()[:12],
                "dynamic_export_id_length": len(dynamic_export_id),
            }
        )
    return identity


def summarize_weixin_current_delta_report(report_path: Path) -> dict[str, Any]:
    if not report_path.exists():
        return {}
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return {"diagnosis": "delta_report_unreadable"}

    baseline_lsof = report.get("baseline_lsof") if isinstance(report.get("baseline_lsof"), list) else []
    lsof_events = report.get("lsof_events") if isinstance(report.get("lsof_events"), list) else []
    unreadable_lsof = report.get("unreadable_lsof") if isinstance(report.get("unreadable_lsof"), list) else []
    baseline_unreadable = [row for row in baseline_lsof if not row.get("exists_as_path")]
    event_unreadable = [row for row in unreadable_lsof if not row.get("exists_as_path")]
    readable_visible_events = [
        row
        for row in report.get("visible_events", [])
        if isinstance(row, dict) and row.get("media_candidate")
    ]
    all_unreadable = sorted(
        baseline_unreadable + event_unreadable,
        key=lambda row: int(row.get("size") or row.get("bytes") or 0),
        reverse=True,
    )
    result = report.get("result") if isinstance(report.get("result"), dict) else {}
    explicit_diagnosis = str(report.get("diagnosis") or "")
    if explicit_diagnosis:
        diagnosis = explicit_diagnosis
    elif result.get("error") == "no_playable_changed_media_file":
        diagnosis = "no_playable_changed_media_file"
    elif all_unreadable and not readable_visible_events:
        diagnosis = "playback_fd_unlinked"
    else:
        diagnosis = "unknown"
    samples = []
    for row in all_unreadable[:5]:
        samples.append(
            {
                "command": row.get("command"),
                "pid": row.get("pid"),
                "fd": row.get("fd"),
                "size": row.get("size"),
                "relative_path": row.get("relative_path"),
            }
        )
    return {
        "diagnosis": diagnosis,
        "result_error": result.get("error"),
        "baseline_unreadable_fd_count": len(baseline_unreadable),
        "unreadable_event_count": len(event_unreadable),
        "lsof_event_count": len(lsof_events),
        "visible_media_event_count": len(readable_visible_events),
        "recent_visible_change_count": len(report.get("recent_visible_changes", [])),
        "largest_unreadable_fd_bytes": int(all_unreadable[0].get("size") or 0) if all_unreadable else 0,
        "sample_unreadable_fds": samples,
    }


def _weixin_provider_flags() -> dict[str, bool]:
    return {
        "JUSTONE_API_KEY": bool(os.environ.get("JUSTONE_API_KEY") or os.environ.get("JUSTONE_TOKEN")),
        "DAJIALA_KEY": bool(os.environ.get("DAJIALA_KEY")),
        "JZL_KEY": bool(os.environ.get("JZL_KEY")),
        "APIFY_TOKEN": bool(os.environ.get("APIFY_TOKEN")),
        "WXSHARES_KEY": bool(os.environ.get("WXSHARES_KEY")),
        "WEIXIN_YUANBAO_COOKIE": bool(os.environ.get("WEIXIN_YUANBAO_COOKIE")),
        "WEIXIN_SPH_RESOLVER_URL": bool(os.environ.get("WEIXIN_SPH_RESOLVER_URL")),
    }


def run_weixin_direct_link_probe(url: str, output: Path, artifacts: Path, log) -> dict[str, Any]:
    script = AUTHORIZED_FETCHERS / "direct_links_to_mp3.py"
    report_src = WORK_ROOT / "direct-link-probes" / "weixin_direct_link_probe.json"
    report_dest = artifacts / "weixin_direct_link_probe.json"
    if report_src.exists():
        report_src.unlink()
    log("Trying Weixin direct link/provider probe first.")
    code = run_streaming(
        [
            sys.executable,
            str(script),
            "--only",
            "weixin",
            "--weixin-link",
            url,
            "--weixin-output",
            str(output),
        ],
        log,
    )
    copied_report = ""
    if report_src.exists():
        artifacts.mkdir(parents=True, exist_ok=True)
        shutil.copy2(report_src, report_dest)
        copied_report = str(report_dest)
    success = code == 0 and output.exists() and output.stat().st_size > 0
    return {
        "name": "direct_link_provider_probe",
        "attempted": True,
        "success": success,
        "exit_code": code,
        "provider_keys": _weixin_provider_flags(),
        "report": copied_report,
    }


def run_weixin_source_vault_artifact(url: str, output: Path, artifacts: Path, log) -> dict[str, Any]:
    stage: dict[str, Any] = {
        "name": "source_vault_artifact",
        "attempted": True,
        "success": False,
    }
    try:
        roots = source_artifact_roots_from_env()
        source, discovery = discover_source_artifact_for_url(url, roots)
        stage["discovery"] = discovery
    except Exception as exc:
        stage["error"] = str(exc)
        return stage

    if source is None:
        stage["skipped_reason"] = "no_matching_authorized_source_artifact"
        return stage

    stage["source_artifact"] = str(source)
    log(f"Trying authorized Weixin Source Vault artifact before opening WeChat: {source}")
    try:
        run_weixin_vendor_source_artifact(source, output, artifacts, log)
    except Exception as exc:
        stage["error"] = str(exc)
        report = artifacts / "weixin_vendor_source_artifact.json"
        if report.exists():
            stage["vendor_source_report"] = str(report)
        return stage

    stage["success"] = True
    report = artifacts / "weixin_vendor_source_artifact.json"
    if report.exists():
        stage["vendor_source_report"] = str(report)
    return stage


def post_open_source_artifact_wait_seconds(value: str | None = None) -> float:
    raw = value if value is not None else os.environ.get("WEIXIN_SOURCE_ARTIFACT_WAIT_SECONDS", "")
    if raw.strip() == "":
        return DEFAULT_POST_OPEN_SOURCE_ARTIFACT_WAIT_SECONDS
    try:
        wait_seconds = float(raw)
    except ValueError:
        return DEFAULT_POST_OPEN_SOURCE_ARTIFACT_WAIT_SECONDS
    return max(0.0, min(wait_seconds, 120.0))


def run_weixin_post_open_source_vault_artifact(
    url: str,
    output: Path,
    artifacts: Path,
    log,
    *,
    wait_seconds: float,
) -> dict[str, Any]:
    stage: dict[str, Any] = {
        "name": "post_open_source_vault_artifact",
        "attempted": True,
        "success": False,
        "wait_seconds": round(max(0.0, wait_seconds), 3),
        "lookup_mode": "bounded_wait" if wait_seconds > 0 else "immediate",
    }
    try:
        roots = source_artifact_roots_from_env()
        if wait_seconds > 0:
            source, discovery = wait_for_source_artifact_for_url(url, roots, wait_seconds=wait_seconds)
        else:
            source, discovery = discover_source_artifact_for_url(url, roots)
        stage["discovery"] = discovery
    except Exception as exc:
        stage["error"] = str(exc)
        return stage

    if source is None:
        stage["skipped_reason"] = "no_matching_authorized_source_artifact_after_open"
        return stage

    stage["source_artifact"] = str(source)
    log(f"Trying post-open Weixin Source Vault artifact before cache scans: {source}")
    try:
        run_weixin_vendor_source_artifact(source, output, artifacts, log)
    except Exception as exc:
        stage["error"] = str(exc)
        report = artifacts / "weixin_vendor_source_artifact.json"
        if report.exists():
            stage["vendor_source_report"] = str(report)
        return stage

    stage["success"] = True
    report = artifacts / "weixin_vendor_source_artifact.json"
    if report.exists():
        stage["vendor_source_report"] = str(report)
    return stage


WEIXIN_RECENT_SOURCE_ROOTS = weixin_recent_source_roots()
WEIXIN_SENSITIVE_STORE_NAMES = {
    "account web data",
    "cookies",
    "history",
    "login data",
    "visited links",
    "web data",
}
WEIXIN_SENSITIVE_PART_HINTS = (
    "chat",
    "chats",
    "contact",
    "contacts",
    "conversation",
    "conversations",
    "message",
    "messages",
    "userinfo",
    "wcdb",
)
DEFAULT_WEIXIN_CAUSAL_WAIT_SECONDS = 12.0
WEIXIN_CAUSAL_MAX_FILE_BYTES = 80_000_000
WEIXIN_CAUSAL_MAX_SNAPSHOT_BYTES = 160_000_000
WEIXIN_CAUSAL_MAX_SNAPSHOT_FILES = 80


def _expand_weixin_report_path(value: object) -> Path:
    raw = str(value or "").strip()
    if raw.startswith("~/"):
        return Path(raw).expanduser()
    return Path(raw).expanduser()


def _safe_relative_path(path: Path) -> str:
    try:
        relative = path.expanduser().resolve().relative_to(Path.home().resolve())
    except (OSError, ValueError):
        return str(path)
    return "~/" + relative.as_posix()


def _is_weixin_sensitive_store_path(path: Path) -> bool:
    parts = [part.lower() for part in path.parts]
    if path.name.lower() in WEIXIN_SENSITIVE_STORE_NAMES:
        return True
    for part in parts:
        if part in WEIXIN_SENSITIVE_STORE_NAMES:
            return True
        if part in WEIXIN_SENSITIVE_PART_HINTS:
            return True
        if any(part.startswith(f"{hint}_") or part.startswith(f"{hint}-") for hint in WEIXIN_SENSITIVE_PART_HINTS):
            return True
    return False


def _append_recent_weixin_source_file(
    files: dict[str, Path],
    path: Path,
    *,
    max_file_bytes: int,
    since_timestamp: float | None = None,
) -> None:
    try:
        resolved = path.expanduser().resolve()
        stat = resolved.stat()
    except OSError:
        return
    if not resolved.is_file():
        return
    if _is_weixin_sensitive_store_path(resolved):
        return
    if stat.st_size <= 0 or stat.st_size > max_file_bytes:
        return
    if since_timestamp is not None and stat.st_mtime < since_timestamp:
        return
    files[str(resolved)] = resolved


def build_weixin_recent_source_file_list(
    marker_report: Path,
    *,
    since_minutes: float = 15,
    runtime_roots: tuple[Path, ...] | list[Path] | None = None,
    now: float | None = None,
    max_file_bytes: int = 80_000_000,
) -> list[Path]:
    """Build the focused source list used after the user confirms Weixin playback."""
    files: dict[str, Path] = {}
    try:
        payload = json.loads(marker_report.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        payload = {}

    for item in payload.get("files_with_hits") or []:
        if not isinstance(item, dict):
            continue
        candidate = item.get("path") or item.get("relative_path")
        if not candidate:
            continue
        _append_recent_weixin_source_file(
            files,
            _expand_weixin_report_path(candidate),
            max_file_bytes=max_file_bytes,
            since_timestamp=None,
        )

    cutoff = (now if now is not None else time.time()) - max(0.0, float(since_minutes)) * 60
    roots = tuple(runtime_roots) if runtime_roots is not None else WEIXIN_RECENT_SOURCE_ROOTS
    for root in roots:
        root = Path(root).expanduser()
        if root.is_file():
            _append_recent_weixin_source_file(files, root, max_file_bytes=max_file_bytes, since_timestamp=cutoff)
            continue
        if not root.exists():
            continue
        try:
            iterator = root.rglob("*")
            for path in iterator:
                _append_recent_weixin_source_file(files, path, max_file_bytes=max_file_bytes, since_timestamp=cutoff)
        except OSError:
            continue

    return sorted(files.values(), key=lambda item: str(item))


def weixin_causal_wait_seconds(duration: float, value: str = "") -> float:
    raw = value if value.strip() else os.environ.get("WEIXIN_CAUSAL_CAPTURE_SECONDS", "")
    if raw.strip():
        try:
            return max(1.0, min(float(raw), 120.0))
        except ValueError:
            pass
    return max(3.0, min(float(duration), DEFAULT_WEIXIN_CAUSAL_WAIT_SECONDS))


def snapshot_weixin_recent_source_state(
    *,
    runtime_roots: tuple[Path, ...] | list[Path] | None = None,
    max_file_bytes: int = WEIXIN_CAUSAL_MAX_FILE_BYTES,
) -> dict[str, tuple[int, int]]:
    state: dict[str, tuple[int, int]] = {}
    roots = tuple(runtime_roots) if runtime_roots is not None else WEIXIN_RECENT_SOURCE_ROOTS
    for root_value in roots:
        root = Path(root_value).expanduser()
        if not root.exists():
            continue
        iterator = (root,) if root.is_file() else root.rglob("*")
        try:
            for path in iterator:
                try:
                    resolved = path.expanduser().resolve()
                    if not resolved.is_file() or _is_weixin_sensitive_store_path(resolved):
                        continue
                    stat = resolved.stat()
                except OSError:
                    continue
                if 0 < stat.st_size <= max_file_bytes:
                    state[str(resolved)] = (int(stat.st_size), int(stat.st_mtime_ns))
        except OSError:
            continue
    return state


def changed_weixin_recent_source_files(
    baseline: dict[str, tuple[int, int]],
    *,
    runtime_roots: tuple[Path, ...] | list[Path] | None = None,
    max_file_bytes: int = WEIXIN_CAUSAL_MAX_FILE_BYTES,
) -> list[Path]:
    current = snapshot_weixin_recent_source_state(
        runtime_roots=runtime_roots,
        max_file_bytes=max_file_bytes,
    )
    changed = [
        Path(path)
        for path, metadata in current.items()
        if baseline.get(path) != metadata
    ]
    return sorted(
        changed,
        key=lambda path: current.get(str(path), (0, 0))[1],
        reverse=True,
    )


def filter_weixin_marker_report_to_baseline(
    marker_report: Path,
    baseline: dict[str, tuple[int, int]],
    *,
    started_at: float,
) -> dict[str, Any]:
    try:
        payload = json.loads(marker_report.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        payload = {}
    fresh_items: list[dict[str, Any]] = []
    candidate_urls: list[str] = []
    redacted_urls: list[str] = []
    seen_urls: set[str] = set()
    share_data_changed = False
    for item in payload.get("files_with_hits") or []:
        if not isinstance(item, dict):
            continue
        candidate = item.get("path") or item.get("relative_path")
        if not candidate:
            continue
        path = _expand_weixin_report_path(candidate)
        try:
            resolved = path.resolve()
            stat = resolved.stat()
            changed = baseline.get(str(resolved)) != (int(stat.st_size), int(stat.st_mtime_ns))
        except OSError:
            try:
                changed = float(item.get("mtime") or 0) >= started_at
            except (TypeError, ValueError):
                changed = False
        if not changed:
            continue
        fresh_items.append(item)
        share_data_changed = share_data_changed or path.name == "Share Data"
        item_urls = item.get("urls") or []
        item_redacted = item.get("redacted_urls") or []
        for index, url in enumerate(item_urls):
            url_text = str(url or "")
            if not url_text or url_text in seen_urls:
                continue
            seen_urls.add(url_text)
            candidate_urls.append(url_text)
            if index < len(item_redacted):
                redacted_urls.append(str(item_redacted[index]))
    payload["files_with_hits"] = fresh_items
    payload["candidate_urls"] = candidate_urls
    payload["redacted_candidate_urls"] = redacted_urls
    payload["candidate_url_count"] = len(candidate_urls)
    payload["causal_delta"] = {
        "baseline_file_count": len(baseline),
        "fresh_file_with_hits_count": len(fresh_items),
        "fresh_candidate_url_count": len(candidate_urls),
        "share_data_changed": share_data_changed,
    }
    marker_report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return dict(payload["causal_delta"])


def snapshot_weixin_probe_sources(
    source_files: list[Path],
    *,
    label: str,
    max_file_bytes: int = 40_000_000,
) -> list[Path]:
    snapshot_dir = WORK_ROOT / "sensitive-artifacts" / "weixin-causal-playback" / f"{timestamp_slug()}-{label}"
    snapshots: list[Path] = []
    total_bytes = 0
    seen: set[str] = set()
    for source in source_files:
        try:
            resolved = source.expanduser().resolve()
            stat = resolved.stat()
        except OSError:
            continue
        if not resolved.is_file() or str(resolved) in seen:
            continue
        seen.add(str(resolved))
        copy_bytes = min(int(stat.st_size), max_file_bytes)
        if (
            copy_bytes <= 0
            or len(snapshots) >= WEIXIN_CAUSAL_MAX_SNAPSHOT_FILES
            or total_bytes + copy_bytes > WEIXIN_CAUSAL_MAX_SNAPSHOT_BYTES
        ):
            continue
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
        destination = snapshot_dir / f"{len(snapshots):03d}-{digest}{resolved.suffix or '.bin'}"
        remaining = copy_bytes
        with resolved.open("rb") as source_handle, destination.open("wb") as destination_handle:
            while remaining > 0:
                chunk = source_handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                destination_handle.write(chunk)
                remaining -= len(chunk)
        if destination.stat().st_size <= 0:
            continue
        snapshots.append(destination)
        total_bytes += destination.stat().st_size
    return snapshots


def run_weixin_recent_marker_scan(
    artifacts: Path,
    log,
    *,
    since_minutes: float = 8,
    max_read_bytes: int = 40_000_000,
) -> Path:
    script = AUTHORIZED_FETCHERS / "weixin_recent_media_marker_scan.py"
    report = artifacts / "weixin_recent_media_marker_scan.json"
    cmd = [
        sys.executable,
        str(script),
        "--since-minutes",
        str(since_minutes),
        "--min-size",
        "100",
        "--max-size",
        "80000000",
        "--max-read-bytes",
        str(max_read_bytes),
        "--output",
        str(report),
    ]
    code = run_streaming(cmd, log)
    if code != 0 and not report.exists():
        raise RuntimeError(f"Weixin recent marker scan failed with exit code {code}")
    return report


def run_weixin_encrypted_candidate_probe_for_sources(
    source_files: list[Path],
    artifacts: Path,
    log,
    *,
    timeout: int = 8,
    max_urls: int = 40,
) -> dict[str, Any]:
    if not source_files:
        return {"result": "no_recent_source_files", "source_file_count": 0}
    script = AUTHORIZED_FETCHERS / "weixin_encrypted_candidate_probe.py"
    report = artifacts / "weixin_live_recent_encrypted_probe_report.json"
    work_dir = artifacts / "weixin-live-recent-encrypted-probe-work"
    sensitive_dir = WORK_ROOT / "sensitive-artifacts" / "weixin-manual-playback-decrypt-pairs" / timestamp_slug()
    cmd = [
        sys.executable,
        str(script),
        *[str(path) for path in source_files],
        "--output",
        str(report),
        "--work-dir",
        str(work_dir),
        "--sensitive-artifact-dir",
        str(sensitive_dir),
        "--max-read-bytes",
        "40000000",
        "--timeout",
        str(timeout),
        "--max-urls",
        str(max_urls),
        "--max-heuristic-keys",
        "80",
        "--max-heuristic-numeric-keys",
        "160",
    ]
    code = run_streaming(cmd, log)
    if report.exists():
        try:
            payload = json.loads(report.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            raise RuntimeError(f"Weixin encrypted candidate probe report is unreadable: {exc}") from exc
        if isinstance(payload, dict):
            payload["exit_code"] = code
            return payload
    raise RuntimeError(f"Weixin encrypted candidate probe failed with exit code {code}")


def capture_weixin_causal_playback_delta(
    artifacts: Path,
    log,
    *,
    baseline: dict[str, tuple[int, int]],
    started_at: float,
    wait_seconds: float,
    playback_assertions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze only this playback's local increment; never probe or download."""

    stage: dict[str, Any] = {
        "name": "causal_playback_runtime_delta_capture",
        "phase": "capture_increment_only",
        "attempted": True,
        "success": False,
        "baseline_file_count": len(baseline),
        "wait_seconds": round(wait_seconds, 3),
        "network_probe_started": False,
        "download_started": False,
    }
    started_monotonic = time.monotonic()
    deadline = started_monotonic + max(1.0, wait_seconds)
    minimum_observation_seconds = min(2.0, max(1.0, wait_seconds))
    changed_files: list[Path] = []
    previous_signature: tuple[tuple[str, int, int], ...] = ()
    stable_samples = 0
    while True:
        changed_files = changed_weixin_recent_source_files(baseline)
        signature_rows: list[tuple[str, int, int]] = []
        for path in changed_files:
            try:
                stat = path.stat()
            except OSError:
                continue
            signature_rows.append((str(path), int(stat.st_size), int(stat.st_mtime_ns)))
        signature = tuple(signature_rows)
        if signature and signature == previous_signature:
            stable_samples += 1
        else:
            stable_samples = 0
            previous_signature = signature
        now = time.monotonic()
        if (
            signature
            and stable_samples >= 1
            and now - started_monotonic >= minimum_observation_seconds
        ):
            break
        if now >= deadline:
            break
        time.sleep(min(0.25, max(0.0, deadline - now)))

    snapshots = snapshot_weixin_probe_sources(changed_files, label="causal-frozen")
    assertions = dict(playback_assertions or {})
    assertion_verified = bool(assertions.get("playback_verified"))
    playback_evidence = assertion_verified
    share_data_changed = any(path.name == "Share Data" for path in changed_files)
    stage.update(
        {
            "success": playback_evidence and bool(snapshots),
            "elapsed_seconds": round(time.monotonic() - started_monotonic, 3),
            "fresh_changed_file_count": len(changed_files),
            "source_file_count": len(changed_files),
            "snapshot_file_count": len(snapshots),
            "snapshot_paths": [str(path) for path in snapshots],
            "share_data_changed": share_data_changed,
            "playback_evidence": playback_evidence,
            "playback_assertions": assertions,
        }
    )
    _write_artifact_json(
        artifacts,
        "weixin_causal_playback_sources.json",
        {
            "source": "causal_playback_runtime_delta",
            "phase": "capture_increment_only",
            "baseline_file_count": len(baseline),
            "fresh_changed_file_count": len(changed_files),
            "source_file_count": len(changed_files),
            "snapshot_file_count": len(snapshots),
            "playback_assertion_verified": assertion_verified,
            "network_probe_started": False,
            "download_started": False,
            "safe_relative_paths": [_safe_relative_path(path) for path in changed_files[:100]],
        },
    )
    if not playback_evidence:
        stage["error"] = "no_fresh_playback_evidence_after_exact_open"
    elif not snapshots:
        stage["error"] = "fresh_playback_evidence_had_no_readable_frozen_increment"
    return stage


def convert_weixin_frozen_playback_delta(
    output: Path,
    artifacts: Path,
    log,
    *,
    capture_stage: dict[str, Any],
    min_duration: float,
) -> dict[str, Any]:
    """Probe and download strictly from a previously frozen capture phase."""

    stage: dict[str, Any] = {
        "name": "frozen_playback_delta_source_conversion",
        "phase": "probe_then_download",
        "attempted": False,
        "success": False,
        "capture_snapshot_file_count": int(capture_stage.get("snapshot_file_count") or 0),
    }
    snapshots = [
        Path(path)
        for path in capture_stage.get("snapshot_paths") or []
        if Path(path).is_file()
    ]
    if not capture_stage.get("success") or not snapshots:
        stage["error"] = "capture_phase_not_complete"
        return stage

    stage["attempted"] = True
    stage["network_probe_started"] = True
    stage["download_started"] = False
    log("Frozen Weixin playback delta found; probing only the preserved increment.")
    probe = run_weixin_encrypted_candidate_probe_for_sources(snapshots, artifacts, log)
    stage["encrypted_candidate_probe_report"] = str(artifacts / "weixin_live_recent_encrypted_probe_report.json")
    stage["encrypted_candidate_probe_result"] = str(probe.get("result") or "")
    stage["candidate_url_count"] = int(probe.get("candidate_url_count") or 0)
    stage["verified_candidate_count"] = int(probe.get("successful_numeric_pair_count") or 0)
    numeric_artifact = str(probe.get("numeric_key_pair_artifact") or "")
    if not numeric_artifact:
        stage["error"] = (
            "frozen_playback_delta_did_not_find_a_verified_numeric_key_pair"
            f": {probe.get('result') or 'unknown'}"
        )
        return stage

    stage["numeric_key_pair_artifact"] = numeric_artifact
    stage["download_started"] = True
    log("Frozen delta produced an MP4-header-verified source; converting the largest candidate.")
    run_weixin_vendor_source_artifact(
        Path(numeric_artifact),
        output,
        artifacts,
        log,
        min_duration=min_duration,
    )
    stage["success"] = True
    return stage


def run_weixin_causal_playback_capture(
    output: Path,
    artifacts: Path,
    log,
    *,
    baseline: dict[str, tuple[int, int]],
    started_at: float,
    wait_seconds: float,
    min_duration: float,
    playback_assertions: dict[str, Any] | None = None,
    target_short_uri: str = "",
) -> dict[str, Any]:
    """Two-phase orchestration with a target-bound frozen-delta checkpoint."""

    checkpoint_path = artifacts / WEIXIN_CAUSAL_CAPTURE_CHECKPOINT_FILENAME
    capture_stage: dict[str, Any] | None = None
    checkpoint_reused = False
    if target_short_uri and checkpoint_path.is_file():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except Exception:
            checkpoint = {}
        checkpoint_capture = checkpoint.get("capture_stage") if isinstance(checkpoint, dict) else None
        checkpoint_snapshots = (
            [Path(path) for path in checkpoint_capture.get("snapshot_paths") or []]
            if isinstance(checkpoint_capture, dict)
            else []
        )
        if (
            isinstance(checkpoint, dict)
            and int(checkpoint.get("schema_version") or 0) == WEIXIN_CAUSAL_CAPTURE_CHECKPOINT_SCHEMA
            and checkpoint.get("target_short_uri") == target_short_uri
            and isinstance(checkpoint_capture, dict)
            and checkpoint_capture.get("success")
            and checkpoint_snapshots
            and all(path.is_file() for path in checkpoint_snapshots)
        ):
            capture_stage = dict(checkpoint_capture)
            capture_stage["resumed_from_checkpoint"] = True
            checkpoint_reused = True
            log("Reusing the target-bound frozen Weixin increment; live capture is not repeated.")

    if capture_stage is None:
        capture_stage = capture_weixin_causal_playback_delta(
            artifacts,
            log,
            baseline=baseline,
            started_at=started_at,
            wait_seconds=wait_seconds,
            playback_assertions=playback_assertions,
        )
        if target_short_uri and capture_stage.get("success"):
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            checkpoint_payload = {
                "schema_version": WEIXIN_CAUSAL_CAPTURE_CHECKPOINT_SCHEMA,
                "target_short_uri": target_short_uri,
                "capture_stage": capture_stage,
            }
            temporary = checkpoint_path.with_suffix(checkpoint_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(checkpoint_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(checkpoint_path)

    if capture_stage.get("success") and target_short_uri:
        mark_existing_pipeline_phase(
            artifacts,
            target_short_uri=target_short_uri,
            phase="causal_capture_complete",
            details={
                "snapshot_file_count": int(capture_stage.get("snapshot_file_count") or 0),
                "checkpoint": str(checkpoint_path),
            },
        )

    conversion_stage = convert_weixin_frozen_playback_delta(
        output,
        artifacts,
        log,
        capture_stage=capture_stage,
        min_duration=min_duration,
    )
    if conversion_stage.get("success") and target_short_uri:
        mark_existing_pipeline_phase(
            artifacts,
            target_short_uri=target_short_uri,
            phase="source_converted",
            details={"route": "frozen_playback_delta_source_conversion"},
        )
    return {
        "name": "causal_playback_runtime_delta",
        "attempted": True,
        "success": bool(conversion_stage.get("success")),
        "playback_evidence": bool(capture_stage.get("playback_evidence")),
        "share_data_changed": bool(capture_stage.get("share_data_changed")),
        "baseline_file_count": len(baseline),
        "fresh_changed_file_count": int(capture_stage.get("fresh_changed_file_count") or 0),
        "snapshot_file_count": int(capture_stage.get("snapshot_file_count") or 0),
        "capture_checkpoint": str(checkpoint_path) if target_short_uri else "",
        "checkpoint_reused": checkpoint_reused,
        "encrypted_candidate_probe_result": conversion_stage.get("encrypted_candidate_probe_result"),
        "verified_candidate_count": int(conversion_stage.get("verified_candidate_count") or 0),
        "capture_phase": capture_stage,
        "conversion_phase": conversion_stage,
        "error": conversion_stage.get("error") or capture_stage.get("error"),
    }


def run_weixin_manual_playback_capture(
    output: Path,
    artifacts: Path,
    log,
    *,
    since_minutes: float = 8,
    min_duration: int = 180,
) -> dict[str, Any]:
    stage: dict[str, Any] = {
        "name": "manual_playback_recent_encrypted_capture",
        "attempted": True,
        "success": False,
        "since_minutes": since_minutes,
    }
    log("Using user-confirmed Weixin playback: scanning recent safe runtime files for encrypted media evidence.")
    marker_report = run_weixin_recent_marker_scan(artifacts, log, since_minutes=since_minutes)
    stage["marker_report"] = str(marker_report)
    recent_files = build_weixin_recent_source_file_list(marker_report, since_minutes=max(since_minutes, 15))
    source_files = [marker_report, *recent_files]
    snapshots = snapshot_weixin_probe_sources(source_files, label="manual")
    stage["source_file_count"] = len(source_files)
    stage["snapshot_file_count"] = len(snapshots)
    try:
        marker_payload = json.loads(marker_report.read_text(encoding="utf-8", errors="replace"))
        marker_candidate_count = int(marker_payload.get("candidate_url_count") or 0)
    except Exception:
        marker_candidate_count = -1
    stage["marker_candidate_url_count"] = marker_candidate_count
    _write_artifact_json(
        artifacts,
        "weixin_manual_playback_sources.json",
        {
            "source": "manual_playback_recent_encrypted_capture",
            "source_file_count": len(source_files),
            "safe_relative_paths": [_safe_relative_path(path) for path in source_files[:100]],
        },
    )
    if marker_candidate_count == 0 or not snapshots:
        stage["error"] = "no_recent_safe_runtime_files_after_user_confirmed_playback"
        return stage

    probe = run_weixin_encrypted_candidate_probe_for_sources(snapshots, artifacts, log)
    stage["encrypted_candidate_probe_report"] = str(artifacts / "weixin_live_recent_encrypted_probe_report.json")
    stage["encrypted_candidate_probe_result"] = str(probe.get("result") or "")
    numeric_artifact = str(probe.get("numeric_key_pair_artifact") or "")
    if numeric_artifact:
        stage["numeric_key_pair_artifact"] = numeric_artifact
        log("Encrypted Weixin probe found a verified numeric key artifact; converting through vendor adapter.")
        run_weixin_vendor_source_artifact(Path(numeric_artifact), output, artifacts, log, min_duration=min_duration)
        stage["success"] = True
        return stage

    stage["error"] = (
        "recent_playback_scan_did_not_find_a_verified_numeric_key_pair"
        f": {probe.get('result') or 'unknown'}"
    )
    return stage


def run_weixin_link(
    url: str,
    output: Path,
    artifacts: Path,
    log,
    duration: int = 300,
    watch_current_only: bool = False,
    manual_playback: bool = False,
    min_duration: float = 180,
    desktop_automation_available: bool = True,
) -> None:
    mode = "manual_playback" if manual_playback else "watch_current" if watch_current_only else "open_then_watch"
    state_path, pipeline_state = load_or_create_pipeline_state(
        artifacts,
        url=url,
        mode=mode,
    )
    resume_action = pipeline_resume_action(pipeline_state)
    diagnostics: dict[str, Any] = {
        "target_url": url,
        "mode": mode,
        "pipeline_state": str(state_path),
        "resume_action": resume_action,
        "stages": [],
    }

    if output.is_file():
        try:
            verification = verify_mp3(output, log, min_duration_seconds=min_duration)
        except Exception as exc:
            mark_pipeline_phase_failure(
                state_path,
                pipeline_state,
                "existing_output_checked",
                error_code="existing_output_failed_full_decode",
            )
            diagnostics["summary"] = f"Existing output failed verification and was preserved: {exc}"
            _write_artifact_json(artifacts, "weixin_link_diagnostics.json", diagnostics)
            raise RuntimeError("Existing Weixin MP3 failed full-decode verification; refusing to overwrite it.") from exc
        mark_pipeline_phase_complete(
            state_path,
            pipeline_state,
            "existing_output_checked",
            details={"exists": True, "bytes": int(verification.get("bytes") or 0)},
        )
        mark_pipeline_phase_complete(
            state_path,
            pipeline_state,
            "source_converted",
            details={"route": "existing_output_reuse"},
        )
        mark_pipeline_phase_complete(
            state_path,
            pipeline_state,
            "output_verified",
            details={
                "duration_seconds": verification.get("duration_seconds"),
                "bytes": int(verification.get("bytes") or 0),
            },
        )
        diagnostics["resume_action"] = "reuse_verified_output"
        diagnostics["stages"].append(
            {
                "name": "verified_existing_output",
                "attempted": True,
                "success": True,
                "duration_seconds": verification.get("duration_seconds"),
                "bytes": int(verification.get("bytes") or 0),
            }
        )
        _write_artifact_json(artifacts, "weixin_link_diagnostics.json", diagnostics)
        log("Existing target-bound Weixin MP3 passed full-decode verification; all capture work was skipped.")
        return

    mark_pipeline_phase_complete(
        state_path,
        pipeline_state,
        "existing_output_checked",
        details={"exists": False},
    )

    if url:
        if pipeline_phase_completed(pipeline_state, "source_vault_checked"):
            source_vault_stage = {
                "name": "source_vault_artifact",
                "attempted": False,
                "success": False,
                "skipped_reason": "completed_in_pipeline_state",
            }
        else:
            source_vault_started = time.perf_counter()
            source_vault_stage = run_weixin_source_vault_artifact(url, output, artifacts, log)
            source_vault_stage["elapsed_seconds"] = round(time.perf_counter() - source_vault_started, 3)
            mark_pipeline_phase_complete(
                state_path,
                pipeline_state,
                "source_vault_checked",
                details={"success": bool(source_vault_stage.get("success"))},
            )
        diagnostics["stages"].append(source_vault_stage)
        if source_vault_stage["success"]:
            mark_pipeline_phase_complete(
                state_path,
                pipeline_state,
                "source_converted",
                details={"route": "source_vault_artifact"},
            )
            _write_artifact_json(artifacts, "weixin_link_diagnostics.json", diagnostics)
            log("Weixin Source Vault artifact created an MP3 without opening WeChat.")
            return

        if manual_playback:
            diagnostics["stages"].append(
                {
                    "name": "short_uri_identity_probe",
                    "attempted": False,
                    "success": False,
                    "skipped_reason": "manual_playback_uses_user_opened_wechat_context",
                }
            )
        else:
            identity_stage: dict[str, Any] = {
                "name": "short_uri_identity_local",
                "attempted": True,
                "success": False,
                "network_request": False,
            }
            try:
                identity = {"short_uri": parse_weixin_short_uri(url)}
                diagnostics["target_identity"] = identity
                identity_stage.update(
                    {
                        "success": True,
                        "short_uri": identity.get("short_uri"),
                    }
                )
                log(f"Parsed Weixin short-link identity locally: {identity.get('short_uri')}.")
            except Exception as exc:
                identity_stage["error"] = str(exc)
                log(f"Weixin local short-link identity parse failed: {exc}")
            diagnostics["stages"].append(identity_stage)

    if pipeline_phase_completed(pipeline_state, "direct_probe_checked"):
        direct = {
            "name": "direct_link_provider_probe",
            "attempted": False,
            "success": False,
            "skipped_reason": "completed_in_pipeline_state",
        }
    else:
        direct_started = time.perf_counter()
        direct = run_weixin_direct_link_probe(url, output, artifacts, log)
        direct["elapsed_seconds"] = round(time.perf_counter() - direct_started, 3)
        mark_pipeline_phase_complete(
            state_path,
            pipeline_state,
            "direct_probe_checked",
            details={"success": bool(direct.get("success"))},
        )
    diagnostics["stages"].append(direct)
    if direct["success"]:
        mark_pipeline_phase_complete(
            state_path,
            pipeline_state,
            "source_converted",
            details={"route": "direct_link_provider_probe"},
        )
        _write_artifact_json(artifacts, "weixin_link_diagnostics.json", diagnostics)
        log("Weixin direct probe created an MP3.")
        return

    if not desktop_automation_available and not manual_playback:
        diagnostics["stages"].append(
            {
                "name": "desktop_filehelper_automation",
                "attempted": False,
                "success": False,
                "skipped_reason": "platform_requires_user_confirmed_manual_playback",
            }
        )
        diagnostics["summary"] = (
            "This platform has no verified File Transfer Assistant automation adapter. "
            "The user must open 文件传输助手, send and open the exact newest link, start real "
            "playback, explicitly confirm that playback is active, and then rerun with "
            "--manual-playback. No unbound runtime scan was started."
        )
        diag_path = _write_artifact_json(artifacts, "weixin_link_diagnostics.json", diagnostics)
        raise RuntimeError(
            "Manual playback is required on this platform; rerun with --manual-playback only "
            f"after the user confirms the exact link is playing. Diagnostics: {diag_path}"
        )

    if manual_playback:
        manual_stage = run_weixin_manual_playback_capture(
            output,
            artifacts,
            log,
            since_minutes=8,
            min_duration=min_duration,
        )
        diagnostics["stages"].append(manual_stage)
        if manual_stage.get("success"):
            mark_pipeline_phase_complete(
                state_path,
                pipeline_state,
                "causal_capture_complete",
                details={"route": "manual_playback_recent_encrypted_capture"},
            )
            mark_pipeline_phase_complete(
                state_path,
                pipeline_state,
                "source_converted",
                details={"route": "manual_playback_recent_encrypted_capture"},
            )
            _write_artifact_json(artifacts, "weixin_link_diagnostics.json", diagnostics)
            log("User-confirmed Weixin playback capture created an MP3.")
            return
        diagnostics["summary"] = (
            "Manual Weixin playback was confirmed, but recent safe runtime files did not yield "
            "a verified encrypted media + numeric key pair."
        )
        diag_path = _write_artifact_json(artifacts, "weixin_link_diagnostics.json", diagnostics)
        raise RuntimeError(f"Weixin link-to-MP3 failed; see diagnostics: {diag_path}")

    resume_frozen_conversion = resume_action == "resume_frozen_conversion"
    if watch_current_only:
        diagnostics["stages"].append(
            {
                "name": "open_current_wechat_playback",
                "attempted": False,
                "success": False,
                "skipped_reason": "watch_current_only",
            }
        )
        log("Skipping WeChat reopen because watch-current mode is enabled.")
    elif resume_frozen_conversion:
        causal_started_at = time.time()
        causal_baseline = {}
        playback_info = {
            "playback_verified": True,
            "activation_method": "frozen_increment_checkpoint",
            "resumed_without_reopen": True,
        }
        diagnostics["stages"].append(
            {
                "name": "open_current_wechat_playback",
                "attempted": False,
                "success": True,
                "skipped_reason": "target_bound_frozen_increment_available",
            }
        )
        diagnostics["stages"].append(
            {
                "name": "video_window_playback_activation",
                "attempted": False,
                "success": True,
                "skipped_reason": "target_bound_frozen_increment_available",
            }
        )
        log("A target-bound frozen increment is available; skipping WeChat reopen and playback activation.")
    else:
        baseline_started = time.perf_counter()
        preopen_started_at = time.time()
        preopen_baseline = snapshot_weixin_recent_source_state()
        causal_started_at = preopen_started_at
        causal_baseline = preopen_baseline
        diagnostics["stages"].append(
            {
                "name": "preopen_runtime_baseline",
                "attempted": True,
                "success": True,
                "file_count": len(preopen_baseline),
                "elapsed_seconds": round(time.perf_counter() - baseline_started, 3),
            }
        )
        open_started = time.perf_counter()
        open_stage: dict[str, Any] = {"name": "open_current_wechat_playback", "attempted": True, "success": False}
        try:
            info = open_weixin_target(url, artifacts, log)
            open_stage.update(
                {
                    "success": True,
                    "method": info.get("method"),
                    "short_uri": info.get("short_uri"),
                    "packet_dir": info.get("packet_dir"),
                }
            )
            log(
                "Weixin target opened via "
                f"{open_stage.get('method')} for short URI {open_stage.get('short_uri')}."
            )
        except Exception as exc:
            open_stage["elapsed_seconds"] = round(time.perf_counter() - open_started, 3)
            open_stage["error"] = str(exc)
            open_stage["fallback_skipped"] = "default_browser_disabled_for_weixin"
            diagnostics["stages"].append(open_stage)
            diagnostics["summary"] = (
                "The exact target was not opened, so unbound playback/cache scans were skipped. "
                "Use the explicit scheme/open-packet route or user-confirmed manual playback."
            )
            diag_path = _write_artifact_json(artifacts, "weixin_link_diagnostics.json", diagnostics)
            mark_pipeline_phase_failure(
                state_path,
                pipeline_state,
                "target_opened",
                error_code="exact_filehelper_open_failed",
            )
            log(f"Weixin File Transfer Assistant open failed; default browser fallback was skipped: {exc}")
            raise RuntimeError(f"Weixin target open failed; see diagnostics: {diag_path}") from exc
        else:
            open_stage["elapsed_seconds"] = round(time.perf_counter() - open_started, 3)
            diagnostics["stages"].append(open_stage)
            mark_pipeline_phase_complete(
                state_path,
                pipeline_state,
                "target_opened",
                details={
                    "method": str(info.get("method") or ""),
                    "sent_new_message": bool(info.get("sent_new_message")),
                    "reused_verified_message": bool(info.get("reused_verified_message")),
                },
            )

        preplay_started_at = 0.0
        preplay_baseline: dict[str, tuple[int, int]] | None = None
        preplay_baseline_elapsed = 0.0

        def snapshot_before_manual_activation() -> None:
            nonlocal preplay_started_at, preplay_baseline, preplay_baseline_elapsed
            preplay_started_at = time.time()
            preplay_baseline_started = time.perf_counter()
            preplay_baseline = snapshot_weixin_recent_source_state()
            preplay_baseline_elapsed = time.perf_counter() - preplay_baseline_started

        playback_stage: dict[str, Any] = {
            "name": "video_window_playback_activation",
            "attempted": True,
            "success": False,
        }
        playback_started = time.perf_counter()
        playback_info = trigger_weixin_video_playback(
            timeout=6,
            before_activation=snapshot_before_manual_activation,
        )
        if playback_info.get("activation_method") != "autoplay" and preplay_baseline is None:
            # Test doubles and older compatible callers may not invoke the hook.
            snapshot_before_manual_activation()
        playback_stage.update(playback_info)
        playback_stage["preclick_baseline_taken"] = preplay_baseline is not None
        playback_stage["baseline_elapsed_seconds"] = round(preplay_baseline_elapsed, 3)
        playback_stage["elapsed_seconds"] = round(time.perf_counter() - playback_started, 3)
        playback_stage["success"] = bool(playback_info.get("playback_verified"))
        diagnostics["stages"].append(playback_stage)
        if not playback_stage["success"]:
            mark_pipeline_phase_failure(
                state_path,
                pipeline_state,
                "playback_verified",
                error_code="player_assertions_missing",
            )
            diagnostics["summary"] = (
                "The exact link was opened, but the video window did not expose both Playing audio "
                "and Video Wake Lock assertions after bounded automatic activation."
            )
            diag_path = _write_artifact_json(artifacts, "weixin_link_diagnostics.json", diagnostics)
            raise RuntimeError(f"Weixin playback did not start; see diagnostics: {diag_path}")
        mark_pipeline_phase_complete(
            state_path,
            pipeline_state,
            "playback_verified",
            details={"activation_method": str(playback_info.get("activation_method") or "")},
        )
        if playback_info.get("activation_method") == "autoplay":
            # Autoplay may have begun before the post-open baseline completed,
            # so retain the pre-open baseline to avoid excluding its first
            # source/cache writes.
            causal_started_at = preopen_started_at
            causal_baseline = preopen_baseline
        else:
            causal_started_at = preplay_started_at
            causal_baseline = preplay_baseline or {}
        diagnostics["stages"].append(
            {
                "name": "selected_causal_baseline",
                "attempted": True,
                "success": True,
                "selection": (
                    "preopen_for_autoplay"
                    if playback_info.get("activation_method") == "autoplay"
                    else "postopen_preclick"
                ),
                "file_count": len(causal_baseline),
            }
        )

    if url and not resume_frozen_conversion:
        post_open_started = time.perf_counter()
        post_open_stage = run_weixin_post_open_source_vault_artifact(
            url,
            output,
            artifacts,
            log,
            wait_seconds=post_open_source_artifact_wait_seconds(),
        )
        post_open_stage["elapsed_seconds"] = round(time.perf_counter() - post_open_started, 3)
        diagnostics["stages"].append(post_open_stage)
        if post_open_stage["success"]:
            mark_pipeline_phase_complete(
                state_path,
                pipeline_state,
                "source_converted",
                details={"route": "post_open_source_vault_artifact"},
            )
            _write_artifact_json(artifacts, "weixin_link_diagnostics.json", diagnostics)
            log("Post-open Weixin Source Vault artifact created an MP3 without cache scans.")
            return
    elif url:
        diagnostics["stages"].append(
            {
                "name": "post_open_source_vault_artifact",
                "attempted": False,
                "success": False,
                "skipped_reason": "resume_frozen_conversion",
            }
        )

    causal_stage: dict[str, Any] | None = None
    if not watch_current_only:
        causal_stage = run_weixin_causal_playback_capture(
            output,
            artifacts,
            log,
            baseline=causal_baseline,
            started_at=causal_started_at,
            wait_seconds=weixin_causal_wait_seconds(duration),
            min_duration=min_duration,
            playback_assertions=playback_info,
            target_short_uri=parse_weixin_short_uri(url),
        )
        diagnostics["stages"].append(causal_stage)
        capture_phase = causal_stage.get("capture_phase") if isinstance(causal_stage, dict) else {}
        if isinstance(capture_phase, dict) and capture_phase.get("success"):
            mark_pipeline_phase_complete(
                state_path,
                pipeline_state,
                "causal_capture_complete",
                details={
                    "snapshot_file_count": int(capture_phase.get("snapshot_file_count") or 0),
                    "checkpoint_reused": bool(causal_stage.get("checkpoint_reused")),
                },
            )
        elif isinstance(capture_phase, dict):
            mark_pipeline_phase_failure(
                state_path,
                pipeline_state,
                "causal_capture_complete",
                error_code=str(capture_phase.get("error") or "fresh_increment_not_frozen"),
            )
        if causal_stage.get("success"):
            mark_pipeline_phase_complete(
                state_path,
                pipeline_state,
                "source_converted",
                details={"route": "causal_playback_runtime_delta"},
            )
            _write_artifact_json(artifacts, "weixin_link_diagnostics.json", diagnostics)
            log("Causal Weixin playback delta created a verified MP3.")
            return
        if not causal_stage.get("playback_evidence"):
            diagnostics["summary"] = (
                "The exact link entry opened, but no new playback-side media marker appeared after the pre-open "
                "baseline. The player was not proven to have started, so stale Radium/Share Data/cache scans were "
                "skipped. Use user-confirmed manual playback and retry within 8 minutes."
            )
            diag_path = _write_artifact_json(artifacts, "weixin_link_diagnostics.json", diagnostics)
            raise RuntimeError(f"Weixin playback did not start; see diagnostics: {diag_path}")

    legacy_duration = max(3, min(int(duration), 15))
    causal_since_minutes = (
        max(1.0, (time.time() - causal_started_at) / 60.0 + 0.5)
        if not watch_current_only
        else 8.0
    )

    source_stage: dict[str, Any] = {"name": "radium_source_url_scan", "attempted": True, "success": False}
    try:
        log("Scanning recent Radium payloads for a playable Weixin source MP4 URL.")
        run_weixin_radium_source(
            output,
            artifacts,
            log,
            duration=legacy_duration,
            min_duration=int(min_duration),
            since_minutes=causal_since_minutes,
        )
        source_stage["success"] = True
        source_stage["report"] = str(artifacts / "weixin_radium_source_report.json")
        diagnostics["stages"].append(source_stage)
        mark_pipeline_phase_complete(
            state_path,
            pipeline_state,
            "source_converted",
            details={"route": "radium_source_url_scan"},
        )
        _write_artifact_json(artifacts, "weixin_link_diagnostics.json", diagnostics)
        return
    except Exception as exc:
        source_stage["error"] = str(exc)
        source_stage["report"] = str(artifacts / "weixin_radium_source_report.json")
        diagnostics["stages"].append(source_stage)
        log(f"Weixin Radium source URL scan did not find a playable source: {exc}")

    profile_stage: dict[str, Any] = {"name": "profile_state_source_url_scan", "attempted": True, "success": False}
    try:
        log("Scanning targeted Weixin profile state for reconstructed playable source URLs.")
        run_weixin_profile_state_source(
            output,
            artifacts,
            log,
            duration=legacy_duration,
            min_duration=int(min_duration),
            since_minutes=causal_since_minutes,
        )
        profile_stage["success"] = True
        profile_stage["report"] = str(artifacts / "weixin_profile_state_report.json")
        diagnostics["stages"].append(profile_stage)
        mark_pipeline_phase_complete(
            state_path,
            pipeline_state,
            "source_converted",
            details={"route": "profile_state_source_url_scan"},
        )
        _write_artifact_json(artifacts, "weixin_link_diagnostics.json", diagnostics)
        return
    except Exception as exc:
        profile_stage["error"] = str(exc)
        profile_stage["report"] = str(artifacts / "weixin_profile_state_report.json")
        diagnostics["stages"].append(profile_stage)
        log(f"Weixin profile state scan did not find a playable source: {exc}")

    cdncomm_stage: dict[str, Any] = {"name": "cdncomm_source_url_scan", "attempted": True, "success": False}
    try:
        log("Scanning Weixin cdncomm metadata for playable media URLs and cache paths.")
        run_weixin_cdncomm_source(
            output,
            artifacts,
            log,
            duration=legacy_duration,
            min_duration=int(min_duration),
            since_minutes=causal_since_minutes,
        )
        cdncomm_stage["success"] = True
        cdncomm_stage["report"] = str(artifacts / "weixin_cdncomm_source_report.json")
        diagnostics["stages"].append(cdncomm_stage)
        mark_pipeline_phase_complete(
            state_path,
            pipeline_state,
            "source_converted",
            details={"route": "cdncomm_source_url_scan"},
        )
        _write_artifact_json(artifacts, "weixin_link_diagnostics.json", diagnostics)
        return
    except Exception as exc:
        cdncomm_stage["error"] = str(exc)
        cdncomm_stage["report"] = str(artifacts / "weixin_cdncomm_source_report.json")
        diagnostics["stages"].append(cdncomm_stage)
        log(f"Weixin cdncomm metadata scan did not find a playable source: {exc}")

    share_data_changed = bool(causal_stage and causal_stage.get("share_data_changed"))
    sharedata_stage: dict[str, Any] = {
        "name": "sharedata_feed_state_probe",
        "attempted": share_data_changed or watch_current_only,
        "success": False,
    }
    if sharedata_stage["attempted"]:
        try:
            log("Trying the fresh Share Data feed state with bounded token candidates.")
            run_weixin_sharedata_feed(
                output,
                artifacts,
                log,
                timeout=5,
                max_token_candidates=6,
                token_since_minutes=max(5, int(causal_since_minutes + 1)),
            )
            sharedata_stage["success"] = True
            sharedata_stage["report"] = str(artifacts / "weixin_sharedata_feed_report.json")
            diagnostics["stages"].append(sharedata_stage)
            mark_pipeline_phase_complete(
                state_path,
                pipeline_state,
                "source_converted",
                details={"route": "sharedata_feed_state_probe"},
            )
            _write_artifact_json(artifacts, "weixin_link_diagnostics.json", diagnostics)
            return
        except Exception as exc:
            sharedata_stage["error"] = str(exc)
            sharedata_stage["report"] = str(artifacts / "weixin_sharedata_feed_report.json")
            diagnostics["stages"].append(sharedata_stage)
            log(f"Weixin Share Data feed state probe did not find a playable source: {exc}")
    else:
        sharedata_stage["skipped_reason"] = "no_fresh_share_data_change"
        diagnostics["stages"].append(sharedata_stage)

    delta_stage: dict[str, Any] = {"name": "current_playback_delta_watch", "attempted": True, "success": False}
    try:
        log("Watching current playback file changes without reading chats or contacts.")
        run_weixin_current_delta(
            output,
            artifacts,
            log,
            duration=legacy_duration,
            min_duration=int(min_duration),
        )
        delta_stage["success"] = True
        delta_stage["diagnostics"] = summarize_weixin_current_delta_report(
            artifacts / "weixin_current_playback_delta.json"
        )
        diagnostics["stages"].append(delta_stage)
        mark_pipeline_phase_complete(
            state_path,
            pipeline_state,
            "source_converted",
            details={"route": "current_playback_delta_watch"},
        )
        _write_artifact_json(artifacts, "weixin_link_diagnostics.json", diagnostics)
        return
    except Exception as exc:
        delta_stage["error"] = str(exc)
        delta_stage["diagnostics"] = summarize_weixin_current_delta_report(
            artifacts / "weixin_current_playback_delta.json"
        )
        diagnostics["stages"].append(delta_stage)
        log(f"Weixin current playback delta watcher did not find a playable file: {exc}")

    cache_stage: dict[str, Any] = {"name": "current_account_playback_cache", "attempted": True, "success": False}
    try:
        log("Use the current logged-in WeChat account and keep the replay playing.")
        log("Falling back to the current run's short Radium playback-cache window.")
        run_weixin_cache(
            output,
            log,
            duration=legacy_duration,
            min_duration=int(min_duration),
            lookback_seconds=180,
            radium_only=True,
        )
        cache_stage["success"] = True
        diagnostics["stages"].append(cache_stage)
        mark_pipeline_phase_complete(
            state_path,
            pipeline_state,
            "source_converted",
            details={"route": "current_account_playback_cache"},
        )
        _write_artifact_json(artifacts, "weixin_link_diagnostics.json", diagnostics)
    except Exception as exc:
        cache_stage["error"] = str(exc)
        diagnostics["stages"].append(cache_stage)
        diagnostics["summary"] = (
            "Direct Weixin link-to-MP3 did not complete. The public/provider probe found no playable media, "
            "the current playback delta watcher found no readable playable media file, "
            "and no playable long media cache appeared for current playback."
        )
        diag_path = _write_artifact_json(artifacts, "weixin_link_diagnostics.json", diagnostics)
        raise RuntimeError(f"Weixin link-to-MP3 failed; see diagnostics: {diag_path}") from exc


def run_weixin_current_delta(
    output: Path,
    artifacts: Path,
    log,
    duration: int = 300,
    min_duration: int = 180,
) -> None:
    script = AUTHORIZED_FETCHERS / "weixin_current_playback_delta_to_mp3.py"
    report = artifacts / "weixin_current_playback_delta.json"
    cmd = [
        sys.executable,
        str(script),
        "--output",
        str(output),
        "--duration",
        str(duration),
        "--min-size",
        "50000",
        "--min-duration",
        str(min_duration),
        "--artifact-dir",
        str(artifacts / "current-playback-delta"),
        "--report",
        str(report),
    ]
    code = run_streaming(cmd, log)
    if code != 0:
        summary = summarize_weixin_current_delta_report(report)
        if summary:
            log("Weixin current playback delta diagnostics: " + json.dumps(summary, ensure_ascii=False))
        raise RuntimeError(
            "No playable Weixin media file changed during the current playback watch."
        )


def run_weixin_sharedata_feed(
    output: Path,
    artifacts: Path,
    log,
    *,
    timeout: int = 18,
    max_token_candidates: int = 24,
    token_since_minutes: int = 720,
) -> None:
    script = AUTHORIZED_FETCHERS / "weixin_sharedata_feed_to_mp3.py"
    report = artifacts / "weixin_sharedata_feed_report.json"
    cmd = [
        sys.executable,
        str(script),
        "--output",
        str(output),
        "--artifact-dir",
        str(artifacts / "weixin-sharedata-feed"),
        "--report",
        str(report),
        "--scan-token-storage",
        "--timeout",
        str(timeout),
        "--max-token-candidates",
        str(max_token_candidates),
        "--token-since-minutes",
        str(token_since_minutes),
    ]
    code = run_streaming(cmd, log)
    if code != 0:
        raise RuntimeError("No playable Weixin media URL was found from Share Data feed state.")


def run_weixin_radium_source(
    output: Path,
    artifacts: Path,
    log,
    duration: int = 300,
    min_duration: int = 180,
    since_minutes: float = 15,
) -> None:
    script = AUTHORIZED_FETCHERS / "weixin_radium_source_to_mp3.py"
    report = artifacts / "weixin_radium_source_report.json"
    cmd = [
        sys.executable,
        str(script),
        "--output",
        str(output),
        "--duration",
        str(duration),
        "--min-duration",
        str(min_duration),
        "--since-minutes",
        str(since_minutes),
        "--report",
        str(report),
    ]
    code = run_streaming(cmd, log)
    if code != 0:
        raise RuntimeError("No playable Weixin source MP4 URL was found in recent Radium payloads.")


def run_weixin_profile_state_source(
    output: Path,
    artifacts: Path,
    log,
    duration: int = 300,
    min_duration: int = 180,
    since_minutes: float = 360,
) -> None:
    script = AUTHORIZED_FETCHERS / "weixin_profile_state_to_mp3.py"
    report = artifacts / "weixin_profile_state_report.json"
    cmd = [
        sys.executable,
        str(script),
        "--output",
        str(output),
        "--duration",
        str(duration),
        "--min-duration",
        str(min_duration),
        "--since-minutes",
        str(since_minutes),
        "--report",
        str(report),
    ]
    code = run_streaming(cmd, log)
    if code != 0:
        raise RuntimeError("No playable Weixin media URL was found in targeted profile state.")


def run_weixin_cdncomm_source(
    output: Path,
    artifacts: Path,
    log,
    duration: int = 300,
    min_duration: int = 180,
    since_minutes: float = 30,
) -> None:
    script = AUTHORIZED_FETCHERS / "weixin_cdncomm_source_to_mp3.py"
    report = artifacts / "weixin_cdncomm_source_report.json"
    cmd = [
        sys.executable,
        str(script),
        "--output",
        str(output),
        "--duration",
        str(duration),
        "--min-duration",
        str(min_duration),
        "--since-minutes",
        str(since_minutes),
        "--report",
        str(report),
    ]
    code = run_streaming(cmd, log)
    if code != 0:
        raise RuntimeError("No playable long Weixin media was found in cdncomm metadata.")


def run_weixin_cache(
    output: Path,
    log,
    duration: int = 300,
    min_duration: int = 180,
    lookback_seconds: int = 120,
    radium_only: bool = False,
) -> None:
    script = AUTHORIZED_FETCHERS / "weixin_cache_watch_to_mp3.py"
    cmd = [
        sys.executable,
        str(script),
        "--output",
        str(output),
        "--duration",
        str(duration),
        "--min-size",
        "50000",
        "--min-duration",
        str(min_duration),
        "--lookback-seconds",
        str(lookback_seconds),
    ]
    if radium_only:
        cmd.append("--radium-only")
    code = run_streaming(cmd, log)
    if code != 0:
        raise RuntimeError(
            "No playable Weixin media/cache file was found for the current account playback."
        )


def run_cache_audit(
    platform: str,
    url: str,
    artifacts: Path,
    log,
    duration: int = 120,
    dirs: list[str] | None = None,
) -> Path:
    if not VIDEO_AUDIO_EXTRACTOR.exists():
        raise RuntimeError(f"video-audio-extractor not found: {VIDEO_AUDIO_EXTRACTOR}")
    target_dirs = [item for item in (dirs or []) if item.strip()] or default_cache_audit_dirs(platform, url)
    if not target_dirs:
        raise RuntimeError("No cache audit directories were provided for this platform.")
    out_prefix = artifacts / "cache_audit"
    cmd = [
        python_executable(),
        "-m",
        "src.main",
        "audit-cache",
        "--dirs",
        *target_dirs,
        "--duration",
        str(duration),
        "--out",
        str(out_prefix),
    ]
    log("Running local cache auditor.")
    log("Audit directories: " + json.dumps(target_dirs, ensure_ascii=False))
    code = run_streaming(cmd, log, cwd=VIDEO_AUDIO_EXTRACTOR)
    if code != 0:
        raise RuntimeError(f"Cache audit failed with exit code {code}")
    return out_prefix.with_suffix(".md")


def run_network_probe(
    url: str,
    output: Path,
    artifacts: Path,
    log,
    duration: int = 120,
    convert: bool = True,
) -> bool:
    if not VIDEO_AUDIO_EXTRACTOR.exists():
        raise RuntimeError(f"video-audio-extractor not found: {VIDEO_AUDIO_EXTRACTOR}")
    if not url:
        raise RuntimeError("A URL is required for network probe.")
    out_prefix = artifacts / "network_probe"
    cmd = [
        python_executable(),
        "-m",
        "src.main",
        "probe-url",
        "--url",
        url,
        "--duration",
        str(duration),
        "--out",
        str(out_prefix),
    ]
    if convert:
        cmd.extend(["--convert-out", str(output)])
    log("Running passive browser network probe.")
    code = run_streaming(cmd, log, cwd=VIDEO_AUDIO_EXTRACTOR)
    if code != 0:
        raise RuntimeError(f"Network probe failed with exit code {code}")
    return output.exists() and output.stat().st_size > 0


def run_blackbox_record(
    url: str,
    output: Path,
    artifacts: Path,
    log,
    duration: int,
    speed: float = 3.0,
    audio_device: str = "",
    wait_audio_timeout: float = 0,
) -> None:
    if not VIDEO_AUDIO_EXTRACTOR.exists():
        raise RuntimeError(f"video-audio-extractor not found: {VIDEO_AUDIO_EXTRACTOR}")
    if not url:
        raise RuntimeError("A URL is required for blackbox recording.")
    if not audio_device:
        raise RuntimeError("Blackbox recording requires an explicit audio device, for example ':1'.")
    no_open = False
    if "weixin.qq.com/sph" in url.lower() or "channels.weixin.qq.com" in url.lower():
        log("Opening Weixin link through the current WeChat account before blackbox recording.")
        open_info = open_weixin_target(url, artifacts, log, method="filehelper")
        log("Weixin blackbox open result: " + json.dumps(open_info, ensure_ascii=False))
        log(
            "Waiting for the WeChat WebView playback page. "
            "If it did not open automatically, open the sent File Transfer Assistant link manually."
        )
        time.sleep(6)
        no_open = True
        if wait_audio_timeout <= 0:
            wait_audio_timeout = 60
    cmd = [
        python_executable(),
        "-m",
        "src.main",
        "blackbox-record",
        "--url",
        url,
        "--speed",
        str(speed),
        "--audio-device",
        audio_device,
        "--duration",
        str(duration),
        "--out",
        str(output),
    ]
    if no_open:
        cmd.append("--no-open")
    if wait_audio_timeout > 0:
        cmd.extend(["--wait-audio-timeout", str(wait_audio_timeout)])
    log(
        "Running explicit blackbox recording fallback. "
        "Use only content you are authorized to save and transcribe."
    )
    code = run_streaming(cmd, log, cwd=VIDEO_AUDIO_EXTRACTOR)
    if code != 0:
        raise RuntimeError(f"Blackbox recording failed with exit code {code}")
    blackbox_log = output.with_suffix(".blackbox.json")
    if blackbox_log.exists():
        artifacts.mkdir(parents=True, exist_ok=True)
        shutil.copy2(blackbox_log, artifacts / blackbox_log.name)


def list_blackbox_audio_devices() -> dict[str, Any]:
    if not VIDEO_AUDIO_EXTRACTOR.exists():
        raise RuntimeError(f"video-audio-extractor not found: {VIDEO_AUDIO_EXTRACTOR}")
    proc = subprocess.run(
        [python_executable(), "-m", "src.main", "audio-devices"],
        cwd=str(VIDEO_AUDIO_EXTRACTOR),
        env=child_env(),
        text=True,
        capture_output=True,
        timeout=20,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-2000:] or proc.stdout[-2000:] or "audio device listing failed")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"audio device listing returned invalid JSON: {proc.stdout[-1000:]}") from exc


def open_url(url: str) -> None:
    subprocess.run(["open", url], cwd=str(PROJECT_ROOT), check=False)
