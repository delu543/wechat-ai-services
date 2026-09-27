#!/usr/bin/env python3
"""Try direct public extraction from the remaining Weixin/Songy links first."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from extract_media_from_artifact import walk as walk_media_urls


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from replay_mp3_studio.config import WORK_ROOT  # noqa: E402
from replay_mp3_studio.user_storage import user_output_root  # noqa: E402
from replay_mp3_studio.weixin_decode_key import decode_weixin_pair_to_mp3  # noqa: E402
from replay_mp3_studio.weixin_source_pairs import (  # noqa: E402
    extract_decode_key_pairs_from_text,
    redacted_pair_summary,
)

OUTPUTS = user_output_root()
WORK_REPORTS = WORK_ROOT / "direct-link-probes"

DEFAULT_WEIXIN = "https://weixin.qq.com/sph/AtKXhlaKjL"
DEFAULT_SONGY = "https://webapp.songy.info/#/courses/details?course_id=784"
WEIXIN_FEED_API = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"
YUANBAO_PARSE_API = "https://yuanbao.tencent.com/api/weixin/get_parse_result"
SONGY_API_BASE = "https://bandu-api.songy.info"
WEIXIN_DIRECT_TIMEOUT_ENV = "WEIXIN_DIRECT_REQUEST_TIMEOUT_SECONDS"

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


def weixin_request_timeout(default: float) -> float:
    raw = os.environ.get(WEIXIN_DIRECT_TIMEOUT_ENV, "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        return float(default)
    return max(1.0, min(value, 120.0))


def save_report(name: str, payload: dict[str, Any]) -> Path:
    WORK_REPORTS.mkdir(parents=True, exist_ok=True)
    path = WORK_REPORTS / f"{name}.json"
    path.write_text(json.dumps(sanitize_report_payload(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> tuple[int, Any]:
    data = None
    merged_headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0",
    }
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        merged_headers["Content-Type"] = "application/json;charset=UTF-8"
    if headers:
        merged_headers.update(headers)
    request = urllib.request.Request(url, data=data, headers=merged_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            return response.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"raw": raw}
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, {"error": "network_unavailable_or_timed_out"}


def display_url(value: str) -> str:
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


def redact_sensitive_text(text: str) -> str:
    redacted = re.sub(r"https?://[^\s\"'<>]+", lambda match: display_url(match.group(0)), text)
    redacted = re.sub(r"((?:token|cookie|auth|sign|encfilekey)=)[^&\s]+", r"\1<redacted>", redacted, flags=re.I)
    return redacted


def _sensitive_report_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", key.lower())
    return normalized in {
        "cookie",
        "cookies",
        "token",
        "generaltoken",
        "auth",
        "authorization",
        "sign",
        "signature",
        "encfilekey",
        "decodekey",
        "decodekeyv2",
        "decryptkey",
    }


def sanitize_report_payload(value: Any, *, key: str = "") -> Any:
    if _sensitive_report_key(key):
        if isinstance(value, bool):
            return value
        if value in (None, "", [], {}):
            return value
        return "<redacted>"
    if isinstance(value, dict):
        return {str(item_key): sanitize_report_payload(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [sanitize_report_payload(item, key=key) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def tail_error(value: Any, limit: int = 500) -> str:
    return redact_sensitive_text(str(value))[-limit:]


def score_url(url: str) -> int:
    lower = url.lower().split("?", 1)[0]
    if lower.endswith((".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".weba")):
        return 0
    if lower.endswith(".m3u8"):
        return 1
    if lower.endswith((".mp4", ".mov", ".webm")) or any(
        marker in lower for marker in ("stodownload", "snsvideodownload", "snscosdownload")
    ):
        return 2
    return 3


def media_urls(payload: Any) -> list[str]:
    urls: list[str] = []
    seen = set()
    for url in walk_media_urls(payload):
        lower = url.lower().split("?", 1)[0]
        if url in seen:
            continue
        if lower.endswith(MEDIA_EXTS) or "m3u8" in lower or any(
            marker in lower for marker in ("stodownload", "snsvideodownload", "snscosdownload")
        ):
            seen.add(url)
            urls.append(url)
    return sorted(urls, key=score_url)


def convert(url: str, output: Path) -> None:
    converter = Path(__file__).with_name("media_url_to_mp3.py")
    subprocess.run([sys.executable, str(converter), url, "--output", str(output)], check=True)


def extract_query_pair(url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    token = query.get("token", [""])[0]
    export_id = query.get("eid", [""])[0] or query.get("exportId", [""])[0]
    return token, export_id


def compact_provider_response(payload: Any) -> dict[str, Any]:
    urls = media_urls(payload)
    summary = {
        "candidate_media_url_count": len(urls),
        "redacted_media_urls": [display_url(url) for url in urls[:8]],
    }
    error_message = compact_error_message(payload)
    if error_message:
        summary["error_message"] = error_message
    return summary


def decode_key_pairs_from_provider_payload(payload: Any, *, path: str) -> list[dict[str, str]]:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return extract_decode_key_pairs_from_text(encoded, path=path)


def try_weixin_decode_key_payload(payload: Any, output: Path, *, label: str) -> tuple[bool, dict[str, Any]]:
    pairs = decode_key_pairs_from_provider_payload(payload, path=label)
    details: dict[str, Any] = {
        "decode_key_pair_count": len(pairs),
        "decode_key_pair_summary": redacted_pair_summary(pairs),
    }
    if not pairs:
        return False, details
    try:
        conversion = decode_weixin_pair_to_mp3(
            pairs[0],
            output,
            work_dir=WORK_REPORTS
            / "decode-key-source"
            / (
                re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-")
                + "-"
                + hashlib.sha256(str(pairs[0].get("url") or "").encode("utf-8")).hexdigest()[:12]
            ),
        )
    except Exception as exc:
        details["status"] = "failed"
        details["error"] = tail_error(exc)
        return False, details
    details["status"] = "created-mp3"
    details["media_source"] = "resolver_decode_key_pair"
    details["conversion"] = conversion
    return True, details


def compact_error_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("error", "errMsg", "msg", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:300]
        if isinstance(value, dict):
            title = value.get("title") or value.get("message") or value.get("errMsg")
            if isinstance(title, str) and title.strip():
                return title.strip()[:300]
    data = payload.get("data")
    if isinstance(data, dict):
        return compact_error_message(data)
    return ""


def walk_dicts(payload: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        found.append(payload)
        for value in payload.values():
            found.extend(walk_dicts(value))
    elif isinstance(payload, list):
        for value in payload:
            found.extend(walk_dicts(value))
    return found


def extract_playable_token_pairs(payload: Any) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add_pair(token: str, export_id: str, source: str) -> None:
        token = token.strip()
        export_id = export_id.strip()
        if not token and not export_id:
            return
        key = (token, export_id)
        if key in seen:
            return
        seen.add(key)
        pairs.append({"token": token, "export_id": export_id, "source": source})

    def visit(value: Any, source: str = "payload") -> None:
        if isinstance(value, str):
            token, export_id = extract_query_pair(value)
            add_pair(token, export_id, source)
            return
        if isinstance(value, list):
            for item in value:
                visit(item, source)
            return
        if not isinstance(value, dict):
            return

        playable = str(value.get("playable_url") or value.get("playableUrl") or "")
        token, export_id = extract_query_pair(playable)
        export_id = export_id or str(
            value.get("wx_export_id")
            or value.get("wxExportId")
            or value.get("export_id")
            or value.get("exportId")
            or ""
        )
        add_pair(token, export_id, "playable_url")
        for key, item in value.items():
            visit(item, str(key))

    visit(payload)
    return pairs


def resolve_playable_payload_to_media(payload: Any, output: Path) -> tuple[bool, dict[str, Any]]:
    pairs = extract_playable_token_pairs(payload)
    details: dict[str, Any] = {
        "playable_pair_count": len(pairs),
        "feed_attempts": [],
    }
    for pair in pairs:
        feed_attempt: dict[str, Any] = {
            "source": pair.get("source", ""),
            "has_general_token": bool(pair.get("token")),
            "has_export_id": bool(pair.get("export_id")),
        }
        details["feed_attempts"].append(feed_attempt)
        if not pair.get("token") or not pair.get("export_id"):
            feed_attempt["status"] = "skipped"
            feed_attempt["error"] = "missing token or export_id"
            continue

        status, feed = fetch_weixin_feed_with_export_token(pair["export_id"], pair["token"])
        urls = media_urls(feed)
        feed_attempt["status"] = status
        feed_attempt["summary"] = compact_provider_response(feed)
        if urls:
            convert(urls[0], output)
            details["media_source"] = "playable_url_feed"
            details["feed_summary"] = compact_provider_response(feed)
            return True, details

    return False, details


def yuanbao_parse_link(link: str, cookie: str) -> tuple[int, Any]:
    return request_json(
        YUANBAO_PARSE_API,
        method="POST",
        payload={"type": "video_channel_url", "url": link, "scene": 1},
        headers={
            "Origin": "https://yuanbao.tencent.com",
            "Referer": "https://yuanbao.tencent.com/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
            ),
            "Cookie": cookie,
        },
        timeout=weixin_request_timeout(12),
    )


def try_weixin_yuanbao(link: str, output: Path) -> tuple[bool, dict[str, Any]]:
    cookie = os.environ.get("WEIXIN_YUANBAO_COOKIE", "").strip()
    stage: dict[str, Any] = {
        "name": "yuanbao-cookie",
        "configured": bool(cookie),
        "status": "skipped",
    }
    if not cookie:
        return False, stage

    status, parsed = yuanbao_parse_link(link, cookie)
    stage["parse_status"] = status
    stage["parse_summary"] = compact_provider_response(parsed)
    if status >= 400:
        stage["status"] = "failed"
        stage["error"] = f"yuanbao parse returned HTTP {status}"
        return False, stage

    decode_success, decode_details = try_weixin_decode_key_payload(parsed, output, label="yuanbao-cookie")
    if decode_details.get("decode_key_pair_count"):
        stage["decode_key_pair_count"] = int(decode_details.get("decode_key_pair_count") or 0)
        stage["decode_key_pair_summary"] = decode_details.get("decode_key_pair_summary", [])
    if decode_success:
        stage["conversion"] = decode_details.get("conversion")
        stage["media_source"] = "yuanbao_decode_key_pair"
        stage["status"] = "created-mp3"
        return True, stage
    if decode_details.get("status") == "failed":
        stage["decode_key_error"] = decode_details.get("error", "")

    parse_data = parsed.get("data") if isinstance(parsed, dict) else {}
    playable_url = str(parse_data.get("playable_url") or parse_data.get("playableUrl") or "")
    wx_export_id = str(parse_data.get("wx_export_id") or parse_data.get("wxExportId") or "")
    token, export_id = extract_query_pair(playable_url)
    export_id = export_id or wx_export_id
    stage["has_playable_url"] = bool(playable_url)
    stage["has_general_token"] = bool(token)
    stage["has_export_id"] = bool(export_id)
    if not token or not export_id:
        stage["status"] = "failed"
        stage["error"] = "yuanbao response did not include token/eid"
        return False, stage

    status, feed = fetch_weixin_feed_with_export_token(export_id, token)
    urls = media_urls(feed)
    stage["feed_status"] = status
    stage["feed_summary"] = compact_provider_response(feed)
    if not urls:
        stage["status"] = "failed"
        stage["error"] = "channels feed returned no media URL"
        return False, stage

    convert(urls[0], output)
    stage["status"] = "created-mp3"
    return True, stage


def sph_resolver_endpoints(base_url: str) -> list[str]:
    base = base_url.rstrip("/")
    if base.endswith("/fetch_video_profile"):
        return [base]
    endpoints = [base + "/fetch_video_profile"]
    if not base.endswith("/api"):
        endpoints.append(base + "/api/fetch_video_profile")
    return endpoints


def sph_resolver_request(
    endpoint: str,
    link: str,
    referer_base: str,
    *,
    method: str = "POST",
    payload: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> tuple[int, Any]:
    return request_json(
        endpoint,
        method=method,
        payload=payload if payload is not None else {"url": link},
        headers={
            "Origin": referer_base.rstrip("/"),
            "Referer": referer_base.rstrip("/") + "/",
            "User-Agent": "Mozilla/5.0",
        },
        timeout=timeout if timeout is not None else weixin_request_timeout(12),
    )


def sph_resolver_request_specs(base_url: str, link: str) -> list[dict[str, Any]]:
    base = base_url.rstrip("/")
    specs: list[dict[str, Any]] = []
    for endpoint in sph_resolver_endpoints(base_url):
        specs.append(
            {
                "name": "worker_fetch_video_profile",
                "method": "POST",
                "endpoint": endpoint,
                "payload": {"url": link},
            }
        )

    api_roots: list[str] = []
    if base.endswith("/api"):
        api_roots.append(base)
    else:
        api_roots.append(base + "/api")
    # wx_channel exposes both /api/channels and /api/search compatible routes.
    for api_root in api_roots:
        encoded = urllib.parse.urlencode({"url": link})
        for namespace in ("channels", "search"):
            specs.append(
                {
                    "name": f"{namespace}_parse_sph",
                    "method": "GET",
                    "endpoint": f"{api_root}/{namespace}/parse_sph?{encoded}",
                    "payload": None,
                }
            )
            specs.append(
                {
                    "name": f"{namespace}_shared_feed_profile",
                    "method": "GET",
                    "endpoint": f"{api_root}/{namespace}/shared_feed/profile?{encoded}",
                    "payload": None,
                }
            )
            specs.append(
                {
                    "name": f"{namespace}_share_resolve_backend",
                    "method": "POST",
                    "endpoint": f"{api_root}/{namespace}/share/resolve",
                    "payload": {"mode": "backend", "urls": [link]},
                }
            )
    return specs


def try_weixin_sph_resolver(link: str, output: Path) -> tuple[bool, dict[str, Any]]:
    base_url = os.environ.get("WEIXIN_SPH_RESOLVER_URL", "").strip()
    stage: dict[str, Any] = {
        "name": "configured-sph-resolver",
        "configured": bool(base_url),
        "resolver": display_url(base_url),
        "status": "skipped",
        "attempts": [],
    }
    if not base_url:
        return False, stage

    started = time.monotonic()
    total_budget = weixin_request_timeout(12)
    deadline = started + total_budget
    stage["timeout_budget_seconds"] = total_budget
    last_status = 0
    last_url_count = 0
    last_pair_count = 0
    for spec in sph_resolver_request_specs(base_url, link):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stage["attempts"].append({"name": "resolver_budget", "status": "exhausted"})
            break
        status, payload = sph_resolver_request(
            spec["endpoint"],
            link,
            base_url,
            method=spec["method"],
            payload=spec.get("payload"),
            timeout=max(1.0, min(4.0, remaining)),
        )
        decode_success, decode_details = try_weixin_decode_key_payload(payload, output, label=spec["name"])
        urls = media_urls(payload)
        playable_success, playable_details = (False, {})
        if not urls and not decode_success:
            playable_success, playable_details = resolve_playable_payload_to_media(payload, output)
        attempt = {
            "name": spec["name"],
            "method": spec["method"],
            "endpoint": display_url(spec["endpoint"]),
            "http_status": status,
            "summary": compact_provider_response(payload),
        }
        if decode_details.get("decode_key_pair_count"):
            attempt["decode_key_summary"] = decode_details
        if playable_details:
            attempt["playable_summary"] = playable_details
        stage["attempts"].append(attempt)
        last_status = status
        last_url_count = len(urls)
        last_pair_count = int(playable_details.get("playable_pair_count", 0)) if playable_details else 0
        if decode_success:
            stage["http_status"] = status
            stage["summary"] = compact_provider_response(payload)
            stage["decode_key_pair_count"] = int(decode_details.get("decode_key_pair_count") or 0)
            stage["decode_key_pair_summary"] = decode_details.get("decode_key_pair_summary", [])
            stage["conversion"] = decode_details.get("conversion")
            stage["media_source"] = "resolver_decode_key_pair"
            stage["status"] = "created-mp3"
            stage["elapsed_seconds"] = round(time.monotonic() - started, 3)
            return True, stage
        if playable_success:
            stage["http_status"] = status
            stage["summary"] = compact_provider_response(payload)
            stage["playable_summary"] = playable_details
            stage["status"] = "created-mp3"
            stage["elapsed_seconds"] = round(time.monotonic() - started, 3)
            return True, stage
        if status < 400 and urls:
            stage["http_status"] = status
            stage["summary"] = compact_provider_response(payload)
            convert(urls[0], output)
            stage["status"] = "created-mp3"
            stage["elapsed_seconds"] = round(time.monotonic() - started, 3)
            return True, stage

    stage["http_status"] = last_status
    stage["status"] = "failed"
    stage["elapsed_seconds"] = round(time.monotonic() - started, 3)
    stage["error"] = (
        "resolver returned no media URL; "
        f"last HTTP {last_status} with {last_url_count} media URL(s) "
        f"and {last_pair_count} playable token pair(s)"
    )
    return False, stage


def try_weixin_authorized_export_id(export_id: str | None, output: Path) -> bool:
    if not export_id:
        return False
    attempted = False
    if os.environ.get("JUSTONE_API_KEY") or os.environ.get("JUSTONE_TOKEN"):
        attempted = True
        script = Path(__file__).with_name("weixin_justone_to_mp3.py")
        try:
            subprocess.run(
                [sys.executable, str(script), "--export-id", export_id, "--output", str(output)],
                check=True,
            )
            return True
        except subprocess.CalledProcessError as exc:
            print(f"JustOne authorized exportId route failed with exit code {exc.returncode}.")
    if os.environ.get("DAJIALA_KEY") or os.environ.get("JZL_KEY"):
        attempted = True
        script = Path(__file__).with_name("weixin_dajiala_to_mp3.py")
        try:
            subprocess.run(
                [sys.executable, str(script), "--export-id", export_id, "--output", str(output)],
                check=True,
            )
            return True
        except subprocess.CalledProcessError as exc:
            print(f"Dajiala/Jizhile authorized exportId route failed with exit code {exc.returncode}.")
    if os.environ.get("APIFY_TOKEN"):
        attempted = True
        script = Path(__file__).with_name("weixin_apify_to_mp3.py")
        try:
            subprocess.run(
                [sys.executable, str(script), "--export-id", export_id, "--output", str(output)],
                check=True,
            )
            return True
        except subprocess.CalledProcessError as exc:
            print(f"Apify authorized exportId route failed with exit code {exc.returncode}.")
    if attempted:
        print("Authorized Weixin exportId routes were attempted but did not create MP3.")
    return False


def try_weixin_authorized_link(link: str, output: Path) -> bool:
    if not (os.environ.get("JUSTONE_API_KEY") or os.environ.get("JUSTONE_TOKEN")):
        return False
    script = Path(__file__).with_name("weixin_justone_to_mp3.py")
    try:
        subprocess.run(
            [sys.executable, str(script), "--link", link, "--output", str(output)],
            check=True,
        )
        return True
    except subprocess.CalledProcessError as exc:
        print(f"JustOne authorized link route failed with exit code {exc.returncode}.")
        return False


def parse_weixin_short_uri(link: str) -> str:
    parsed = urllib.parse.urlparse(link)
    if parsed.path.startswith("/sph/"):
        return parsed.path.rsplit("/", 1)[-1]
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("id", "shortUri", "sph"):
        if query.get(key):
            return query[key][0]
    if parsed.path:
        tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if tail:
            return tail
    raise SystemExit(f"Cannot identify Weixin short URI from {link}")


def fetch_weixin_feed(payload: dict[str, Any], short_uri: str) -> tuple[int, Any]:
    rid = str(int(time.time() * 1000))
    page_url = "https://channels.weixin.qq.com/finder-preview/pages/sph"
    url = WEIXIN_FEED_API + "?" + urllib.parse.urlencode({"_rid": rid, "_pageUrl": page_url})
    return request_json(
        url,
        method="POST",
        payload=payload,
        headers={
            "Origin": "https://channels.weixin.qq.com",
            "Referer": f"https://channels.weixin.qq.com/finder-preview/pages/sph?id={short_uri}",
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
            ),
        },
        timeout=weixin_request_timeout(5),
    )


def fetch_weixin_feed_with_export_token(export_id: str, general_token: str) -> tuple[int, Any]:
    rid = str(int(time.time() * 1000))
    page_url = "https://channels.weixin.qq.com/finder-preview/pages/feed"
    url = WEIXIN_FEED_API + "?" + urllib.parse.urlencode({"_rid": rid, "_pageUrl": page_url})
    referer_query = urllib.parse.urlencode(
        {
            "entry_card_type": "48",
            "comment_scene": "39",
            "appid": "0",
            "token": general_token,
            "entry_scene": "0",
            "eid": export_id,
        }
    )
    return request_json(
        url,
        method="POST",
        payload={"baseReq": {"generalToken": general_token}, "exportId": export_id},
        headers={
            "Origin": "https://channels.weixin.qq.com",
            "Referer": f"https://channels.weixin.qq.com/finder-preview/pages/feed?{referer_query}",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
            ),
        },
        timeout=weixin_request_timeout(5),
    )


def direct_weixin(link: str, output: Path) -> bool:
    short_uri = parse_weixin_short_uri(link)
    attempts: list[dict[str, Any]] = []

    success, yuanbao_stage = try_weixin_yuanbao(link, output)
    attempts.append(yuanbao_stage)
    if success:
        report = {
            "target": "weixin",
            "link": link,
            "short_uri": short_uri,
            "candidate_media_urls": ["yuanbao-cookie-authorized"],
            "attempts": attempts,
        }
        report_path = save_report("weixin_direct_link_probe", report)
        print(f"Weixin authorized Yuanbao route created MP3. Probe report: {report_path}")
        return True

    success, resolver_stage = try_weixin_sph_resolver(link, output)
    attempts.append(resolver_stage)
    if success:
        report = {
            "target": "weixin",
            "link": link,
            "short_uri": short_uri,
            "candidate_media_urls": ["configured-sph-resolver"],
            "attempts": attempts,
        }
        report_path = save_report("weixin_direct_link_probe", report)
        print(f"Weixin configured SPH resolver route created MP3. Probe report: {report_path}")
        return True

    if try_weixin_authorized_link(link, output):
        report = {
            "target": "weixin",
            "link": link,
            "short_uri": short_uri,
            "candidate_media_urls": ["justone-authorized-link"],
            "attempts": [*attempts, {"name": "justone-link", "status": "created-mp3"}],
        }
        report_path = save_report("weixin_direct_link_probe", report)
        print(f"Weixin authorized JustOne link route created MP3. Probe report: {report_path}")
        return True

    status, first = fetch_weixin_feed({"baseReq": {"generalToken": ""}, "shortUri": short_uri}, short_uri)
    attempts.append({"name": "shortUri", "status": status, "response": first})
    urls = media_urls(first)

    scene = first.get("data", {}).get("sceneInfo") if isinstance(first, dict) else {}
    export_id = scene.get("dynamicExportId") if isinstance(scene, dict) else None
    if not urls and export_id:
        status, second = fetch_weixin_feed({"baseReq": {"generalToken": ""}, "exportId": export_id}, short_uri)
        attempts.append({"name": "exportId", "status": status, "response": second})
        urls = media_urls(second)

    report = {
        "target": "weixin",
        "link": link,
        "short_uri": short_uri,
        "candidate_media_urls": urls,
        "attempts": attempts,
    }
    report_path = save_report("weixin_direct_link_probe", report)

    if urls:
        convert(urls[0], output)
        print(f"Weixin direct media found. Probe report: {report_path}")
        return True

    if try_weixin_authorized_export_id(export_id, output):
        print(f"Weixin authorized exportId route created MP3. Probe report: {report_path}")
        return True

    print(f"Weixin direct probe found no media URL. Probe report: {report_path}")
    return False


def parse_songy_course_id(link: str) -> str:
    parsed = urllib.parse.urlparse(link)
    candidates = [parsed.query, parsed.fragment.split("?", 1)[1] if "?" in parsed.fragment else ""]
    for query_text in candidates:
        query = urllib.parse.parse_qs(query_text)
        if query.get("course_id"):
            return query["course_id"][0]
    match = re.search(r"course[_/-]?id[=/](\d+)|courses?/(\d+)", link)
    if match:
        return next(group for group in match.groups() if group)
    raise SystemExit(f"Cannot identify Songy course_id from {link}")


def fetch_songy(path: str, token: str = "") -> tuple[int, Any]:
    headers = {"Origin": "https://webapp.songy.info", "Referer": "https://webapp.songy.info/"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return request_json(f"{SONGY_API_BASE}{path}", headers=headers)


def direct_songy(link: str, output: Path, token: str = "") -> bool:
    course_id = parse_songy_course_id(link)
    attempts: list[dict[str, Any]] = []
    urls: list[str] = []

    for path in (f"/v2/courses/{course_id}", f"/v2/courses/{course_id}/contents"):
        status, payload = fetch_songy(path, token)
        attempts.append({"path": path, "status": status, "response": payload})
        urls.extend(media_urls(payload))

    unique = []
    seen = set()
    for url in sorted(urls, key=score_url):
        if url not in seen:
            seen.add(url)
            unique.append(url)

    report = {
        "target": "songy",
        "link": link,
        "course_id": course_id,
        "used_token": bool(token),
        "candidate_media_urls": unique,
        "attempts": attempts,
    }
    report_path = save_report("songy_direct_link_probe", report)

    if unique:
        convert(unique[0], output)
        print(f"Songy direct media found. Probe report: {report_path}")
        return True

    print(f"Songy direct probe found no media URL. Probe report: {report_path}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weixin-link", default=DEFAULT_WEIXIN)
    parser.add_argument("--songy-link", default=DEFAULT_SONGY)
    parser.add_argument("--weixin-output", default=str(OUTPUTS / "weixin_video_channel.mp3"))
    parser.add_argument("--songy-output", default=str(OUTPUTS / "songy_course_784.mp3"))
    parser.add_argument("--songy-token", default=os.environ.get("SONGY_BEARER_TOKEN", ""))
    parser.add_argument("--only", choices=["all", "weixin", "songy"], default="all")
    args = parser.parse_args()

    made_any = False
    if args.only in ("all", "weixin"):
        made_any = direct_weixin(args.weixin_link, Path(args.weixin_output).expanduser().resolve()) or made_any
    if args.only in ("all", "songy"):
        made_any = direct_songy(
            args.songy_link,
            Path(args.songy_output).expanduser().resolve(),
            args.songy_token,
        ) or made_any

    if not made_any:
        print("No direct MP3 was created from the remaining public links.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
