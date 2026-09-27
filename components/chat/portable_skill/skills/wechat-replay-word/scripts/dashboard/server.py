"""Private loopback dashboard. GET-only; no queue mutation or arbitrary files."""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
from urllib.parse import quote, urlsplit
from state import BatchView


def serve(root, port, connection_file):
    view = BatchView(root)
    token = secrets.token_urlsafe(24)
    prefix = '/' + token
    page = Path(__file__).with_name('index.html').read_text().replace('NONCE_VALUE', token).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body, kind, filename=None):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'nonce-" + token +
                             "'; script-src 'nonce-" + token + "'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            if filename:
                self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(filename))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            host = '127.0.0.1:' + str(self.server.server_port)
            if self.headers.get('Host') != host or self.headers.get('Origin') not in (None, 'http://' + host):
                return self.reply(403, b'Forbidden', 'text/plain')
            route = urlsplit(self.path).path
            if route == prefix + '/':
                return self.reply(200, page, 'text/html; charset=utf-8')
            if route == prefix + '/api/status':
                try:
                    result = json.dumps(view.snapshot(), ensure_ascii=False).encode()
                    return self.reply(200, result, 'application/json; charset=utf-8')
                except Exception:
                    return self.reply(503, b'{"error":"status_unavailable"}', 'application/json')
            if route.startswith(prefix + '/word/'):
                ident = route.removeprefix(prefix + '/word/')
                if ident.isascii() and ident.isdigit() and len(ident) <= 5:
                    try:
                        path = view.download(int(ident))
                        return self.reply(200, path.read_bytes(),
                                          'application/vnd.openxmlformats-officedocument.wordprocessingml.document', path.name)
                    except (OSError, ValueError):
                        pass
            return self.reply(404, b'Not found', 'text/plain')

    server = HTTPServer(('127.0.0.1', port), Handler)
    result = {'url': f'http://127.0.0.1:{server.server_port}{prefix}/', 'pid': os.getpid(), 'read_only': True}
    if connection_file:
        with open(connection_file, 'x') as f:
            json.dump(result, f)
    print(json.dumps(result), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--task-root', required=True, type=Path)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--connection-file', type=Path)
    args = parser.parse_args()
    serve(args.task_root, args.port, args.connection_file)
