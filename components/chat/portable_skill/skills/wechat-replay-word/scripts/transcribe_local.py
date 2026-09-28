"""Offline local-media ASR; immutable source/config-bound block checkpoints.

Adapted from the local course_worker.py; no course API, fixed names or network.
Uses mlx-whisper 0.4.3 locally on Apple silicon. Other backends can share JSON.
"""
import argparse
import fcntl
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import time
import wave

PCM_SYNC_FILTER = 'aresample=async=1:first_pts=0'


def decode_pcm_command(ffmpeg, media, output, decoder=None):
    """Decode on the source presentation timeline, retaining timestamp gaps."""
    if decoder not in (None, 'aac_at'):
        raise ValueError('unsupported_audio_decoder')
    decoder_args = ['-c:a', decoder] if decoder else []
    return [ffmpeg, '-nostdin', '-v', 'error', '-xerror', *decoder_args, '-i', str(media),
            '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000',
            '-af', PCM_SYNC_FILTER, '-c:a', 'pcm_s16le', str(output)]


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for data in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def signature(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def select_segments(raw, offset, start, end, duration):
    """Assign overlap words by midpoint, preserving genuine repeated speech."""
    result = []
    for s in raw:
        words = s.get('words') or []
        if not words and s.get('text', '').strip():
            raise ValueError('missing_word_timestamps')
        selected = [w for w in words if start <= offset + (w['start'] + w['end']) / 2 < end]
        if not selected:
            continue
        a = max(0, offset + selected[0]['start'])
        b = min(duration, offset + selected[-1]['end'])
        if not 0 <= a <= b <= duration:
            raise ValueError('invalid_segment_timing')
        result.append(dict(start=a, end=b, text=''.join(w['word'] for w in selected).strip(),
                           avg_logprob=s.get('avg_logprob'), no_speech_prob=s.get('no_speech_prob'),
                           compression_ratio=s.get('compression_ratio')))
    return result


def run(args):
    import imageio_ffmpeg
    import numpy as np
    media, model, work = Path(args.media).resolve(), Path(args.model).resolve(), Path(args.work).resolve()
    if not media.is_file() or not (model / 'weights.safetensors').is_file():
        raise ValueError('local_media_and_model_required')
    work.mkdir(parents=True, exist_ok=True)
    with (work / 'asr.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        params = dict(language=args.language, task='transcribe', verbose=None,
                      temperature=[0, .2, .4, .6, .8, 1], compression_ratio_threshold=2.4,
                      logprob_threshold=-1., no_speech_threshold=.6,
                      hallucination_silence_threshold=2., condition_on_previous_text=False,
                      initial_prompt=None, word_timestamps=True)
        config = dict(schema=2, block_seconds=900, overlap_seconds=2,
                      pcm_timeline_filter=PCM_SYNC_FILTER,
                      identity={'account': args.account, 'replay': args.replay},
                      source_sha256=digest(media), model_weights_sha256=digest(model/'weights.safetensors'),
                      model_config_sha256=digest(model/'config.json'),
                      mlx_whisper=importlib.metadata.version('mlx-whisper'), params=params)
        if args.audio_decoder:
            config['audio_decoder'] = args.audio_decoder
        key = signature(config)
        target = work / key
        target.mkdir(exist_ok=True)
        save(target / 'config.json', config)
        output = target / 'transcript.json'
        if output.exists():
            existing = json.loads(output.read_text())
            if existing.get('config_signature') == key and existing.get('complete'):
                save(work / 'current-transcript.json',
                     {'transcript': str(output), 'config_signature': key})
                print(json.dumps({'status': 'reused', 'transcript': str(output)}), flush=True)
                return
            raise ValueError('existing_transcript_needs_review')
        wav = target / 'audio16k.wav'
        wavmeta = target / 'audio16k.json'
        if wav.exists():
            if not wavmeta.exists() or json.loads(wavmeta.read_text())['sha256'] != digest(wav):
                raise ValueError('existing_audio_needs_review')
        else:
            partial = target / 'audio16k.partial.wav'
            if partial.exists():
                raise ValueError('partial_audio_preserved_needs_review')
            cmd = decode_pcm_command(imageio_ffmpeg.get_ffmpeg_exe(), media, partial,
                                     args.audio_decoder)
            p = subprocess.run(cmd, capture_output=True, timeout=1800)
            if p.returncode:
                raise RuntimeError('full_audio_decode_failed')
            partial.replace(wav)
            save(wavmeta, {'sha256': digest(wav), 'source_sha256': config['source_sha256']})
        with wave.open(str(wav)) as f:
            assert f.getframerate() == 16000 and f.getnchannels() == 1 and f.getsampwidth() == 2
            n = f.getnframes()
            duration = n / 16000
            if args.expected_duration and abs(duration - args.expected_duration) > 1:
                raise ValueError('source_duration_mismatch')
            segments, blocks = [], []
            import mlx_whisper
            for index, start in enumerate(range(0, n, 900 * 16000)):
                end = min(n, start + 900 * 16000)
                lo, hi = max(0, start - 32000), min(n, end + 32000)
                f.setpos(lo)
                pcm = f.readframes(hi - lo)
                if len(pcm) != (hi-lo)*2:
                    raise ValueError('truncated_audio_window')
                input_hash = hashlib.sha256(pcm).hexdigest()
                blockpath = target / 'blocks' / f'{index:04d}.json'
                if blockpath.exists():
                    block = json.loads(blockpath.read_text())
                    if block.get('input_hash') != input_hash or block.get('config_signature') != key:
                        raise ValueError('checkpoint_identity_mismatch')
                else:
                    began = time.time()
                    audio = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
                    raw = mlx_whisper.transcribe(audio, path_or_hf_repo=str(model), **params)
                    ss = select_segments(raw['segments'], lo/16000, start/16000, end/16000, duration)
                    block = dict(index=index, start=start/16000, end=end/16000, status='complete',
                                 config_signature=key, input_hash=input_hash, segments=ss,
                                 raw_segments=raw['segments'], wall_seconds=time.time()-began)
                    save(blockpath, block)
                if block.get('status') != 'complete':
                    raise ValueError('incomplete_checkpoint')
                segments.extend(block['segments'])
                blocks.append({k: block[k] for k in ('index', 'start', 'end', 'status', 'input_hash')})
                print(json.dumps({'block': index+1, 'processed_seconds': end/16000,
                                  'total_seconds': duration, 'segments': len(segments)}), flush=True)
        if not segments:
            raise ValueError('empty_transcript_needs_review')
        issues = []
        for i, s in enumerate(segments):
            if (s.get('avg_logprob') or 0) < -1 or (s.get('compression_ratio') or 0) > 2.4:
                issues.append({'segment': i, 'type': 'low_confidence'})
            if i and s['start'] < segments[i-1]['end']:
                issues.append({'segment': i, 'type': 'timing_overlap_review'})
        save(output, dict(complete=True, completeness='all_audio_blocks_processed_not_human_verbatim_certified',
                          config_signature=key, identity=config['identity'], source_sha256=config['source_sha256'],
                          duration=duration, blocks=blocks, segments=segments, quality_issues=issues))
        save(work / 'current-transcript.json',
             {'transcript': str(output), 'config_signature': key})
        print(json.dumps({'status': 'completed', 'transcript': str(output), 'issues': len(issues)}), flush=True)


if __name__ == '__main__':
    os.umask(0o077)
    p = argparse.ArgumentParser()
    for name in ('media', 'model', 'work', 'account', 'replay'):
        p.add_argument('--'+name, required=True)
    p.add_argument('--language', default='zh')
    p.add_argument('--expected-duration', type=float, default=0)
    p.add_argument('--audio-decoder', choices=('aac_at',))
    run(p.parse_args())
