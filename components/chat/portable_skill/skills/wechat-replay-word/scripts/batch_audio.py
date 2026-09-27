"""Audio-only streaming: keep verified audio, never persist full account videos.

FFmpeg HTTPS stream-copy uses the existing VPN endpoint, TLS validation remains
enabled. Completed files are immutable; failed partials are preserved. This is
not a full-video downloader and does not claim full-video byte verification.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request
from contextlib import nullcontext
from urllib.parse import urlsplit
from transcribe_local import digest, save
from range_audio_source import source as parallel_source


def validate_url(url):
    u = urlsplit(url)
    if (u.scheme != 'https' or u.username or u.password or u.port not in (None, 443)
            or not any((u.hostname or '').endswith('.'+d) or u.hostname == d
                       for d in ('video.qq.com', 'video.weixin.qq.com', 'wxapp.tc.qq.com'))):
        raise ValueError('unapproved_media_url')
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def probe(url, proxy):
    opener = urllib.request.build_opener(SafeRedirect(), urllib.request.ProxyHandler({'https': proxy}))
    req = urllib.request.Request(validate_url(url), headers={'Range': 'bytes=0-63', 'Accept-Encoding': 'identity'})
    with opener.open(req, timeout=30) as r:
        body = r.read(64)
        match = re.fullmatch(r'bytes 0-63/(\d+)', r.headers.get('Content-Range', ''))
        if r.status != 206 or not match or body[4:8] != b'ftyp':
            raise ValueError('not_plain_mp4_range_source')
        return {'source_bytes': int(match[1]), 'prefix_sha256': hashlib.sha256(body).hexdigest()}


def decoded_duration(ffmpeg, path):
    p = subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-xerror', '-i', str(path),
                        '-map', '0:a:0', '-vn', '-progress', 'pipe:1', '-f', 'null', '-'],
                       capture_output=True, timeout=1800)
    times = re.findall(rb'out_time_us=(\d+)', p.stdout)
    if p.returncode or not times:
        raise ValueError('full_audio_decode_failed')
    return int(times[-1])/1_000_000


def acquire(item, folder, cfg):
    """Compatibility entry point; all callers share the bounded recovery implementation."""
    from recovery_audio import acquire_recovery
    return acquire_recovery(item, folder, cfg)
