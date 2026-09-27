"""Resolve catalog rounding/staleness against source container metadata, not a looser threshold."""
import re
import subprocess
from range_audio_source import source


def inspect_duration(ffmpeg, url, total, proxy, redirect_handler):
    with source(url, total, proxy, redirect_handler) as (local, transfer):
        result = subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-i', local,
            '-map', '0:a:0', '-t', '0', '-f', 'null', '-'], capture_output=True, timeout=120)
    # Never return raw diagnostics, local paths or media URLs.
    matches = re.findall(rb'Duration: (\d+):(\d{2}):(\d{2}\.\d+)', result.stderr)
    if result.returncode or len(matches) != 1:
        raise ValueError('source_container_duration_unavailable')
    hours, minutes, seconds = map(float, matches[0])
    duration = 3600 * hours + 60 * minutes + seconds
    if not duration > 0 or minutes >= 60 or seconds >= 60:
        raise ValueError('source_container_duration_invalid')
    return {'duration': duration, 'source_bytes': total,
            'method': 'source_container_ffmpeg_metadata', 'transfer': transfer}
