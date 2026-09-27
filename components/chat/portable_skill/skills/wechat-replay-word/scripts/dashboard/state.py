"""Read-only, sanitized projection of a replay batch. Never imports worker code."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time


class BatchView:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        self.cache = {}
        self.latest = 0
        self.read_errors = 0

    def safe(self, path):
        path = Path(path)
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise ValueError('unsafe_task_path')
        return path

    def read(self, path, default):
        try:
            path = self.safe(path)
            if not path.exists():
                return default
            st = path.stat()
            self.latest = max(self.latest, st.st_mtime)
            if st.st_size > 20 * 1024 * 1024:
                raise ValueError('oversized_state')
            stamp = (st.st_mtime_ns, st.st_size)
            cached = self.cache.get(str(path))
            if cached and cached[0] == stamp:
                return cached[1]
            result = json.loads(path.read_text())
            self.cache[str(path)] = (stamp, result)
            return result
        except (OSError, ValueError):
            self.read_errors += 1
            return default

    def processes(self):
        """Match this task's script + config; PID alone can be reused."""
        try:
            rows = subprocess.check_output(
                ['ps', '-axo', 'pid=,stat=,etime=,command='], text=True, timeout=5).splitlines()
        except (OSError, subprocess.SubprocessError):
            return None
        result = []
        for row in rows:
            fields = row.strip().split(None, 3)
            if len(fields) != 4:
                continue
            pid, stat, elapsed, command = fields
            invocation = re.fullmatch(
                r'.*?python(?:\d+(?:\.\d+)*)?\s+([^;\n]+?\.py)\s+(.+)', command)
            if not invocation or invocation[1].startswith('-'):
                continue
            script, arguments = invocation.groups()
            main = (Path(script).name == 'run_account_batch.py'
                    and arguments.strip() == str(self.root / 'batch_config.json'))
            recovery = (Path(script).name == 'recover_failed_batch.py'
                        and arguments.strip() == '--task-root ' + str(self.root))
            if main or recovery:
                result.append({'pid': int(pid), 'paused': 'T' in stat,
                               'zombie': 'Z' in stat, 'elapsed': elapsed,
                               'kind': 'recovery' if recovery else 'main'})
        return result

    def word(self, path):
        try:
            path = self.safe(path)
            if not path.is_file():
                return False, False
            proof = self.read(path.with_suffix('.verification.json'), {})
            stamp = (path.stat().st_mtime_ns, path.stat().st_size)
            key = 'hash:' + str(path)
            cached = self.cache.get(key)
            if not cached or cached[0] != stamp:
                cached = (stamp, hashlib.sha256(path.read_bytes()).hexdigest())
                self.cache[key] = cached
            valid = proof.get('body_exact_match') and proof.get('docx_sha256') == cached[1]
            return bool(valid), bool(valid and proof.get('render_review_complete'))
        except (OSError, ValueError):
            return False, False

    def snapshot(self):
        self.latest = 0
        self.read_errors = 0
        cfg = self.read(self.root / 'batch_config.json', {})
        plan = self.read(self.root / 'batch/plan.private.json', {})
        statuses = self.read(self.root / 'batch/status.json', {})
        recovery = self.read(self.root / 'recovery/status.json', {})
        processes = self.processes()
        alive = bool(processes and any(not p['zombie'] for p in processes))
        paused = bool(processes and all(p['paused'] for p in processes))
        items = []
        for entry in plan.get('entries', []):
            ordinal = entry['ordinal']
            key = f'{ordinal:04d}'
            folder = self.root / 'batch/items' / key
            status = statuses.get(key, {})
            repaired = recovery.get(key, {})
            if repaired and status.get('status') not in ('word_draft_ready', 'word_verified', 'verified_sample_reused', 'asr_running'):
                status = {'status': 'recovery_' + repaired.get('status', ''), 'reason': repaired.get('reason', '')}
                if repaired.get('status') != 'audio_ready':
                    folder = self.root / 'recovery/items' / key
            audio = self.read(folder / 'audio-verification.json', {})
            attempts = [self.read(p, {}) for p in sorted(folder.glob('audio-attempt-*.json'))]
            blocks = [self.read(p, {}) for p in folder.glob('asr/*/blocks/*.json')]
            processed = max((b.get('end', 0) for b in blocks if b.get('status') == 'complete'), default=0)
            duration = audio.get('duration', status.get('audio_seconds', 0))
            ready, reviewed = self.word(status['word']) if status.get('word') else (False, False)
            stage = 'waiting'
            reason = ''
            if ready:
                stage = 'reviewed' if reviewed else 'word_ready'
            elif status.get('word'):
                stage, reason = 'error', 'word_verification_missing_or_mismatch'
            elif status.get('status') == 'recovery_downloading':
                stage = 'downloading' if alive and not paused else 'interrupted'
            elif status.get('status') == 'recovery_retry_pending':
                stage, reason = 'retry_pending', status.get('reason', '')
            elif status.get('status') in ('needs_review', 'recovery_error'):
                stage, reason = 'error', status.get('reason', 'needs_review')
            elif status.get('status') == 'asr_running':
                stage = 'transcribing' if alive and not paused else 'interrupted'
            elif audio.get('full_audio_decode'):
                stage = 'audio_ready'
            elif attempts:
                last = attempts[-1]
                if last.get('status') == 'running':
                    stage = 'downloading' if alive and not paused else 'interrupted'
                elif last.get('status') == 'failed':
                    stage = 'error' if len(attempts) >= 2 else 'retry_pending'
                    reason = last.get('reason', 'download_failed')
            partial_bytes = 0
            for p in folder.glob('*.partial.m4a'):
                try:
                    st = self.safe(p).stat()
                    self.latest = max(self.latest, st.st_mtime)
                    partial_bytes += st.st_size
                except (OSError, ValueError):
                    pass
            # Do not render arbitrary exception strings, signed URLs or raw logs.
            if not isinstance(reason, str) or not all(c.isalnum() or c in '_-' for c in reason):
                reason = 'see_local_diagnostics'
            items.append({'ordinal': ordinal, 'title': str(entry.get('title', ''))[:500],
                          'date': entry.get('date', ''), 'stage': stage, 'reason': reason,
                          'audio_ready': bool(audio.get('full_audio_decode') or ready),
                          'word_ready': ready, 'reviewed': reviewed, 'duration': duration,
                          'processed_seconds': processed, 'blocks': len(blocks),
                          'attempts': len(attempts), 'partial_bytes': partial_bytes})
        counts = Counter(i['stage'] for i in items)
        age = max(0, time.time() - self.latest) if self.latest else None
        complete = bool(items) and all(i['reviewed'] for i in items)
        if complete:
            state = 'complete'
        elif processes is None:
            state = 'unknown'
        elif paused:
            state = 'paused'
        elif not alive:
            state = 'stopped'
        elif age is not None and age > 1200:
            state = 'stale'
        elif counts['error']:
            state = 'running_with_errors'
        else:
            state = 'running'
        return {'account': cfg.get('account_name', '直播回放'), 'state': state,
                'total': len(items), 'catalog_frozen': bool(plan.get('catalog_sha256')),
                'counts': dict(counts), 'audio_ready': sum(i['audio_ready'] for i in items),
                'word_ready': sum(i['word_ready'] for i in items),
                'reviewed': sum(i['reviewed'] for i in items), 'items': items,
                'processes': processes, 'seconds_since_evidence': age,
                'read_errors': self.read_errors, 'free_gib': round(shutil.disk_usage(self.root).free / 1024**3, 1),
                'updated_at': time.time(), 'read_only': True}

    def download(self, ordinal):
        key = f'{ordinal:04d}'
        statuses = self.read(self.root / 'batch/status.json', {})
        path = statuses.get(key, {}).get('word')
        if not path or not self.word(path)[0]:
            raise ValueError('verified_word_unavailable')
        return self.safe(path)
