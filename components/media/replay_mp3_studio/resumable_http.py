from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional


USER_AGENT = "replay-mp3-studio/1.0"
CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.I)
RangeReader = Callable[[str, int, int, int], tuple[bytes, int, Optional[int]]]
_PWRITE = getattr(os, "pwrite", None)


class RangeUnsupportedError(RuntimeError):
    pass


def _write_at(
    fd: int,
    data: bytes,
    offset: int,
    *,
    seek_lock: threading.Lock | None = None,
) -> None:
    """Write a complete buffer at an offset on POSIX and Windows.

    POSIX pwrite does not mutate the shared file position. Windows has no pwrite,
    so its seek/write fallback must be serialized while range downloads remain
    parallel on the network side.
    """

    view = memoryview(data)
    written_total = 0
    if _PWRITE is not None:
        while written_total < len(view):
            written = _PWRITE(fd, view[written_total:], offset + written_total)
            if written <= 0:
                raise OSError("Offset write returned without making progress.")
            written_total += written
        return

    if seek_lock is not None:
        seek_lock.acquire()
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        while written_total < len(view):
            written = os.write(fd, view[written_total:])
            if written <= 0:
                raise OSError("Seek/write fallback returned without making progress.")
            written_total += written
    finally:
        if seek_lock is not None:
            seek_lock.release()


def read_http_range(url: str, start: int, end: int, timeout: int = 90) -> tuple[bytes, int, int | None]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "identity",
            "Range": f"bytes={start}-{end}",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = int(getattr(response, "status", 0) or response.getcode())
        if status == 200:
            raise RangeUnsupportedError("Remote source ignored byte-range requests.")
        if status != 206:
            raise RuntimeError(f"HTTP range request returned status {status}.")
        match = CONTENT_RANGE_RE.fullmatch(str(response.headers.get("Content-Range") or "").strip())
        if not match:
            raise RuntimeError("HTTP range response omitted a valid Content-Range.")
        actual_start, actual_end = int(match.group(1)), int(match.group(2))
        if actual_start != start or actual_end < start or actual_end > end:
            raise RuntimeError("HTTP range response boundaries did not match the request.")
        total = None if match.group(3) == "*" else int(match.group(3))
        expected = actual_end - actual_start + 1
        data = response.read(expected + 1)
        if len(data) != expected:
            raise RuntimeError("HTTP range response length did not match Content-Range.")
        return data, status, total


def _bounded_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _hash_span(path: Path, start: int, length: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = length
        while remaining:
            block = handle.read(min(4 * 1024 * 1024, remaining))
            if not block:
                raise RuntimeError("Partial download ended during checksum verification.")
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def _write_state(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _fetch_span(
    reader: RangeReader,
    url: str,
    start: int,
    end: int,
    expected_size: int,
    max_retries: int,
) -> tuple[bytes, int]:
    parts: list[bytes] = []
    cursor = start
    retry_count = 0
    while cursor <= end:
        last_error: Exception | None = None
        for attempt in range(max_retries):
            try:
                data, status, total = reader(url, cursor, end, 90)
                if status != 206:
                    if status == 200:
                        raise RangeUnsupportedError("Remote source ignored byte-range requests.")
                    raise RuntimeError(f"HTTP range request returned status {status}.")
                if total and total != expected_size:
                    raise RuntimeError("Remote source size changed during download.")
                if not data or len(data) > end - cursor + 1:
                    raise RuntimeError("HTTP range response had an invalid byte count.")
                parts.append(data)
                cursor += len(data)
                retry_count += attempt
                break
            except RangeUnsupportedError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt + 1 < max_retries:
                    time.sleep(min(6.0, 0.5 * (attempt + 1)))
        else:
            raise RuntimeError(
                f"Range {cursor}-{end} failed after {max_retries} attempts: "
                f"{type(last_error).__name__}"
            )
    payload = b"".join(parts)
    if len(payload) != end - start + 1:
        raise RuntimeError("Completed range has an unexpected byte count.")
    return payload, retry_count


def _validated_legacy_prefix(
    legacy: Path,
    reader: RangeReader,
    url: str,
    expected_size: int,
    max_retries: int,
) -> int:
    if not legacy.is_file():
        return 0
    size = legacy.stat().st_size
    if size <= 0 or size > expected_size:
        return 0
    sample_size = min(256 * 1024, size)
    offsets = sorted({0, max(0, size // 2 - sample_size // 2), size - sample_size})
    with legacy.open("rb") as handle:
        for offset in offsets:
            handle.seek(offset)
            local = handle.read(sample_size)
            remote, _ = _fetch_span(
                reader, url, offset, offset + sample_size - 1, expected_size, max_retries
            )
            if hashlib.sha256(local).digest() != hashlib.sha256(remote).digest():
                return 0
    return size


def download_by_ranges(
    url: str,
    target: Path,
    *,
    expected_size: int,
    range_reader: RangeReader = read_http_range,
    workers: int | None = None,
    chunk_size: int | None = None,
    max_retries: int = 6,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    if expected_size <= 0:
        raise ValueError("expected_size must be positive.")
    workers = workers or _bounded_env("REPLAY_MP3_HTTP_WORKERS", 8, 1, 16)
    chunk_size = chunk_size or _bounded_env(
        "REPLAY_MP3_HTTP_CHUNK_BYTES", 2 * 1024 * 1024, 256 * 1024, 8 * 1024 * 1024
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        if target.stat().st_size != expected_size:
            raise RuntimeError("Existing encrypted source has an unexpected size.")
        return {
            "mode": "reused_complete",
            "expected_bytes": expected_size,
            "reused_bytes": expected_size,
            "downloaded_bytes": 0,
            "worker_count": workers,
            "wall_seconds": round(time.monotonic() - started, 3),
        }

    part = target.with_name(f"{target.name}.ranges.part")
    state_path = target.with_name(f"{target.name}.ranges.json")
    legacy = target.with_name(f"{target.name}.part")
    source_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    if part.exists() != state_path.exists():
        raise RuntimeError("Range checkpoint is incomplete; both data and state are required.")

    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        expected_identity = (expected_size, chunk_size, source_hash)
        actual_identity = (
            int(state.get("expected_bytes") or 0),
            int(state.get("chunk_bytes") or 0),
            str(state.get("source_sha256_16") or ""),
        )
        if actual_identity != expected_identity or part.stat().st_size != expected_size:
            raise RuntimeError("Range checkpoint belongs to a different media source.")
    else:
        fd = os.open(
            part,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            os.ftruncate(fd, expected_size)
            prefix_bytes = _validated_legacy_prefix(
                legacy, range_reader, url, expected_size, max_retries
            )
            if prefix_bytes:
                with legacy.open("rb") as source:
                    offset = 0
                    for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
                        _write_at(fd, block, offset)
                        offset += len(block)
                os.fsync(fd)
        finally:
            os.close(fd)
        state = {
            "version": 1,
            "expected_bytes": expected_size,
            "chunk_bytes": chunk_size,
            "source_sha256_16": source_hash,
            "prefix_bytes": prefix_bytes,
            "prefix_sha256": _hash_span(part, 0, prefix_bytes) if prefix_bytes else "",
            "ranges": {},
        }
        _write_state(state_path, state)

    prefix_bytes = int(state.get("prefix_bytes") or 0)
    if prefix_bytes and _hash_span(part, 0, prefix_bytes) != str(state.get("prefix_sha256") or ""):
        raise RuntimeError("Legacy prefix checkpoint failed checksum verification.")
    ranges = state.get("ranges")
    if not isinstance(ranges, dict):
        raise RuntimeError("Range checkpoint has an invalid range map.")

    valid_ranges: dict[str, str] = {}
    reused_bytes = prefix_bytes
    for key, digest in ranges.items():
        match = re.fullmatch(r"(\d+)-(\d+)", str(key))
        if not match:
            continue
        start, end = int(match.group(1)), int(match.group(2))
        if start < prefix_bytes or end < start or end >= expected_size:
            continue
        if _hash_span(part, start, end - start + 1) == str(digest):
            valid_ranges[key] = str(digest)
            reused_bytes += end - start + 1
    if len(valid_ranges) != len(ranges):
        state["ranges"] = ranges = valid_ranges
        _write_state(state_path, state)

    pending: list[tuple[int, int]] = []
    cursor = prefix_bytes
    while cursor < expected_size:
        end = min(expected_size - 1, cursor + chunk_size - 1)
        if f"{cursor}-{end}" not in ranges:
            pending.append((cursor, end))
        cursor = end + 1

    lock = threading.Lock()
    file_lock = threading.Lock()
    downloaded_bytes = 0
    retry_count = 0
    fd = os.open(part, os.O_RDWR | getattr(os, "O_BINARY", 0))

    def fetch_and_store(span: tuple[int, int]) -> None:
        nonlocal downloaded_bytes, retry_count
        start, end = span
        data, retries = _fetch_span(
            range_reader, url, start, end, expected_size, max_retries
        )
        _write_at(fd, data, start, seek_lock=file_lock)
        digest = hashlib.sha256(data).hexdigest()
        with lock:
            ranges[f"{start}-{end}"] = digest
            downloaded_bytes += len(data)
            retry_count += retries
            _write_state(state_path, state)
            if progress:
                progress(reused_bytes + downloaded_bytes, expected_size)

    first_error: Exception | None = None
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(fetch_and_store, span) for span in pending]
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    first_error = first_error or exc
        if first_error:
            raise first_error
        os.fsync(fd)
    finally:
        os.close(fd)

    if reused_bytes + downloaded_bytes != expected_size:
        raise RuntimeError("Range download did not cover the complete media source.")
    for key, digest in ranges.items():
        start, end = (int(value) for value in key.split("-", 1))
        if _hash_span(part, start, end - start + 1) != digest:
            raise RuntimeError("Final range checksum verification failed.")
    os.replace(part, target)
    return {
        "mode": "parallel_ranges",
        "expected_bytes": expected_size,
        "reused_bytes": reused_bytes,
        "downloaded_bytes": downloaded_bytes,
        "worker_count": workers,
        "chunk_bytes": chunk_size,
        "range_count": len(ranges),
        "retry_count": retry_count,
        "source_sha256_16": source_hash,
        "wall_seconds": round(time.monotonic() - started, 3),
    }
