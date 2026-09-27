"""Refresh only Weixin Finder scripts; no cache deletion or CSP relaxation."""
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

STATIC_HOST = 'res.wx.qq.com'
STATIC_PREFIX = '/t/wx_fed/finder/'
PAGES = {'/web/pages/feed', '/web/pages/home', '/web/pages/live', '/web/pages/liveReplay'}


def is_static(url):
    u = urlsplit(url)
    return (u.scheme == 'https' and u.hostname == STATIC_HOST and
            u.path.startswith(STATIC_PREFIX) and u.path.endswith('.js'))


def is_page(url):
    u = urlsplit(url)
    return u.scheme == 'https' and u.hostname == 'channels.weixin.qq.com' and u.path in PAGES


def refresh_ref(ref, base, tag):
    absolute = urljoin(base, ref)
    if not is_static(absolute):
        return ref
    # Preserve the original relative reference: Vite may resolve "js/..."
    # against its own base rather than this module's URL.
    u = urlsplit(ref)
    params = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != 'codex_catalog_v']
    params.append(('codex_catalog_v', tag))
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(params), u.fragment))


def rewrite_html(source, base, tag):
    # Rewrite static JS URL attributes, not inline scripts, nonce or integrity.
    pattern = re.compile(r'((?:src|href)\s*=\s*)(["\'])([^"\']+)(\2)', re.I)
    return pattern.sub(lambda m: m[1]+m[2]+refresh_ref(m[3], base, tag)+m[4], source)


def rewrite_js(source, base, tag):
    # ES static imports, dynamic imports and Vite dependency arrays only.
    pattern = re.compile(r'(["\'])((?:\.{0,2}/|js/|https://res\.wx\.qq\.com/)[^"\'\s]+\.js(?:\?[^"\'\s]*)?)(\1)')
    return pattern.sub(lambda m: m[1]+refresh_ref(m[2], base, tag)+m[3], source)


def safe_route(url):
    u = urlsplit(url)
    if is_static(url):
        return 'static:'+u.path[:500]
    if is_page(url):
        return 'page:'+u.path
    return 'other_allowed_resource'
