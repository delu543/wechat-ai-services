#!/usr/bin/env python3
"""Classify redacted Weixin candidate media URLs from preserved source snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from outputs.authorized_fetchers import weixin_multi_open_capture as capture  # noqa: E402


def classify_first_bytes(data: bytes) -> str:
    if not data:
        return "empty"
    if data.startswith(b"#EXTM3U"):
        return "hls_playlist"
    if b"ftyp" in data[:64]:
        return "mp4_container"
    if data[:3] == b"ID3" or data[:2] == b"\xff\xfb":
        return "mp3_audio"
    stripped = data[:512].lstrip()
    if stripped.startswith((b"{", b"[", b"<!DOCTYPE", b"<html", b"<?xml")):
        return "text_or_json"
    printable = sum(32 <= byte <= 126 or byte in (9, 10, 13) for byte in data[:256])
    if data and printable > min(len(data), 256) * 0.85:
        return "text_or_json"
    return "binary_unknown_or_encrypted"


def query_key_sample(url: str) -> list[str]:
    parsed = urlsplit(url)
    keys = sorted({part.split("=", 1)[0] for part in parsed.query.split("&") if part})
    return keys[:20]


def path_hint(url: str) -> str:
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return "/"
    return "/" + "/".join(parts[:3]) + ("/..." if len(parts) > 3 else "")


def nearby_numeric_fingerprints(text: str, start: int, end: int, *, radius: int = 160) -> list[dict[str, int | str]]:
    context = text[max(0, start - radius) : min(len(text), end + radius)]
    fingerprints: list[dict[str, int | str]] = []
    seen: set[str] = set()
    for number in re.findall(r"\b\d{6,20}\b", context):
        if number in seen:
            continue
        seen.add(number)
        fingerprints.append(
            {
                "numeric_sha256_12": hashlib.sha256(number.encode("utf-8")).hexdigest()[:12],
                "digits": len(number),
            }
        )
    return fingerprints[:20]


def snapshot_reports(capture_dir: Path) -> list[Path]:
    return sorted(capture_dir.glob("round-*/source-snapshots.json"))


def extract_candidates(capture_dir: Path) -> dict[str, dict]:
    candidates: dict[str, dict] = {}
    for report in snapshot_reports(capture_dir):
        payload = json.loads(report.read_text(encoding="utf-8"))
        round_name = report.parent.name
        for snapshot in payload.get("snapshots") or []:
            path = Path(str(snapshot.get("source_snapshot_path") or ""))
            if not path.exists() or not path.is_file():
                continue
            text = path.read_bytes().decode("utf-8", errors="ignore")
            for regex in (capture.RAW_URL_RE, capture.ENCODED_URL_RE):
                for match in regex.finditer(text):
                    url = capture.clean_url(match.group(0))
                    if not any(
                        marker in url
                        for marker in (
                            "finder.video.qq.com",
                            "wxapp.tc.qq.com",
                            "wximg.wxs.qq.com",
                            "stodownload",
                            "snscosdownload",
                            ".m3u8",
                            ".mp4",
                        )
                    ):
                        continue
                    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
                    parsed = urlsplit(url)
                    item = candidates.setdefault(
                        url_hash,
                        {
                            "_url": url,
                            "hash": url_hash,
                            "redacted_url": capture.redact_url(url),
                            "host": parsed.netloc,
                            "path_hint": path_hint(url),
                            "query_key_count": len(query_key_sample(url)),
                            "query_keys_sample": query_key_sample(url),
                            "rounds": set(),
                            "snapshot_count": 0,
                            "source_path_redacted_sample": set(),
                            "nearby_numeric_candidates": [],
                        },
                    )
                    item["rounds"].add(round_name)
                    item["snapshot_count"] += 1
                    source_redacted = str(snapshot.get("source_path_redacted") or "")
                    if source_redacted:
                        item["source_path_redacted_sample"].add(source_redacted)
                    item["nearby_numeric_candidates"].extend(
                        nearby_numeric_fingerprints(text, match.start(), match.end())
                    )
    return candidates


def probe_url(url: str, *, timeout: int = 10) -> dict:
    result = {
        "range_status": None,
        "content_type": None,
        "content_length": None,
        "content_range": None,
        "accept_ranges": None,
        "first_bytes_class": None,
        "first_bytes_hex": None,
        "error_class": None,
    }
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Range": "bytes=0-4095"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(4096)
            result.update(
                {
                    "range_status": response.status,
                    "content_type": response.headers.get("Content-Type"),
                    "content_length": response.headers.get("Content-Length"),
                    "content_range": response.headers.get("Content-Range"),
                    "accept_ranges": response.headers.get("Accept-Ranges"),
                    "first_bytes_class": classify_first_bytes(body),
                    "first_bytes_hex": body[:16].hex(),
                }
            )
    except urllib.error.HTTPError as exc:
        body = exc.read(512)
        result.update(
            {
                "range_status": exc.code,
                "content_type": exc.headers.get("Content-Type"),
                "content_length": exc.headers.get("Content-Length"),
                "content_range": exc.headers.get("Content-Range"),
                "accept_ranges": exc.headers.get("Accept-Ranges"),
                "first_bytes_class": classify_first_bytes(body),
                "first_bytes_hex": body[:16].hex(),
                "error_class": "http_error",
            }
        )
    except Exception as exc:
        result["error_class"] = type(exc).__name__
    return result


def redacted_candidate(item: dict, *, probe: bool, timeout: int) -> dict:
    raw_url = str(item.pop("_url"))
    output = dict(item)
    output["rounds"] = sorted(output["rounds"])
    output["source_path_redacted_sample"] = sorted(output["source_path_redacted_sample"])[:5]
    deduped_numbers: list[dict[str, int | str]] = []
    seen: set[tuple[str, int]] = set()
    for number in output["nearby_numeric_candidates"]:
        key = (str(number.get("numeric_sha256_12") or ""), int(number.get("digits") or 0))
        if key in seen:
            continue
        seen.add(key)
        deduped_numbers.append(number)
    output["nearby_numeric_candidates"] = deduped_numbers[:20]
    if probe:
        output.update(probe_url(raw_url, timeout=timeout))
    return output


def build_classification(capture_dir: Path, *, probe: bool = True, timeout: int = 10) -> dict:
    candidates = extract_candidates(capture_dir)
    results = [
        redacted_candidate(item, probe=probe, timeout=timeout)
        for _url_hash, item in sorted(candidates.items())
    ]
    return {
        "capture_dir": str(capture_dir),
        "source_snapshot_report_count": len(snapshot_reports(capture_dir)),
        "unique_candidate_url_count": len(results),
        "probe_enabled": probe,
        "probe_results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-probe", action="store_true")
    parser.add_argument("--timeout", type=int, default=10)
    args = parser.parse_args(argv)

    report = build_classification(args.capture_dir, probe=not args.no_probe, timeout=args.timeout)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(args.output), "unique_candidate_url_count": report["unique_candidate_url_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
