#!/usr/bin/env python3
"""Build the deterministic, no-Python/no-pip Windows portable release asset."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import bootstrap  # noqa: E402


LOCK_PATH = ROOT / "scripts" / "windows-portable.lock.json"
TEMPLATE = ROOT / "scripts" / "install-windows-offline.template.ps1"
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)
MARKER = bootstrap.MARKER


def digest(path: Path) -> str:
    algorithm = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            algorithm.update(chunk)
    return algorithm.hexdigest()


def safe_archive(archive: zipfile.ZipFile) -> None:
    seen: set[str] = set()
    total = 0
    for info in archive.infolist():
        path = PurePosixPath(info.filename)
        if path.is_absolute() or ".." in path.parts or info.filename in seen:
            raise RuntimeError(f"unsafe or duplicate archive entry: {info.filename}")
        seen.add(info.filename)
        total += info.file_size
    if len(seen) > 50_000 or total > 1_000_000_000:
        raise RuntimeError("input archive exceeds the portable release bounds")


def verify_input(path: Path, expected_sha256: str) -> None:
    if not path.is_file():
        raise RuntimeError(f"required input is missing: {path}")
    actual = digest(path)
    if actual != expected_sha256:
        raise RuntimeError(f"SHA-256 mismatch for {path.name}: {actual}")
    with zipfile.ZipFile(path) as archive:
        safe_archive(archive)


def copy_runtime_source(staging: Path, version: str) -> None:
    runtime = staging / "runtime"
    skill = staging / "skill"
    runtime.mkdir(parents=True)
    skill.mkdir(parents=True)
    for name in bootstrap.COPY_FILES:
        shutil.copy2(ROOT / name, runtime / name)
    for name in bootstrap.COPY_DIRS:
        shutil.copytree(
            ROOT / name,
            runtime / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
    shutil.copytree(
        ROOT / "portable_skill" / "weixin-replay-to-mp3",
        skill,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )
    (runtime / MARKER).write_bytes((version + "\n").encode("ascii"))
    (skill / MARKER).write_bytes((version + "\n").encode("ascii"))
    launcher = (
        "@echo off\r\n"
        '"%~dp0work\\venv\\Scripts\\python.exe" '
        '"%~dp0weixin_replay_cli.py" %*\r\n'
    )
    (runtime / "weixin-replay-to-mp3.cmd").write_bytes(launcher.encode("ascii"))


def zip_tree(root: Path) -> bytes:
    buffer = io.BytesIO()
    # Stored entries make the release byte-for-byte reproducible across the
    # different zlib builds on macOS development hosts and Windows runners.
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        paths = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
        for path in paths:
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(relative, FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 0
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


def render_installer(version: str, manifest_bytes: bytes) -> bytes:
    template = TEMPLATE.read_text(encoding="ascii")
    replacements = {
        "__PORTABLE_VERSION__": version,
        "__PACKAGE_MANIFEST_SHA256__": hashlib.sha256(manifest_bytes).hexdigest(),
        "__PACKAGE_MANIFEST_BYTES__": str(len(manifest_bytes)),
    }
    for marker, value in replacements.items():
        if template.count(marker) != 1:
            raise RuntimeError(f"template marker must appear exactly once: {marker}")
        template = template.replace(marker, value)
    return template.encode("ascii")


def package_record(path: Path, relative: str) -> dict[str, object]:
    return {"path": relative, "bytes": path.stat().st_size, "sha256": digest(path)}


def build_bundle(python_zip: Path, wheelhouse: Path, output: Path) -> dict[str, object]:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    version = (ROOT / "VERSION").read_text(encoding="ascii").strip()
    verify_input(python_zip, lock["python"]["sha256"])
    expected_wheels = []
    for item in lock["wheels"]:
        wheel = wheelhouse / item["filename"]
        verify_input(wheel, item["sha256"])
        expected_wheels.append((item, wheel))

    with tempfile.TemporaryDirectory(prefix="weixin-windows-portable-") as temporary:
        temporary_root = Path(temporary)
        source_tree = temporary_root / "source-tree"
        copy_runtime_source(source_tree, version)
        source_zip = temporary_root / "runtime-source.zip"
        source_zip.write_bytes(zip_tree(source_tree))
        package_dir = temporary_root / "packages"
        package_dir.mkdir()
        shutil.copy2(source_zip, package_dir / source_zip.name)
        shutil.copy2(python_zip, package_dir / lock["python"]["filename"])
        wheels_dir = package_dir / "wheels"
        wheels_dir.mkdir()
        for _, wheel in expected_wheels:
            shutil.copy2(wheel, wheels_dir / wheel.name)

        package_files = [
            package_record(package_dir / "runtime-source.zip", "packages/runtime-source.zip"),
            package_record(
                package_dir / lock["python"]["filename"],
                "packages/" + lock["python"]["filename"],
            ),
            *[
                package_record(wheels_dir / wheel.name, "packages/wheels/" + wheel.name)
                for _, wheel in expected_wheels
            ],
        ]
        manifest = {
            "format": 1,
            "product": "weixin-replay-to-mp3",
            "version": version,
            "architecture": lock["architecture"],
            "python": lock["python"],
            "wheels": lock["wheels"],
            "files": package_files,
        }
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
        ).encode("ascii")
        installer = render_installer(version, manifest_bytes)
        root_name = f"weixin-replay-to-mp3-windows-portable-v{version}"
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary_output = output.with_suffix(output.suffix + ".tmp")
        with zipfile.ZipFile(temporary_output, "w", compression=zipfile.ZIP_STORED) as archive:
            entries = [
                ("install-offline.ps1", installer),
                ("package-manifest.json", manifest_bytes),
            ]
            for relative, payload in entries:
                info = zipfile.ZipInfo(f"{root_name}/{relative}", FIXED_ZIP_TIME)
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = 0
                archive.writestr(info, payload)
            package_paths = sorted(
                package_dir.rglob("*"),
                key=lambda item: item.relative_to(package_dir).as_posix(),
            )
            for path in package_paths:
                if not path.is_file():
                    continue
                relative = path.relative_to(temporary_root).as_posix()
                info = zipfile.ZipInfo(f"{root_name}/{relative}", FIXED_ZIP_TIME)
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = 0
                archive.writestr(info, path.read_bytes())
        os.replace(temporary_output, output)

    metadata = {
        "status": "built",
        "version": version,
        "architecture": lock["architecture"],
        "asset": output.name,
        "bytes": output.stat().st_size,
        "sha256": digest(output),
        "python_version": lock["python"]["version"],
        "wheel_count": len(expected_wheels),
    }
    output.with_suffix(output.suffix + ".json").write_text(
        json.dumps(metadata, sort_keys=True, indent=2) + "\n", encoding="ascii"
    )
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-embed", required=True, type=Path)
    parser.add_argument("--wheelhouse", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    version = (ROOT / "VERSION").read_text(encoding="ascii").strip()
    output = args.output or (
        ROOT / "dist" / f"weixin-replay-to-mp3-windows-portable-v{version}.zip"
    )
    print(json.dumps(build_bundle(args.python_embed, args.wheelhouse, output), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
