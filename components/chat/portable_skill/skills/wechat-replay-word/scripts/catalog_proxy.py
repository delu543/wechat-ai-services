"""Narrow response adapter. Does not install trust or change network settings."""
import hmac
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from mitmproxy import http
from catalog_protocol import Catalog, HOSTS, identifier, private_save
from page_loader import is_static, is_page, rewrite_html, rewrite_js, safe_route

PREFIX = '/__codex_replay_catalog/'
EXPORTS = re.compile(r'\bexport\s*\{([^{}]+)\}\s*;?\s*(?://[^\n]*)?\s*$')
NAME = re.compile(r'[A-Za-z_$][A-Za-z0-9_$]*')


def inject_module(source, script, config):
    match = EXPORTS.search(source)
    if not match or '__codexReplayCatalogV1' in source:
        raise ValueError('module_shape_changed')
    locals_ = []
    for spec in match[1].split(','):
        name = re.split(r'\s+as\s+', spec.strip())[0]
        if not NAME.fullmatch(name):
            raise ValueError('unknown_export_shape')
        locals_.append(name)
    payload = script.replace('__CODEX_EXPORTS__', '{'+','.join(locals_)+'}')
    payload = payload.replace('__CODEX_CONFIG__', json.dumps(config, ensure_ascii=True))
    return source[:match.start()]+';\n'+payload+'\n'+source[match.start():]


class CatalogProxy:
    def __init__(self, config, folder):
        self.config = config
        self.folder = Path(folder)
        self.catalog = None
        self.script = Path(__file__).with_name('inject_catalog.js').read_text()
        if self.folder.exists() and any(self.folder.iterdir()):
            raise ValueError('existing_capture_preserved')
        self.endpoint = PREFIX+config['token']
        self.stats = {'injected_modules': 0, 'rejected_collectors': 0, 'accepted_pages': 0}
        self.cache_tag = str(time.time_ns())
        self.events = {}

    def event(self, key):
        if len(self.events) < 250 or key in self.events:
            self.events[key] = self.events.get(key, 0)+1
        private_save(self.folder/'status.json', {**self.stats, 'events': self.events})

    def tls_failed_client(self, data):
        if data.context.client.sni in HOSTS:
            self.event('tls_client_failed:'+data.context.client.sni)

    def tls_established_client(self, data):
        if data.context.client.sni in HOSTS:
            self.event('tls_client_ok:'+data.context.client.sni)

    @staticmethod
    def module_request(request):
        u = urlsplit(request.url)
        return (u.scheme == 'https' and u.hostname == 'res.wx.qq.com' and
                '/t/wx_fed/finder/' in u.path and
                re.fullmatch(r'virtual_svg-icons-register\.publish[^/]*\.js', u.path.rsplit('/', 1)[-1]) is not None)

    def requestheaders(self, flow):
        if flow.request.host not in HOSTS:
            return
        path = urlsplit(flow.request.url).path
        if not path.startswith(PREFIX):
            self.event('request:'+safe_route(flow.request.url))
        if flow.request.host == HOSTS[0] and path.startswith(PREFIX):
            flow.metadata['catalog_collector'] = True
            # Respond before an upstream request can leak the task token.
            flow.response = http.Response.make(403, b'{"error":"rejected"}',
                                              {'Content-Type': 'application/json', 'Cache-Control': 'no-store'})
            length = flow.request.headers.get('content-length', '')
            if (path == self.endpoint and flow.request.method == 'POST' and
                flow.request.scheme == 'https' and
                flow.request.headers.get('origin') == 'https://channels.weixin.qq.com' and
                hmac.compare_digest(flow.request.headers.get('x-codex-catalog', ''), self.config['token']) and
                length.isdigit() and int(length) <= 2_000_000):
                flow.metadata['catalog_authorized'] = True
            else:
                self.stats['rejected_collectors'] += 1
        elif is_static(flow.request.url) or is_page(flow.request.url):
            # Refetch only Finder JS/page dependencies, never delete application caches.
            for key in ('if-none-match', 'if-modified-since'):
                flow.request.headers.pop(key, None)
            flow.request.headers['accept-encoding'] = 'identity'

    def request(self, flow):
        if not flow.metadata.get('catalog_authorized'):
            return
        try:
            if len(flow.request.raw_content or b'') > 2_000_000:
                raise ValueError('oversized_body')
            body = json.loads(flow.request.content)
            if body.get('kind') == 'bind':
                if self.catalog or body.get('name') != self.config['targetName'] or body.get('title') != self.config['seedTitle']:
                    raise ValueError('target_mismatch_or_already_bound')
                self.catalog = Catalog(identifier(body['account']))
                reply = {'accepted': True}
            elif body.get('kind') == 'page' and self.catalog:
                reply = self.catalog.add(body)
                self.stats['accepted_pages'] += 1
            else:
                raise ValueError('unknown_operation')
            private_save(self.folder/'catalog.private.json', self.catalog.snapshot())
            private_save(self.folder/'status.json', self.stats)
            flow.response = http.Response.make(200, json.dumps(reply).encode(),
                {'Content-Type': 'application/json', 'Cache-Control': 'no-store'})
        except (ValueError, TypeError, KeyError):
            self.stats['rejected_collectors'] += 1
            flow.response = http.Response.make(400, b'{"error":"invalid_catalog_contract"}')
        finally:
            # No persistent headers, cookies, request body or response flow dump.
            flow.request.content = b''

    def response(self, flow):
        if flow.metadata.get('catalog_collector') or flow.request.host not in HOSTS:
            return
        self.event('response:'+str(flow.response.status_code)+':'+safe_route(flow.request.url))
        if flow.response.status_code != 200 or len(flow.response.raw_content or b'') > 15_000_000:
            return
        url = flow.request.url
        if not (is_static(url) or is_page(url)):
            return
        original = flow.response.get_text(strict=False)
        modified = (rewrite_html(original, url, self.cache_tag) if is_page(url)
                    else rewrite_js(original, url, self.cache_tag))
        if not self.module_request(flow.request):
            if modified != original:
                flow.response.text = modified
                flow.response.headers['cache-control'] = 'no-store'
                for key in ('etag', 'content-md5', 'last-modified'):
                    flow.response.headers.pop(key, None)
                self.event('cache_refreshed:'+safe_route(url))
            return
        public = {key: self.config[key] for key in ('token', 'targetName', 'seedTitle', 'maxPages')}
        public['endpoint'] = self.endpoint
        try:
            modified = inject_module(modified, self.script, public)
        except (ValueError, UnicodeError):
            self.stats['module_schema_rejected'] = self.stats.get('module_schema_rejected', 0)+1
            private_save(self.folder/'status.json', self.stats)
            return
        flow.response.text = modified
        for key in ('etag', 'content-md5', 'last-modified'):
            flow.response.headers.pop(key, None)
        flow.response.headers['cache-control'] = 'no-store'
        self.stats['injected_modules'] += 1
        private_save(self.folder/'status.json', self.stats)
