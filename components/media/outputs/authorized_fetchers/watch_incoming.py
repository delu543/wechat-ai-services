#!/usr/bin/env python3
"""Watch incoming/ and auto-process stable authorized files."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INCOMING = ROOT / "incoming"
DEFAULT_OUTPUTS = ROOT / "outputs"


def snapshot(path: Path) -> dict[Path, tuple[int, int]]:
    if not path.exists():
        return {}
    return {
        item: (item.stat().st_size, item.stat().st_mtime_ns)
        for item in path.iterdir()
        if not item.name.startswith(".") and item.exists()
    }


def stable_candidates(incoming: Path, seen: dict[Path, tuple[int, int]]) -> list[Path]:
    now = snapshot(incoming)
    stable: list[Path] = []
    for path, state in now.items():
        if seen.get(path) == state:
            stable.append(path)
    seen.clear()
    seen.update(now)
    return sorted(stable, key=lambda item: item.name.lower())


def process(incoming: Path, outputs: Path, dry_run: bool) -> tuple[int, str]:
    script = Path(__file__).with_name("process_incoming.py")
    cmd = [
        sys.executable,
        str(script),
        "--incoming",
        str(incoming),
        "--outputs",
        str(outputs),
        "--continue-on-error",
    ]
    if dry_run:
        cmd.append("--dry-run")
    result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return result.returncode, result.stdout


def append_log(log_path: Path, text: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n[{stamp}]\n{text.rstrip()}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incoming", default=str(DEFAULT_INCOMING))
    parser.add_argument("--outputs", default=str(DEFAULT_OUTPUTS))
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--once", action="store_true", help="Run one stability check and exit")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log", default=str(ROOT / "work" / "incoming-watch.log"))
    args = parser.parse_args()

    incoming = Path(args.incoming).expanduser().resolve()
    outputs = Path(args.outputs).expanduser().resolve()
    log_path = Path(args.log).expanduser().resolve()
    incoming.mkdir(parents=True, exist_ok=True)
    outputs.mkdir(parents=True, exist_ok=True)
    print(f"Watching: {incoming}")
    print(f"Outputs: {outputs}")
    print(f"Log: {log_path}")
    seen = snapshot(incoming)
    processed: set[tuple[Path, tuple[int, int]]] = set()

    try:
        while True:
            time.sleep(max(args.interval, 0.2))
            stable = []
            for path in stable_candidates(incoming, seen):
                state = snapshot(incoming).get(path)
                if state and (path, state) not in processed:
                    stable.append(path)
                    processed.add((path, state))
            if stable:
                names = ", ".join(path.name for path in stable)
                print(f"Stable incoming file(s): {names}", flush=True)
                code, output = process(incoming, outputs, args.dry_run)
                message = f"stable: {names}\nexit={code}\n{output}"
                print(message, flush=True)
                append_log(log_path, message)
            if args.once:
                break
    except KeyboardInterrupt:
        print("\nWatcher stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
