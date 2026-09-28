"""Explicit, isolated runtimes. No installation or account access on import."""
from __future__ import annotations
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REPLAY = ROOT / 'components/chat/portable_skill/skills/wechat-replay-word/scripts'
SERVICES = ('chat', 'media', 'articles', 'replay', 'courses')


def home():
    if sys.platform == 'win32':
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 28, None, 0, buf):
            raise RuntimeError('cannot_locate_local_appdata')
        return Path(buf.value) / 'WeChatAIServices'
    return Path.home() / 'Library/Application Support/WeChatAIServices'


def private_dir(path):
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
            raise ValueError('symlink_or_junction_rejected')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if sys.platform != 'win32' and (path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077):
        raise ValueError('private_directory_required')
    return path


def python_path(service):
    name = 'replay' if service == 'courses' else service
    root = home() / 'runtimes' / name
    return root / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')


def supported(service):
    if service in ('replay', 'courses'):
        return sys.platform == 'darwin' and platform.machine() == 'arm64'
    return sys.platform == 'darwin' or (sys.platform == 'win32' and service in ('chat', 'media'))


IMPORTS = {'chat': 'import Crypto,zstandard,imageio_ffmpeg',
           'media': 'import imageio_ffmpeg,yt_dlp',
           'articles': 'import bs4,httpx,markdownify,yaml,docx,v8serialize',
           'replay': 'import mlx_whisper,mitmproxy,cryptography,imageio_ffmpeg,docx'}
IMPORTS['chat'] = 'import Crypto,zstandard,imageio_ffmpeg; import ' + ('pysilk' if sys.platform == 'win32' else 'pilk')


def doctor(service):
    selected = 'replay' if service == 'courses' else service
    python = python_path(service)
    ready = False
    if supported(service) and python.is_file():
        result = subprocess.run([str(python), '-c', IMPORTS[selected]], capture_output=True, timeout=60)
        ready = result.returncode == 0
    if service == 'chat' and sys.platform == 'darwin':
        ready = ready and (ROOT/'components/chat/.build/release/wechat-voice-mp4').is_file()
    if service == 'media' and ready:
        probe = subprocess.run([str(python), '-c',
            'import imageio_ffmpeg,subprocess; subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-version"], check=True, stdout=subprocess.DEVNULL)'],
            capture_output=True, timeout=30)
        ready = probe.returncode == 0 and any((python.parent/n).is_file() for n in ('deno', 'deno.exe'))
    report = {'service': service, 'platform_supported': supported(service),
            'state': 'environment_ready' if ready else 'needs_install' if supported(service) else 'unsupported_platform',
            'account_checked': False, 'real_flow_verified': False,
            'model_downloaded': False, 'network_settings_changed': False}
    if service == 'replay':
        renderer = shutil.which('libreoffice') or shutil.which('soffice')
        if not renderer and sys.platform == 'darwin':
            candidate = Path('/Applications/LibreOffice.app/Contents/MacOS/soffice')
            renderer = str(candidate) if candidate.is_file() else None
        report['word_renderer_available'] = bool(renderer)
    return report


def install(service):
    if not supported(service):
        raise ValueError('unsupported_platform')
    if not (3, 10) <= sys.version_info[:2] < (3, 14):
        raise ValueError('use_standard_cpython_310_to_313')
    selected = 'replay' if service == 'courses' else service
    if selected == 'replay' and sys.version_info[:2] < (3, 12):
        raise ValueError('replay_requires_cpython_312_or_313')
    if selected == 'chat' and sys.platform == 'win32' and sys.version_info[:2] != (3, 12):
        raise ValueError('windows_chat_requires_cpython_312_x64')
    private_dir(home()); private_dir(home()/'runtimes')
    python = python_path(service); envroot = python.parent.parent
    marker = envroot / '.wechat-ai-services'
    if envroot.exists() and not marker.is_file():
        raise ValueError('unmanaged_runtime_preserved')
    if not envroot.exists():
        private_dir(envroot)
        marker.write_text(selected, encoding='utf-8')
    if not python.exists():
        subprocess.run([sys.executable, '-m', 'venv', str(envroot)], check=True)
    if selected in ('chat', 'media'):
        filename = 'requirements-windows.txt' if sys.platform == 'win32' else ('requirements-runtime.txt' if selected == 'chat' else 'requirements-macos.txt')
        req = ROOT/'components'/selected/('scripts' if selected == 'chat' else '')/filename
        command = [str(python), '-m', 'pip', 'install', '--require-hashes', '-r', str(req)]
    else:
        req = ROOT/'requirements'/f'{selected}.txt'
        command = [str(python), '-m', 'pip', 'install', '-r', str(req)]
    subprocess.run(command, check=True)
    if selected == 'chat' and sys.platform == 'darwin':
        subprocess.run(['swift', 'build', '-c', 'release'], cwd=ROOT/'components/chat', check=True)
    result = doctor(service)
    if result['state'] != 'environment_ready':
        raise RuntimeError('installed_dependencies_failed_doctor')
    return result


def child_env():
    env = os.environ.copy()
    env['WEIXIN_REPLAY_DATA_ROOT'] = str(home()/'media/data')
    env['WEIXIN_REPLAY_OUTPUT_ROOT'] = str(home()/'media/exports')
    env['PYTHONUTF8'] = '1'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    return env
