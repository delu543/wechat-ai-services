from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


MEDIA_KINDS = {"mp4", "m4a", "fmp4", "aac", "mp3", "mpeg-ts", "webm"}


@dataclass
class HeaderClassification:
    kind: str
    confidence: str
    detail: str
    media_candidate: bool


def read_header(path: Path, max_bytes: int = 256) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(max(64, min(max_bytes, 256)))
    except OSError:
        return b""


def header_hex(data: bytes, limit: int = 256) -> str:
    return data[:limit].hex(" ")


def _has_box(data: bytes, box: bytes) -> bool:
    return box in data[:256]


def classify_header(data: bytes, path: Optional[Path] = None) -> HeaderClassification:
    suffix = path.suffix.lower() if path else ""

    if not data:
        if suffix in {".ldb", ".log"}:
            return HeaderClassification("leveldb/ldb/log", "medium", "Extension suggests LevelDB table/log.", False)
        return HeaderClassification("unknown", "low", "No readable header bytes.", False)

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return HeaderClassification("png", "high", "PNG signature.", False)
    if data.startswith(b"\xff\xd8\xff"):
        return HeaderClassification("jpg", "high", "JPEG SOI marker.", False)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return HeaderClassification("webp", "high", "RIFF WEBP signature.", False)
    if data.startswith(b"\x1f\x8b"):
        return HeaderClassification("gzip", "high", "Gzip signature.", False)
    if suffix == ".br":
        return HeaderClassification("brotli", "medium", "Brotli has no fixed magic; extension is .br.", False)
    if data.startswith(b"SQLite format 3\x00"):
        return HeaderClassification("sqlite", "high", "SQLite database header.", False)
    if suffix in {".ldb", ".log"}:
        return HeaderClassification("leveldb/ldb/log", "medium", "Extension suggests LevelDB table/log.", False)
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return HeaderClassification("webm", "high", "EBML header.", True)
    if data.startswith(b"ID3") or (len(data) > 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return HeaderClassification("mp3", "high", "ID3 tag or MPEG audio frame sync.", True)
    if len(data) > 2 and data[0] == 0xFF and (data[1] & 0xF0) == 0xF0:
        return HeaderClassification("aac", "high", "ADTS frame sync.", True)
    if len(data) >= 376 and data[0] == 0x47 and data[188] == 0x47:
        return HeaderClassification("mpeg-ts", "high", "MPEG-TS sync byte at 188-byte packet interval.", True)
    if len(data) >= 8 and data[4:8] in {b"ftyp", b"styp"}:
        brands = data[8:32]
        if suffix == ".m4a" or b"M4A" in brands:
            return HeaderClassification("m4a", "high", "ISO BMFF ftyp with M4A brand or extension.", True)
        if data[4:8] == b"styp" or b"dash" in brands or b"iso6" in brands or _has_box(data, b"moof"):
            return HeaderClassification("fmp4", "high", "Fragmented MP4 style ISO BMFF header.", True)
        return HeaderClassification("mp4", "high", "ISO BMFF ftyp header.", True)

    if suffix in {".mp4", ".m4v", ".mov"}:
        return HeaderClassification("mp4", "low", "Extension is media-like, but header was not conclusive.", True)
    if suffix == ".m4a":
        return HeaderClassification("m4a", "low", "Extension is .m4a, but header was not conclusive.", True)
    if suffix in {".m4s", ".cmfv", ".cmfa"}:
        return HeaderClassification("fmp4", "low", "Extension is fragmented media-like.", True)
    if suffix == ".ts":
        return HeaderClassification("mpeg-ts", "low", "Extension is .ts, but header was not conclusive.", True)
    if suffix in {".aac", ".mp3", ".webm"}:
        return HeaderClassification(suffix.lstrip("."), "low", "Extension is media-like, but header was not conclusive.", True)

    return HeaderClassification("unknown", "low", "No known file signature matched.", False)


def likely_artifact_role(classification: HeaderClassification, path: Path) -> str:
    kind = classification.kind
    lower = str(path).lower()
    if kind in MEDIA_KINDS:
        return "media file or media fragment"
    if kind in {"sqlite"} or lower.endswith((".db", ".sqlite", ".db-wal", ".db-shm")):
        return "database/cache index"
    if kind == "leveldb/ldb/log" or "leveldb" in lower:
        return "cache index/log/state"
    if kind in {"jpg", "png", "webp"}:
        return "thumbnail/image"
    if kind in {"gzip", "brotli"}:
        return "compressed web/cache payload"
    if "cache" in lower or "storage" in lower or "state" in lower:
        return "cache/state file"
    return "unknown"
