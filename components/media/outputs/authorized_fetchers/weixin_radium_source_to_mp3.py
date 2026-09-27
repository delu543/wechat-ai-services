#!/usr/bin/env python3
"""Extract a playable Weixin Channels source URL from recent Radium payloads.

This is the reusable version of the successful low-intrusion route:
while the authorized desktop WeChat playback page is playing, recent Radium
blob/log payloads may contain a signed `finder.video.qq.com/stodownload` MP4
URL. The URL is kept only in the local sensitive report; console output uses a
redacted form.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
RADIIUM_ROOTS = [
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium",
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/net/cdncomm",
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/net/kvcomm",
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/log/radium",
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/log/player",
]
SKIP_PARTS = (
    "account web data",
    "cookies",
    "favicons",
    "history",
    "visited links",
    "web data",
)
RAW_URL_RE = re.compile(rb"https?://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]{20,}")
ENCODED_URL_RE = re.compile(rb"https?%3A%2F%2F[A-Za-z0-9._~%:/?#\[\]@!$&()*+,;=%-]{20,}", re.I)
TEXT_URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]{20,}", re.I)
SOURCE_HINTS = (
    "finder.video.qq.com",
    "stodownload",
    "snsvideodownload",
    "snscosdownload",
)
SKIP_URL_HINTS = (
    "ads_svp",
    "/reserved/ads",
    "imageview2",
    "format/webp",
    "thumb",
)


@dataclass
class Candidate:
    url: str
    redacted_url: str
    source_path: str
    relative_path: str
    file_bytes: int
    file_mtime: float
    score: int


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


def should_skip(path: Path) -> bool:
    lower = str(path).lower()
    return any(part in lower for part in SKIP_PARTS)


def redact_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except Exception:
        return "<unparseable-url>"
    path = parsed.path
    if len(path) > 90:
        path = path[:45] + "..." + path[-30:]
    return f"{parsed.scheme}://{parsed.netloc}{path}?<redacted>" if parsed.query else f"{parsed.scheme}://{parsed.netloc}{path}"


def clean_url(url: str) -> str:
    url = (
        url.replace("\\/", "/")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\u003A", ":")
        .replace("\\u003a", ":")
        .replace("\\u0026", "&")
        .replace("\\u0026amp;", "&")
        .strip()
        .strip("\"'<>")
    )
    for sep in ("\x00", "\x01", "\x02", "\x03", "\x04", "\n", "\r", "\t"):
        if sep in url:
            url = url.split(sep, 1)[0]
    for tail in ('"}', '"}', "}',", "'}", "</", '",', "'),", ");"):
        if tail in url:
            url = url.split(tail, 1)[0]
    return urllib.parse.unquote(url).rstrip(").,;\"'")


def decode_match(raw: bytes) -> str:
    return clean_url(raw.decode("utf-8", errors="ignore"))


def text_variants(data: bytes) -> list[str]:
    text = data.decode("utf-8", errors="ignore")
    variants = [text]
    decoded = text
    for _ in range(3):
        next_decoded = urllib.parse.unquote(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded
    if decoded != text:
        variants.append(decoded)
    slash_decoded = (
        decoded.replace("\\/", "/")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\u003A", ":")
        .replace("\\u003a", ":")
        .replace("\\u0026", "&")
        .replace("\\u0026amp;", "&")
    )
    if slash_decoded not in variants:
        variants.append(slash_decoded)
    return variants


def source_score(url: str) -> int:
    lower = url.lower()
    score = 100
    if "finder.video.qq.com" in lower:
        score -= 60
    if "stodownload" in lower:
        score -= 25
    if "snsvideodownload" in lower or "snscosdownload" in lower:
        score -= 10
    if lower.endswith(".m3u8") or ".m3u8?" in lower:
        score -= 5
    return score


def is_source_url(url: str) -> bool:
    lower = url.lower()
    if any(hint in lower for hint in SKIP_URL_HINTS):
        return False
    if "stodownload" in lower or "snsvideodownload" in lower or "snscosdownload" in lower:
        return True
    return "finder.video.qq.com" in lower and (".mp4" in lower or ".m3u8" in lower)


def iter_recent_files(since: float, min_size: int, max_size: int) -> list[Path]:
    files: list[Path] = []
    for root in RADIIUM_ROOTS:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if not should_skip(Path(dirpath) / name)]
            for name in filenames:
                path = Path(dirpath) / name
                if should_skip(path):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_mtime < since or stat.st_size < min_size or stat.st_size > max_size:
                    continue
                files.append(path)
    return sorted(files, key=lambda item: item.stat().st_mtime, reverse=True)


def scan_file(path: Path, max_read_bytes: int) -> list[Candidate]:
    stat = path.stat()
    with path.open("rb") as handle:
        data = handle.read(max_read_bytes)
    candidates: list[Candidate] = []
    for regex in (RAW_URL_RE, ENCODED_URL_RE):
        for match in regex.finditer(data):
            url = decode_match(match.group(0))
            if not url.startswith(("http://", "https://")) or not is_source_url(url):
                continue
            candidates.append(
                Candidate(
                    url=url,
                    redacted_url=redact_url(url),
                    source_path=str(path),
                    relative_path=safe_rel(path),
                    file_bytes=stat.st_size,
                    file_mtime=stat.st_mtime,
                    score=source_score(url),
                )
            )
    for variant in text_variants(data):
        for match in TEXT_URL_RE.finditer(variant):
            url = clean_url(match.group(0))
            if not url.startswith(("http://", "https://")) or not is_source_url(url):
                continue
            candidates.append(
                Candidate(
                    url=url,
                    redacted_url=redact_url(url),
                    source_path=str(path),
                    relative_path=safe_rel(path),
                    file_bytes=stat.st_size,
                    file_mtime=stat.st_mtime,
                    score=source_score(url),
                )
            )
    return candidates


def unique_candidates(candidates: list[Candidate]) -> list[Candidate]:
    seen: set[str] = set()
    unique: list[Candidate] = []
    for candidate in sorted(candidates, key=lambda item: (item.score, -item.file_mtime)):
        if candidate.url in seen:
            continue
        seen.add(candidate.url)
        unique.append(candidate)
    return unique


def snapshot_candidate_sources(
    candidates: list[Candidate],
    snapshot_dir: Path,
    *,
    max_read_bytes: int,
) -> dict:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshots: list[dict] = []
    seen: set[str] = set()
    for candidate in candidates:
        source = Path(candidate.source_path)
        source_key = str(source)
        if source_key in seen:
            continue
        seen.add(source_key)
        try:
            stat = source.stat()
            data = source.read_bytes()[:max_read_bytes]
        except OSError:
            continue
        digest = hashlib.sha256(data).hexdigest()[:16]
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", source.name)[:80] or "source"
        target = snapshot_dir / f"{len(snapshots) + 1:03d}-{digest}-{safe_name}"
        if not target.exists():
            target.write_bytes(data)
        snapshots.append(
            {
                "source_path_redacted": safe_rel(source),
                "source_snapshot_path": str(target),
                "source_size": stat.st_size,
                "bytes_copied": len(data),
                "sha256_16": digest,
                "truncated": stat.st_size > len(data),
            }
        )
    return {
        "snapshot_dir": str(snapshot_dir),
        "snapshot_count": len(snapshots),
        "snapshots": snapshots,
    }


def duration_seconds(output: str) -> float:
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def classify_initial_payload(headers: dict[str, str], data: bytes) -> dict:
    normalized_headers = {key.lower(): value for key, value in headers.items()}
    content_type = normalized_headers.get("content-type", "")
    head = data[:32]
    if len(data) >= 12 and data[4:8] == b"ftyp":
        signature = "mp4"
    elif data.lstrip().startswith(b"#EXTM3U"):
        signature = "hls"
    elif data.startswith(b"ID3"):
        signature = "id3"
    else:
        signature = "unknown"
    return {
        "status": normalized_headers.get(":status", ""),
        "content_type": content_type,
        "content_range": normalized_headers.get("content-range", ""),
        "content_length": normalized_headers.get("content-length", ""),
        "first16_hex": head[:16].hex(),
        "container_signature": signature,
        "encrypted_or_obfuscated": bool(
            data and "video/mp4" in content_type.lower() and signature == "unknown"
        ),
    }


def http_range_probe(url: str, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Range": "bytes=0-4095",
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://channels.weixin.qq.com/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(4096)
            headers = {key.lower(): value for key, value in response.headers.items()}
            headers[":status"] = str(getattr(response, "status", ""))
    except urllib.error.HTTPError as exc:
        headers = {key.lower(): value for key, value in exc.headers.items()}
        headers[":status"] = str(exc.code)
        try:
            data = exc.read(4096)
        except Exception:
            data = b""
        payload = classify_initial_payload(headers, data)
        payload["http_error"] = str(exc.code)
        return payload
    except Exception as exc:
        return {"error": str(exc)}
    return classify_initial_payload(headers, data)


def probe(candidate: Candidate, timeout: float) -> dict:
    ffmpeg = find_ffmpeg()
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", candidate.url],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except Exception as exc:
        return {**asdict(candidate), "probe_error": str(exc)}
    stream_info = f"{proc.stdout}\n{proc.stderr}"
    http_probe = {}
    if "stodownload" in candidate.url.lower() or "finder.video.qq.com" in candidate.url.lower():
        http_probe = http_range_probe(candidate.url, timeout=min(timeout, 8))
    diagnosis = ""
    if http_probe.get("encrypted_or_obfuscated"):
        diagnosis = "encrypted_or_obfuscated_media_without_decode_key"
    return {
        **asdict(candidate),
        "returncode": proc.returncode,
        "duration": duration_seconds(stream_info),
        "audio": "Audio:" in stream_info,
        "video": "Video:" in stream_info,
        "http_range": http_probe,
        "diagnosis": diagnosis,
        "stderr_head": proc.stderr[:1600].replace(candidate.url, "<redacted-url>"),
    }


def convert(candidate: Candidate, output: Path, timeout: float) -> None:
    ffmpeg = find_ffmpeg()
    output.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-i",
            candidate.url,
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
        stderr = proc.stderr.replace(candidate.url, "<redacted-url>")
        raise RuntimeError(f"ffmpeg conversion failed: {stderr[-2000:]}")


def verify_output(output: Path, min_duration: float, timeout: float) -> dict:
    ffmpeg = find_ffmpeg()
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(output), "-f", "null", "-"],
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    stream_info = f"{proc.stdout}\n{proc.stderr}"
    duration = duration_seconds(stream_info)
    ok = proc.returncode == 0 and "Audio:" in stream_info and duration >= min_duration
    return {
        "ok": ok,
        "duration": duration,
        "bytes": output.stat().st_size if output.exists() else 0,
        "stderr_tail": proc.stderr[-1200:],
    }


def write_report(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_once(args: argparse.Namespace, report: dict) -> bool:
    since = time.time() - args.since_minutes * 60
    candidates: list[Candidate] = []
    for path in iter_recent_files(since, args.min_size, args.max_size):
        try:
            candidates.extend(scan_file(path, args.max_read_bytes))
        except OSError:
            continue
    candidates = unique_candidates(candidates)
    report["candidate_count"] = len(candidates)
    report["candidates"] = [asdict(candidate) for candidate in candidates[: args.max_candidates]]
    report["redacted_candidates"] = [
        {
            "redacted_url": candidate.redacted_url,
            "relative_path": candidate.relative_path,
            "file_bytes": candidate.file_bytes,
            "score": candidate.score,
        }
        for candidate in candidates[: args.max_candidates]
    ]
    if args.source_snapshot_dir and candidates:
        report["source_snapshots"] = snapshot_candidate_sources(
            candidates[: args.max_candidates],
            Path(args.source_snapshot_dir).expanduser().resolve(),
            max_read_bytes=args.source_snapshot_max_read_bytes,
        )
    report["probes"] = []
    for candidate in candidates[: args.max_candidates]:
        probe_result = probe(candidate, timeout=args.probe_timeout)
        report["probes"].append(probe_result)
        if not probe_result.get("audio") or float(probe_result.get("duration") or 0) < args.min_duration:
            continue
        if args.list_only:
            report["result"] = {"status": "found", "selected": probe_result}
            return True
        convert(candidate, Path(args.output).expanduser().resolve(), timeout=args.convert_timeout)
        verify = verify_output(Path(args.output).expanduser().resolve(), args.min_duration, args.probe_timeout)
        report["result"] = {"status": "converted", "selected": probe_result, "verify": verify}
        if verify["ok"]:
            print(
                "converted Weixin source URL "
                f"{candidate.redacted_url} -> {Path(args.output).expanduser().resolve()}",
                flush=True,
            )
            return True
        if Path(args.output).expanduser().exists():
            Path(args.output).expanduser().unlink()
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "outputs" / "weixin_video_channel.mp3"))
    parser.add_argument("--report", default=str(ROOT / "work/sensitive-artifacts/weixin_radium_source_report.json"))
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--poll-interval", type=float, default=2)
    parser.add_argument("--since-minutes", type=float, default=15)
    parser.add_argument("--min-size", type=int, default=512)
    parser.add_argument("--max-size", type=int, default=300_000_000)
    parser.add_argument("--max-read-bytes", type=int, default=120_000_000)
    parser.add_argument("--max-candidates", type=int, default=12)
    parser.add_argument("--min-duration", type=float, default=180)
    parser.add_argument("--probe-timeout", type=float, default=20)
    parser.add_argument("--convert-timeout", type=float, default=7200)
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument(
        "--source-snapshot-dir",
        default="",
        help="Sensitive directory where source files that contained candidates should be copied immediately.",
    )
    parser.add_argument("--source-snapshot-max-read-bytes", type=int, default=40_000_000)
    args = parser.parse_args()

    report_path = Path(args.report).expanduser().resolve()
    report = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "roots": [str(root) for root in RADIIUM_ROOTS if root.exists()],
        "since_minutes": args.since_minutes,
        "min_duration": args.min_duration,
        "candidate_count": 0,
        "candidates": [],
        "redacted_candidates": [],
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
    print(f"No playable Weixin source URL found in recent Radium payloads. Report: {report_path}")
    write_report(report_path, report)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
