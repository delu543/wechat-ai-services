"""Bounded in-memory parallel Range source for FFmpeg, no video file on disk.

Only the supplied, validated CDN URL is fetched; requests use normal verified
HTTPS through the existing proxy. The temporary localhost route is random and
has no arbitrary-URL/RPC interface. Stop it as soon as this one audio is done.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import secrets
import threading
import urllib.error
import urllib.request


class RangeAccessBlocked(ValueError):
    pass


def fetch_range(opener, url, start, end, total):
    for attempt in range(2):
        try:
            request = urllib.request.Request(url, headers={'Range': f'bytes={start}-{end}',
                                                         'Accept-Encoding': 'identity'})
            with opener.open(request, timeout=45) as response:
                expected = f'bytes {start}-{end}/{total}'
                if response.status != 206 or response.headers.get('Content-Range') != expected:
                    raise ValueError('range_identity_mismatch')
                result = response.read(end-start+2)
                if len(result) != end-start+1: raise ValueError('range_truncated')
                return result
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 429):
                raise RangeAccessBlocked('access_or_rate_limit') from None
            if attempt: raise ValueError('verified_range_fetch_failed') from None
        except Exception:
            if attempt: raise ValueError('verified_range_fetch_failed') from None


def bounds(header, total):
    if not header: return 0, total-1
    m = re.fullmatch(r'bytes=(\d+)-(\d*)', header)
    if not m: raise ValueError('unsupported_range')
    start, end = int(m[1]), int(m[2]) if m[2] else total-1
    if not 0 <= start <= end < total: raise ValueError('invalid_range')
    return start, end


@contextmanager
def source(url, total, proxy, redirect_handler):
    route = '/'+secrets.token_hex(24)
    opener = urllib.request.build_opener(redirect_handler(), urllib.request.ProxyHandler({'https': proxy}))
    stats = {'remote_bytes': 0, 'requests': 0}
    stopped = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_HEAD(self): self.handle_media(head=True)
        def do_GET(self): self.handle_media(head=False)
        def handle_media(self, head):
            if self.path != route:
                self.send_error(404); return
            try: start, end = bounds(self.headers.get('Range'), total)
            except ValueError:
                self.send_error(416); return
            self.send_response(206 if self.headers.get('Range') else 200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Length', str(end-start+1))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Cache-Control', 'no-store')
            if self.headers.get('Range'): self.send_header('Content-Range', f'bytes {start}-{end}/{total}')
            self.end_headers()
            if head: return
            block = 2*1024*1024
            try:
                # At most eight 2MiB blocks per request, not the whole file.
                with ThreadPoolExecutor(max_workers=8) as pool:
                    while start <= end and not stopped.is_set():
                        spans = [(p, min(end, p+block-1)) for p in range(start, min(end+1, start+8*block), block)]
                        futures = [pool.submit(fetch_range, opener, url, a, b, total) for a, b in spans]
                        for future in futures:
                            data = future.result(); stats['remote_bytes'] += len(data); stats['requests'] += 1
                            if stopped.is_set(): return
                            self.wfile.write(data)
                        start = spans[-1][1]+1
            except RangeAccessBlocked:
                stats['failure'] = 'access_or_rate_limit'
                stopped.set()
                self.close_connection = True
            except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
                self.close_connection = True

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}{route}', stats
    finally:
        stopped.set(); server.shutdown(); server.server_close(); thread.join(timeout=2)
