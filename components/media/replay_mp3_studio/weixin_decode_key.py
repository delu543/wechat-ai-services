from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .resumable_http import RangeUnsupportedError, download_by_ranges, read_http_range
from .utils import find_ffmpeg


PROJECT_ROOT = Path(__file__).resolve().parents[1]
KEYSTREAM_SIZE = 131072
WASM_ASSET_URLS = {
    "wasm_video_decode.js": (
        "https://raw.githubusercontent.com/Evil0ctal/WeChat-Channels-Video-File-Decryption/"
        "main/wechat_files/wasm_video_decode.js"
    ),
    "wasm_video_decode.wasm": (
        "https://raw.githubusercontent.com/Evil0ctal/WeChat-Channels-Video-File-Decryption/"
        "main/wechat_files/wasm_video_decode.wasm"
    ),
}


def decode_key_fingerprint(decode_key: str) -> dict[str, Any]:
    key = str(decode_key or "")
    return {
        "decode_key_sha256_12": hashlib.sha256(key.encode("utf-8")).hexdigest()[:12] if key else "",
        "decode_key_length": len(key),
    }


def numeric_key_fingerprint(key: int | str) -> dict[str, Any]:
    value = str(key or "")
    return {
        "numeric_key_sha256_12": hashlib.sha256(value.encode("utf-8")).hexdigest()[:12] if value else "",
        "numeric_key_digits": len(value),
    }


def decrypt_weixin_encrypted_bytes(encrypted: bytes, keystream: bytes) -> bytes:
    decrypted = bytearray(encrypted)
    decrypt_len = min(KEYSTREAM_SIZE, len(encrypted), len(keystream))
    for index in range(decrypt_len):
        decrypted[index] ^= keystream[index]
    return bytes(decrypted)


MASK64 = (1 << 64) - 1


def _u64(value: int) -> int:
    return value & MASK64


def _isaac_mix(values: tuple[int, int, int, int, int, int, int, int]) -> tuple[int, int, int, int, int, int, int, int]:
    a, b, c, d, e, f, g, h = values
    a = _u64(a - e)
    f = _u64(f ^ (h >> 9))
    h = _u64(h + a)
    b = _u64(b - f)
    g = _u64(g ^ (a << 9))
    a = _u64(a + b)
    c = _u64(c - g)
    h = _u64(h ^ (b >> 23))
    b = _u64(b + c)
    d = _u64(d - h)
    a = _u64(a ^ (c << 15))
    c = _u64(c + d)
    e = _u64(e - a)
    b = _u64(b ^ (d >> 14))
    d = _u64(d + e)
    f = _u64(f - b)
    c = _u64(c ^ (e << 20))
    e = _u64(e + f)
    g = _u64(g - c)
    d = _u64(d ^ (f >> 17))
    f = _u64(f + g)
    h = _u64(h - d)
    e = _u64(e ^ (g << 14))
    g = _u64(g + h)
    return a, b, c, d, e, f, g, h


class _Isaac64:
    def __init__(self, key: int):
        self.rand_cnt = 255
        self.seed = [0] * 256
        self.mm = [0] * 256
        self.aa = 0
        self.bb = 0
        self.cc = 0
        self._init(key)

    def _init(self, key: int) -> None:
        golden = 0x9E3779B97F4A7C13
        a = b = c = d = e = f = g = h = golden
        self.seed[0] = _u64(int(key))
        for _ in range(4):
            a, b, c, d, e, f, g, h = _isaac_mix((a, b, c, d, e, f, g, h))
        for index in range(0, 256, 8):
            a = _u64(a + self.seed[index])
            b = _u64(b + self.seed[index + 1])
            c = _u64(c + self.seed[index + 2])
            d = _u64(d + self.seed[index + 3])
            e = _u64(e + self.seed[index + 4])
            f = _u64(f + self.seed[index + 5])
            g = _u64(g + self.seed[index + 6])
            h = _u64(h + self.seed[index + 7])
            a, b, c, d, e, f, g, h = _isaac_mix((a, b, c, d, e, f, g, h))
            self.mm[index : index + 8] = [a, b, c, d, e, f, g, h]
        for index in range(0, 256, 8):
            a = _u64(a + self.mm[index])
            b = _u64(b + self.mm[index + 1])
            c = _u64(c + self.mm[index + 2])
            d = _u64(d + self.mm[index + 3])
            e = _u64(e + self.mm[index + 4])
            f = _u64(f + self.mm[index + 5])
            g = _u64(g + self.mm[index + 6])
            h = _u64(h + self.mm[index + 7])
            a, b, c, d, e, f, g, h = _isaac_mix((a, b, c, d, e, f, g, h))
            self.mm[index : index + 8] = [a, b, c, d, e, f, g, h]
        self._isaac64()

    def _isaac64(self) -> None:
        self.cc = _u64(self.cc + 1)
        self.bb = _u64(self.bb + self.cc)
        for index in range(256):
            if index % 4 == 0:
                self.aa = _u64(~(self.aa ^ _u64(self.aa << 21)))
            elif index % 4 == 1:
                self.aa = _u64(self.aa ^ (self.aa >> 5))
            elif index % 4 == 2:
                self.aa = _u64(self.aa ^ _u64(self.aa << 12))
            else:
                self.aa = _u64(self.aa ^ (self.aa >> 33))
            self.aa = _u64(self.aa + self.mm[(index + 128) % 256])
            x = self.mm[index]
            y = _u64(self.mm[(x >> 3) % 256] + self.aa + self.bb)
            self.mm[index] = y
            self.bb = _u64(self.mm[(y >> 11) % 256] + x)
            self.seed[index] = self.bb

    def random(self) -> int:
        result = self.seed[self.rand_cnt]
        if self.rand_cnt == 0:
            self._isaac64()
            self.rand_cnt = 255
        else:
            self.rand_cnt -= 1
        return result


def decrypt_weixin_numeric_key_bytes(encrypted: bytes, key: int | str, enc_limit: int = KEYSTREAM_SIZE) -> bytes:
    decrypted = bytearray(encrypted)
    decrypt_len = min(max(int(enc_limit or KEYSTREAM_SIZE), 0), len(decrypted))
    ctx = _Isaac64(int(key))
    for index in range(0, decrypt_len, 8):
        block = ctx.random().to_bytes(8, "big")
        for offset, key_byte in enumerate(block):
            real_index = index + offset
            if real_index >= decrypt_len:
                break
            decrypted[real_index] ^= key_byte
    return bytes(decrypted)


def assert_mp4_header(payload: bytes) -> None:
    if len(payload) < 8 or payload[4:8] != b"ftyp":
        raise RuntimeError("Weixin decode_key decryption failed: MP4 ftyp signature not found.")


def ensure_wasm_assets(
    wasm_dir: Path,
    *,
    downloader: Callable[[str, Path], None] | None = None,
) -> dict[str, Any]:
    wasm_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[str] = []
    existing: list[str] = []
    fetch = downloader or _download_file
    for filename, url in WASM_ASSET_URLS.items():
        target = wasm_dir / filename
        if target.exists() and target.stat().st_size > 0:
            existing.append(filename)
            continue
        fetch(url, target)
        downloaded.append(filename)
    return {
        "wasm_dir": str(wasm_dir),
        "downloaded": downloaded,
        "existing": existing,
    }


def _expected_size_from_headers(headers: Any) -> int | None:
    content_range = str(headers.get("Content-Range") or "")
    match = re.search(r"/(\d+)\s*$", content_range)
    if match:
        return int(match.group(1))
    content_length = str(headers.get("Content-Length") or "").strip()
    if content_length.isdigit():
        return int(content_length)
    return None


def _http_expected_size(url: str) -> int | None:
    request = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "replay-mp3-studio/1.0", "Accept-Encoding": "identity"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return _expected_size_from_headers(response.headers)
    except Exception:
        pass
    range_probe = urllib.request.Request(
        url,
        headers={
            "User-Agent": "replay-mp3-studio/1.0",
            "Accept-Encoding": "identity",
            "Range": "bytes=0-0",
        },
    )
    try:
        with urllib.request.urlopen(range_probe, timeout=30) as response:
            size = _expected_size_from_headers(response.headers)
            response.read(1)
            return size
    except Exception:
        return None


def _read_http_range(url: str, start: int, end: int, timeout: int = 60) -> tuple[bytes, int, int | None]:
    return read_http_range(url, start, end, timeout)


def _download_file_by_ranges(
    url: str,
    target: Path,
    *,
    expected_size: int,
    chunk_size: int = 8 * 1024 * 1024,
    max_retries: int = 6,
) -> dict[str, Any]:
    return download_by_ranges(
        url,
        target,
        expected_size=expected_size,
        range_reader=_read_http_range,
        chunk_size=chunk_size,
        max_retries=max_retries,
    )


def _download_file(url: str, target: Path, *, expected_size: int | None = None) -> dict[str, Any]:
    started = time.monotonic()
    target.parent.mkdir(parents=True, exist_ok=True)
    expected_size = expected_size or _http_expected_size(url)
    if expected_size and expected_size > 8 * 1024 * 1024:
        try:
            return _download_file_by_ranges(url, target, expected_size=expected_size, chunk_size=2 * 1024 * 1024)
        except RangeUnsupportedError:
            pass
    if expected_size and target.is_file() and target.stat().st_size == expected_size:
        return {
            "mode": "reused_complete",
            "expected_bytes": expected_size,
            "reused_bytes": expected_size,
            "downloaded_bytes": 0,
            "wall_seconds": round(time.monotonic() - started, 3),
        }
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "replay-mp3-studio/1.0", "Accept-Encoding": "identity"},
    )
    part = target.with_name(f"{target.name}.stream.part")
    with urllib.request.urlopen(request, timeout=120) as response, part.open("wb") as handle:
        expected_size = expected_size or _expected_size_from_headers(response.headers)
        shutil.copyfileobj(response, handle)
        handle.flush()
        os.fsync(handle.fileno())
    if expected_size and part.stat().st_size != expected_size:
        raise RuntimeError(f"Downloaded file is incomplete: {part.stat().st_size} != {expected_size} bytes.")
    part.replace(target)
    return {
        "mode": "stream",
        "expected_bytes": expected_size or target.stat().st_size,
        "reused_bytes": 0,
        "downloaded_bytes": target.stat().st_size,
        "wall_seconds": round(time.monotonic() - started, 3),
    }


def generate_keystream_via_node(
    decode_key: str,
    *,
    wasm_dir: Path,
    work_dir: Path,
    runner: Callable[..., Any] = subprocess.run,
    timeout: int = 60,
) -> bytes:
    work_dir.mkdir(parents=True, exist_ok=True)
    helper = PROJECT_ROOT / "tools" / "weixin_keystream_wasm.js"
    with tempfile.TemporaryDirectory(prefix="weixin-key-", dir=str(work_dir)) as tmp:
        tmp_path = Path(tmp)
        key_file = tmp_path / "decode-key.txt"
        out_file = tmp_path / "keystream.bin"
        key_file.write_text(str(decode_key), encoding="utf-8")
        command = [
            "node",
            str(helper),
            "--decode-key-file",
            str(key_file),
            "--wasm-dir",
            str(wasm_dir),
            "--out",
            str(out_file),
        ]
        proc = runner(command, text=True, capture_output=True, timeout=timeout, cwd=str(PROJECT_ROOT))
        if int(getattr(proc, "returncode", 0)) != 0:
            stderr = str(getattr(proc, "stderr", "") or "")[-1000:]
            stdout = str(getattr(proc, "stdout", "") or "")[-1000:]
            raise RuntimeError(f"Weixin keystream generation failed: {stderr or stdout}")
        data = out_file.read_bytes()
    if len(data) != KEYSTREAM_SIZE:
        raise RuntimeError(f"Weixin keystream size mismatch: {len(data)} != {KEYSTREAM_SIZE}")
    return data


def download_encrypted_video(
    url: str,
    target: Path,
    *,
    downloader: Callable[[str, Path], None] | None = None,
    expected_size: int | None = None,
) -> dict[str, Any]:
    if downloader:
        downloader(url, target)
        downloaded_size = target.stat().st_size if target.is_file() else 0
        download = {"mode": "custom", "expected_bytes": expected_size or downloaded_size}
    else:
        download = _download_file(url, target, expected_size=expected_size)
    if not target.exists() or target.stat().st_size <= 0:
        raise RuntimeError("Encrypted Weixin video download produced an empty file.")
    if expected_size and target.stat().st_size != expected_size:
        raise RuntimeError(
            f"Encrypted Weixin video has an unexpected size: {target.stat().st_size} != {expected_size} bytes."
        )
    return download


def _pair_expected_size(pair: dict[str, Any]) -> int | None:
    for field in ("expected_bytes", "encrypted_bytes", "content_length", "file_size", "fileSize", "bytes"):
        try:
            size = int(pair.get(field) or 0)
        except (TypeError, ValueError):
            continue
        if size > 0:
            return size
    content_range = str(pair.get("content_range") or "")
    match = re.search(r"/(\d+)\s*$", content_range)
    return int(match.group(1)) if match else None


def _decrypt_prefix_file(
    encrypted: Path,
    decrypted: Path,
    *,
    prefix_bytes: int,
    transform: Callable[[bytes], bytes],
) -> dict[str, Any]:
    started = time.monotonic()
    temporary = decrypted.with_name(f"{decrypted.name}.decrypting")
    with encrypted.open("rb") as source, temporary.open("wb") as target:
        prefix = source.read(max(1, prefix_bytes))
        plain_prefix = transform(prefix)
        assert_mp4_header(plain_prefix)
        target.write(plain_prefix)
        shutil.copyfileobj(source, target, length=4 * 1024 * 1024)
        target.flush()
        os.fsync(target.fileno())
    if temporary.stat().st_size != encrypted.stat().st_size:
        raise RuntimeError("Decrypted Weixin media size differs from its encrypted source.")
    temporary.replace(decrypted)
    return {
        "mode": "prefix_stream_copy",
        "encrypted_bytes": encrypted.stat().st_size,
        "decrypted_bytes": decrypted.stat().st_size,
        "prefix_bytes": min(prefix_bytes, encrypted.stat().st_size),
        "wall_seconds": round(time.monotonic() - started, 3),
    }


def convert_mp4_to_mp3(
    source_mp4: Path,
    output_mp3: Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
    timeout: int = 300,
) -> dict[str, Any]:
    output_mp3.parent.mkdir(parents=True, exist_ok=True)
    command = [
        find_ffmpeg(),
        "-hide_banner",
        "-y",
        "-i",
        str(source_mp4),
        "-vn",
        "-acodec",
        "libmp3lame",
        "-q:a",
        "2",
        str(output_mp3),
    ]
    proc = runner(command, text=True, capture_output=True, timeout=timeout, cwd=str(PROJECT_ROOT))
    payload: dict[str, Any] = {
        "exit_code": int(getattr(proc, "returncode", 0)),
        "command": [command[0], "-hide_banner", "-y", "-i", "<decrypted-mp4>", "-vn", "-acodec", "libmp3lame", "-q:a", "2", str(output_mp3)],
    }
    stderr = str(getattr(proc, "stderr", "") or "")
    stdout = str(getattr(proc, "stdout", "") or "")
    if stdout:
        payload["stdout_tail"] = stdout[-1000:]
    if stderr:
        payload["stderr_tail"] = stderr[-1000:]
    if payload["exit_code"] != 0:
        raise RuntimeError("ffmpeg conversion from decrypted Weixin MP4 to MP3 failed.")
    if not output_mp3.exists() or output_mp3.stat().st_size <= 0:
        raise RuntimeError("ffmpeg conversion did not create an MP3.")
    return payload


def decode_weixin_pair_to_mp3(
    pair: dict[str, str],
    output_mp3: Path,
    *,
    work_dir: Path,
    runner: Callable[..., Any] = subprocess.run,
    downloader: Callable[[str, Path], None] | None = None,
    keystream_generator: Callable[..., bytes] | None = None,
    timeout: int = 300,
) -> dict[str, Any]:
    started = time.monotonic()
    url = str(pair.get("url") or "")
    decode_key = str(pair.get("decode_key") or "")
    if not url or not decode_key:
        raise RuntimeError("Weixin decode pair requires both url and decode_key.")
    work_dir.mkdir(parents=True, exist_ok=True)
    wasm_dir = work_dir / "wechat-wasm"
    encrypted = work_dir / "encrypted.mp4"
    decrypted = work_dir / "decrypted.mp4"
    ensure = ensure_wasm_assets(wasm_dir, downloader=downloader)
    expected_size = _pair_expected_size(pair)
    download = download_encrypted_video(
        url, encrypted, downloader=downloader, expected_size=expected_size
    )
    generator = keystream_generator or generate_keystream_via_node
    keystream = generator(decode_key, wasm_dir=wasm_dir, work_dir=work_dir, runner=runner, timeout=timeout)
    decryption = _decrypt_prefix_file(
        encrypted,
        decrypted,
        prefix_bytes=KEYSTREAM_SIZE,
        transform=lambda payload: decrypt_weixin_encrypted_bytes(payload, keystream),
    )
    conversion = convert_mp4_to_mp3(decrypted, output_mp3, runner=runner, timeout=timeout)
    report = {
        "ok": True,
        "url_host_path": _url_host_path(url),
        **decode_key_fingerprint(decode_key),
        "work_dir": str(work_dir),
        "encrypted_bytes": encrypted.stat().st_size,
        "decrypted_mp4": str(decrypted),
        "output_mp3": str(output_mp3),
        "download": download,
        "decryption": decryption,
        "wasm_assets": ensure,
        "conversion": conversion,
        "wall_seconds": round(time.monotonic() - started, 3),
    }
    (work_dir / "decode-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def decode_weixin_numeric_key_pair_to_mp3(
    pair: dict[str, Any],
    output_mp3: Path,
    *,
    work_dir: Path,
    runner: Callable[..., Any] = subprocess.run,
    downloader: Callable[[str, Path], None] | None = None,
    timeout: int = 300,
) -> dict[str, Any]:
    started = time.monotonic()
    url = str(pair.get("url") or "")
    key = int(pair.get("key") or 0)
    enc_limit = int(pair.get("enc_limit") or pair.get("encLimit") or KEYSTREAM_SIZE)
    if not url or key <= 0:
        raise RuntimeError("Weixin numeric key pair requires both url and positive key.")
    work_dir.mkdir(parents=True, exist_ok=True)
    encrypted = work_dir / "encrypted-by-numeric-key.mp4"
    decrypted = work_dir / "decrypted-by-numeric-key.mp4"
    expected_size = _pair_expected_size(pair)
    download = download_encrypted_video(
        url, encrypted, downloader=downloader, expected_size=expected_size
    )
    decryption = _decrypt_prefix_file(
        encrypted,
        decrypted,
        prefix_bytes=enc_limit,
        transform=lambda payload: decrypt_weixin_numeric_key_bytes(payload, key, enc_limit=enc_limit),
    )
    conversion = convert_mp4_to_mp3(decrypted, output_mp3, runner=runner, timeout=timeout)
    report = {
        "ok": True,
        "url_host_path": _url_host_path(url),
        **numeric_key_fingerprint(key),
        "enc_limit": enc_limit,
        "work_dir": str(work_dir),
        "encrypted_bytes": encrypted.stat().st_size,
        "decrypted_mp4": str(decrypted),
        "output_mp3": str(output_mp3),
        "download": download,
        "decryption": decryption,
        "conversion": conversion,
        "wall_seconds": round(time.monotonic() - started, 3),
    }
    (work_dir / "numeric-key-decode-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _url_host_path(url: str) -> str:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return f"{parsed.netloc}{parsed.path}"
    except Exception:
        return "<unparsed>"
