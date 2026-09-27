#!/usr/bin/env python3
"""Convert a Songy HAR/JSON/text artifact to MP3.

The artifact can contain direct media URLs, a saved contents payload, or an
authorized Bearer token captured from a company/test session. Tokens are used
in memory and are never printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

from extract_media_from_artifact import MEDIA_PATTERN, score, walk
from songy_token_to_mp3 import MEDIA_EXTS, MEDIA_KEYS, score_url


TOKEN_PATTERN = re.compile(r"\bBearer\s+([A-Za-z0-9._~+/=-]{12,})", re.I)
RAW_TOKEN_KEYS = {
    "token",
    "access_token",
    "auth_token",
    "authorization",
    "id_token",
    "permanent_code",
}
DEFAULT_API_BASE = "https://bandu-api.songy.info"
URL_PATTERN = re.compile(r"https?://[^\s\"'<>)]+", re.I)
MEDIA_CONTEXT_KEYS = {
    "media_url",
    "media_urls",
    "raw_url",
    "download_url",
    "play_url",
    "playurl",
    "url",
    "src",
    "href",
    "currentsrc",
}


def load_artifact(path: Path) -> tuple[str, Any | None]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        return raw, json.loads(raw)
    except Exception:
        return raw, None


def direct_media_urls(raw: str, payload: Any | None) -> list[str]:
    urls = walk(payload) if payload is not None else []
    contextual = contextual_media_urls(payload)
    urls.extend(contextual)
    urls.extend(match.group(0).replace("\\/", "/") for match in MEDIA_PATTERN.finditer(raw))
    unique: list[str] = []
    seen = set()
    for url in urls:
        cleaned = url.strip().strip('",')
        lower = cleaned.lower().split("?", 1)[0]
        if cleaned and cleaned not in seen and (
            MEDIA_PATTERN.search(cleaned) or lower.endswith(MEDIA_EXTS) or cleaned in contextual
        ):
            unique.append(cleaned)
            seen.add(cleaned)
    unique.sort(key=score)
    return unique


def contextual_media_urls(value: Any, parent_key: str = "") -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        media_context = dict_has_media_content_type(value)
        for key, item in value.items():
            normalized = str(key).replace("-", "_").lower()
            if isinstance(item, str):
                for url in urls_from_string(item):
                    if should_accept_context_url(normalized, parent_key, media_context):
                        urls.append(url)
            urls.extend(contextual_media_urls(item, normalized))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                for url in urls_from_string(item):
                    if parent_key in MEDIA_CONTEXT_KEYS and parent_key != "url":
                        urls.append(url)
            urls.extend(contextual_media_urls(item, parent_key))
    return urls


def dict_has_media_content_type(value: dict[str, Any]) -> bool:
    candidates: list[str] = []
    for key in ("content_type", "contentType", "mime", "mimeType"):
        item = value.get(key)
        if isinstance(item, str):
            candidates.append(item)
    headers = value.get("headers")
    if isinstance(headers, dict):
        for key, item in headers.items():
            if str(key).lower() == "content-type" and isinstance(item, str):
                candidates.append(item)
    return any(is_media_content_type(item) for item in candidates)


def is_media_content_type(value: str) -> bool:
    lower = value.lower()
    return lower.startswith(("audio/", "video/")) or "mpegurl" in lower or "octet-stream" in lower


def should_accept_context_url(key: str, parent_key: str, media_context: bool) -> bool:
    if key in MEDIA_CONTEXT_KEYS and (media_context or key != "url"):
        return True
    if parent_key in MEDIA_CONTEXT_KEYS and parent_key != "url":
        return True
    if media_context and key in {"url", "src", "href", "request_url"}:
        return True
    return any(part in key for part in ("media", "audio", "video", "download", "raw"))


def urls_from_string(value: str) -> list[str]:
    return [match.group(0).replace("\\/", "/").strip('",') for match in URL_PATTERN.finditer(value)]


def bearer_tokens(raw: str, payload: Any | None) -> list[str]:
    tokens = TOKEN_PATTERN.findall(raw)
    if isinstance(payload, dict) and isinstance(payload.get("log"), dict):
        for entry in payload["log"].get("entries") or []:
            request = entry.get("request") or {}
            response = entry.get("response") or {}
            for header in request.get("headers") or []:
                name = str(header.get("name", ""))
                value = str(header.get("value", ""))
                if name.lower() == "authorization":
                    tokens.extend(TOKEN_PATTERN.findall(value))
                    if value.lower().startswith("bearer "):
                        tokens.append(value.split(None, 1)[1])
            content = response.get("content") or {}
            text = decode_har_content(content)
            if isinstance(text, str):
                try:
                    body = json.loads(text)
                    tokens.extend(raw_tokens_from_payload(body))
                except Exception:
                    tokens.extend(TOKEN_PATTERN.findall(text))
    if payload is not None:
        tokens.extend(raw_tokens_from_payload(payload))
    unique: list[str] = []
    seen = set()
    for token in tokens:
        cleaned = clean_token(token)
        if cleaned and cleaned not in seen:
            unique.append(cleaned)
            seen.add(cleaned)
    return unique


def decode_har_content(content: dict[str, Any]) -> str:
    text = content.get("text")
    if not isinstance(text, str):
        return ""
    if content.get("encoding") == "base64":
        try:
            return base64.b64decode(text).decode("utf-8", "replace")
        except Exception:
            return ""
    return text


def raw_tokens_from_payload(value: Any) -> list[str]:
    tokens: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).replace("-", "_").lower()
            if normalized in RAW_TOKEN_KEYS and isinstance(item, str):
                tokens.extend(token_candidates_from_string(item))
            tokens.extend(raw_tokens_from_payload(item))
    elif isinstance(value, list):
        for item in value:
            tokens.extend(raw_tokens_from_payload(item))
    elif isinstance(value, str):
        tokens.extend(TOKEN_PATTERN.findall(value))
        stripped = value.strip()
        if looks_like_json(stripped):
            try:
                tokens.extend(raw_tokens_from_payload(json.loads(stripped)))
            except Exception:
                pass
    return tokens


def token_candidates_from_string(value: str) -> list[str]:
    candidates = TOKEN_PATTERN.findall(value)
    stripped = clean_token(value)
    if stripped and len(stripped) >= 12:
        candidates.append(stripped)
    return candidates


def clean_token(value: str) -> str:
    token = value.strip().strip('"').strip("'")
    if token.lower().startswith("bearer "):
        token = token.split(None, 1)[1].strip()
    if token.startswith("{") or token.startswith("["):
        return ""
    if any(space in token for space in ("\n", "\r", "\t", " ")):
        return ""
    return token


def looks_like_json(value: str) -> bool:
    return (value.startswith("{") and value.endswith("}")) or (
        value.startswith("[") and value.endswith("]")
    )


def fetch_contents(api_base: str, course_id: str, token: str) -> Any:
    req = urllib.request.Request(
        f"{api_base.rstrip('/')}/v2/courses/{course_id}/contents",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Origin": "https://webapp.songy.info",
            "Referer": "https://webapp.songy.info/",
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def media_from_contents(payload: Any) -> list[str]:
    urls: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in MEDIA_KEYS and isinstance(item, str) and item:
                    urls.append(item)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    unique: list[str] = []
    seen = set()
    for url in urls:
        lower = url.lower()
        if url not in seen and (lower.split("?", 1)[0].endswith(MEDIA_EXTS) or "m3u8" in lower):
            unique.append(url)
            seen.add(url)
    unique.sort(key=score_url)
    return unique


def convert(url: str, output: Path) -> None:
    converter = Path(__file__).with_name("media_url_to_mp3.py")
    subprocess.run([sys.executable, str(converter), url, "--output", str(output)], check=True)


def try_convert_urls(urls: list[str], output: Path, label: str) -> tuple[bool, Exception | None]:
    last_error: Exception | None = None
    for index, url in enumerate(urls, start=1):
        try:
            print(f"Trying {label} media URL {index}/{len(urls)}.")
            convert(url, output)
            return True, None
        except Exception as exc:
            last_error = exc
            print(f"Media URL {index} did not convert; trying next available route.")
    return False, last_error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact")
    parser.add_argument("--course-id", default="784")
    parser.add_argument("--output", required=True)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--list-only", action="store_true")
    args = parser.parse_args()

    artifact = Path(args.artifact).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    raw, payload = load_artifact(artifact)

    direct = direct_media_urls(raw, payload)
    last_error: Exception | None = None
    if direct:
        print(f"Found {len(direct)} direct media URL(s).")
        if args.list_only:
            return 0
        ok, last_error = try_convert_urls(direct, output, "direct")
        if ok:
            return 0

    tokens = bearer_tokens(raw, payload)
    if not tokens:
        if last_error is not None:
            raise SystemExit(f"Direct media URL candidates did not convert: {last_error}")
        raise SystemExit("No Songy media URL or Bearer token found in artifact.")
    print(f"Found {len(tokens)} Bearer token candidate(s); token values are not printed.")
    if args.list_only:
        return 0

    for token in tokens:
        try:
            contents = fetch_contents(args.api_base, args.course_id, token)
            urls = media_from_contents(contents)
            if not urls:
                raise RuntimeError("Authorized contents response contained no media URL.")
            ok, convert_error = try_convert_urls(urls, output, "authorized contents")
            if ok:
                return 0
            raise RuntimeError(f"Authorized contents media URLs did not convert: {convert_error}")
        except Exception as exc:
            last_error = exc
    raise SystemExit(f"Bearer token candidates did not produce media: {last_error}")


if __name__ == "__main__":
    raise SystemExit(main())
