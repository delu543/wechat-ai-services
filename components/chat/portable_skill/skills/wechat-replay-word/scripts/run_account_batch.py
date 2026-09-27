"""Frozen catalog -> bounded audio workers -> one local ASR -> draft Word files.

No UI, certificates, cloud, cleanup, automatic model download or publishing.
Word drafts are NOT marked delivery-ready until separate rendered-page review.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from zoneinfo import ZoneInfo
from recovery_audio import acquire_recovery as acquire, NetworkBlocked
from transcribe_local import digest, save


def make_plan(catalog, cfg):
    if not catalog.get('complete') or not catalog.get('pages') or catalog['pages'][-1]['continueFlag'] != 0:
        raise ValueError('catalog_terminal_required')
    items = catalog['items']
    if len({i['id'] for i in items}) != len(items) or any(i['account'] != catalog['account'] for i in items):
        raise ValueError('catalog_identity_error')
    account = hashlib.sha256(catalog['account'].encode()).hexdigest()[:24]
    entries = []
    for n, item in enumerate(items, 1):
        replay = hashlib.sha256(item['id'].encode()).hexdigest()[:24]
        entries.append({'ordinal': n, 'account_id': account, 'replay_id': replay,
                        'title': item['title'], 'date': datetime.fromtimestamp(item['created'],
                        ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d'), 'source_url':
                        '微信视频号账号直播回放目录 入口 '+cfg['seed_url']})
    return entries


def run(cfg, limit):
    scripts = Path(__file__).resolve().parent
    root = Path(cfg['root']); batch = root/'batch'; batch.mkdir(exist_ok=True)
    with (batch/'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        catalog_path = root/'capture-session/capture/catalog.private.json'
        catalog = json.loads(catalog_path.read_text())
        entries = make_plan(catalog, cfg)
        plan_path = batch/'plan.private.json'
        plan = {'catalog_sha256': digest(catalog_path), 'entries': entries}
        if plan_path.exists() and json.loads(plan_path.read_text()) != plan:
            raise ValueError('frozen_plan_changed')
        if not plan_path.exists(): save(plan_path, plan)
        statuses_path = batch/'status.json'
        statuses = json.loads(statuses_path.read_text()) if statuses_path.exists() else {}
        env = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false')
        # Explicit already-verified seed mapping: never repeat the initial sample.
        reused = set()
        if cfg.get('sample_manifest'):
            sample = json.loads(Path(cfg['sample_manifest']).read_text())['replays'][0]
            sample_transcript = json.loads(Path(sample['transcript']).read_text())
            matches = [i for i, item in enumerate(catalog['items']) if item['title'] == sample['title']
                       and len(item['media']) == 1 and abs(item['media'][0]['videoPlayLen']-sample_transcript['duration']) < 1]
            if len(matches) != 1 or not sample_transcript.get('complete'):
                raise ValueError('seed_reuse_binding_changed')
            reused.add(matches[0])
            statuses[f'{matches[0]+1:04d}'] = {'status': 'verified_sample_reused', 'transcript': sample['transcript'],
                                              'word': cfg['sample_word']}
        todo = [i for i in range(len(entries)) if i not in reused]
        if limit: todo = todo[:limit]
        save(statuses_path, statuses)
        with ThreadPoolExecutor(max_workers=2) as workers:
            pending = []
            for index in todo:
                key = f'{index+1:04d}'
                if statuses.get(key, {}).get('status') in ('word_draft_ready', 'word_verified'):
                    word = Path(statuses[key]['word'])
                    proof = json.loads(word.with_suffix('.verification.json').read_text())
                    if not proof.get('body_exact_match') or digest(word) != proof.get('docx_sha256'):
                        raise ValueError('completed_word_checkpoint_mismatch')
                    continue
                pending.append(index)
            remaining = iter(pending)
            futures = {}
            def schedule():
                next_index = next(remaining, None)
                if next_index is not None:
                    futures[next_index] = workers.submit(acquire, catalog['items'][next_index],
                        batch/'items'/f'{next_index+1:04d}', cfg)
            schedule(); schedule()
            for index in pending:
                key = f'{index+1:04d}'; folder = batch/'items'/key; entry = entries[index]
                try:
                    report = futures[index].result()
                    statuses[key] = {'status': 'asr_running', 'audio_seconds': report['duration'], 'updated': time.time()}
                    save(statuses_path, statuses)
                    asrwork = folder/'asr'; logs = folder/'asr.log'
                    argv = [cfg['asr_python'], str(scripts/'transcribe_local.py'), '--media', str(folder/'audio.m4a'),
                            '--model', cfg['model'], '--work', str(asrwork), '--account', entry['account_id'],
                            '--replay', entry['replay_id'], '--expected-duration', str(report['duration'])]
                    attempts_path = folder/'asr-attempts.json'
                    attempts = json.loads(attempts_path.read_text()) if attempts_path.exists() else []
                    if len(attempts) >= 2: raise ValueError('asr_attempt_limit')
                    attempts.append({'started': time.time()}); save(attempts_path, attempts)
                    with logs.open('ab') as stream:
                        p = subprocess.run(argv, stdout=stream, stderr=stream, env=env, timeout=7200)
                    attempts[-1]['returncode'] = p.returncode; save(attempts_path, attempts)
                    if p.returncode: raise ValueError('local_asr_failed')
                    transcripts = list(asrwork.glob('*/transcript.json'))
                    if len(transcripts) != 1: raise ValueError('ambiguous_transcript')
                    entry = dict(entry, transcript=str(transcripts[0]))
                    manifest = {'account_name': cfg['account_name'], 'catalog_complete': False, 'replays': [entry]}
                    manifest_path = folder/'word-manifest.json'; save(manifest_path, manifest)
                    safe_title = re.sub(r'[^\w\u3400-\u9fff-]', '_', entry['title'])[:60]
                    word = root/'outputs/account'/f'{key}_{entry["date"]}_{safe_title}.docx'
                    p = subprocess.run([cfg['word_python'], str(scripts/'build_word.py'), str(manifest_path), str(word)],
                                       capture_output=True, timeout=180)
                    if p.returncode: raise ValueError('word_build_failed')
                    statuses[key] = {'status': 'word_draft_ready', 'transcript': str(transcripts[0]),
                                     'word': str(word), 'body_exact_match': True, 'render_review_complete': False}
                except NetworkBlocked as e:
                    statuses[key] = {'status': 'needs_review', 'error_type': 'NetworkBlocked', 'reason': str(e)}
                    save(statuses_path, statuses)
                    for future in futures.values(): future.cancel()
                    raise
                except Exception as e:
                    # Known errors have fixed identifiers; never expose signed media data.
                    statuses[key] = {'status': 'needs_review', 'error_type': type(e).__name__,
                                     'reason': str(e) if isinstance(e, ValueError) else 'see_local_stage_evidence'}
                save(statuses_path, statuses)
                schedule()
                print(json.dumps({'ordinal': index+1, 'status': statuses[key]['status']}, ensure_ascii=False), flush=True)
        counts = {}
        for item in statuses.values(): counts[item['status']] = counts.get(item['status'], 0)+1
        summary = {'catalog_count': len(entries), 'counts': counts, 'delivery_complete': False,
                   'next': 'render_and_inspect_all_word_pages_then_package', 'finished_at': time.time()}
        save(batch/'run-summary.json', summary); print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    os.umask(0o077)
    p = argparse.ArgumentParser(); p.add_argument('config', type=Path); p.add_argument('--limit', type=int, default=0)
    args = p.parse_args(); run(json.loads(args.config.read_text()), args.limit)
