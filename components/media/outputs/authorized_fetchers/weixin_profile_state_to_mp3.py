#!/usr/bin/env python3
"""Extract Weixin Channels media candidates from Radium profile state.

The first successful Weixin capture came from a playable
`finder.video.qq.com/.../stodownload` URL surfaced in the desktop WeChat
embedded-browser state. This helper scans only targeted Radium profile files
where that state has appeared before, separates script/code residuals from
runtime data, reconstructs media URLs using the same simple transformations
visible in the page bundle, and relies on ffmpeg/ffprobe before converting.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
PROFILE_ROOT = (
    Path.home()
    / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium/web/profiles"
)
TARGET_TOP_LEVEL_FILES = {
    "Favicons",
    "History",
    "Network Persistent State",
    "Share Data",
}
TARGET_SUBDIRS = (
    "Local Storage/leveldb",
)
OPTIONAL_SUBDIRS = (
    "Cache/Cache_Data",
    "Service Worker/CacheStorage",
    "IndexedDB",
)
TEXTISH_SUFFIXES = {"", ".log", ".ldb", ".sqlite", ".db"}
RAW_URL_RE = re.compile(rb"https?://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]{20,}")
ENCODED_URL_RE = re.compile(rb"https?%3A%2F%2F[A-Za-z0-9._~%:/?#\[\]@!$&()*+,;=%-]{20,}", re.I)
TEXT_URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]{20,}", re.I)
MEDIA_URL_HINTS = (
    "stodownload",
    "snsvideodownload",
    "snscosdownload",
    ".m3u8",
    ".mp4",
    ".m4a",
    ".aac",
)
SKIP_URL_HINTS = (
    "/reserved/ads",
    "ads_svp",
    "imageview2",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    "image/",
    "format/webp",
    "thumb",
)
SCRIPT_RESIDUAL_MARKERS = (
    "__codex_bridge_out",
    "STUDIO_ENDPOINT",
    "weixin_current_page_bookmarklet",
    "finderH5Auth",
    "FinderGetLiveInfo",
    "FinderGetCommentDetail",
    "const replayFrom",
    "Running FinderGetLiveInfo",
)
URL_TOKEN_RE = re.compile(
    r"(?:urlToken|url_token)[^&?A-Za-z0-9]{0,64}"
    r"([&?][^\"'\s,}\]]{8,1200}|token=[^\"'\s,}\]]{8,1200}|[A-Za-z0-9._%+\-/=]{24,1200})",
    re.I,
)


@dataclass
class ProfileCandidate:
    kind: str
    value: str
    redacted_value: str
    source_path: str
    relative_path: str
    source_bytes: int
    source_mtime: float
    score: int
    reason: str
    script_residual: bool


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
    raise SystemExit("ffmpeg not found. Set FFMPEG=/path/to/ffmpeg.")


def safe_rel(path: Path) -> str:
    try:
        relative = path.expanduser().resolve().relative_to(Path.home().resolve())
    except (OSError, ValueError):
        return str(path)
    return "~/" + relative.as_posix()


def redact_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except Exception:
        return "<unparseable-url>"
    path = parsed.path
    if len(path) > 90:
        path = path[:45] + "..." + path[-30:]
    return f"{parsed.scheme}://{parsed.netloc}{path}?<redacted>" if parsed.query else url


def clean_text(value: str) -> str:
    cleaned = (
        urllib.parse.unquote(value)
        .replace("\\/", "/")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\u003A", ":")
        .replace("\\u003a", ":")
        .replace("\\u0026", "&")
        .replace("\\u0026amp;", "&")
        .replace("&amp;", "&")
        .strip()
        .strip("\"'<>")
    )
    for sep in ("\x00", "\x01", "\x02", "\x03", "\x04", "\n", "\r", "\t"):
        if sep in cleaned:
            cleaned = cleaned.split(sep, 1)[0]
    for tail in ('"}', '"}', "}',", "'}", "</", '",', "'),", ");"):
        if tail in cleaned:
            cleaned = cleaned.split(tail, 1)[0]
    return cleaned.rstrip(").,;\"'")


def text_variants(data: bytes) -> list[str]:
    text = data.decode("utf-8", errors="ignore")
    variants = [text]
    decoded = text
    for _ in range(3):
        next_decoded = urllib.parse.unquote(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded
    variants.append(decoded)
    variants.append(
        decoded.replace("\\/", "/")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\u003A", ":")
        .replace("\\u003a", ":")
        .replace("\\u0026", "&")
        .replace("&amp;", "&")
    )
    unique: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        if variant not in seen:
            seen.add(variant)
            unique.append(variant)
    return unique


def is_media_url(url: str) -> bool:
    lower = url.lower()
    if any(hint in lower for hint in SKIP_URL_HINTS):
        return False
    return any(hint in lower for hint in MEDIA_URL_HINTS)


def score_url(url: str, reason: str, script_residual: bool) -> int:
    lower = url.lower()
    score = 100
    if "finder.video.qq.com" in lower:
        score -= 45
    if "stodownload" in lower:
        score -= 35
    if "snsvideodownload" in lower or "snscosdownload" in lower:
        score -= 12
    if "token=" in lower:
        score -= 8
    if "web=1" in lower:
        score -= 3
    if "fexam=1" in lower:
        score -= 3
    if reason == "rebuilt_from_url_token":
        score -= 5
    if script_residual:
        score += 80
    return score


def normalize_media_url(url: str, token: str = "") -> str:
    value = clean_text(url)
    value = value.replace("http://wxapp.tc.qq.com", "https://finder.video.qq.com")
    value = value.replace("https://wxapp.tc.qq.com", "https://finder.video.qq.com")
    if value.startswith("http://finder.video.qq.com"):
        value = "https://" + value[len("http://") :]
    token = clean_text(token)
    if token and "token=" not in value.lower():
        sep = "&" if "?" in value else "?"
        if token.startswith(("&", "?")):
            value = value + token
        elif token.lower().startswith("token="):
            value = value + sep + token
        elif "token=" in token.lower():
            value = value + sep + token.lstrip("&?")
        else:
            value = value + sep + "token=" + token
    query = urllib.parse.parse_qs(urlsplit(value).query)
    if "web" not in query:
        value += ("&" if "?" in value else "?") + "web=1"
    if "fexam" not in query:
        value += ("&" if "?" in value else "?") + "fexam=1"
    return value


def looks_like_script_residual(context: str) -> bool:
    return any(marker in context for marker in SCRIPT_RESIDUAL_MARKERS)


def candidate_from_url(
    url: str,
    path: Path,
    stat: os.stat_result,
    reason: str,
    context: str,
) -> ProfileCandidate | None:
    normalized = normalize_media_url(url)
    if not normalized.startswith(("http://", "https://")) or not is_media_url(normalized):
        return None
    script_residual = looks_like_script_residual(context)
    return ProfileCandidate(
        kind="url",
        value=normalized,
        redacted_value=redact_url(normalized),
        source_path=str(path),
        relative_path=safe_rel(path),
        source_bytes=stat.st_size,
        source_mtime=stat.st_mtime,
        score=score_url(normalized, reason, script_residual),
        reason=reason,
        script_residual=script_residual,
    )


def nearby_context(text: str, start: int, end: int, radius: int) -> str:
    return text[max(0, start - radius) : min(len(text), end + radius)]


def token_variants_from_context(context: str) -> list[str]:
    variants: list[str] = []
    for match in URL_TOKEN_RE.finditer(context):
        token = clean_text(match.group(1))
        if len(token) >= 12 and token not in variants:
            variants.append(token)
    return variants[:6]


def scan_file(path: Path, max_read_bytes: int, context_radius: int) -> list[ProfileCandidate]:
    stat = path.stat()
    with path.open("rb") as handle:
        data = handle.read(max_read_bytes)
    candidates: list[ProfileCandidate] = []
    for variant in text_variants(data):
        for match in TEXT_URL_RE.finditer(variant):
            raw_url = clean_text(match.group(0))
            context = nearby_context(variant, match.start(), match.end(), context_radius)
            direct = candidate_from_url(raw_url, path, stat, "direct_url", context)
            if direct:
                candidates.append(direct)
            if "stodownload" not in raw_url.lower() and "download" not in raw_url.lower():
                continue
            for token in token_variants_from_context(context):
                rebuilt = candidate_from_url(
                    normalize_media_url(raw_url, token),
                    path,
                    stat,
                    "rebuilt_from_url_token",
                    context,
                )
                if rebuilt:
                    candidates.append(rebuilt)
    for regex in (RAW_URL_RE, ENCODED_URL_RE):
        for match in regex.finditer(data):
            raw_url = clean_text(match.group(0).decode("utf-8", errors="ignore"))
            text = data.decode("utf-8", errors="ignore")
            context = nearby_context(text, max(0, match.start()), min(len(text), match.end()), context_radius)
            direct = candidate_from_url(raw_url, path, stat, "binary_url", context)
            if direct:
                candidates.append(direct)
    unique: dict[str, ProfileCandidate] = {}
    for candidate in candidates:
        unique.setdefault(candidate.value, candidate)
    return list(unique.values())


def iter_target_files(
    since: float,
    max_file_bytes: int,
    max_files: int,
    include_cache_data: bool,
) -> list[Path]:
    if not PROFILE_ROOT.exists():
        return []
    roots: list[Path] = []
    for profile in PROFILE_ROOT.glob("*"):
        if not profile.is_dir():
            continue
        for name in TARGET_TOP_LEVEL_FILES:
            roots.append(profile / name)
        for rel in TARGET_SUBDIRS:
            roots.append(profile / rel)
        if include_cache_data:
            for rel in OPTIONAL_SUBDIRS:
                roots.append(profile / rel)
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            candidates = [root]
        elif root.is_dir():
            candidates = []
            for dirpath, _dirnames, filenames in os.walk(root):
                for filename in filenames:
                    candidates.append(Path(dirpath) / filename)
        else:
            continue
        for path in candidates:
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size <= 0 or stat.st_size > max_file_bytes:
                continue
            if stat.st_mtime < since:
                continue
            if path.name == "LOCK" or path.suffix not in TEXTISH_SUFFIXES:
                continue
            files.append(path)
    unique = {str(path): path for path in files}
    return sorted(unique.values(), key=lambda item: item.stat().st_mtime_ns, reverse=True)[:max_files]


def unique_candidates(candidates: list[ProfileCandidate], probe_script_residuals: bool) -> list[ProfileCandidate]:
    seen: set[str] = set()
    unique: list[ProfileCandidate] = []
    for candidate in sorted(candidates, key=lambda item: (item.score, -item.source_mtime)):
        if candidate.value in seen:
            continue
        seen.add(candidate.value)
        if candidate.script_residual and not probe_script_residuals:
            unique.append(candidate)
            continue
        unique.append(candidate)
    return unique


def duration_seconds(output: str) -> float:
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def probe(candidate: ProfileCandidate, timeout: float) -> dict:
    ffmpeg = find_ffmpeg()
    if candidate.script_residual:
        return {
            **asdict(candidate),
            "skipped": "script_residual",
            "audio": False,
            "video": False,
            "duration": 0.0,
        }
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", candidate.value],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except Exception as exc:
        return {**asdict(candidate), "probe_error": str(exc), "audio": False, "video": False, "duration": 0.0}
    stream_info = f"{proc.stdout}\n{proc.stderr}"
    return {
        **asdict(candidate),
        "returncode": proc.returncode,
        "duration": duration_seconds(stream_info),
        "audio": "Audio:" in stream_info,
        "video": "Video:" in stream_info,
        "stderr_head": proc.stderr[:1600].replace(candidate.value, "<redacted-url>"),
    }


def convert(candidate: ProfileCandidate, output: Path, timeout: float) -> None:
    ffmpeg = find_ffmpeg()
    output.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-i",
            candidate.value,
            "-vn",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "128k",
            str(output),
        ],
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.replace(candidate.value, "<redacted-url>")
        raise RuntimeError(f"ffmpeg conversion failed: {stderr[-2000:]}")


def verify_output(output: Path, min_duration: float, timeout: float) -> dict:
    if not output.exists():
        return {"ok": False, "duration": 0.0, "bytes": 0, "error": "missing_output"}
    ffmpeg = find_ffmpeg()
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(output), "-f", "null", "-"],
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    stream_info = f"{proc.stdout}\n{proc.stderr}"
    duration = duration_seconds(stream_info)
    return {
        "ok": proc.returncode == 0 and "Audio:" in stream_info and duration >= min_duration,
        "duration": duration,
        "bytes": output.stat().st_size,
        "stderr_tail": proc.stderr[-1200:],
    }


def write_report(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_once(args: argparse.Namespace, report: dict) -> bool:
    since = time.time() - args.since_minutes * 60
    files = iter_target_files(since, args.max_file_bytes, args.max_files, args.include_cache_data)
    report["scanned_files"] = [
        {"relative_path": safe_rel(path), "bytes": path.stat().st_size, "mtime": path.stat().st_mtime}
        for path in files[:200]
    ]
    candidates: list[ProfileCandidate] = []
    for path in files:
        try:
            candidates.extend(scan_file(path, args.max_read_bytes, args.context_radius))
        except OSError:
            continue
    candidates = unique_candidates(candidates, args.probe_script_residuals)
    report["candidate_count"] = len(candidates)
    report["redacted_candidates"] = [
        {
            "redacted_value": item.redacted_value,
            "relative_path": item.relative_path,
            "source_bytes": item.source_bytes,
            "score": item.score,
            "reason": item.reason,
            "script_residual": item.script_residual,
        }
        for item in candidates[: args.max_candidates]
    ]
    report["candidates"] = [asdict(item) for item in candidates[: args.max_candidates]]
    report["probes"] = []
    for candidate in candidates[: args.max_candidates]:
        probe_result = probe(candidate, args.probe_timeout)
        report["probes"].append(probe_result)
        if not probe_result.get("audio") or float(probe_result.get("duration") or 0) < args.min_duration:
            continue
        if args.list_only:
            report["result"] = {"status": "found", "selected": probe_result}
            return True
        convert(candidate, Path(args.output).expanduser().resolve(), args.convert_timeout)
        verify = verify_output(Path(args.output).expanduser().resolve(), args.min_duration, args.probe_timeout)
        report["result"] = {"status": "converted", "selected": probe_result, "verify": verify}
        if verify.get("ok"):
            print(
                "converted Weixin profile-state URL "
                f"{candidate.redacted_value} -> {Path(args.output).expanduser().resolve()}",
                flush=True,
            )
            return True
        if Path(args.output).expanduser().exists():
            Path(args.output).expanduser().unlink()
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "outputs" / "weixin_profile_state.mp3"))
    parser.add_argument("--report", default=str(ROOT / "work/sensitive-artifacts/weixin_profile_state_report.json"))
    parser.add_argument("--duration", type=float, default=45)
    parser.add_argument("--poll-interval", type=float, default=1.5)
    parser.add_argument("--since-minutes", type=float, default=360)
    parser.add_argument("--max-file-bytes", type=int, default=8_000_000)
    parser.add_argument("--max-read-bytes", type=int, default=8_000_000)
    parser.add_argument("--max-files", type=int, default=400)
    parser.add_argument("--max-candidates", type=int, default=30)
    parser.add_argument("--context-radius", type=int, default=3000)
    parser.add_argument("--min-duration", type=float, default=180)
    parser.add_argument("--probe-timeout", type=float, default=20)
    parser.add_argument("--convert-timeout", type=float, default=7200)
    parser.add_argument("--include-cache-data", action="store_true")
    parser.add_argument("--probe-script-residuals", action="store_true")
    parser.add_argument("--list-only", action="store_true")
    args = parser.parse_args()

    report_path = Path(args.report).expanduser().resolve()
    report = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "profile_root": str(PROFILE_ROOT),
        "since_minutes": args.since_minutes,
        "min_duration": args.min_duration,
        "scanned_files": [],
        "candidate_count": 0,
        "redacted_candidates": [],
        "candidates": [],
        "probes": [],
        "result": {"status": "not_found"},
    }
    deadline = time.time() + args.duration
    while time.time() < deadline:
        if run_once(args, report):
            write_report(report_path, report)
            return 0
        write_report(report_path, report)
        time.sleep(args.poll_interval)
    print(f"No playable Weixin profile-state media found. Report: {report_path}")
    write_report(report_path, report)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
