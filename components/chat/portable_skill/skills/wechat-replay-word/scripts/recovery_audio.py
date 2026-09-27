"""One explicitly authorized recovery round. Preserve old evidence and stop on shared outages."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.error
from batch_audio import probe, SafeRedirect, decoded_duration, validate_url
from range_audio_source import source
from transcribe_local import digest, save
from media_duration import inspect_duration


class NetworkBlocked(RuntimeError):
    pass


def classify(exc):
    if isinstance(exc, NetworkBlocked):
        return str(exc), False
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403, 429):
            return 'access_or_rate_limit', False
        return 'http_' + str(exc.code), exc.code >= 500
    if isinstance(exc, (urllib.error.URLError, TimeoutError, subprocess.TimeoutExpired)):
        return 'network_unavailable', True
    if isinstance(exc, ValueError):
        return str(exc), str(exc) == 'stream_network_interrupted'
    return 'stage_' + type(exc).__name__, False


def acquire_recovery(item, folder, cfg, notify=lambda value: None):
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    final = folder / 'audio.m4a'
    proofpath = folder / 'audio-verification.json'
    identity = hashlib.sha256((item['account'] + '\0' + item['id']).encode()).hexdigest()
    if proofpath.exists():
        proof = json.loads(proofpath.read_text())
        if proof.get('identity') != identity or not proof.get('full_audio_decode') or digest(final) != proof.get('sha256'):
            raise ValueError('recovery_checkpoint_mismatch')
        return proof
    if final.exists():
        raise ValueError('unverified_recovery_preserved')
    if len(item['media']) != 1 or item['media'][0]['videoPlayLen'] <= 0:
        raise ValueError('ambiguous_media')
    media = item['media'][0]
    url = validate_url(media['url'] + media.get('urlToken', ''))
    expected = float(media['videoPlayLen'])
    for attempt in (1, 2):
        attempt_path = folder / f'audio-attempt-{attempt}.json'
        partial = folder / f'audio-attempt-{attempt}.partial.m4a'
        if attempt_path.exists() or partial.exists():
            continue
        if shutil.disk_usage(folder).free < cfg.get('min_free_bytes', 20 * 1024**3):
            raise ValueError('disk_safety_floor')
        began = time.time()
        save(attempt_path, {'status': 'running', 'started': began, 'phase': 'probe'})
        notify({'status': 'downloading', 'attempt': attempt})
        try:
            # Probe and transfer share ONE retry budget; probe errors are no longer lost.
            remote = probe(url, cfg['proxy'])
            with source(url, remote['source_bytes'], cfg['proxy'], SafeRedirect) as (local, transfer):
                cmd = [cfg['ffmpeg'], '-nostdin', '-v', 'error', '-xerror', '-n',
                       '-rw_timeout', '60000000', '-i', local, '-map', '0:a:0',
                       '-vn', '-sn', '-dn', '-c:a', 'copy', '-map_metadata', '-1', str(partial)]
                p = subprocess.run(cmd, capture_output=True, timeout=3600)
            if transfer.get('failure') == 'access_or_rate_limit':
                raise NetworkBlocked('access_or_rate_limit')
            if p.returncode:
                reason = 'stream_network_interrupted' if p.returncode == 251 else 'stream_decode_failed'
                raise ValueError(reason)
            duration = decoded_duration(cfg['ffmpeg'], partial)
            source_duration = None
            if abs(duration - expected) > 1.5:
                source_duration = inspect_duration(cfg['ffmpeg'], url, remote['source_bytes'], cfg['proxy'], SafeRedirect)
                save(folder / 'duration-review.json', {'expected': expected, 'decoded': duration,
                     'source_container': source_duration, 'full_audio_decode': True, 'attempt': attempt})
                if abs(duration - source_duration['duration']) > 1.5:
                    raise ValueError('source_duration_review_required')
            proof = dict(remote, identity=identity, expected_duration=expected, duration=duration,
                         sha256=digest(partial), bytes=partial.stat().st_size,
                         wall_seconds=time.time()-began, full_audio_decode=True,
                         full_video_downloaded=False, transfer=transfer)
            if source_duration:
                proof['source_container'] = source_duration
                proof['catalog_duration_differs'] = True
            partial.replace(final)
            save(proofpath, proof)
            save(attempt_path, {'status': 'completed', 'wall_seconds': time.time()-began})
            return proof
        except Exception as exc:
            reason, transient = classify(exc)
            save(attempt_path, {'status': 'failed', 'reason': reason,
                                'wall_seconds': time.time()-began})
            notify({'status': 'retry_pending' if transient and attempt == 1 else 'error', 'reason': reason})
            if reason == 'access_or_rate_limit':
                raise NetworkBlocked(reason) from None
            if transient:
                if attempt == 2:
                    raise NetworkBlocked('shared_network_outage') from None
                time.sleep(15)
            else:
                raise ValueError(reason) from None
    raise ValueError('recovery_attempt_limit')
