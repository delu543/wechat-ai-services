"""Strict local catalog contract; never persist raw HTTP flows or credentials."""
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

HOSTS = ('channels.weixin.qq.com', 'res.wx.qq.com')
MEDIA_SUFFIXES = ('video.qq.com', 'video.weixin.qq.com', 'wxapp.tc.qq.com')


def text(value, limit=2048):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError('invalid_string')
    return value


def identifier(value):
    value = text(value, 256)
    if not value or any(ord(c) < 32 for c in value):
        raise ValueError('invalid_identifier')
    return value


def media_url(value):
    value = text(value, 8192)
    u = urlsplit(value)
    if u.scheme != 'https' or u.username or u.password or u.port not in (None, 443):
        raise ValueError('invalid_media_url')
    if not any(u.hostname == h or (u.hostname or '').endswith('.'+h) for h in MEDIA_SUFFIXES):
        raise ValueError('unexpected_media_host')
    return value


def clean_item(item, account):
    if not isinstance(item, dict) or item.get('account') != account:
        raise ValueError('wrong_account')
    result = {key: identifier(item[key]) for key in ('id', 'account')}
    result['nonce'] = text(item.get('nonce', ''), 512)
    result['title'] = text(item.get('title', ''))
    result['created'] = item.get('created', 0)
    if type(result['created']) is not int or not 0 <= result['created'] < 10**13:
        raise ValueError('invalid_date')
    result['media'] = []
    media = item.get('media', [])
    if not isinstance(media, list) or len(media) > 10:
        raise ValueError('invalid_media_list')
    for m in media:
        row = {'url': media_url(m['url'])}
        for key in ('urlToken', 'decodeKey'):
            row[key] = text(m.get(key, ''), 8192 if key == 'urlToken' else 512)
        for key in ('fileSize', 'encLimit', 'videoPlayLen'):
            value = m.get(key, 0)
            if type(value) not in (int, float) or not 0 <= value < 10**14:
                raise ValueError('invalid_media_number')
            row[key] = value
        result['media'].append(row)
    return result


class Catalog:
    def __init__(self, account):
        self.account = identifier(account)
        self.items = {}
        self.pages = []
        self.cursor = ''
        self.complete = False
        self.seen_cursors = {''}

    def add(self, body):
        if self.complete or body.get('account') != self.account:
            raise ValueError('closed_or_wrong_account')
        if body.get('index') != len(self.pages) or body.get('cursor') != self.cursor:
            raise ValueError('out_of_order_page')
        flag = body.get('continueFlag')
        if type(flag) is not int or flag not in (0, 1):
            raise ValueError('unknown_end_marker')
        cursor = text(body.get('next', ''), 16384)
        if flag and (not cursor or cursor in self.seen_cursors):
            raise ValueError('cursor_cycle')
        raw = body.get('items')
        if not isinstance(raw, list) or len(raw) > 100:
            raise ValueError('invalid_page_size')
        clean = [clean_item(item, self.account) for item in raw]
        new = {i['id']: i for i in clean if i['id'] not in self.items}
        if flag and not new:
            raise ValueError('no_catalog_progress')
        self.items.update(new)
        self.pages.append({'index': len(self.pages), 'count': len(clean), 'new': len(new),
                           'continueFlag': flag, 'cursor_sha256': hashlib.sha256(cursor.encode()).hexdigest()})
        self.cursor = cursor
        self.seen_cursors.add(cursor)
        self.complete = flag == 0
        return {'accepted': True, 'unique': len(self.items), 'complete': self.complete}

    def snapshot(self):
        return {'account': self.account, 'complete': self.complete, 'pages': self.pages,
                'next_cursor': self.cursor, 'items': list(self.items.values())}


def private_save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temp = path.with_suffix('.partial')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
