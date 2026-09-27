#!/usr/bin/env python3
"""Convert an authorized Weixin Channels link with the Studio link pipeline."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from replay_mp3_studio.config import WORK_ROOT  # noqa: E402
from replay_mp3_studio.extractors import run_weixin_link  # noqa: E402
from replay_mp3_studio.user_storage import ensure_private_dir, user_output_root  # noqa: E402
from replay_mp3_studio.utils import verify_mp3  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Weixin Channels short link, for example https://weixin.qq.com/sph/AHCIZNAGQb")
    default_output = str(user_output_root() / "weixin_video_channel.mp3")
    parser.add_argument("--output", default=default_output)
    parser.add_argument("--artifact-dir", default="")
    parser.add_argument("--duration", type=int, default=300)
    parser.add_argument("--watch-current", action="store_true", help="Do not reopen WeChat; inspect the current playing session.")
    parser.add_argument("--min-duration", type=float, default=180)
    args = parser.parse_args()

    output = Path(args.output).expanduser().resolve()
    artifact_dir = (
        Path(args.artifact_dir).expanduser().resolve()
        if args.artifact_dir
        else WORK_ROOT / "sensitive-artifacts" / "weixin-link-runs" / time.strftime("%Y%m%d-%H%M%S")
    )
    ensure_private_dir(artifact_dir)
    if args.output == default_output:
        ensure_private_dir(output.parent)
    else:
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def log(message: str) -> None:
        print(message, flush=True)

    run_weixin_link(
        args.url,
        output,
        artifact_dir,
        log,
        duration=args.duration,
        watch_current_only=args.watch_current,
        min_duration=args.min_duration,
    )
    verify_mp3(output, log, min_duration_seconds=args.min_duration)
    print(f"Converted Weixin link -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
