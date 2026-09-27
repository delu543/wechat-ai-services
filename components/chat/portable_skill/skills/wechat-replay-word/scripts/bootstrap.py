"""Explicit user-local replay dependencies. Doctor never installs or opens WeChat."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import uuid

PACKAGES = ['mitmproxy==12.2.3', 'cryptography==48.0.1', 'mlx-whisper==0.4.3',
            'imageio-ffmpeg==0.6.0', 'python-docx==1.2.0']


def paths():
    base = Path.home() / 'Library/Application Support/WeChatAIServicesReplayWord'
    return base, base / 'runtime/bin/python'


def doctor():
    base, python = paths()
    supported = sys.platform == 'darwin' and platform.machine() == 'arm64'
    ready = False
    if supported and python.is_file():
        result = subprocess.run([str(python), '-c',
            'import mitmproxy,cryptography,mlx_whisper,imageio_ffmpeg,docx'],
            capture_output=True, timeout=60)
        ready = result.returncode == 0
    return {'platform_supported': supported, 'runtime_ready': ready,
            'state': 'ready_for_local_model' if ready else 'needs_install' if supported else 'unsupported_live_capture_platform',
            'wechat_accessed': False, 'network_settings_changed': False,
            'model_auto_downloaded': False}


def install():
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise ValueError('current_live_word_route_requires_apple_silicon')
    if not (3, 12) <= sys.version_info[:2] < (3, 14):
        raise ValueError('use_standard_cpython_312_or_313')
    base, python = paths()
    if base.is_symlink(): raise ValueError('unsafe_runtime_root')
    if base.exists() and base.stat().st_mode & 0o077: raise ValueError('private_runtime_mode_required')
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not python.exists():
        if (base / 'runtime').exists(): raise ValueError('incomplete_runtime_preserved')
        subprocess.run([sys.executable, '-m', 'venv', str(base / 'runtime')], check=True)
    subprocess.run([str(python), '-m', 'pip', 'install', '--only-binary=:all:', *PACKAGES], check=True)
    print(json.dumps(doctor()))


def configure(args):
    if not doctor()['runtime_ready']: raise ValueError('runtime_not_ready')
    model = args.model.resolve()
    if not (model/'weights.safetensors').is_file() or not (model/'config.json').is_file():
        raise ValueError('local_mlx_model_required')
    from network_profile import inspect_network
    network = inspect_network(args.service)
    base, python = paths()
    root = base / 'tasks' / uuid.uuid4().hex
    root.mkdir(parents=True, mode=0o700)
    result = subprocess.check_output([str(python), '-c', 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())'], text=True)
    cfg = {'root': str(root), 'account_name': args.name, 'seed_url': args.seed,
           'proxy': network['upstream'], 'parallel_ranges': True, 'min_free_bytes': 20*1024**3,
           'ffmpeg': result.strip(), 'asr_python': str(python), 'word_python': str(python), 'model': str(model)}
    from catalog_protocol import private_save
    private_save(root/'batch_config.json', cfg)
    print(json.dumps({'task_root': str(root), 'state': 'configured_not_captured'}, ensure_ascii=False))


if __name__ == '__main__':
    os.umask(0o077)
    p = argparse.ArgumentParser(); sub = p.add_subparsers(dest='action', required=True)
    sub.add_parser('doctor')
    sub.add_parser('install')
    c = sub.add_parser('configure')
    c.add_argument('--name', required=True); c.add_argument('--seed', required=True)
    c.add_argument('--model', type=Path, required=True); c.add_argument('--service', required=True)
    args = p.parse_args()
    if args.action == 'doctor': print(json.dumps(doctor()))
    elif args.action == 'install': install()
    else: configure(args)
