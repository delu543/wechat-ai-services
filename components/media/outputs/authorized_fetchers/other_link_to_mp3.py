#!/usr/bin/env python3
"""Route supported "other" links to a local MP3 conversion script."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from replay_mp3_studio.web_tools import (  # noqa: E402
    javascript_runtime_arguments,
    yt_dlp_command,
)

URL_RE = re.compile(r"https?://[^\s\"'<>]+")
MEDIA_EXTS = (
    ".m3u8",
    ".mp3",
    ".m4a",
    ".aac",
    ".wav",
    ".ogg",
    ".opus",
    ".weba",
    ".mp4",
    ".mov",
    ".webm",
)


class ConversionError(RuntimeError):
    def __init__(self, message: str, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.details = details or {}


def is_media_url(url: str) -> bool:
    lower_url = url.lower()
    lower_path = lower_url.split("?", 1)[0]
    return lower_path.endswith(MEDIA_EXTS) or any(
        marker in lower_url for marker in ("stodownload", "snsvideodownload", "snscosdownload")
    )


def script_kind(url: str) -> str:
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


def missing_script_message(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc or "unknown"
    return f"无法处理 {host}：只接受 http/https 媒体或网页链接。"


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def display_arg(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        netloc = parsed.netloc
        if parsed.username:
            host = parsed.hostname or ""
            port = f":{parsed.port}" if parsed.port else ""
            netloc = f"<auth>@{host}{port}"
        base = urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        return f"{base}?<redacted>" if parsed.query else base
    return value


def redact_command(command: list[str]) -> list[str]:
    return [display_arg(part) for part in command]


def redact_text(text: str, sensitive_values: list[str]) -> str:
    redacted = text
    for value in sensitive_values:
        redacted = redacted.replace(value, display_arg(value))
    redacted = URL_RE.sub(lambda match: display_arg(match.group(0)), redacted)
    return redacted


def tail_text(value: str, limit: int = 4000) -> str:
    return value[-limit:] if len(value) > limit else value


def normalize_proxy(value: str) -> str:
    proxy = value.strip()
    if not proxy:
        return ""
    if "://" not in proxy:
        proxy = "http://" + proxy
    return proxy


def system_proxy_url() -> str:
    if sys.platform != "darwin":
        proxies = urllib.request.getproxies()
        return normalize_proxy(
            str(proxies.get("https") or proxies.get("http") or proxies.get("all") or "")
        )
    try:
        proc = subprocess.run(
            ["scutil", "--proxy"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    values: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if " : " not in line:
            continue
        key, raw_value = line.strip().split(" : ", 1)
        values[key.strip()] = raw_value.strip()
    if values.get("HTTPSEnable") == "1" and values.get("HTTPSProxy") and values.get("HTTPSPort"):
        return f"http://{values['HTTPSProxy']}:{values['HTTPSPort']}"
    if values.get("HTTPEnable") == "1" and values.get("HTTPProxy") and values.get("HTTPPort"):
        return f"http://{values['HTTPProxy']}:{values['HTTPPort']}"
    if values.get("SOCKSEnable") == "1" and values.get("SOCKSProxy") and values.get("SOCKSPort"):
        return f"socks5://{values['SOCKSProxy']}:{values['SOCKSPort']}"
    return ""


def youtube_proxy_candidates(mode: str = "auto") -> list[str]:
    normalized_mode = (mode or "auto").strip()
    if normalized_mode.lower() in {"", "auto"}:
        for name in ("YT_DLP_PROXY", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            proxy = normalize_proxy(os.environ.get(name, ""))
            if proxy:
                return [proxy]
        proxy = system_proxy_url()
        return [proxy] if proxy else [""]
    if normalized_mode.lower() in {"none", "off", "direct"}:
        return [""]
    return [normalize_proxy(normalized_mode)]


def run_direct_media(url: str, output: Path) -> None:
    script = Path(__file__).with_name("media_url_to_mp3.py")
    subprocess.run([sys.executable, str(script), url, "--output", str(output)], check=True)


def find_ffmpeg() -> str:
    env = os.environ.get("FFMPEG")
    if env and Path(env).exists():
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    candidates = sorted(
        [
            *(ROOT / "work" / "venv" / "lib").glob(
                "python*/site-packages/imageio_ffmpeg/binaries/ffmpeg-*"
            ),
            *(ROOT / "work" / "venv" / "Lib" / "site-packages").glob(
                "imageio_ffmpeg/binaries/ffmpeg-*"
            ),
        ]
    )
    if candidates:
        return str(candidates[0])
    raise ConversionError("ffmpeg not found. Set FFMPEG=/path/to/ffmpeg.")


def youtube_attempt_command(
    url: str,
    template: str,
    client: str,
    ffmpeg: str,
    sample_seconds: int = 0,
    proxy: str = "",
) -> list[str]:
    command = [
        *yt_dlp_command(),
        *javascript_runtime_arguments(ROOT),
        "--no-playlist",
        "--force-overwrites",
        "--force-ipv4",
        "--socket-timeout",
        "20",
        "--retries",
        "3",
        "--fragment-retries",
        "3",
        "--extractor-retries",
        "2",
        "--ffmpeg-location",
        ffmpeg,
        "-f",
        "bestaudio/best",
        "--extract-audio",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "128K",
    ]
    if proxy:
        command.extend(["--proxy", proxy])
    if client:
        command.extend(["--extractor-args", f"youtube:player_client={client}"])
    if sample_seconds > 0:
        command.extend(["--download-sections", f"*0-{sample_seconds}"])
    command.extend(["-o", template, url])
    return command


def run_text_command(command: list[str], timeout: int = 90) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return 124, stdout, stderr + "\ncommand timed out"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def youtube_metadata(url: str, proxy: str = "") -> dict[str, Any]:
    command = [
        *yt_dlp_command(),
        *javascript_runtime_arguments(ROOT),
        "--no-playlist",
        "--skip-download",
        "--dump-json",
        "--force-ipv4",
        "--socket-timeout",
        "20",
    ]
    if proxy:
        command.extend(["--proxy", proxy])
    command.append(url)
    rc, stdout, stderr = run_text_command(command, timeout=90)
    result: dict[str, Any] = {
        "returncode": rc,
        "command": redact_command(command),
        "proxy": display_arg(proxy) if proxy else "direct",
    }
    if rc != 0:
        result["stderr_tail"] = tail_text(redact_text(stderr, [url]))
        return result
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        result["error"] = "yt-dlp metadata output was not JSON"
        return result
    formats = payload.get("formats") if isinstance(payload.get("formats"), list) else []
    media_formats = []
    for item in formats:
        if not isinstance(item, dict):
            continue
        if item.get("acodec") == "none" and item.get("vcodec") == "none":
            continue
        media_formats.append(
            {
                "format_id": item.get("format_id"),
                "ext": item.get("ext"),
                "protocol": item.get("protocol"),
                "vcodec": item.get("vcodec"),
                "acodec": item.get("acodec"),
                "filesize": item.get("filesize") or item.get("filesize_approx"),
                "tbr": item.get("tbr"),
                "format_note": item.get("format_note"),
            }
        )
    result.update(
        {
            "id": payload.get("id"),
            "title": payload.get("title"),
            "duration": payload.get("duration"),
            "availability": payload.get("availability"),
            "format_count": len(formats),
            "media_formats": media_formats[:12],
        }
    )
    return result


def youtube_cdn_probe(url: str, proxy: str = "") -> dict[str, Any]:
    command = [
        *yt_dlp_command(),
        *javascript_runtime_arguments(ROOT),
        "--no-playlist",
        "-f",
        "18/bestaudio/best",
        "--get-url",
        "--force-ipv4",
        "--socket-timeout",
        "20",
    ]
    if proxy:
        command.extend(["--proxy", proxy])
    command.append(url)
    rc, stdout, stderr = run_text_command(command, timeout=90)
    result: dict[str, Any] = {
        "returncode": rc,
        "command": redact_command(command),
        "proxy": display_arg(proxy) if proxy else "direct",
    }
    if rc != 0:
        result["stderr_tail"] = tail_text(redact_text(stderr, [url]))
        return result
    media_url = stdout.strip().splitlines()[-1] if stdout.strip() else ""
    if not media_url:
        result["error"] = "yt-dlp did not return a media URL"
        return result
    parsed = urllib.parse.urlparse(media_url)
    result["media_url_host"] = parsed.netloc
    curl_executable = shutil.which("curl")
    if not curl_executable:
        result.update(
            {
                "curl_returncode": None,
                "curl_summary": "curl_unavailable_probe_skipped",
                "curl_stderr_tail": "",
            }
        )
        return result
    curl_cmd = [
        curl_executable,
        "-L",
        "--range",
        "0-2047",
        "--max-time",
        "45",
        "-sS",
        "-o",
        os.devnull,
        "-w",
        "HTTP:%{http_code} SIZE:%{size_download} TIME:%{time_total}",
    ]
    if proxy:
        curl_cmd.extend(["-x", proxy])
    curl_cmd.append(media_url)
    curl = subprocess.run(curl_cmd, text=True, capture_output=True, timeout=60)
    result.update(
        {
            "curl_returncode": curl.returncode,
            "curl_summary": curl.stdout.strip(),
            "curl_stderr_tail": tail_text(redact_text(curl.stderr or "", [media_url])),
        }
    )
    return result


def classify_youtube_failure(attempts: list[dict[str, Any]], cdn_probe: dict[str, Any]) -> str:
    text = "\n".join(str(item.get("stderr_tail") or "") for item in attempts)
    probe_text = f"{cdn_probe.get('curl_returncode')} {cdn_probe.get('curl_summary')} {cdn_probe.get('curl_stderr_tail')}"
    combined = (text + "\n" + probe_text).lower()
    if "operation timed out" in combined or "curl_returncode': 28" in combined or "http:000" in combined:
        return "youtube_cdn_unreachable_or_timed_out"
    if "po token" in combined or "gvs po token" in combined:
        return "po_token_required_for_some_clients"
    if "the page needs to be reloaded" in combined:
        return "youtube_client_reload_required"
    if any(item.get("returncode") == 124 for item in attempts):
        return "local_command_timeout"
    return "youtube_download_failed"


def run_youtube(url: str, output: Path, sample_seconds: int = 0, proxy_mode: str = "auto") -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg()
    clients = ["", "web", "android", "mweb"]
    failures: list[dict[str, Any]] = []
    proxy_attempts: list[dict[str, Any]] = []
    last_metadata: dict[str, Any] = {}
    last_cdn_probe: dict[str, Any] = {}
    proxies = youtube_proxy_candidates(proxy_mode)
    for proxy in proxies:
        metadata = youtube_metadata(url, proxy=proxy)
        cdn_probe = youtube_cdn_probe(url, proxy=proxy)
        last_metadata = metadata
        last_cdn_probe = cdn_probe
        proxy_failures: list[dict[str, Any]] = []
        for client in clients:
            with tempfile.TemporaryDirectory(prefix="other-youtube-") as tmp:
                template = str(Path(tmp) / "download.%(ext)s")
                command = youtube_attempt_command(url, template, client, ffmpeg, sample_seconds, proxy=proxy)
                proc = subprocess.run(command, text=True, capture_output=True)
                sensitive_values = [url, proxy]
                stdout = tail_text(redact_text(proc.stdout or "", sensitive_values))
                stderr = tail_text(redact_text(proc.stderr or "", sensitive_values))
                attempt = {
                    "client": client or "default",
                    "proxy": display_arg(proxy) if proxy else "direct",
                    "returncode": proc.returncode,
                    "command": redact_command(command),
                    "stdout_tail": stdout,
                    "stderr_tail": stderr,
                }
                if proc.returncode == 0:
                    mp3s = sorted(Path(tmp).glob("download*.mp3"))
                    if mp3s:
                        if output.exists():
                            output.unlink()
                        shutil.move(str(mp3s[0]), str(output))
                        return {
                            "youtube_client": client or "default",
                            "proxy": display_arg(proxy) if proxy else "direct",
                            "ffmpeg": ffmpeg,
                            "metadata": metadata,
                            "cdn_probe": cdn_probe,
                            "sample_seconds": sample_seconds,
                            "attempts": failures + proxy_failures + [attempt],
                            "proxy_attempts": proxy_attempts
                            + [
                                {
                                    "proxy": display_arg(proxy) if proxy else "direct",
                                    "metadata": metadata,
                                    "cdn_probe": cdn_probe,
                                    "attempts": proxy_failures + [attempt],
                                }
                            ],
                        }
                    attempt["error"] = "yt-dlp finished without producing MP3"
                proxy_failures.append(attempt)
        failures.extend(proxy_failures)
        proxy_attempts.append(
            {
                "proxy": display_arg(proxy) if proxy else "direct",
                "metadata": metadata,
                "cdn_probe": cdn_probe,
                "attempts": proxy_failures,
            }
        )
    failure_category = classify_youtube_failure(failures, last_cdn_probe)
    raise ConversionError(
        "YouTube 脚本存在，但当前链接未能下载为 MP3；未读取账号 Cookie、Token 或绕过鉴权。"
        f"失败分类：{failure_category}。请查看报告中的元数据、CDN 预检和 yt-dlp 尾部日志。",
        {
            "ffmpeg": ffmpeg,
            "proxy": ", ".join(display_arg(item) if item else "direct" for item in proxies),
            "metadata": last_metadata,
            "cdn_probe": last_cdn_probe,
            "sample_seconds": sample_seconds,
            "failure_category": failure_category,
            "attempts": failures,
            "proxy_attempts": proxy_attempts,
        },
    )


def run_ytdlp_site(
    url: str,
    output: Path,
    *,
    kind: str,
    sample_seconds: int = 0,
    proxy_mode: str = "auto",
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg()
    failures: list[dict[str, Any]] = []
    proxies = youtube_proxy_candidates(proxy_mode)
    for proxy in proxies:
        with tempfile.TemporaryDirectory(prefix=f"other-{kind}-") as tmp:
            template = str(Path(tmp) / "download.%(ext)s")
            command = youtube_attempt_command(
                url,
                template,
                "",
                ffmpeg,
                sample_seconds,
                proxy=proxy,
            )
            try:
                proc = subprocess.run(command, text=True, capture_output=True, check=False)
            except OSError as exc:
                failures.append(
                    {
                        "provider": kind,
                        "proxy": display_arg(proxy) if proxy else "direct",
                        "command": redact_command(command),
                        "error": str(exc),
                    }
                )
                continue
            attempt = {
                "provider": kind,
                "proxy": display_arg(proxy) if proxy else "direct",
                "returncode": proc.returncode,
                "command": redact_command(command),
                "stdout_tail": tail_text(redact_text(proc.stdout or "", [url, proxy])),
                "stderr_tail": tail_text(redact_text(proc.stderr or "", [url, proxy])),
            }
            if proc.returncode == 0:
                mp3s = sorted(Path(tmp).glob("download*.mp3"))
                if mp3s:
                    if output.exists():
                        output.unlink()
                    shutil.move(str(mp3s[0]), str(output))
                    return {
                        "provider": kind,
                        "proxy": display_arg(proxy) if proxy else "direct",
                        "ffmpeg": ffmpeg,
                        "sample_seconds": sample_seconds,
                        "attempts": failures + [attempt],
                    }
                attempt["error"] = "yt-dlp finished without producing MP3"
            failures.append(attempt)
    raise ConversionError(
        f"{kind} 链接未能下载为 MP3；工具未自动读取浏览器 Cookie、Token 或账号凭据。",
        {
            "provider": kind,
            "ffmpeg": ffmpeg,
            "sample_seconds": sample_seconds,
            "attempts": failures,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", default="")
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--sample-seconds", type=int, default=0)
    parser.add_argument(
        "--proxy",
        default="auto",
        help="YouTube proxy mode: auto, none/direct, or an explicit proxy URL such as http://127.0.0.1:7897.",
    )
    args = parser.parse_args()

    output = Path(args.output).expanduser().resolve()
    report_path = Path(args.report).expanduser().resolve() if args.report else output.with_suffix(".other.json")
    kind = script_kind(args.url)
    report = {
        "url_host": urllib.parse.urlparse(args.url).netloc,
        "script_kind": kind,
        "output": str(output),
        "status": "planned" if kind else "missing_script",
    }
    write_report(report_path, report)

    if not kind:
        print(missing_script_message(args.url))
        return 3
    if args.list_only:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    try:
        details: dict[str, Any] = {}
        if kind == "direct_media":
            run_direct_media(args.url, output)
        elif kind == "youtube":
            details = run_youtube(args.url, output, sample_seconds=args.sample_seconds, proxy_mode=args.proxy)
        elif kind in {"x", "yt_dlp"}:
            details = run_ytdlp_site(
                args.url,
                output,
                kind=kind,
                sample_seconds=args.sample_seconds,
                proxy_mode=args.proxy,
            )
        else:
            print(missing_script_message(args.url))
            return 3
    except ConversionError as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        report.update(exc.details)
        write_report(report_path, report)
        print(str(exc), file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        report["status"] = "failed"
        report["error"] = f"command failed with exit code {exc.returncode}"
        report["command"] = redact_command([str(part) for part in exc.cmd])
        write_report(report_path, report)
        print(report["error"], file=sys.stderr)
        return exc.returncode or 1

    report["status"] = "converted"
    report["bytes"] = output.stat().st_size if output.exists() else 0
    report.update(details)
    write_report(report_path, report)
    print(f"Converted other link using {kind}: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
