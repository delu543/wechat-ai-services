#!/usr/bin/env python3
"""Read-only publication contract; no private fixture data or account access."""
from __future__ import annotations
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DIRS = {'components', 'services', 'skills', 'scripts', 'requirements', 'docs'}
FILES = {'.gitignore', 'AGENTS.md', 'README.md', 'LICENSE.md', 'wechat_ai.py',
         'sources.lock.json', 'source-files.json'}
FORBIDDEN_PARTS = {'work', '.codex', '.git', '.build', '.venv', '__pycache__', 'tests', 'node_modules'}
FORBIDDEN_SUFFIXES = {'.pyc', '.sqlite', '.db', '.docx', '.mp3', '.mp4', '.m4a', '.wav',
                      '.flac', '.pem', '.key', '.safetensors', '.png', '.jpg', '.zip'}


def candidates():
    found = []
    for name in sorted(DIRS | FILES):
        entry = ROOT/name
        if entry.is_file():
            found.append(entry); continue
        if entry.is_dir():
            found.extend(p for p in entry.rglob('*') if p.is_file() and
                not (set(p.relative_to(ROOT).parts) & FORBIDDEN_PARTS) and not p.name.startswith('test_'))
    return sorted(found)


def check():
    errors = []; files = candidates()
    for path in files:
        rel = path.relative_to(ROOT)
        if path.is_symlink() or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            errors.append(f'forbidden_file:{rel}'); continue
        if path.name.startswith('.env'):
            errors.append(f'environment_file:{rel}')
        try:
            content = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            errors.append(f'non_text_source:{rel}'); continue
        patterns = [r'/Users/[^/\s]+/(?:Documents|\.codex|\.cache)/',
                    r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
                    r'gh[pousr]_[A-Za-z0-9]{30,}', r'sk-[A-Za-z0-9_-]{30,}',
                    r'https?://[^\s"<>]+[?&](?:pass_ticket|appmsg_token|exportkey)=[^\s<>"&]{12,}']
        if any(re.search(pattern, content) for pattern in patterns):
            errors.append(f'sensitive_pattern:{rel}')
        if path.suffix == '.py':
            try:
                ast.parse(content, filename=str(rel))
            except SyntaxError:
                errors.append(f'python_syntax:{rel}')
    for path in (ROOT/'skills').glob('*/SKILL.md'):
        text = path.read_text(encoding='utf-8')
        if not text.startswith('---\nname: '+path.parent.name+'\n') or 'description:' not in text:
            errors.append(f'skill_metadata:{path.parent.name}')
        if 'setup' in path.parent.name and 'allow_implicit_invocation: false' not in (path.parent/'agents/openai.yaml').read_text():
            errors.append('setup_must_be_explicit')
        for link in re.findall(r'\]\(([^)]+)\)', text):
            if not link.startswith(('http', '#')) and not (path.parent/link).is_file():
                errors.append(f'broken_skill_reference:{path.parent.name}:{link}')
    if (ROOT/'.git').exists():
        tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
        allowed = {str(p.relative_to(ROOT)) for p in files}
        errors.extend('tracked_outside_public_contract:'+p for p in tracked if p and p not in allowed)
    manifest = ROOT/'source-files.json'
    if manifest.is_file():
        expected = json.loads(manifest.read_text())
        actual = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in files if p != manifest}
        if expected != actual:
            errors.append('source_manifest_mismatch')
    return {'files_checked': len(files), 'independent_skills': len(list((ROOT/'skills').glob('*/SKILL.md'))),
            'errors': errors, 'passed': not errors}


if __name__ == '__main__':
    result = check()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['passed'] else 1)
