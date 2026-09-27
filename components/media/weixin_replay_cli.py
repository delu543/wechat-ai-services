#!/usr/bin/env python3
"""Codex-facing CLI for authorized replay and video links."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs as urllib_parse_qs
from urllib.parse import urlsplit, urlunsplit

from replay_mp3_studio.user_storage import (
    activate_profile,
    ensure_private_dir,
    ensure_profile_layout,
    private_umask,
    storage_namespace,
    user_data_root,
    user_output_root,
)
from replay_mp3_studio.platform_support import (
    SUPPORTED_SYSTEMS,
    desktop_automation_mode,
    wechat_installed_or_running,
)


ROOT = Path(__file__).resolve().parent
VIDEO_AUDIO_EXTRACTOR_ROOT = ROOT / "video-audio-extractor"
SHORT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{5,80}$")
MIN_PYTHON = (3, 10)


def _configure_console_stream(stream: Any) -> None:
    """Keep JSON/log output encodable in legacy Windows PowerShell code pages."""

    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        reconfigure(errors="backslashreplace")
    except (OSError, ValueError):
        pass


_configure_console_stream(sys.stdout)
_configure_console_stream(sys.stderr)


@dataclass(frozen=True)
class LinkTarget:
    url: str
    platform: str
    target_id: str
    output_prefix: str


def canonical_link(value: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "weixin.qq.com":
        raise ValueError("Only https://weixin.qq.com/sph/<id> links are accepted.")
    match = re.fullmatch(r"/sph/([^/]+)", parsed.path.rstrip("/"))
    if not match or not SHORT_ID_RE.fullmatch(match.group(1)):
        raise ValueError("Invalid Weixin Channels short link.")
    short_id = match.group(1)
    return urlunsplit(("https", "weixin.qq.com", f"/sph/{short_id}", "", "")), short_id


def _bounded_id(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "")).strip("-_")
    return cleaned[:80] or fallback


def _link_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _host_is(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def classify_link(value: str) -> LinkTarget:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only http/https replay, video, or media links are accepted.")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("The supplied link has an invalid port.") from exc
    if parsed.username or parsed.password:
        raise ValueError("Links containing embedded credentials are not accepted.")
    host = parsed.hostname.lower().rstrip(".")
    comparable_host = host.removeprefix("www.")
    if host == "weixin.qq.com":
        link, short_id = canonical_link(raw)
        return LinkTarget(link, "weixin", short_id, "weixin")

    query = urllib_parse_qs(parsed.query)
    if _host_is(host, "xhslink.com") or _host_is(host, "xiaohongshu.com"):
        match = re.search(r"/live_replay/(\d+)", parsed.path)
        clip_id = next(
            (
                values[0]
                for key in ("share_source_id", "clip_id", "id")
                if (values := query.get(key))
            ),
            match.group(1) if match else "",
        )
        return LinkTarget(
            raw,
            "xiaohongshu",
            _bounded_id(clip_id, _link_hash(raw)),
            "xiaohongshu",
        )

    if host == "youtu.be" or _host_is(host, "youtube.com"):
        video_id = (query.get("v") or [""])[0]
        if not video_id:
            parts = [part for part in parsed.path.split("/") if part]
            video_id = parts[-1] if parts else ""
        return LinkTarget(raw, "youtube", _bounded_id(video_id, _link_hash(raw)), "youtube")

    if host == "x.com" or _host_is(host, "twitter.com"):
        status_match = re.search(r"/status/(\d+)", parsed.path)
        status_id = status_match.group(1) if status_match else ""
        return LinkTarget(raw, "x", _bounded_id(status_id, _link_hash(raw)), "x")

    if _host_is(host, "songy.info"):
        fragment_query = urllib_parse_qs(urlsplit(parsed.fragment).query)
        course_id = (query.get("course_id") or fragment_query.get("course_id") or [""])[0]
        return LinkTarget(raw, "songy", _bounded_id(course_id, _link_hash(raw)), "songy")

    host_slug = _bounded_id(comparable_host, "web")[:32].lower()
    return LinkTarget(raw, "web", f"{host_slug}-{_link_hash(raw)}", "web")


def output_path(short_id: str, explicit: str = "", profile: str = "") -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    return (user_output_root(profile) / f"weixin_{short_id}.mp3").resolve()


def target_output_path(target: LinkTarget, explicit: str = "", profile: str = "") -> Path:
    if target.platform == "weixin":
        return output_path(target.target_id, explicit, profile)
    if explicit:
        return Path(explicit).expanduser().resolve()
    return (user_output_root(profile) / f"{target.output_prefix}_{target.target_id}.mp3").resolve()


def run_dir(short_id: str, mode: str, profile: str = "") -> Path:
    suffix = "" if mode == "auto" else f"-{mode}"
    return ensure_private_dir(
        user_data_root(profile) / "runs" / "weixin-link" / f"{short_id}{suffix}"
    )


def target_run_dir(target: LinkTarget, mode: str, profile: str = "") -> Path:
    if target.platform == "weixin":
        return run_dir(target.target_id, mode, profile)
    suffix = "" if mode == "auto" else f"-{mode}"
    return ensure_private_dir(
        user_data_root(profile)
        / "runs"
        / f"{target.platform}-link"
        / f"{target.target_id}{suffix}"
    )


def prepare_output_parent(output: Path, *, managed_default: bool) -> Path:
    if managed_default:
        return ensure_private_dir(output.parent)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return output.parent


def preflight_payload(profile: str = "") -> dict[str, Any]:
    resolved_profile = activate_profile(profile)
    selected_system = platform.system()
    wechat_installed, wechat_running = wechat_installed_or_running(system=selected_system)
    checks: dict[str, Any] = {
        "platform": selected_system,
        "platform_supported": selected_system in SUPPORTED_SYSTEMS,
        "macos": selected_system == "Darwin",
        "windows": selected_system == "Windows",
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "python_supported": sys.version_info >= MIN_PYTHON,
        "swift": bool(shutil.which("swift")),
        "node_optional": bool(shutil.which("node")),
        "wechat_installed": wechat_installed,
        "wechat_running": wechat_running,
        "desktop_automation_mode": desktop_automation_mode(selected_system),
    }
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from replay_mp3_studio.utils import find_ffmpeg

        checks["ffmpeg"] = find_ffmpeg()
        checks["ffmpeg_ready"] = True
    except Exception:
        checks["ffmpeg"] = ""
        checks["ffmpeg_ready"] = False
    try:
        from replay_mp3_studio.web_tools import web_tools_status

        checks.update(web_tools_status(ROOT))
    except Exception:
        checks.update(
            {
                "yt_dlp_ready": False,
                "yt_dlp_version": "",
                "javascript_runtime_ready": False,
                "javascript_runtime": "",
            }
        )
    checks["web_link_ready"] = all(
        bool(checks[key])
        for key in ("python_supported", "ffmpeg_ready", "yt_dlp_ready", "javascript_runtime_ready")
    )
    checks["xiaohongshu_ready"] = all(
        bool(checks[key]) for key in ("python_supported", "ffmpeg_ready")
    )
    checks["automatic_filehelper_ready"] = all(
        bool(checks[key])
        for key in ("macos", "python_supported", "swift", "wechat_installed", "ffmpeg_ready")
    )
    checks["manual_playback_ready"] = all(
        bool(checks[key])
        for key in ("windows", "python_supported", "wechat_installed", "ffmpeg_ready")
    )
    checks["ready"] = bool(
        checks["platform_supported"] and checks["web_link_ready"] and checks["xiaohongshu_ready"]
    )
    checks["accessibility"] = (
        "verified_on_first_guarded_ui_use"
        if selected_system == "Darwin"
        else "not_used_by_windows_manual_playback_route"
    )
    if selected_system == "Windows":
        checks["windows_manual_steps"] = [
            "Open official desktop WeChat and select the conversation named 文件传输助手.",
            "Send the exact supplied weixin.qq.com/sph link there and open the newest matching message.",
            "Start real video playback, then explicitly confirm playback to Codex.",
            "Run this CLI with --manual-playback; do not run an unbound scan before confirmation.",
        ]
        if not checks["web_link_ready"]:
            checks["next_action"] = "install_pinned_web_and_media_dependencies"
        elif checks["manual_playback_ready"]:
            checks["next_action"] = "ready_for_all_links_manual_weixin_only_when_requested"
        else:
            checks["next_action"] = "ready_for_non_weixin_links_start_wechat_for_weixin"
    checks["data_isolation"] = {
        "schema": "v1",
        "namespace": storage_namespace(resolved_profile),
        "data_root": str(user_data_root(resolved_profile)),
        "output_root": str(user_output_root(resolved_profile)),
        "boundary": "local_os_account_plus_optional_profile",
    }
    return checks


def cmd_preflight(args: argparse.Namespace) -> int:
    payload = preflight_payload(args.profile)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ready"] else 2


def cmd_verify(args: argparse.Namespace) -> int:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from replay_mp3_studio.utils import verify_mp3

    result = verify_mp3(
        Path(args.path).expanduser().resolve(), print, min_duration_seconds=args.min_duration
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    with private_umask():
        return _cmd_run_private(args)


def _cmd_run_private(args: argparse.Namespace) -> int:
    selected_system = platform.system()
    if selected_system not in SUPPORTED_SYSTEMS:
        raise RuntimeError("This workflow supports local macOS and Windows runtimes only.")
    target = classify_link(args.url)
    if args.manual_playback and target.platform != "weixin":
        raise ValueError("--manual-playback is only valid for a Weixin Channels link.")
    if selected_system == "Windows" and args.manual_playback and target.platform == "weixin":
        _wechat_installed, wechat_running = wechat_installed_or_running(system=selected_system)
        if not wechat_running:
            raise RuntimeError(
                "Windows manual playback was requested, but official desktop WeChat is not "
                "running. Start WeChat, open the exact newest link in 文件传输助手, start "
                "playback, and retry only after the user confirms it is playing."
            )
    link = target.url
    mode = "manual" if args.manual_playback else "auto"
    resolved_profile = activate_profile(args.profile)
    isolation = ensure_profile_layout(resolved_profile)
    output = target_output_path(target, args.output, resolved_profile)
    artifacts = target_run_dir(target, mode, resolved_profile)
    prepare_output_parent(output, managed_default=not bool(args.output))

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from replay_mp3_studio.extractors import (
        run_other_site,
        run_songy_direct_link,
        run_weixin_link,
        run_xiaohongshu,
    )
    from replay_mp3_studio.utils import verify_mp3

    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    log = lambda message: print(message, flush=True)
    if output.exists():
        verification = verify_mp3(output, log, min_duration_seconds=args.min_duration)
        status = "reused_verified_output"
    else:
        if target.platform == "weixin":
            run_weixin_link(
                link,
                output,
                artifacts,
                log,
                duration=args.capture_window,
                manual_playback=args.manual_playback,
                min_duration=args.min_duration,
                desktop_automation_available=selected_system == "Darwin",
            )
        elif target.platform == "xiaohongshu":
            run_xiaohongshu(link, output, artifacts, log)
        elif target.platform == "songy":
            run_songy_direct_link(link, output, artifacts, log)
        else:
            run_other_site(link, output, artifacts, log)
        verification = verify_mp3(
            output,
            log,
            min_duration_seconds=args.min_duration,
        )
        status = "completed"
    result = {
        "status": status,
        "target_platform": target.platform,
        "target_id": target.target_id,
        "target_sha256_12": hashlib.sha256(link.encode("utf-8")).hexdigest()[:12],
        "storage_namespace": isolation["namespace"],
        "output": str(output),
        "output_bytes": int(verification.get("bytes") or 0),
        "duration_seconds": verification.get("duration_seconds"),
        "artifacts": str(artifacts),
        "started_at": started_at,
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if target.platform == "weixin":
        result["target_short_id"] = target.target_id
    result_path = artifacts / "public-result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        result_path.chmod(0o600)
    except OSError:
        pass
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def local_file_output(source: Path, explicit: str = "", profile: str = "") -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    safe_stem = re.sub(r"[\\/:*?\"<>|]+", "-", source.stem).strip(" .-") or "converted-media"
    return (user_output_root(profile) / f"{safe_stem}.mp3").resolve()


def cmd_convert_file(args: argparse.Namespace) -> int:
    with private_umask():
        source = Path(args.path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Local media file not found: {source}")
        resolved_profile = activate_profile(args.profile)
        ensure_profile_layout(resolved_profile)
        output = local_file_output(source, args.output, resolved_profile)
        prepare_output_parent(output, managed_default=not bool(args.output))

        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from replay_mp3_studio.utils import find_ffmpeg, verify_mp3

        if output == source:
            verification = verify_mp3(source, print, min_duration_seconds=args.min_duration)
            result = {"status": "verified_existing_mp3", "output": str(source), **verification}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if output.exists():
            verification = verify_mp3(output, print, min_duration_seconds=args.min_duration)
            result = {"status": "reused_verified_output", "output": str(output), **verification}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{output.stem}-",
            suffix=".partial.mp3",
            dir=str(output.parent),
        )
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            temporary.unlink()
            proc = subprocess.run(
                [
                    find_ffmpeg(),
                    "-hide_banner",
                    "-y",
                    "-i",
                    str(source),
                    "-vn",
                    "-codec:a",
                    "libmp3lame",
                    "-b:a",
                    args.bitrate,
                    str(temporary),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout or "ffmpeg conversion failed")[-3000:])
            verification = verify_mp3(
                temporary,
                print,
                min_duration_seconds=args.min_duration,
            )
            temporary.replace(output)
        finally:
            if temporary.exists():
                temporary.unlink()
        final_verification = verify_mp3(output, print, min_duration_seconds=args.min_duration)
        result = {
            "status": "completed",
            "route": "authorized_local_media_file",
            "source": str(source),
            "output": str(output),
            "output_bytes": int(final_verification.get("bytes") or verification.get("bytes") or 0),
            "duration_seconds": final_verification.get("duration_seconds"),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0


def _blackbox_functions():
    if not VIDEO_AUDIO_EXTRACTOR_ROOT.is_dir():
        raise RuntimeError("The packaged video-audio-extractor runtime is missing.")
    if str(VIDEO_AUDIO_EXTRACTOR_ROOT) not in sys.path:
        sys.path.insert(0, str(VIDEO_AUDIO_EXTRACTOR_ROOT))
    from src.blackbox_recorder import list_avfoundation_devices, run_blackbox_record

    return list_avfoundation_devices, run_blackbox_record


def cmd_audio_devices(args: argparse.Namespace) -> int:
    list_devices, _record = _blackbox_functions()
    print(json.dumps(list_devices(), ensure_ascii=False, indent=2))
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    if platform.system() not in SUPPORTED_SYSTEMS:
        raise RuntimeError("Audio recording fallback is supported on macOS and Windows only.")
    if not args.playback_confirmed:
        raise ValueError(
            "Recording is explicit-only. Add --playback-confirmed only after the user confirms "
            "the exact supplied link is already playing."
        )
    if not args.audio_device:
        raise ValueError("Select an explicit device from the audio-devices command first.")
    if args.duration <= 0 or args.speed <= 0:
        raise ValueError("--duration and --speed must be positive.")

    target = classify_link(args.url)
    link = target.url
    resolved_profile = activate_profile(args.profile)
    ensure_profile_layout(resolved_profile)
    output = target_output_path(target, args.output, resolved_profile)
    prepare_output_parent(output, managed_default=not bool(args.output))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from replay_mp3_studio.utils import verify_mp3

    if output.exists():
        verification = verify_mp3(output, print, min_duration_seconds=args.min_duration)
        print(
            json.dumps(
                {"status": "reused_verified_output", "route": "existing_output", **verification},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    _list_devices, record = _blackbox_functions()
    report = record(
        url=link,
        speed=args.speed,
        out_path=str(output),
        duration=args.duration,
        audio_device=args.audio_device,
        open_url=False,
        wait_audio_timeout=args.wait_audio_timeout,
    )
    verification = verify_mp3(output, print, min_duration_seconds=args.min_duration)
    result = {
        "status": "completed",
        "route": "explicit_audio_recording_fallback",
        "target_platform": target.platform,
        "target_id": target.target_id,
        "capture_backend": report.get("capture_backend"),
        "recorded_wall_seconds": args.duration,
        "confirmed_playback_speed": args.speed,
        "output": str(output),
        "output_bytes": int(verification.get("bytes") or 0),
        "duration_seconds": verification.get("duration_seconds"),
    }
    if target.platform == "weixin":
        result["target_short_id"] = target.target_id
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    check = commands.add_parser("preflight", help="Read-only prerequisite check")
    check.add_argument("--profile", default="")
    check.set_defaults(func=cmd_preflight)
    run = commands.add_parser("run", help="Convert one authorized replay, video, or media link")
    run.add_argument("url")
    run.add_argument("--output", default="")
    run.add_argument("--min-duration", type=float, default=1.0)
    run.add_argument("--capture-window", type=int, default=300)
    run.add_argument("--manual-playback", action="store_true")
    run.add_argument("--profile", default="")
    run.set_defaults(func=cmd_run)
    convert_file = commands.add_parser(
        "convert-file",
        help="Convert one user-authorized local media file to a fully verified MP3",
    )
    convert_file.add_argument("path")
    convert_file.add_argument("--output", default="")
    convert_file.add_argument("--bitrate", default="128k")
    convert_file.add_argument("--min-duration", type=float, default=0.0)
    convert_file.add_argument("--profile", default="")
    convert_file.set_defaults(func=cmd_convert_file)
    devices = commands.add_parser(
        "audio-devices",
        help="List explicit macOS/Windows audio inputs for the recording fallback",
    )
    devices.set_defaults(func=cmd_audio_devices)
    record = commands.add_parser(
        "record",
        help="Explicit fallback: record user-confirmed playback and restore its real speed",
    )
    record.add_argument("url")
    record.add_argument("--audio-device", required=True)
    record.add_argument("--duration", type=float, required=True, help="Wall-clock recording seconds")
    record.add_argument("--speed", type=float, default=1.0, help="Actual player speed already selected")
    record.add_argument("--output", default="")
    record.add_argument("--min-duration", type=float, default=0.0)
    record.add_argument("--wait-audio-timeout", type=float, default=30.0)
    record.add_argument("--playback-confirmed", action="store_true")
    record.add_argument("--profile", default="")
    record.set_defaults(func=cmd_record)
    verify = commands.add_parser("verify", help="Full-decode an MP3")
    verify.add_argument("path")
    verify.add_argument("--min-duration", type=float, default=0.0)
    verify.set_defaults(func=cmd_verify)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return int(args.func(args))
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
