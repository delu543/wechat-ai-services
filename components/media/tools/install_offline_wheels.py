#!/usr/bin/env python3
"""Safely expand fixed wheel archives into the portable Windows runtime."""

from __future__ import annotations

import argparse
import json
import shutil
import zipfile
from pathlib import Path, PurePosixPath


def mapped_path(name: str, site_packages: Path, scripts: Path) -> Path | None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise RuntimeError(f"unsafe wheel path: {name}")
    if path.parts[0].endswith(".data"):
        if len(path.parts) < 3:
            return None
        category = path.parts[1]
        relative = Path(*path.parts[2:])
        if category in {"purelib", "platlib"}:
            return site_packages / relative
        if category == "scripts":
            return scripts / relative
        return None
    return site_packages / Path(*path.parts)


def extract_wheels(wheels: list[Path], site_packages: Path, scripts: Path) -> dict[str, object]:
    site_packages.mkdir(parents=True, exist_ok=True)
    scripts.mkdir(parents=True, exist_ok=True)
    written: set[Path] = set()
    extracted = 0
    for wheel in wheels:
        if not wheel.is_file() or wheel.suffix.lower() != ".whl":
            raise RuntimeError(f"wheel is missing or invalid: {wheel}")
        with zipfile.ZipFile(wheel) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                target = mapped_path(info.filename, site_packages, scripts)
                if target is None:
                    continue
                resolved = target.resolve()
                allowed = (
                    resolved.is_relative_to(site_packages.resolve())
                    or resolved.is_relative_to(scripts.resolve())
                )
                if not allowed or resolved in written:
                    raise RuntimeError(f"duplicate or unsafe wheel target: {info.filename}")
                if info.file_size > 200_000_000:
                    raise RuntimeError(f"wheel entry exceeds the bounded size: {info.filename}")
                resolved.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, resolved.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
                written.add(resolved)
                extracted += 1

    deno_script = scripts / "deno.exe"
    if not deno_script.is_file():
        deno_candidates = sorted(site_packages.rglob("deno.exe"))
        if len(deno_candidates) != 1:
            raise RuntimeError("the fixed Deno wheel did not contain exactly one deno.exe")
        shutil.copy2(deno_candidates[0], deno_script)

    ffmpeg = sorted(site_packages.glob("imageio_ffmpeg/binaries/ffmpeg-*.exe"))
    required = {
        "ffmpeg": len(ffmpeg) == 1,
        "yt_dlp": (site_packages / "yt_dlp" / "__main__.py").is_file(),
        "yt_dlp_ejs": any(site_packages.glob("yt_dlp_ejs*")),
        "deno": deno_script.is_file(),
    }
    if not all(required.values()):
        raise RuntimeError(f"portable dependency verification failed: {required}")
    return {"status": "ready", "files": extracted, **required}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-packages", type=Path, required=True)
    parser.add_argument("--scripts", type=Path, required=True)
    parser.add_argument("wheels", nargs="+", type=Path)
    args = parser.parse_args()
    result = extract_wheels(args.wheels, args.site_packages, args.scripts)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
