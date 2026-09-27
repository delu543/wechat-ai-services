#!/usr/bin/env python3
"""Probe encrypted Weixin media candidates against local key material.

The report is intentionally sanitized: raw signed URLs and raw key values are
used only in memory during the local probe. Output contains redacted URLs,
field names, lengths, hashes, and first-byte classifications.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from replay_mp3_studio.weixin_decode_key import (  # noqa: E402
    decrypt_weixin_encrypted_bytes,
    decrypt_weixin_numeric_key_bytes,
    decode_key_fingerprint,
    ensure_wasm_assets,
    generate_keystream_via_node,
    numeric_key_fingerprint,
)
from replay_mp3_studio.weixin_source_pairs import (  # noqa: E402
    extract_decode_key_pairs_from_file,
    extract_numeric_key_pairs_from_file,
    redact_url,
)
from outputs.authorized_fetchers import weixin_multi_open_capture as capture  # noqa: E402
from outputs.authorized_fetchers.weixin_candidate_url_classifier import classify_first_bytes  # noqa: E402


STRING_KEY_FIELD_RE = re.compile(
    r"(?i)(?:^|[^A-Za-z0-9_])\\?[\"']?"
    r"(?P<field>decode[_-]?key|decrypt[_-]?key|media[_-]?decode[_-]?key|exportkey)"
    r"\\?[\"']?(?:\s)*[:=](?:\\?[\"']?|\s)*"
    r"(?P<value>[A-Za-z0-9_+=/-]{6,256})"
)


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _key_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _positive_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _content_range_total(value: Any) -> int:
    text = str(value or "")
    if "/" not in text:
        return 0
    return _positive_int(text.rsplit("/", 1)[-1].strip())


def _range_expected_bytes(summary: dict[str, Any]) -> int:
    return _content_range_total(summary.get("content_range")) or _positive_int(summary.get("content_length"))


def _snapshot_paths_from_report(path: Path) -> list[Path]:
    if path.name != "source-snapshots.json":
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    result: list[Path] = []
    for item in payload.get("snapshots") or []:
        if not isinstance(item, dict):
            continue
        snapshot = Path(str(item.get("source_snapshot_path") or "")).expanduser()
        if snapshot.exists() and snapshot.is_file():
            result.append(snapshot.resolve())
    return result


def _iter_files(paths: list[Path]) -> list[Path]:
    files: dict[str, Path] = {}
    for source in paths:
        source = source.expanduser().resolve()
        if not source.exists():
            continue
        if source.is_file():
            files[str(source)] = source
            for snapshot in _snapshot_paths_from_report(source):
                files[str(snapshot)] = snapshot
            continue
        for path in source.rglob("*"):
            if path.is_file():
                files[str(path)] = path
                for snapshot in _snapshot_paths_from_report(path):
                    files[str(snapshot)] = snapshot
    return sorted(files.values())


def _read_text(path: Path, max_read_bytes: int) -> str:
    return path.read_bytes()[:max_read_bytes].decode("utf-8", errors="ignore")


def collect_media_urls(files: list[Path], *, max_read_bytes: int) -> list[dict[str, str]]:
    seen: set[str] = set()
    urls: list[dict[str, str]] = []
    for path in files:
        try:
            text = _read_text(path, max_read_bytes)
        except OSError:
            continue
        for regex in (capture.RAW_URL_RE, capture.ENCODED_URL_RE):
            for match in regex.finditer(text):
                url = capture.clean_url(match.group(0))
                lower = url.lower()
                if not url.startswith(("http://", "https://")):
                    continue
                if not (
                    "stodownload" in lower
                    or "snsvideodownload" in lower
                    or "snscosdownload" in lower
                    or "finder.video.qq.com" in lower
                ):
                    continue
                if url in seen:
                    continue
                seen.add(url)
                urls.append({"url": url, "path": str(path)})
    return urls


def collect_decode_pairs(files: list[Path], *, max_read_bytes: int) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for path in files:
        try:
            file_pairs = extract_decode_key_pairs_from_file(path, max_read_bytes=max_read_bytes)
        except Exception:
            continue
        for pair in file_pairs:
            key = (str(pair.get("url") or ""), str(pair.get("decode_key") or ""), str(pair.get("path") or ""))
            if key in seen:
                continue
            seen.add(key)
            pairs.append(pair)
    return pairs


def collect_numeric_pairs(files: list[Path], *, max_read_bytes: int) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for path in files:
        try:
            file_pairs = extract_numeric_key_pairs_from_file(path, max_read_bytes=max_read_bytes)
        except Exception:
            continue
        for pair in file_pairs:
            try:
                numeric = int(pair.get("key") or 0)
            except (TypeError, ValueError):
                continue
            key = (str(pair.get("url") or ""), numeric, str(pair.get("path") or ""))
            if key in seen:
                continue
            seen.add(key)
            pairs.append(pair)
    return pairs


def collect_string_key_candidates(files: list[Path], *, max_read_bytes: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for path in files:
        try:
            text = _read_text(path, max_read_bytes)
        except OSError:
            continue
        for match in STRING_KEY_FIELD_RE.finditer(text):
            value = capture.clean_url(str(match.group("value") or ""))
            if not value or value.lower() in {"null", "undefined", "false", "true"}:
                continue
            item_key = (str(path), str(match.group("field") or "").lower(), _key_hash(value))
            if item_key in seen:
                continue
            seen.add(item_key)
            candidates.append(
                {
                    "field": str(match.group("field") or ""),
                    "value": value,
                    "path": str(path),
                }
            )
    return candidates


def collect_nearby_numeric_candidates(
    files: list[Path],
    *,
    max_read_bytes: int,
    radius: int = 1024,
) -> dict[str, list[dict[str, Any]]]:
    by_url_hash: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, int, str]] = set()
    for path in files:
        try:
            text = _read_text(path, max_read_bytes)
        except OSError:
            continue
        for regex in (capture.RAW_URL_RE, capture.ENCODED_URL_RE):
            for match in regex.finditer(text):
                url = capture.clean_url(match.group(0))
                lower = url.lower()
                if not (
                    "stodownload" in lower
                    or "snsvideodownload" in lower
                    or "snscosdownload" in lower
                    or "finder.video.qq.com" in lower
                ):
                    continue
                url_hash = _url_hash(url)
                context = text[max(0, match.start() - radius) : min(len(text), match.end() + radius)]
                enc_limit_matches = re.findall(
                    r"(?i)(?:enc[_-]?limit|enc[_-]?len|encrypt[_-]?len|encrypted[_-]?len)[^0-9]{0,16}(\d{1,20})",
                    context,
                )
                enc_limits = [int(value) for value in enc_limit_matches if int(value) > 0]
                if not enc_limits:
                    enc_limits = [131072, 65536, 4096]
                for number in re.findall(r"\b\d{6,20}\b", context):
                    numeric = int(number)
                    if numeric <= 0:
                        continue
                    for enc_limit in enc_limits[:3]:
                        item_key = (url_hash, numeric, str(path), enc_limit)
                        if item_key in seen:
                            continue
                        seen.add(item_key)
                        by_url_hash.setdefault(url_hash, []).append(
                            {
                                "key": numeric,
                                "enc_limit": enc_limit,
                                "path": str(path),
                            }
                        )
    return by_url_hash


def range_probe(url: str, *, size: int, timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://channels.weixin.qq.com/",
            "Range": f"bytes=0-{max(size - 1, 0)}",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(size)
            status = int(response.status)
            headers = response.headers
    except urllib.error.HTTPError as exc:
        body = exc.read(size)
        status = int(exc.code)
        headers = exc.headers
    except Exception as exc:
        return {
            "ok": False,
            "error_class": type(exc).__name__,
            "prefix": b"",
            "summary": {"error_class": type(exc).__name__},
        }
    summary = {
        "range_status": status,
        "content_type": headers.get("Content-Type"),
        "content_length": headers.get("Content-Length"),
        "content_range": headers.get("Content-Range"),
        "first_bytes_class": classify_first_bytes(body),
        "first16_hex": body[:16].hex(),
        "prefix_bytes": len(body),
        "prefix_sha256_12": hashlib.sha256(body).hexdigest()[:12] if body else "",
    }
    return {"ok": bool(body), "prefix": body, "summary": summary}


def _decrypt_classification(payload: bytes) -> dict[str, Any]:
    return {
        "first_bytes_class": classify_first_bytes(payload),
        "first16_hex": payload[:16].hex(),
        "mp4_header": len(payload) >= 8 and payload[4:8] == b"ftyp",
    }


def _prepare_wasm_dir(work_dir: Path, wasm_dir: Path | None) -> Path:
    target = work_dir / "wechat-wasm"
    if wasm_dir:
        target.mkdir(parents=True, exist_ok=True)
        for filename in ("wasm_video_decode.js", "wasm_video_decode.wasm"):
            source = wasm_dir / filename
            if source.exists() and not (target / filename).exists():
                shutil.copy2(source, target / filename)
    ensure_wasm_assets(target)
    return target


def test_string_key(
    prefix: bytes,
    decode_key: str,
    *,
    work_dir: Path,
    wasm_dir: Path,
    timeout: int,
) -> dict[str, Any]:
    keystream = generate_keystream_via_node(
        decode_key,
        wasm_dir=wasm_dir,
        work_dir=work_dir,
        timeout=timeout,
    )
    return _decrypt_classification(decrypt_weixin_encrypted_bytes(prefix, keystream))


def test_numeric_key(prefix: bytes, key: int, *, enc_limit: int) -> dict[str, Any]:
    return _decrypt_classification(decrypt_weixin_numeric_key_bytes(prefix, key, enc_limit=enc_limit))


def build_probe_report(
    paths: list[Path],
    *,
    work_dir: Path,
    wasm_dir: Path | None = None,
    sensitive_artifact_dir: Path | None = None,
    max_read_bytes: int = 40_000_000,
    range_bytes: int = 131_072,
    timeout: int = 20,
    max_urls: int = 8,
    max_heuristic_keys: int = 8,
    max_heuristic_numeric_keys: int = 12,
) -> dict[str, Any]:
    files = _iter_files(paths)
    urls = collect_media_urls(files, max_read_bytes=max_read_bytes)[:max_urls]
    decode_pairs = collect_decode_pairs(files, max_read_bytes=max_read_bytes)
    numeric_pairs = collect_numeric_pairs(files, max_read_bytes=max_read_bytes)
    heuristic_keys = collect_string_key_candidates(files, max_read_bytes=max_read_bytes)[:max_heuristic_keys]
    heuristic_numeric_by_url = collect_nearby_numeric_candidates(files, max_read_bytes=max_read_bytes)
    heuristic_numeric_count = sum(len(items) for items in heuristic_numeric_by_url.values())
    prepared_wasm_dir: Path | None = None
    probes: list[dict[str, Any]] = []
    success = False
    successful_numeric_pairs: list[dict[str, Any]] = []
    for url_item in urls:
        url = str(url_item["url"])
        url_hash = _url_hash(url)
        range_result = range_probe(url, size=range_bytes, timeout=timeout)
        prefix = bytes(range_result.get("prefix") or b"")
        attempts: list[dict[str, Any]] = []
        if prefix and classify_first_bytes(prefix) != "mp4_container":
            for pair in decode_pairs:
                if str(pair.get("url") or "") != url:
                    continue
                try:
                    if prepared_wasm_dir is None:
                        prepared_wasm_dir = _prepare_wasm_dir(work_dir, wasm_dir)
                    classified = test_string_key(
                        prefix,
                        str(pair.get("decode_key") or ""),
                        work_dir=work_dir,
                        wasm_dir=prepared_wasm_dir,
                        timeout=timeout,
                    )
                    success = success or bool(classified.get("mp4_header"))
                    error_class = ""
                except Exception as exc:
                    classified = {}
                    error_class = type(exc).__name__
                attempts.append(
                    {
                        "kind": "decode_key_pair",
                        "key_field": str(pair.get("key_field") or ""),
                        **decode_key_fingerprint(str(pair.get("decode_key") or "")),
                        "result": classified,
                        "error_class": error_class,
                    }
                )
            for pair in numeric_pairs:
                if str(pair.get("url") or "") != url:
                    continue
                try:
                    key = int(pair.get("key") or 0)
                    enc_limit = int(pair.get("enc_limit") or 131072)
                    classified = test_numeric_key(prefix, key, enc_limit=enc_limit)
                    success = success or bool(classified.get("mp4_header"))
                    if classified.get("mp4_header"):
                        range_summary = range_result.get("summary") or {}
                        successful_numeric_pairs.append(
                            {
                                "url": url,
                                "key": key,
                                "encLimit": enc_limit,
                                "expected_bytes": _range_expected_bytes(range_summary),
                                "content_type": str(range_summary.get("content_type") or ""),
                                "url_sha256_16": url_hash,
                                "source": "numeric_key_pair",
                            }
                        )
                    error_class = ""
                except Exception as exc:
                    classified = {}
                    error_class = type(exc).__name__
                attempts.append(
                    {
                        "kind": "numeric_key_pair",
                        "key_field": str(pair.get("key_field") or ""),
                        **numeric_key_fingerprint(pair.get("key") or ""),
                        "enc_limit": int(pair.get("enc_limit") or 131072),
                        "result": classified,
                        "error_class": error_class,
                    }
                )
            if not attempts:
                for key_item in heuristic_keys:
                    try:
                        if prepared_wasm_dir is None:
                            prepared_wasm_dir = _prepare_wasm_dir(work_dir, wasm_dir)
                        classified = test_string_key(
                            prefix,
                            str(key_item.get("value") or ""),
                            work_dir=work_dir,
                            wasm_dir=prepared_wasm_dir,
                            timeout=timeout,
                        )
                        success = success or bool(classified.get("mp4_header"))
                        error_class = ""
                    except Exception as exc:
                        classified = {}
                        error_class = type(exc).__name__
                    attempts.append(
                        {
                            "kind": "heuristic_string_key",
                            "key_field": str(key_item.get("field") or ""),
                            "decode_key_sha256_12": _key_hash(str(key_item.get("value") or "")),
                            "decode_key_length": len(str(key_item.get("value") or "")),
                            "result": classified,
                            "error_class": error_class,
                        }
                    )
                for key_item in heuristic_numeric_by_url.get(url_hash, [])[:max_heuristic_numeric_keys]:
                    try:
                        key = int(key_item.get("key") or 0)
                        enc_limit = int(key_item.get("enc_limit") or 131072)
                        classified = test_numeric_key(prefix, key, enc_limit=enc_limit)
                        success = success or bool(classified.get("mp4_header"))
                        if classified.get("mp4_header"):
                            range_summary = range_result.get("summary") or {}
                            successful_numeric_pairs.append(
                                {
                                    "url": url,
                                    "key": key,
                                    "encLimit": enc_limit,
                                    "expected_bytes": _range_expected_bytes(range_summary),
                                    "content_type": str(range_summary.get("content_type") or ""),
                                    "url_sha256_16": url_hash,
                                    "source": "heuristic_numeric_key",
                                }
                            )
                        error_class = ""
                    except Exception as exc:
                        classified = {}
                        error_class = type(exc).__name__
                    attempts.append(
                        {
                            "kind": "heuristic_numeric_key",
                            "key_field": "nearby_numeric",
                            **numeric_key_fingerprint(key_item.get("key") or ""),
                            "enc_limit": int(key_item.get("enc_limit") or 131072),
                            "result": classified,
                            "error_class": error_class,
                        }
                    )
        probes.append(
            {
                "url": redact_url(url),
                "url_sha256_16": url_hash,
                "source_path_redacted": capture.safe_rel(Path(url_item["path"])),
                "http_range": range_result.get("summary") or {},
                "attempt_count": len(attempts),
                "attempts": attempts,
            }
        )
    report = {
        "source_file_count": len(files),
        "candidate_url_count": len(urls),
        "decode_key_pair_count": len(decode_pairs),
        "numeric_key_pair_count": len(numeric_pairs),
        "heuristic_string_key_count": len(heuristic_keys),
        "heuristic_numeric_key_count": heuristic_numeric_count,
        "result": "mp4_header_decrypted" if success else "encrypted_candidate_key_missing_or_mismatch",
        "raw_values_in_report": False,
        "probes": probes,
    }
    if successful_numeric_pairs:
        artifact_dir = (sensitive_artifact_dir or work_dir).expanduser().resolve()
        artifact_dir.mkdir(parents=True, exist_ok=True)
        unique_successes: list[dict[str, Any]] = []
        seen_successes: set[tuple[str, int, int]] = set()
        for pair in successful_numeric_pairs:
            key = (str(pair.get("url") or ""), int(pair.get("key") or 0), int(pair.get("encLimit") or 0))
            if key in seen_successes:
                continue
            seen_successes.add(key)
            unique_successes.append(pair)
        unique_successes.sort(key=lambda pair: _positive_int(pair.get("expected_bytes")), reverse=True)
        digest = hashlib.sha256(
            json.dumps(unique_successes, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:12]
        artifact = artifact_dir / f"successful-numeric-key-pairs-{digest}.json"
        artifact.write_text(
            json.dumps({"pairs": unique_successes}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        report["successful_numeric_pair_count"] = len(unique_successes)
        report["numeric_key_pair_artifact"] = str(artifact)
    else:
        report["successful_numeric_pair_count"] = 0
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="Sensitive source files or directories to scan.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--sensitive-artifact-dir", type=Path, default=None)
    parser.add_argument("--wasm-dir", type=Path, default=None)
    parser.add_argument("--max-read-bytes", type=int, default=40_000_000)
    parser.add_argument("--range-bytes", type=int, default=131_072)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--max-urls", type=int, default=8)
    parser.add_argument("--max-heuristic-keys", type=int, default=8)
    parser.add_argument("--max-heuristic-numeric-keys", type=int, default=12)
    args = parser.parse_args(argv)

    args.work_dir.mkdir(parents=True, exist_ok=True)
    report = build_probe_report(
        args.paths,
        work_dir=args.work_dir,
        wasm_dir=args.wasm_dir,
        sensitive_artifact_dir=args.sensitive_artifact_dir,
        max_read_bytes=args.max_read_bytes,
        range_bytes=args.range_bytes,
        timeout=args.timeout,
        max_urls=args.max_urls,
        max_heuristic_keys=args.max_heuristic_keys,
        max_heuristic_numeric_keys=args.max_heuristic_numeric_keys,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "result": report["result"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if report["result"] == "mp4_header_decrypted" else 2


if __name__ == "__main__":
    raise SystemExit(main())
