"""Windows source-kit installer; installation never touches WeChat or keys."""
from __future__ import annotations

import ctypes
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PARTS = ("content_vault", "direct_vault", "live_tools", "portable_skill", "scripts",
                "Package.swift", "README.md", "AGENTS.md", "PRIVACY.md", "SECURITY.md", "docs")
EXCLUDE = {".git", ".codex", ".build", "__pycache__", "work", "outputs", "tasks", ".venv"}
SKILLS = ("wechat-local-export", "wechat-local-export-setup", "wechat-replay-word")
MANAGED = "wechat-local-export-windows-v1"


def support_path():
    buffer = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 28, None, 0, buffer) != 0:
        raise RuntimeError("无法定位私有安装根目录")
    return Path(buffer.value) / "WeChatAIServicesChat"


def source_files(root=ROOT):
    paths = []
    for name in SOURCE_PARTS:
        source = root / name
        if not source.exists():
            raise RuntimeError("源码包不完整")
        for path in ([source] if source.is_file() else source.rglob("*")):
            relative = path.relative_to(root)
            if any(part in EXCLUDE for part in relative.parts):
                continue
            if path.is_symlink() or (sys.platform == "win32" and path.is_junction()):
                raise RuntimeError("源码含不受支持的链接")
            if path.is_file() and path.suffix not in {".pyc", ".pyo"}:
                paths.append(relative)
    return sorted(paths)


def source_digest(root=ROOT):
    digest = hashlib.sha256()
    for relative in source_files(root):
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256((root / relative).read_bytes()).digest())
    return digest.hexdigest()


def runtime_path(support):
    return support / "tools/python/Scripts/python.exe"


def check_dependencies():
    for name in ("win32api", "win32security", "win32crypt", "psutil", "Crypto", "zstandard", "pysilk", "imageio_ffmpeg"):
        importlib.import_module(name)


def register(support):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from live_tools.platform_support import is_private, private_mkdir, require_no_reparse
    from live_tools.windows.state import write_json
    check_dependencies()
    require_no_reparse(support)
    if not is_private(support):
        raise RuntimeError("现有安装根目录权限不安全；拒绝修改")
    release = source_digest()
    destination = support / "app/releases" / release
    if destination.exists():
        if source_digest(destination) != release:
            raise RuntimeError("现有版本内容变化；拒绝覆盖")
    else:
        private_mkdir(destination)
        for relative in source_files():
            target = destination / relative
            private_mkdir(target.parent)
            with target.open("xb") as handle:
                handle.write((ROOT / relative).read_bytes())
        if source_digest(destination) != release:
            raise RuntimeError("源码副本摘要不一致")
    skill_root = Path.home() / ".agents/skills"
    require_no_reparse(skill_root)
    skill_root.mkdir(parents=True, exist_ok=True)
    # Check every destination before moving any existing skill.
    for name in SKILLS:
        target = skill_root / name
        require_no_reparse(target)
        if target.exists():
            marker = target / ".windows-managed.json"
            if not marker.is_file() or json.loads(marker.read_text()).get("owner") != MANAGED:
                raise RuntimeError("存在非本安装器管理的同名 Skill；保留原件并停止")
    for name in SKILLS:
        target = skill_root / name
        if target.exists():
            marker = json.loads((target / ".windows-managed.json").read_text())
            if marker.get("release") == release:
                continue
            backup = support / "skill-backups" / (name + "-" + secrets.token_hex(12))
            private_mkdir(backup.parent)
            target.rename(backup)  # Preserve prior managed version; no deletion.
        private_mkdir(target)
        shutil.copytree(destination / "portable_skill/skills" / name, target, dirs_exist_ok=True)
        write_json(target / ".windows-managed.json", {"owner": MANAGED, "release": release})
    private_mkdir(support / "tasks")
    private_mkdir(support / "exports")
    write_json(support / "app/current.json", {"release": release, "owner": MANAGED})
    return {"status": "installed", "platform": "windows-preview", "source_verified": True,
            "skills_registered": list(SKILLS), "wechat_accessed": False, "keys_initialized": False}


def main(argv=None):
    action = (argv or sys.argv[1:] or ["doctor"])[0]
    try:
        if action not in {"doctor", "install", "register"}:
            raise RuntimeError("不支持的安装动作")
        if sys.platform != "win32" or sys.version_info[:2] != (3, 12) or sys.maxsize <= 2**32:
            raise RuntimeError("需要 Windows 10/11 x64 和 CPython 3.12")
        support = support_path()
        runtime = runtime_path(support)
        if action == "doctor":
            ready = runtime.is_file() and (support / "app/current.json").is_file()
            if ready:
                check_dependencies()
                if str(ROOT) not in sys.path:
                    sys.path.insert(0, str(ROOT))
                from live_tools.platform_support import is_private, require_no_reparse
                require_no_reparse(support)
                marker = json.loads((support / "app/current.json").read_text())
                ready = is_private(support) and marker.get("owner") == MANAGED and marker.get("release") == source_digest()
            print(json.dumps({"status": "ready" if ready else "needs_install",
                              "wechat_accessed": False, "writes_performed": False}))
            return 0 if ready else 2
        if action == "install":
            if not support.is_dir() or not (support / ".windows-bootstrap-owner").is_file():
                raise RuntimeError("请通过 codex_bootstrap.ps1 创建私有安装根目录")
            # Bootstrap root ACL was established by PowerShell before this step.
            if not runtime.exists():
                venv.EnvBuilder(with_pip=True).create(support / "tools/python")
            subprocess.run([str(runtime), "-m", "pip", "--isolated", "install", "--disable-pip-version-check",
                            "--only-binary=:all:", "--require-hashes", "--no-deps",
                            "--index-url", "https://pypi.org/simple", "-r", str(ROOT / "scripts/requirements-windows.txt")],
                           check=True, timeout=600)
            return subprocess.run([str(runtime), str(Path(__file__).resolve()), "register"], check=False).returncode
        print(json.dumps(register(support), ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        # Report code locations, never exception text, local paths or frame values.
        locations = []
        trace = error.__traceback__
        while trace:
            code = Path(trace.tb_frame.f_code.co_filename)
            if code.is_relative_to(ROOT):
                locations.append(f"{code.relative_to(ROOT).as_posix()}:{trace.tb_lineno}")
            trace = trace.tb_next
        print(json.dumps({"status": "blocked", "error_type": type(error).__name__,
                          "code_locations": locations,
                          "detail": "Windows 安装未通过：检查 Python 3.12 x64、依赖、VC++ 运行库、私有目录权限及同名 Skill；未访问微信"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
