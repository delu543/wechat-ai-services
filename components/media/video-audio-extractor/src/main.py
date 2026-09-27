from __future__ import annotations

import argparse
import json
from pathlib import Path

from .blackbox_recorder import list_avfoundation_devices, run_blackbox_record
from .cache_auditor import run_audit
from .ffmpeg_tools import convert_to_mp3, probe_media
from .network_probe import run_probe


def cmd_audit_cache(args: argparse.Namespace) -> int:
    report = run_audit(
        dirs=args.dirs,
        duration=args.duration,
        out_prefix=args.out,
        interval=args.interval,
        hash_max_mb=args.hash_max_mb,
        header_bytes=args.header_bytes,
        probe_min_kb=args.probe_min_kb,
    )
    print(json.dumps(report["outputs"], ensure_ascii=False, indent=2))
    return 0


def cmd_probe_url(args: argparse.Namespace) -> int:
    report = run_probe(
        url=args.url,
        duration=args.duration,
        out_prefix=args.out,
        headless=args.headless,
        profile_dir=args.profile_dir,
        save_sensitive_urls=args.save_sensitive_urls,
        max_probes=args.max_probes,
        convert_out=args.convert_out,
    )
    print(json.dumps(report.get("outputs", {}), ensure_ascii=False, indent=2))
    return 0 if not report.get("error") else 2


def cmd_convert_file(args: argparse.Namespace) -> int:
    input_path = str(Path(args.input).expanduser().resolve())
    tempo = 1.0 / args.recorded_speed
    result = convert_to_mp3(input_path, Path(args.out).expanduser().resolve(), tempo=tempo)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_convert_url(args: argparse.Namespace) -> int:
    tempo = 1.0 / args.recorded_speed
    result = convert_to_mp3(args.url, Path(args.out).expanduser().resolve(), tempo=tempo)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_probe_file(args: argparse.Namespace) -> int:
    result = probe_media(str(Path(args.input).expanduser().resolve()))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("recognized") else 2


def cmd_blackbox_record(args: argparse.Namespace) -> int:
    run_blackbox_record(
        url=args.url,
        speed=args.speed,
        out_path=args.out,
        duration=args.duration,
        audio_device=args.audio_device,
        open_url=not args.no_open,
        keep_fast=args.keep_fast,
        raw_only=args.raw_only,
        wait_audio_timeout=args.wait_audio_timeout,
        audio_threshold_db=args.audio_threshold_db,
        audio_probe_seconds=args.audio_probe_seconds,
    )
    return 0


def cmd_audio_devices(args: argparse.Namespace) -> int:
    print(json.dumps(list_avfoundation_devices(), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local video/audio extraction helper.")
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit-cache", help="Audit local cache/temp directories for changed media-like files.")
    audit.add_argument("--dirs", nargs="+", required=True, help="Directories or files to watch.")
    audit.add_argument("--duration", type=float, default=120)
    audit.add_argument("--interval", type=float, default=1)
    audit.add_argument("--out", required=True, help="Output prefix, for example reports/audit_001")
    audit.add_argument("--hash-max-mb", type=float, default=50)
    audit.add_argument("--header-bytes", type=int, default=256)
    audit.add_argument("--probe-min-kb", type=float, default=32)
    audit.set_defaults(func=cmd_audit_cache)

    probe = sub.add_parser("probe-url", help="Passively observe browser network requests for media candidates.")
    probe.add_argument("--url", required=True)
    probe.add_argument("--duration", type=float, default=120)
    probe.add_argument("--out", required=True, help="Output prefix, for example reports/probe_001")
    probe.add_argument("--headless", action="store_true")
    probe.add_argument("--profile-dir", default="", help="Optional persistent browser profile directory.")
    probe.add_argument("--save-sensitive-urls", action="store_true", help="Store raw candidate URLs locally in JSON.")
    probe.add_argument("--max-probes", type=int, default=20)
    probe.add_argument("--convert-out", default="", help="Optional MP3 output if an observed URL has audio.")
    probe.set_defaults(func=cmd_probe_url)

    probe_file = sub.add_parser("probe-file", help="Run ffprobe/ffmpeg fallback on one local file.")
    probe_file.add_argument("--input", required=True)
    probe_file.set_defaults(func=cmd_probe_file)

    convert_file = sub.add_parser("convert-file", help="Convert a local candidate media file to MP3.")
    convert_file.add_argument("--input", required=True)
    convert_file.add_argument("--out", required=True)
    convert_file.add_argument("--recorded-speed", type=float, default=1.0, help="Use 3 for a 3x recording that needs restoration.")
    convert_file.set_defaults(func=cmd_convert_file)

    convert_url = sub.add_parser("convert-url", help="Convert a candidate media URL to MP3.")
    convert_url.add_argument("--url", required=True)
    convert_url.add_argument("--out", required=True)
    convert_url.add_argument("--recorded-speed", type=float, default=1.0)
    convert_url.set_defaults(func=cmd_convert_url)

    blackbox = sub.add_parser("blackbox-record", help="Explicit user-started fallback audio recording.")
    blackbox.add_argument("--url", required=True)
    blackbox.add_argument("--speed", type=float, default=3)
    blackbox.add_argument("--out", required=True)
    blackbox.add_argument("--duration", type=float, default=None, help="Seconds to record. If omitted, stop manually.")
    blackbox.add_argument(
        "--audio-device",
        default="",
        help="macOS avfoundation input (for example ':1') or Windows DirectShow audio device name.",
    )
    blackbox.add_argument("--no-open", action="store_true")
    blackbox.add_argument("--keep-fast", action="store_true")
    blackbox.add_argument("--raw-only", action="store_true", help="Only capture the fast recording; skip tempo restoration.")
    blackbox.add_argument(
        "--wait-audio-timeout",
        type=float,
        default=0,
        help="Wait up to N seconds for audible playback before the real recording starts.",
    )
    blackbox.add_argument("--audio-threshold-db", type=float, default=-60.0)
    blackbox.add_argument("--audio-probe-seconds", type=float, default=1.5)
    blackbox.set_defaults(func=cmd_blackbox_record)

    devices = sub.add_parser("audio-devices", help="List macOS or Windows audio inputs for blackbox recording.")
    devices.set_defaults(func=cmd_audio_devices)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
