#!/usr/bin/env python3
"""One explicit router, independent consent and runtime contracts."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from services.runtime import ROOT, REPLAY, SERVICES, home, private_dir, python_path, doctor, install, child_env, supported


def command(service, args):
    if not supported(service):
        raise ValueError('unsupported_platform')
    python = python_path(service)
    if not python.is_file():
        raise ValueError('run_explicit_install_for_selected_service')
    env = child_env()
    env['PATH'] = str(python.parent) + os.pathsep + env.get('PATH', '')
    if service == 'chat':
        entry = ROOT/'components/chat/portable_skill/scripts/dev_backend.py'
        cwd = entry.parents[2]
    elif service == 'media':
        entry = ROOT/'components/media/weixin_replay_cli.py'; cwd = entry.parent
        env['FFMPEG'] = subprocess.check_output([str(python), '-c',
            'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())'], text=True, timeout=30).strip()
    elif service == 'articles':
        entry = ROOT/'services/article_entry.py'; cwd = ROOT
    elif service == 'courses':
        entry = ROOT/'services/courses.py'; cwd = ROOT
    else:
        allowed = {'configure': 'bootstrap.py', 'capture': 'capture_session.py',
                   'prepare': 'prepare_capture.py', 'batch': 'run_account_batch.py',
                   'dashboard': 'dashboard/server.py', 'transcribe': 'transcribe_local.py',
                   'word': 'build_word.py'}
        if not args or args[0] not in allowed:
            raise ValueError('replay_action_required: '+','.join(allowed))
        action, args = args[0], args[1:]
        entry = REPLAY/allowed[action]; cwd = REPLAY
        if action == 'configure':
            # This adapter uses the suite runtime while retaining upstream network/config rules.
            entry = ROOT/'services/replay_config.py'; cwd = ROOT
    return [str(python), str(entry), *args], cwd, env


def main():
    p = argparse.ArgumentParser(description='微信 AI 服务：先 doctor，再按需 install/run；不自动初始化账号。')
    sub = p.add_subparsers(dest='action', required=True)
    for action in ('doctor', 'install'):
        x = sub.add_parser(action); x.add_argument('service', choices=SERVICES + (('all',) if action == 'doctor' else ()))
    sub.add_parser('capabilities')
    r = sub.add_parser('run'); r.add_argument('service', choices=SERVICES); r.add_argument('args', nargs=argparse.REMAINDER)
    a = p.parse_args()
    if a.action == 'capabilities':
        print((ROOT/'docs/capabilities.json').read_text(encoding='utf-8')); return 0
    if a.action == 'doctor':
        result = [doctor(s) for s in SERVICES] if a.service == 'all' else doctor(a.service)
        print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
    if a.action == 'install':
        print(json.dumps(install(a.service), ensure_ascii=False)); return 0
    args = a.args[1:] if a.args[:1] == ['--'] else a.args
    argv, cwd, env = command(a.service, args)
    return subprocess.call(argv, cwd=cwd, env=env)


if __name__ == '__main__':
    os.umask(0o077)
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({'status': 'needs_attention', 'reason': str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
