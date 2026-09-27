from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


MEDIA_URL_RE = re.compile(
    r"https?://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]{12,}"
    r"(?:stodownload|snsvideodownload|snscosdownload|\.m3u8|\.mp4)"
    r"[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]*",
    re.I,
)
ENCODED_MEDIA_URL_RE = re.compile(
    r"https?%3A%2F%2F[A-Za-z0-9._~%:/?#\[\]@!$&()*+,;=%-]{12,}"
    r"(?:stodownload|snsvideodownload|snscosdownload|\\.m3u8|\\.mp4)"
    r"[A-Za-z0-9._~%:/?#\[\]@!$&()*+,;=%-]*",
    re.I,
)
DECODE_KEY_RE = re.compile(
    r"(?i)(?:^|[^A-Za-z0-9_])(?:decode[_-]?key|decodeKey|decodekey)"
    r"(?:\\?\"|\\?'|\s)*[:=](?:\\?\"|\\?'|\s)*([A-Za-z0-9_-]{6,96})"
)
KEY_MARKER_FIELD_RE = re.compile(
    r"(?i)(?:^|[^A-Za-z0-9_])\\?[\"']?"
    r"(?P<field>"
    r"decode[_-]?key|decrypt[_-]?key|media[_-]?decode[_-]?key|"
    r"file[_-]?decode[_-]?key|video[_-]?decode[_-]?key|"
    r"play[_-]?decode[_-]?key|drm[_-]?key|media[_-]?key"
    r")"
    r"\\?[\"']?(?:\s)*[:=](?:\\?[\"']?|\s)*"
    r"(?P<value>[A-Za-z0-9_+=/-]{6,256})"
)
PAIRABLE_KEY_FIELD_RE = re.compile(
    r"(?i)(?:^|[^A-Za-z0-9_])\\?[\"']?"
    r"(?P<field>"
    r"decode[_-]?key|decrypt[_-]?key|media[_-]?decode[_-]?key|"
    r"file[_-]?decode[_-]?key|video[_-]?decode[_-]?key|"
    r"play[_-]?decode[_-]?key"
    r")"
    r"\\?[\"']?(?:\s)*[:=](?:\\?[\"']?|\s)*"
    r"(?P<value>[A-Za-z0-9_+=/-]{6,256})"
)
NUMERIC_KEY_FIELD_RE = re.compile(
    r"(?i)(?:^|[^A-Za-z0-9_])\\?[\"']?"
    r"(?P<field>key|decode[_-]?key|decrypt[_-]?key|media[_-]?key|video[_-]?decrypt[_-]?key)"
    r"\\?[\"']?(?:\s)*[:=](?:\\?[\"']?|\s)*"
    r"(?P<value>\d{1,20})(?=[^A-Za-z0-9_]|$)"
)
ENC_LIMIT_FIELD_RE = re.compile(
    r"(?i)(?:^|[^A-Za-z0-9_])\\?[\"']?"
    r"(?P<field>enc[_-]?limit|enc[_-]?len|encrypt[_-]?len|encrypted[_-]?len)"
    r"\\?[\"']?(?:\s)*[:=](?:\\?[\"']?|\s)*"
    r"(?P<value>\d{1,20})(?=[^A-Za-z0-9_]|$)"
)
SKIP_URL_HINTS = ("thumb", "cover", "imageview2", "format/webp", ".jpg", ".jpeg", ".png", ".webp")


def clean_text(value: str) -> str:
    cleaned = urllib.parse.unquote(value)
    cleaned = (
        cleaned.replace("\\/", "/")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\u003A", ":")
        .replace("\\u003a", ":")
        .replace("\\u0026", "&")
        .replace("\\u0026amp;", "&")
        .replace("&amp;", "&")
    )
    for sep in ("\x00", "\x01", "\x02", "\x03", "\x04", "\n", "\r", "\t"):
        if sep in cleaned:
            cleaned = cleaned.split(sep, 1)[0]
    for tail in ('"}', '"}', "}',", "'}", "</", '",', "'),", ");"):
        if tail in cleaned:
            cleaned = cleaned.split(tail, 1)[0]
    return cleaned.strip().strip("\"'<>").rstrip(").,;\"'")


def text_variants(data: bytes | str) -> list[str]:
    text = data.decode("utf-8", errors="ignore") if isinstance(data, bytes) else str(data)
    variants = [text]
    decoded = text
    for _ in range(3):
        next_decoded = urllib.parse.unquote(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded
        variants.append(decoded)
    slash_decoded = (
        decoded.replace("\\/", "/")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\u003A", ":")
        .replace("\\u003a", ":")
        .replace("\\u0026", "&")
        .replace("\\u0026amp;", "&")
        .replace("&amp;", "&")
    )
    variants.append(slash_decoded)
    variants.append(clean_text(decoded))
    unique: list[str] = []
    seen: set[str] = set()
    for item in variants:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def redact_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except Exception:
        return "<unparseable-url>"
    path = parsed.path
    if len(path) > 90:
        path = path[:45] + "..." + path[-30:]
    return f"{parsed.scheme}://{parsed.netloc}{path}?<redacted>" if parsed.query else f"{parsed.scheme}://{parsed.netloc}{path}"


def _media_matches(text: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for regex in (MEDIA_URL_RE, ENCODED_MEDIA_URL_RE):
        for match in regex.finditer(text):
            url = clean_text(match.group(0))
            lower = url.lower()
            if not url.startswith(("http://", "https://")):
                continue
            if any(hint in lower for hint in SKIP_URL_HINTS):
                continue
            matches.append({"url": url, "start": match.start(), "end": match.end()})
    return matches


def _decode_key_matches(text: str, *, allow_key_aliases: bool = True) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for match in DECODE_KEY_RE.finditer(text):
        key = clean_text(match.group(1))
        if not key or key.lower() in {"null", "undefined", "false", "true"}:
            continue
        matches.append({"decode_key": key, "key_field": "decodeKey", "start": match.start(), "end": match.end()})
    if not allow_key_aliases:
        return matches
    seen = {(str(item.get("decode_key") or ""), int(item.get("start") or 0), int(item.get("end") or 0)) for item in matches}
    for match in PAIRABLE_KEY_FIELD_RE.finditer(text):
        key = clean_text(str(match.group("value") or ""))
        if not key or key.lower() in {"null", "undefined", "false", "true"}:
            continue
        item_key = (key, match.start(), match.end())
        if item_key in seen:
            continue
        seen.add(item_key)
        matches.append(
            {
                "decode_key": key,
                "key_field": str(match.group("field") or ""),
                "start": match.start(),
                "end": match.end(),
            }
        )
    return matches


def _numeric_key_matches(text: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for match in NUMERIC_KEY_FIELD_RE.finditer(text):
        value = clean_text(str(match.group("value") or ""))
        if not value:
            continue
        key = int(value)
        if key <= 0:
            continue
        matches.append(
            {
                "key": key,
                "key_field": str(match.group("field") or ""),
                "start": match.start(),
                "end": match.end(),
            }
        )
    return matches


def _enc_limit_matches(text: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for match in ENC_LIMIT_FIELD_RE.finditer(text):
        value = clean_text(str(match.group("value") or ""))
        if not value:
            continue
        enc_limit = int(value)
        if enc_limit <= 0:
            continue
        matches.append({"enc_limit": enc_limit, "start": match.start(), "end": match.end()})
    return matches


def _nearest_enc_limit(limits: list[dict[str, Any]], marker_center: int, context_radius: int) -> int:
    if not limits:
        return 131072
    nearest = min(
        limits,
        key=lambda item: abs(marker_center - int((int(item["start"]) + int(item["end"])) / 2)),
    )
    distance = abs(marker_center - int((int(nearest["start"]) + int(nearest["end"])) / 2))
    if distance > context_radius:
        return 131072
    return int(nearest.get("enc_limit") or 131072)


def _nearest_media_marker(urls: list[dict[str, Any]], marker_center: int, context_radius: int) -> dict[str, Any]:
    if not urls:
        return {"nearest_media_url": "", "nearest_media_distance": None, "near_media": False}
    nearest = min(
        urls,
        key=lambda item: abs(marker_center - int((item["start"] + item["end"]) / 2)),
    )
    distance = abs(marker_center - int((nearest["start"] + nearest["end"]) / 2))
    return {
        "nearest_media_url": redact_url(str(nearest.get("url") or "")),
        "nearest_media_distance": distance,
        "near_media": distance <= context_radius,
    }


def _decode_key_marker_matches(text: str, *, path: str, context_radius: int) -> list[dict[str, Any]]:
    urls = _media_matches(text)
    markers: list[dict[str, Any]] = []
    for match in KEY_MARKER_FIELD_RE.finditer(text):
        field_name = str(match.group("field") or "")
        value = clean_text(str(match.group("value") or ""))
        if not value or value.lower() in {"null", "undefined", "false", "true"}:
            continue
        marker_center = int((match.start() + match.end()) / 2)
        nearest = _nearest_media_marker(urls, marker_center, context_radius)
        markers.append(
            {
                "field_name": field_name,
                "value_sha256_12": hashlib.sha256(value.encode("utf-8")).hexdigest()[:12],
                "value_length": len(value),
                "path": path,
                **nearest,
            }
        )
    return markers


def decode_key_marker_inventory_from_text(
    value: bytes | str,
    *,
    path: str = "$",
    context_radius: int = 16000,
) -> dict[str, Any]:
    markers: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for variant in text_variants(value):
        for marker in _decode_key_marker_matches(variant, path=path, context_radius=context_radius):
            marker_key = (
                str(marker.get("path") or ""),
                str(marker.get("field_name") or "").lower(),
                str(marker.get("value_sha256_12") or ""),
                str(marker.get("nearest_media_url") or ""),
            )
            if marker_key in seen:
                continue
            seen.add(marker_key)
            markers.append(marker)
    field_counts: dict[str, int] = {}
    for marker in markers:
        field_name = str(marker.get("field_name") or "")
        field_counts[field_name] = field_counts.get(field_name, 0) + 1
    return {
        "path": path,
        "marker_count": len(markers),
        "near_media_count": sum(1 for marker in markers if marker.get("near_media")),
        "field_counts": dict(sorted(field_counts.items())),
        "markers": markers[:80],
    }


def decode_key_marker_inventory_from_file(
    path: Path,
    *,
    max_read_bytes: int = 80_000_000,
    context_radius: int = 16000,
) -> dict[str, Any]:
    data = path.read_bytes()[:max_read_bytes]
    return decode_key_marker_inventory_from_text(data, path=str(path), context_radius=context_radius)


def merge_decode_key_marker_inventories(inventories: list[dict[str, Any]]) -> dict[str, Any]:
    markers: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for inventory in inventories:
        for marker in inventory.get("markers") or []:
            if not isinstance(marker, dict):
                continue
            marker_key = (
                str(marker.get("path") or ""),
                str(marker.get("field_name") or "").lower(),
                str(marker.get("value_sha256_12") or ""),
                str(marker.get("nearest_media_url") or ""),
            )
            if marker_key in seen:
                continue
            seen.add(marker_key)
            markers.append(marker)
    field_counts: dict[str, int] = {}
    for marker in markers:
        field_name = str(marker.get("field_name") or "")
        field_counts[field_name] = field_counts.get(field_name, 0) + 1
    return {
        "marker_count": len(markers),
        "near_media_count": sum(1 for marker in markers if marker.get("near_media")),
        "field_counts": dict(sorted(field_counts.items())),
        "markers": markers[:120],
    }


def extract_decode_key_pairs_from_text(
    value: bytes | str,
    *,
    path: str = "$",
    context_radius: int = 16000,
    allow_key_aliases: bool = True,
) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for variant in text_variants(value):
        urls = _media_matches(variant)
        keys = _decode_key_matches(variant, allow_key_aliases=allow_key_aliases)
        for key_item in keys:
            key_center = int((key_item["start"] + key_item["end"]) / 2)
            for url_item in urls:
                url_center = int((url_item["start"] + url_item["end"]) / 2)
                if abs(key_center - url_center) > context_radius:
                    continue
                pair_key = (url_item["url"], key_item["decode_key"], path)
                if pair_key in seen:
                    continue
                seen.add(pair_key)
                pairs.append(
                    {
                        "url": url_item["url"],
                        "decode_key": key_item["decode_key"],
                        "key_field": str(key_item.get("key_field") or ""),
                        "path": path,
                        "evidence": "same_local_context",
                    }
                )
    return pairs


def extract_numeric_key_pairs_from_text(
    value: bytes | str,
    *,
    path: str = "$",
    context_radius: int = 16000,
) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for variant in text_variants(value):
        urls = _media_matches(variant)
        keys = _numeric_key_matches(variant)
        limits = _enc_limit_matches(variant)
        for key_item in keys:
            key_center = int((int(key_item["start"]) + int(key_item["end"])) / 2)
            for url_item in urls:
                url_center = int((int(url_item["start"]) + int(url_item["end"])) / 2)
                if abs(key_center - url_center) > context_radius:
                    continue
                pair_key = (str(url_item["url"]), int(key_item["key"]), path)
                if pair_key in seen:
                    continue
                seen.add(pair_key)
                pairs.append(
                    {
                        "url": str(url_item["url"]),
                        "key": int(key_item["key"]),
                        "enc_limit": _nearest_enc_limit(limits, key_center, context_radius),
                        "key_field": str(key_item.get("key_field") or ""),
                        "path": path,
                        "evidence": "same_local_context",
                    }
                )
    return pairs


def extract_decode_key_pairs_from_file(
    path: Path,
    *,
    max_read_bytes: int = 80_000_000,
    context_radius: int = 16000,
    allow_key_aliases: bool = True,
) -> list[dict[str, str]]:
    data = path.read_bytes()[:max_read_bytes]
    return extract_decode_key_pairs_from_text(
        data,
        path=str(path),
        context_radius=context_radius,
        allow_key_aliases=allow_key_aliases,
    )


def extract_numeric_key_pairs_from_file(
    path: Path,
    *,
    max_read_bytes: int = 80_000_000,
    context_radius: int = 16000,
) -> list[dict[str, Any]]:
    data = path.read_bytes()[:max_read_bytes]
    return extract_numeric_key_pairs_from_text(data, path=str(path), context_radius=context_radius)


def redacted_pair_summary(pairs: list[dict[str, str]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for pair in pairs:
        key = str(pair.get("decode_key") or "")
        summary.append(
            {
                "url": redact_url(str(pair.get("url") or "")),
                "decode_key_sha256_12": hashlib.sha256(key.encode("utf-8")).hexdigest()[:12] if key else "",
                "decode_key_length": len(key),
                "key_field": str(pair.get("key_field") or ""),
                "path": str(pair.get("path") or ""),
                "evidence": str(pair.get("evidence") or ""),
            }
        )
    return summary


def redacted_numeric_key_pair_summary(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for pair in pairs:
        key = str(pair.get("key") or "")
        item = {
            "url": redact_url(str(pair.get("url") or "")),
            "numeric_key_sha256_12": hashlib.sha256(key.encode("utf-8")).hexdigest()[:12] if key else "",
            "numeric_key_digits": len(key),
            "enc_limit": int(pair.get("enc_limit") or 131072),
            "key_field": str(pair.get("key_field") or ""),
            "path": str(pair.get("path") or ""),
            "evidence": str(pair.get("evidence") or ""),
        }
        for field in ("expected_bytes", "content_type", "url_sha256_16", "source"):
            value = pair.get(field)
            if value not in (None, ""):
                item[field] = value
        summary.append(item)
    return summary


def write_sensitive_pair_artifact(pairs: list[dict[str, str]], artifact_root: Path, *, label: str) -> Path:
    artifact_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha256(json.dumps(pairs, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    path = artifact_root / f"{stamp}-{label}-{digest}.json"
    path.write_text(json.dumps({"pairs": pairs}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_sensitive_pair_artifact(path: Path) -> list[dict[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    pairs = payload.get("pairs") if isinstance(payload, dict) else None
    return [item for item in pairs if isinstance(item, dict)] if isinstance(pairs, list) else []
