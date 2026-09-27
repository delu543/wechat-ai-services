#!/usr/bin/env python3
"""Convert a local Weixin source-listener export into MP3 without running the listener."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from replay_mp3_studio.weixin_vendor_sources import convert_vendor_source_to_mp3  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert local ltaoo/qiye45/self-built Weixin source-listener artifacts to MP3."
    )
    parser.add_argument("source", help="Local file or directory exported by an authorized source listener.")
    parser.add_argument("--output", required=True, help="Output MP3 path.")
    parser.add_argument("--report", default="", help="Sanitized JSON report path.")
    parser.add_argument("--work-dir", default="", help="Private work directory for decode/download artifacts.")
    parser.add_argument("--min-duration", type=float, default=0.0)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)

    try:
        result = convert_vendor_source_to_mp3(
            Path(args.source),
            Path(args.output),
            report_path=Path(args.report) if args.report else None,
            work_dir=Path(args.work_dir) if args.work_dir else None,
            min_duration_seconds=args.min_duration,
            timeout=args.timeout,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
