from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from .utils import find_ffmpeg, verify_mp3
from .weixin_decode_key import decode_weixin_numeric_key_pair_to_mp3, decode_weixin_pair_to_mp3
from .weixin_source_pairs import (
    extract_decode_key_pairs_from_file,
    extract_numeric_key_pairs_from_file,
    redact_url,
    redacted_numeric_key_pair_summary,
    redacted_pair_summary,
)


MEDIA_SUFFIXES = {".mp4", ".flv", ".m4a", ".mp3", ".mov", ".webm", ".m3u8", ".aac", ".wav"}
TEXT_SCAN_SUFFIXES = {".json", ".har", ".txt", ".log", ".html", ".htm", ".xml", ".js"}
EXECUTABLE_SUFFIXES = {".exe", ".dll", ".pyd", ".sys", ".bat", ".ps1", ".cmd", ".msi"}


def _sha256_12(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def _redact_text(text: str) -> str:
    redacted_query_marker = "__WEIXIN_VENDOR_REDACTED_QUERY__"
    text = text.replace("?<redacted>", f"?{redacted_query_marker}")
    text = re.sub(r"https?://[^\s\"'<>]+", lambda match: redact_url(match.group(0)), text)
    text = re.sub(r"((?:token|cookie|auth|sign|encfilekey)=)[^&\s]+", r"\1<redacted>", text, flags=re.I)
    text = re.sub(
        r"(?i)((?:decode[_-]?key|decrypt[_-]?key|media[_-]?decode[_-]?key)\s*[:=]\s*)[A-Za-z0-9_+=/-]{6,256}",
        r"\1<redacted>",
        text,
    )
    text = re.sub(
        r"(?i)((?:^|[^A-Za-z0-9_])(?:key|decode[_-]?key|decrypt[_-]?key|media[_-]?key)\s*[:=]\s*)\d{1,20}",
        r"\1<redacted>",
        text,
    )
    text = text.replace(f"?{redacted_query_marker}", "?<redacted>")
    return text


def _sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _sanitize_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _write_report(report_path: Path | None, payload: dict[str, Any]) -> dict[str, Any]:
    safe_payload = _sanitize_payload(payload)
    if report_path:
        report_path = report_path.expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(safe_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return safe_payload


def _remove_partial_output(output: Path) -> None:
    if output.exists():
        try:
            output.unlink()
        except OSError:
            pass


def _iter_files(path: Path) -> list[Path]:
    target = path.expanduser().resolve()
    if not target.exists():
        raise FileNotFoundError(f"Vendor source path not found: {target}")
    if target.is_file():
        return [target]
    return sorted(item for item in target.rglob("*") if item.is_file())


def _is_media_file(path: Path) -> bool:
    return path.suffix.lower() in MEDIA_SUFFIXES and path.stat().st_size > 0


def _is_text_scan_file(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SCAN_SUFFIXES and path.stat().st_size > 0


def _is_executable_file(path: Path) -> bool:
    return path.suffix.lower() in EXECUTABLE_SUFFIXES


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


def _numeric_pair_expected_bytes(pair: dict[str, Any]) -> int:
    for key in ("expected_bytes", "encrypted_bytes", "content_length", "file_size", "fileSize", "bytes"):
        size = _positive_int(pair.get(key))
        if size:
            return size
    return _content_range_total(pair.get("content_range"))


def _sorted_numeric_pairs(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        pair
        for _index, pair in sorted(
            enumerate(pairs),
            key=lambda item: (-_numeric_pair_expected_bytes(item[1]), item[0]),
        )
    ]


def _media_record(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "suffix": path.suffix.lower(),
        "bytes": path.stat().st_size,
        "sha256_12": _sha256_12(path),
    }


def local_media_candidates(path: Path) -> list[Path]:
    files = _iter_files(path)
    media = [item for item in files if _is_media_file(item)]
    return sorted(media, key=lambda item: (item.stat().st_size, item.name), reverse=True)


def load_vendor_decode_pairs(path: Path) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in _iter_files(path):
        if not _is_text_scan_file(item):
            continue
        try:
            file_pairs = extract_decode_key_pairs_from_file(item)
        except Exception:
            continue
        for pair in file_pairs:
            if str(pair.get("decode_key") or "").isdigit():
                continue
            key = (
                str(pair.get("url") or ""),
                str(pair.get("decode_key") or ""),
                str(pair.get("path") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            pairs.append(pair)
    return pairs


def _iter_structured_numeric_pair_candidates(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("pairs", "numeric_pairs", "numeric_key_pairs", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _structured_numeric_pairs_from_file(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    pairs: list[dict[str, Any]] = []
    for item in _iter_structured_numeric_pair_candidates(payload):
        url = str(item.get("url") or item.get("media_url") or item.get("videoUrl") or "").strip()
        key = _positive_int(item.get("key") or item.get("numeric_key") or item.get("decodeKey"))
        if not url or key <= 0:
            continue
        pair: dict[str, Any] = {
            "url": url,
            "key": key,
            "enc_limit": _positive_int(
                item.get("enc_limit") or item.get("encLimit") or item.get("enc_len") or item.get("encrypt_len")
            )
            or 131072,
            "key_field": str(item.get("key_field") or "key"),
            "path": str(path),
            "evidence": str(item.get("evidence") or "same_local_context"),
        }
        expected_bytes = _numeric_pair_expected_bytes(item)
        if expected_bytes:
            pair["expected_bytes"] = expected_bytes
        for field in ("content_type", "url_sha256_16", "source"):
            value = item.get(field)
            if value not in (None, ""):
                pair[field] = value
        pairs.append(pair)
    return pairs


def load_vendor_numeric_key_pairs(path: Path) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for item in _iter_files(path):
        if not _is_text_scan_file(item):
            continue
        try:
            file_pairs = _structured_numeric_pairs_from_file(item) or extract_numeric_key_pairs_from_file(item)
        except Exception:
            continue
        for pair in file_pairs:
            key = (
                str(pair.get("url") or ""),
                int(pair.get("key") or 0),
                str(pair.get("path") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            pairs.append(pair)
    return _sorted_numeric_pairs(pairs)


def scan_vendor_source(path: Path) -> dict[str, Any]:
    target = path.expanduser().resolve()
    files = _iter_files(target)
    media = local_media_candidates(target)
    pairs = load_vendor_decode_pairs(target)
    numeric_pairs = load_vendor_numeric_key_pairs(target)
    executable_count = sum(1 for item in files if _is_executable_file(item))
    text_scan_count = sum(1 for item in files if _is_text_scan_file(item))
    report = {
        "source_path": str(target),
        "source_is_directory": target.is_dir(),
        "file_count": len(files),
        "text_file_scanned_count": text_scan_count,
        "skipped_executable_count": executable_count,
        "local_media_candidate_count": len(media),
        "local_media_candidates": [_media_record(item) for item in media[:20]],
        "decode_key_pair_count": len(pairs),
        "decode_key_pair_summary": redacted_pair_summary(pairs),
        "numeric_key_pair_count": len(numeric_pairs),
        "numeric_key_pair_summary": redacted_numeric_key_pair_summary(numeric_pairs),
        "raw_values_in_report": False,
    }
    return _sanitize_payload(report)


def _convert_local_media(
    media: Path,
    output: Path,
    *,
    runner: Callable[..., Any],
    timeout: int,
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    if media.suffix.lower() == ".mp3":
        source = media.resolve()
        target = output.resolve()
        if source != target:
            temporary = output.with_name(f".{output.name}.copy.part")
            shutil.copy2(source, temporary)
            if temporary.stat().st_size != source.stat().st_size:
                raise RuntimeError("Local MP3 copy produced an unexpected byte count.")
            temporary.replace(output)
            mode = "copied_verified_local_mp3"
        else:
            mode = "reused_verified_local_mp3"
        return {
            "mode": mode,
            "source_bytes": source.stat().st_size,
            "output_bytes": output.stat().st_size,
            "exit_code": 0,
        }
    command = [
        find_ffmpeg(),
        "-hide_banner",
        "-y",
        "-i",
        str(media),
        "-vn",
        "-acodec",
        "libmp3lame",
        "-ab",
        "192k",
        "-f",
        "mp3",
        str(output),
    ]
    proc = runner(command, text=True, capture_output=True, timeout=timeout)
    result: dict[str, Any] = {
        "command": [_redact_text(str(part)) for part in command],
        "exit_code": int(getattr(proc, "returncode", 0)),
    }
    stdout = str(getattr(proc, "stdout", "") or "")
    stderr = str(getattr(proc, "stderr", "") or "")
    if stdout:
        result["stdout_tail"] = _redact_text(stdout[-2000:])
    if stderr:
        result["stderr_tail"] = _redact_text(stderr[-2000:])
    if result["exit_code"] != 0:
        raise RuntimeError("Vendor local media conversion failed.")
    if not output.exists() or output.stat().st_size <= 0:
        raise RuntimeError("Vendor local media conversion did not create output.")
    return result


def convert_vendor_source_to_mp3(
    path: Path,
    output: Path,
    *,
    report_path: Path | None = None,
    work_dir: Path | None = None,
    runner: Callable[..., Any] = subprocess.run,
    verifier: Callable[..., dict[str, Any]] = verify_mp3,
    decoder: Callable[..., dict[str, Any]] = decode_weixin_pair_to_mp3,
    numeric_decoder: Callable[..., dict[str, Any]] = decode_weixin_numeric_key_pair_to_mp3,
    timeout: int = 300,
    min_duration_seconds: float = 0.0,
) -> dict[str, Any]:
    target = path.expanduser().resolve()
    output = output.expanduser().resolve()
    work_dir = (work_dir or output.parent / "vendor-source-work").expanduser().resolve()
    scan = scan_vendor_source(target)
    numeric_pairs = load_vendor_numeric_key_pairs(target)
    raw_pairs = load_vendor_decode_pairs(target)
    result: dict[str, Any] = {
        "source_path": str(target),
        "output": str(output),
        "scan": scan,
        "source_kind": "",
        "mp3_complete": False,
    }
    if numeric_pairs:
        conversion_errors: list[dict[str, Any]] = []
        numeric_work_dirs: dict[str, Path] = {}
        for pair in numeric_pairs:
            source_url = str(pair.get("url") or "")
            if source_url not in numeric_work_dirs:
                source_number = len(numeric_work_dirs) + 1
                source_name = "numeric-key-source" if source_number == 1 else f"numeric-key-source-{source_number}"
                numeric_work_dirs[source_url] = work_dir / source_name
            try:
                conversion = numeric_decoder(
                    pair,
                    output,
                    work_dir=numeric_work_dirs[source_url],
                    timeout=timeout,
                )
                verification = verifier(output, lambda _message: None, min_duration_seconds=min_duration_seconds)
            except Exception as exc:
                conversion_errors.append(
                    {
                        "numeric_key_pair": redacted_numeric_key_pair_summary([pair])[0],
                        "error": _redact_text(str(exc)),
                    }
                )
                _remove_partial_output(output)
                continue
            result.update(
                {
                    "source_kind": "numeric_key_pair",
                    "numeric_key_conversion": conversion,
                    "numeric_key_pair_summary": redacted_numeric_key_pair_summary([pair]),
                    "numeric_key_conversion_errors": conversion_errors,
                    "verification": verification,
                    "mp3_complete": True,
                }
            )
            return _write_report(report_path, result)
        result["numeric_key_conversion_errors"] = conversion_errors
        _write_report(report_path, result)
        raise RuntimeError("No numeric-key Weixin source candidate produced a complete MP3.")
    elif raw_pairs:
        conversion_errors = []
        decode_work_dirs: dict[str, Path] = {}
        for pair in raw_pairs:
            source_url = str(pair.get("url") or "")
            if source_url not in decode_work_dirs:
                source_number = len(decode_work_dirs) + 1
                source_name = "decode-key-source" if source_number == 1 else f"decode-key-source-{source_number}"
                decode_work_dirs[source_url] = work_dir / source_name
            try:
                conversion = decoder(
                    pair,
                    output,
                    work_dir=decode_work_dirs[source_url],
                    timeout=timeout,
                )
                verification = verifier(output, lambda _message: None, min_duration_seconds=min_duration_seconds)
            except Exception as exc:
                conversion_errors.append(
                    {
                        "decode_key_pair": redacted_pair_summary([pair])[0],
                        "error": _redact_text(str(exc)),
                    }
                )
                _remove_partial_output(output)
                continue
            result.update(
                {
                    "source_kind": "decode_key_pair",
                    "decode_key_conversion": conversion,
                    "decode_key_pair_summary": redacted_pair_summary([pair]),
                    "decode_key_conversion_errors": conversion_errors,
                    "verification": verification,
                    "mp3_complete": True,
                }
            )
            return _write_report(report_path, result)
        result["decode_key_conversion_errors"] = conversion_errors
        _write_report(report_path, result)
        raise RuntimeError("No decode-key Weixin source candidate produced a complete MP3.")
    else:
        media = local_media_candidates(target)
        if not media:
            message = "No local media file or media URL plus decode_key/numeric_key pair found in vendor source."
            result["error"] = message
            _write_report(report_path, result)
            raise RuntimeError(message)
        conversion_errors: list[dict[str, Any]] = []
        selected: Path | None = None
        conversion: dict[str, Any] | None = None
        for candidate in media:
            try:
                conversion = _convert_local_media(candidate, output, runner=runner, timeout=timeout)
            except Exception as exc:
                conversion_errors.append(
                    {
                        "media": _media_record(candidate),
                        "error": _redact_text(str(exc)),
                    }
                )
                if output.exists():
                    try:
                        output.unlink()
                    except OSError:
                        pass
                continue
            selected = candidate
            break
        if selected is None or conversion is None:
            result["local_media_conversion_errors"] = conversion_errors
            _write_report(report_path, result)
            raise RuntimeError("No local vendor media candidate could be converted to MP3.")
        result.update(
            {
                "source_kind": "local_media_file",
                "selected_media": _media_record(selected),
                "local_media_conversion": conversion,
                "local_media_conversion_errors": conversion_errors,
            }
        )
    result["verification"] = verifier(output, lambda _message: None, min_duration_seconds=min_duration_seconds)
    result["mp3_complete"] = True
    return _write_report(report_path, result)
