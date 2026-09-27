"""Portable collection adapter: audio-first selection, serial ASR, full Word.

The course platform's account-specific acquisition stays outside this adapter.
Inputs are authorized local media, grouped in an explicit frozen manifest.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.runtime import REPLAY, home, private_dir
sys.path.insert(0, str(REPLAY))
from transcribe_local import digest, save, signature
from build_word import build


def read_manifest(path):
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema') != 1 or not data.get('collections'):
        raise ValueError('collection_manifest_schema_1_required')
    collections, seen = [], set()
    for collection in data['collections']:
        cid = str(collection['id'])
        if not cid or cid in seen or not collection.get('lessons'):
            raise ValueError('empty_or_duplicate_collection')
        seen.add(cid); lessons = []; ids = set()
        for lesson in collection['lessons']:
            lid = str(lesson['id'])
            if not lid or lid in ids:
                raise ValueError('empty_or_duplicate_lesson')
            ids.add(lid)
            candidates = lesson.get('sources', [])
            audios = [s for s in candidates if s.get('kind') == 'audio']
            videos = [s for s in candidates if s.get('kind') == 'video']
            chosen = audios or videos
            if len(chosen) != 1:
                raise ValueError('one_unambiguous_preferred_source_required')
            source = Path(chosen[0]['path']).expanduser()
            if not source.is_absolute():
                source = path.parent/source
            source = source.resolve()
            if not source.is_file():
                raise ValueError('selected_local_media_missing')
            texts = lesson.get('source_text', [])
            if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
                raise ValueError('source_text_must_be_list')
            lessons.append({'id': lid, 'title': str(lesson['title']), 'media': str(source),
                'kind': chosen[0]['kind'], 'source_sha256': digest(source), 'source_text': texts})
        collections.append({'id': cid, 'title': str(collection['title']), 'lessons': lessons})
    return {'schema': 1, 'collections': collections}


def run(manifest, model, retry_failed=False):
    frozen = read_manifest(manifest)
    model = Path(model).resolve()
    if not all((model/n).is_file() for n in ('weights.safetensors', 'config.json')):
        raise ValueError('local_model_required')
    identity = signature({'manifest': frozen, 'model': digest(model/'weights.safetensors'),
                          'config': digest(model/'config.json')})
    base = private_dir(home()/'courses/tasks'/identity)
    # One suite-wide ASR worker; replay batch remains separately explicit.
    with (private_dir(home()/'courses')/'worker.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        save(base/'manifest.json', frozen)
        statepath = base/'status.json'
        state = json.loads(statepath.read_text()) if statepath.exists() else {'lessons': {}, 'collections': {}}
        for collection in frozen['collections']:
            entries = []; failed = False
            for lesson in collection['lessons']:
                key = signature({'collection': collection['id'], 'lesson': lesson['id']})
                previous = state['lessons'].get(key, {})
                if previous.get('status') == 'failed' and (not retry_failed or previous.get('attempts', 0) >= 2):
                    failed = True; continue
                task = private_dir(base/'lessons'/key)
                attempts = previous.get('attempts', 0)
                try:
                    if digest(lesson['media']) != lesson['source_sha256']:
                        raise ValueError('source_changed')
                    if previous.get('status') == 'complete':
                        transcript = Path(previous['transcript'])
                        if digest(transcript) != previous['sha256']:
                            raise ValueError('transcript_changed')
                    else:
                        attempts += 1
                        proc = subprocess.run([sys.executable, str(REPLAY/'transcribe_local.py'),
                            '--media', lesson['media'], '--model', str(model), '--work', str(task),
                            '--account', collection['id'], '--replay', lesson['id']],
                            text=True, capture_output=True)
                        if proc.returncode:
                            raise RuntimeError('asr_failed_evidence_retained')
                        response = json.loads(proc.stdout.strip().splitlines()[-1])
                        transcript = Path(response['transcript'])
                    state['lessons'][key] = {'status': 'complete', 'attempts': attempts,
                        'transcript': str(transcript), 'sha256': digest(transcript)}
                    entries.append({'account_id': collection['id'], 'replay_id': lesson['id'],
                        'title': lesson['title'], 'source_url': '用户授权本地媒体',
                        'transcript': str(transcript), 'source_text': lesson['source_text']})
                except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
                    state['lessons'][key] = {'status': 'failed', 'attempts': attempts,
                        'reason': 'needs_local_review'}
                    failed = True
                save(statepath, state)
            ckey = signature({'collection': collection['id']})
            if failed or len(entries) != len(collection['lessons']):
                state['collections'][ckey] = {'status': 'incomplete_no_word'}
            else:
                wordmanifest = base/(ckey+'.manifest.json')
                save(wordmanifest, {'kind': 'course', 'account_name': collection['title'],
                    'catalog_complete': True, 'replays': entries})
                output = base/'Word'/(ckey+'.docx')
                report = build(wordmanifest, output)
                state['collections'][ckey] = {'status': 'body_verified_render_review_pending',
                    'output': str(output), 'verification': report}
            save(statepath, state)
        print(json.dumps({'task': str(base), 'state': state}, ensure_ascii=False))
        return 2 if any(v['status'] != 'complete' for v in state['lessons'].values()) else 0


def main():
    p = argparse.ArgumentParser(); sub = p.add_subparsers(dest='action', required=True)
    plan = sub.add_parser('plan'); plan.add_argument('manifest')
    r = sub.add_parser('run'); r.add_argument('manifest'); r.add_argument('--model', required=True)
    r.add_argument('--retry-failed', action='store_true', help='Explicit retry after resolving the cause; two attempts maximum.')
    a = p.parse_args()
    if a.action == 'plan':
        frozen = read_manifest(a.manifest)
        print(json.dumps({'collections': len(frozen['collections']),
            'lessons': sum(len(c['lessons']) for c in frozen['collections']),
            'manifest_sha256': signature(frozen)}, ensure_ascii=False)); return 0
    return run(a.manifest, a.model, a.retry_failed)


if __name__ == '__main__':
    os.umask(0o077)
    raise SystemExit(main())
