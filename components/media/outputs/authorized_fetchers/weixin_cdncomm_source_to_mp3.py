#!/usr/bin/env python3
"""Extract playable Weixin media candidates from cdncomm metadata.

The desktop WeChat player often writes short-lived CDN bookkeeping files under
`app_data/net/cdncomm`. These files are much cheaper to scan than the full
Radium browser profile and may contain either a signed media URL or a local
temporary cache path. This helper treats those records as evidence, then relies
on ffmpeg/ffprobe output before converting anything to MP3.
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
CDNCOMM_ROOT = Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/net/cdncomm"
RAW_URL_RE = re.compile(rb"https?://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]{20,}")
ENCODED_URL_RE = re.compile(rb"https?%3A%2F%2F[A-Za-z0-9._~%:/?#\[\]@!$&()*+,;=%-]{20,}", re.I)
LOCAL_PATH_RE = re.compile(rb"/Users/[^\x00\r\n\t\"'<>]{20,}")
MEDIA_HINTS = (
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
    "format/webp",
    "thumb",
)


@dataclass
class CdnCandidate:
    source_kind: str
    value: str
    redacted_value: str
    source_path: str
    relative_path: str
    source_bytes: int
    source_mtime: float
    score: int
    query_duration: float
    query_index: str


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


def redact_value(value: str) -> str:
    if value.startswith(("http://", "https://")):
        return redact_url(value)
    home = str(Path.home())
    if value.startswith(home + "/"):
        return "~/" + value[len(home) + 1 :]
    return value


def clean_fragment(text: str) -> str:
    value = (
        urllib.parse.unquote(text)
        .replace("\\/", "/")
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
        if sep in value:
            value = value.split(sep, 1)[0]
    for tail in ('"}', '"}', "}',", "'}", "</", '",', "'),", ");"):
        if tail in value:
            value = value.split(tail, 1)[0]
    return value.rstrip(").,;\"'")


def is_media_url(url: str) -> bool:
    lower = url.lower()
    if any(hint in lower for hint in SKIP_URL_HINTS):
        return False
    return any(hint in lower for hint in MEDIA_HINTS)


def query_duration(url: str) -> float:
    try:
        params = urllib.parse.parse_qs(urlsplit(url).query)
    except Exception:
        return 0.0
    for key in ("dur", "duration"):
        values = params.get(key) or []
        if values:
            try:
                return float(values[0])
            except ValueError:
                return 0.0
    return 0.0


def query_index(url: str) -> str:
    try:
        params = urllib.parse.parse_qs(urlsplit(url).query)
    except Exception:
        return ""
    values = params.get("idx") or params.get("index") or []
    return str(values[0]) if values else ""


def score_value(value: str) -> int:
    lower = value.lower()
    score = 100
    if "finder.video.qq.com" in lower:
        score -= 50
    if "stodownload" in lower:
        score -= 35
    if "snsvideodownload" in lower or "snscosdownload" in lower:
        score -= 15
    if lower.endswith((".mp4", ".m4a", ".aac", ".m3u8")):
        score -= 10
    q_dur = query_duration(value) if value.startswith(("http://", "https://")) else 0
    if q_dur and q_dur < 30:
        score += 20
    return score


def iter_cdn_files(root: Path, since: float, max_depth: int, max_files: int) -> list[Path]:
    if not root.exists():
        return []
    files: list[Path] = []
    direct_roots = [root / "cdn" / "download", root / "cdn" / "upload", root]
    seen_roots: set[str] = set()
    for direct_root in direct_roots:
        if not direct_root.exists():
            continue
        root_key = str(direct_root)
        if root_key in seen_roots:
            continue
        seen_roots.add(root_key)
        try:
            with os.scandir(direct_root) as entries:
                for entry in entries:
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    name = entry.name.lower()
                    if stat.st_mtime < since:
                        continue
                    if "cdninfo" not in name and not name.endswith((".cache", ".tmp")):
                        continue
                    files.append(Path(entry.path))
        except OSError:
            continue
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack and len(files) < max_files:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if depth < max_depth:
                                stack.append((Path(entry.path), depth + 1))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    name = entry.name.lower()
                    if stat.st_mtime < since:
                        continue
                    if "cdninfo" not in name and not name.endswith((".cache", ".tmp")):
                        continue
                    files.append(Path(entry.path))
        except OSError:
            continue
    unique: dict[str, Path] = {}
    for item in files:
        unique[str(item)] = item
    return sorted(unique.values(), key=lambda item: item.stat().st_mtime, reverse=True)[:max_files]


def scan_file(path: Path, max_read_bytes: int) -> list[CdnCandidate]:
    stat = path.stat()
    with path.open("rb") as handle:
        data = handle.read(max_read_bytes)
    candidates: list[CdnCandidate] = []
    for regex in (RAW_URL_RE, ENCODED_URL_RE):
        for match in regex.finditer(data):
            url = clean_fragment(match.group(0).decode("utf-8", errors="ignore"))
            if not url.startswith(("http://", "https://")) or not is_media_url(url):
                continue
            candidates.append(
                CdnCandidate(
                    source_kind="url",
                    value=url,
                    redacted_value=redact_value(url),
                    source_path=str(path),
                    relative_path=safe_rel(path),
                    source_bytes=stat.st_size,
                    source_mtime=stat.st_mtime,
                    score=score_value(url),
                    query_duration=query_duration(url),
                    query_index=query_index(url),
                )
            )
    for match in LOCAL_PATH_RE.finditer(data):
        value = clean_fragment(match.group(0).decode("utf-8", errors="ignore"))
        lower = value.lower()
        if "/sns/video/" not in lower and "/video" not in lower:
            continue
        candidates.append(
            CdnCandidate(
                source_kind="local_path",
                value=value,
                redacted_value=redact_value(value),
                source_path=str(path),
                relative_path=safe_rel(path),
                source_bytes=stat.st_size,
                source_mtime=stat.st_mtime,
                score=score_value(value),
                query_duration=0.0,
                query_index="",
            )
        )
    return candidates


def unique_candidates(candidates: list[CdnCandidate]) -> list[CdnCandidate]:
    seen: set[tuple[str, str]] = set()
    unique: list[CdnCandidate] = []
    for candidate in sorted(candidates, key=lambda item: (item.score, -item.source_mtime)):
        key = (candidate.source_kind, candidate.value)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def duration_seconds(output: str) -> float:
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def probe(candidate: CdnCandidate, timeout: float) -> dict:
    ffmpeg = find_ffmpeg()
    input_value = candidate.value
    if candidate.source_kind == "local_path" and not Path(input_value).exists():
        return {**asdict(candidate), "exists": False, "audio": False, "video": False, "duration": 0.0}
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", input_value],
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
        "exists": True,
        "duration": duration_seconds(stream_info),
        "audio": "Audio:" in stream_info,
        "video": "Video:" in stream_info,
        "stderr_head": proc.stderr[:1600].replace(input_value, "<redacted-input>"),
    }


def convert(candidate: CdnCandidate, output: Path, timeout: float) -> None:
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
        stderr = proc.stderr.replace(candidate.value, "<redacted-input>")
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
    candidates: list[CdnCandidate] = []
    for path in iter_cdn_files(Path(args.root).expanduser(), since, args.max_depth, args.max_files):
        try:
            candidates.extend(scan_file(path, args.max_read_bytes))
        except OSError:
            continue
    candidates = unique_candidates(candidates)
    report["candidate_count"] = len(candidates)
    report["redacted_candidates"] = [
        {
            "source_kind": item.source_kind,
            "redacted_value": item.redacted_value,
            "relative_path": item.relative_path,
            "source_bytes": item.source_bytes,
            "score": item.score,
            "query_duration": item.query_duration,
            "query_index": item.query_index,
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
            print(f"converted cdncomm candidate {candidate.redacted_value} -> {Path(args.output).expanduser().resolve()}")
            return True
        if Path(args.output).expanduser().exists():
            Path(args.output).expanduser().unlink()
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(CDNCOMM_ROOT))
    parser.add_argument("--output", default=str(ROOT / "outputs" / "weixin_cdncomm.mp3"))
    parser.add_argument("--report", default=str(ROOT / "work/sensitive-artifacts/weixin_cdncomm_source_report.json"))
    parser.add_argument("--duration", type=float, default=45)
    parser.add_argument("--poll-interval", type=float, default=1.5)
    parser.add_argument("--since-minutes", type=float, default=30)
    parser.add_argument("--max-depth", type=int, default=4)
    parser.add_argument("--max-files", type=int, default=5000)
    parser.add_argument("--max-read-bytes", type=int, default=2_000_000)
    parser.add_argument("--max-candidates", type=int, default=20)
    parser.add_argument("--min-duration", type=float, default=180)
    parser.add_argument("--probe-timeout", type=float, default=20)
    parser.add_argument("--convert-timeout", type=float, default=7200)
    parser.add_argument("--list-only", action="store_true")
    args = parser.parse_args()

    report_path = Path(args.report).expanduser().resolve()
    report = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "root": str(Path(args.root).expanduser()),
        "since_minutes": args.since_minutes,
        "min_duration": args.min_duration,
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
    print(f"No playable long Weixin cdncomm media found. Report: {report_path}")
    write_report(report_path, report)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
