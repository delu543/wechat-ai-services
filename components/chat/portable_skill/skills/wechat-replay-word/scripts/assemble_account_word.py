"""Assemble one account Word only after every source replay Word passed review.

Reads the frozen catalog plan and per-item proofs. Never fetches media, runs ASR,
changes worker status or deletes intermediate evidence.
"""
import argparse
import json
from pathlib import Path

from build_word import build, clock, groups, validate_transcript
from docx import Document
from run_account_batch import make_plan
from transcribe_local import digest, save


READY = {'word_draft_ready', 'word_verified', 'verified_sample_reused'}


def source_manifest(config):
    root = Path(config['root']).resolve()
    batch = root / 'batch'
    catalog = root / 'capture-session/capture/catalog.private.json'
    directory = json.loads(catalog.read_text())
    if not directory.get('complete') or not directory.get('pages') or directory['pages'][-1].get('continueFlag') != 0:
        raise ValueError('catalog_terminal_required')
    plan = json.loads((batch / 'plan.private.json').read_text())
    statuses = json.loads((batch / 'status.json').read_text())
    if plan.get('catalog_sha256') != digest(catalog):
        raise ValueError('frozen_catalog_hash_changed')
    entries = plan.get('entries') or []
    if (not entries or len(entries) != len(directory.get('items', []))
            or len({(e['account_id'], e['replay_id']) for e in entries}) != len(entries)):
        raise ValueError('missing_or_duplicate_replay_identity')
    if entries != make_plan(directory, config):
        raise ValueError('frozen_plan_changed')
    if len(statuses) != len(entries):
        raise ValueError('account_results_incomplete')
    ready = []
    for ordinal, entry in enumerate(entries, 1):
        item = statuses.get(f'{ordinal:04d}', {})
        if item.get('status') not in READY:
            raise ValueError(f'replay_not_ready:{ordinal:04d}')
        word = Path(item['word'])
        proof = json.loads(word.with_suffix('.verification.json').read_text())
        if not (proof.get('body_exact_match') and proof.get('render_review_complete')
                and proof.get('docx_sha256') == digest(word)
                and proof.get('replay_count') == 1):
            raise ValueError(f'source_word_not_reviewed:{ordinal:04d}')
        transcript = Path(item['transcript'])
        if not transcript.is_file():
            raise ValueError(f'source_transcript_missing:{ordinal:04d}')
        data = json.loads(transcript.read_text())
        validate_transcript(data)
        if data.get('identity') != {'account': entry['account_id'], 'replay': entry['replay_id']}:
            raise ValueError(f'source_transcript_identity_mismatch:{ordinal:04d}')
        expected = [f"[{clock(chunk[0]['start'])}–{clock(chunk[-1]['end'])}] " +
                    ''.join(segment['text'] for segment in chunk)
                    for chunk in groups(data['segments'])]
        actual = [paragraph.text for paragraph in Document(word).paragraphs
                  if paragraph.style.name == 'TranscriptBody']
        if actual != expected or proof.get('paragraph_count') != len(expected):
            raise ValueError(f'source_word_transcript_mismatch:{ordinal:04d}')
        ready.append(dict(entry, transcript=str(transcript)))
    return {'account_name': config['account_name'], 'catalog_complete': True,
            'continuous_sections': True, 'replays': ready}


def assemble(config_path, output):
    config = json.loads(Path(config_path).read_text())
    manifest = source_manifest(config)
    output = Path(output).resolve()
    evidence = output.with_suffix('.source-manifest.json')
    if evidence.exists() and json.loads(evidence.read_text()) != manifest:
        raise ValueError('existing_source_manifest_changed')
    if not evidence.exists():
        save(evidence, manifest)
    report = build(evidence, output)
    if report['replay_count'] != len(manifest['replays']):
        raise ValueError('assembled_replay_count_mismatch')
    return {'word': str(output), 'replay_count': report['replay_count'],
            'paragraph_count': report['paragraph_count'],
            'docx_sha256': report['docx_sha256'],
            'render_review_complete': report['render_review_complete']}


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('config', type=Path)
    p.add_argument('output', type=Path)
    a = p.parse_args()
    print(json.dumps(assemble(a.config, a.output), ensure_ascii=False))
