#!/usr/bin/env python3
"""Local task console and same-origin authenticated OpenCode web gateway."""
import base64
import hashlib
import hmac
import http.client
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import select
import socket
import time
import urllib.parse
from common import STATE, api, artifact_dir, config, public_task, read_json, redact, task, tasks
import quota
import service
import management
import delegate
from common import CONFIG, HttpFailure, locked, update, write_json

WEB = Path(__file__).resolve().parent.parent / 'web'


def session_url(t):
    if t.get('session_deleted') or not t.get('session_id') or not t.get('directory'):
        return None
    directory = base64.urlsafe_b64encode((t.get('session_directory') or t['directory']).encode()).decode().rstrip('=')
    return '/' + directory + '/session/' + t['session_id']


def service_health():
    hb = read_json(STATE / 'heartbeat.json', {})
    records = read_json(STATE / 'services.json', {})
    pool_ok = time.time() - hb.get('time', 0) < 45 and service.alive(records.get('pool'))
    try:
        server_ok = bool(api('/global/health', timeout=1).get('healthy'))
    except Exception:
        server_ok = False
    return {'pool': bool(pool_ok), 'server': server_ok}


def state():
    entries = []
    for t in reversed(tasks()):
        p = public_task(t)
        p['group_id'] = t.get('group_id') or t['source_dir']
        p['group_title'] = t.get('group_title') or Path(t['source_dir']).name
        p['session_url'] = session_url(t)
        entries.append(p)
    health = service_health()
    return redact({'tasks': entries, 'quota': quota.view(read_json(STATE / 'quota.json', {})),
                   'pool_healthy': all(health.values()), 'services': health,
                   'profiles': {k: {'label': v.get('label', v['model']), 'model': v['model']} for k, v in config()['profiles'].items()},
                   'updated_at': time.time(), 'max_parallel': config()['max_parallel']})


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass  # No URLs, cookies, prompts or credentials in access logs.

    def reply(self, status, content, mime='application/json', headers=None):
        raw = content if isinstance(content, bytes) else json.dumps(content, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('X-Frame-Options', 'DENY')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def authorize(self, bootstrap=False):
        origin = config()['console_url']
        expected = urllib.parse.urlsplit(origin).netloc
        if self.headers.get('Host') != expected:
            self.reply(403, {'error': 'Invalid local host'})
            return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site' or self.headers.get('Origin', origin) != origin:
            self.reply(403, {'error': 'Cross-origin request rejected'})
            return False
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
        except Exception:
            self.reply(403, {'error': 'Invalid cookie'})
            return False
        auth = cookie.get(getattr(self.server, 'cookie_name', 'delegate_console'))
        valid = auth is not None and secrets.compare_digest(auth.value, self.server.cookie)
        if valid or (bootstrap and self.headers.get('Sec-Fetch-Dest', 'document') == 'document'):
            return True
        self.reply(401, {'error': 'Open /console in this browser first'})
        return False

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if not self.authorize(bootstrap=path == '/console'):
            return
        if path == '/console':
            self.reply(200, (WEB / 'index.html').read_bytes(), 'text/html; charset=utf-8',
                       {'Set-Cookie': getattr(self.server, 'cookie_name', 'delegate_console') + '=' + self.server.cookie + '; HttpOnly; SameSite=Strict; Path=/',
                        'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"})
        elif path in ('/console-assets/app.js', '/console-assets/style.css', '/console-assets/manage.js', '/console-assets/i18n.js'):
            name = path.rsplit('/', 1)[1]
            self.reply(200, (WEB / name).read_bytes(), 'text/javascript' if name.endswith('.js') else 'text/css')
        elif path.startswith('/console-assets/'):
            self.reply(404, {'error': 'Unknown console asset'})
        elif path in ('/console-api/models', '/console-api/settings', '/console-api/sessions', '/console-api/workspaces', '/console-api/cleanup'):
            try:
                fn = {'/console-api/models': management.catalog, '/console-api/settings': management.settings, '/console-api/sessions': management.sessions, '/console-api/workspaces': management.workspaces, '/console-api/cleanup': __import__('cleanup').preview}[path]
                self.reply(200, redact(fn()))
            except (ValueError, HttpFailure) as e:
                self.reply(503, {'error': str(e)})
        elif path == '/console-api/state':
            self.reply(200, state())
        elif path.startswith('/console-api/task/'):
            try:
                t = task(path.rsplit('/', 1)[1])
                result = read_json(artifact_dir(t['id']) / 'result.json', {})
                self.reply(200, redact({'task': public_task(t), 'objective': t['objective'],
                           'acceptance': t['acceptance'], 'report': result.get('worker_report'),
                           'changes': result.get('changes'), 'errors': t.get('errors', result.get('errors', [])),
                           'pending': result.get('pending', []), 'session_url': session_url(t)}))
            except ValueError:
                self.reply(404, {'error': 'Task not found'})
        else:
            self.proxy()

    def read_body(self):
        try:
            size = int(self.headers.get('Content-Length', 0))
            if size < 0 or size > 65536:
                raise ValueError('Request body too large')
            body = json.loads(self.rfile.read(size) or b'{}')
            if not isinstance(body, dict):
                raise ValueError('JSON object required')
            return body
        except (json.JSONDecodeError, UnicodeError):
            raise ValueError('Invalid JSON body')

    def do_POST(self):
        if not self.authorize():
            return
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith('/console-api/'):
            return self.proxy()
        try:
            body = self.read_body()
            if path == '/console-api/quota':
                result = quota.refresh()
            elif path == '/console-api/settings':
                with locked('configuration'):
                    with locked():
                        write_json(STATE / 'maintenance.json', {'time': time.time()})
                    try:
                        result = management.save_settings(body)
                        service.reload_runtime()
                        result['restart_required'] = False
                    finally:
                        (STATE / 'maintenance.json').unlink(missing_ok=True)
            elif path == '/console-api/workspaces':
                result = management.save_workspace(body)
            elif path == '/console-api/cleanup':
                import cleanup
                result = cleanup.run(apply=body.get('apply') is True, force=False)
            elif path.startswith('/console-api/task/') and path.endswith('/steer'):
                import steering
                result = steering.send(path.split('/')[-2], body.get('text'), body.get('request_id'))
            elif path == '/console-api/tasks':
                result = delegate.submit(body)
            elif path == '/console-api/sessions':
                result = management.create_session(body)
            elif path.startswith('/console-api/session/'):
                result = management.update_session(path.rsplit('/', 1)[1], body)
            elif path.startswith('/console-api/task/') and path.endswith('/cancel'):
                t = task(path.split('/')[-2])
                result = public_task(update(t['id'], cancel_requested=True))
            else:
                self.reply(404, {'error': 'Unknown console action'})
                return
            self.reply(200, redact(result))
        except (ValueError, KeyError, TypeError) as e:
            self.reply(400, {'error': str(e)})
        except (HttpFailure, RuntimeError, OSError) as e:
            self.reply(503, {'error': 'Local service could not apply the request: ' + str(e)})

    def do_PATCH(self):
        if self.authorize():
            self.proxy()

    def do_DELETE(self):
        if self.authorize():
            self.proxy()

    def proxy(self):
        upstream = urllib.parse.urlsplit(config()['server_url'])
        pw = (STATE / 'server-password').read_text().strip()
        auth = 'Basic ' + base64.b64encode(('opencode:' + pw).encode()).decode()
        if self.headers.get('Upgrade', '').lower() == 'websocket':
            return self.websocket(upstream, auth)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in
                   {'host', 'authorization', 'cookie', 'connection', 'accept-encoding', 'transfer-encoding'}}
        headers['Authorization'] = auth
        length = int(self.headers.get('Content-Length', 0))
        if length > 32 * 1024 * 1024:
            self.reply(413, {'error': 'Request too large'})
            return
        body = self.rfile.read(length) if length else None
        conn = http.client.HTTPConnection(upstream.hostname, upstream.port, timeout=60)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            response = conn.getresponse()
            self.send_response(response.status)
            for k, v in response.getheaders():
                if k.lower() not in {'transfer-encoding', 'connection', 'set-cookie', 'access-control-allow-origin', 'content-length'}:
                    self.send_header(k, v)
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            while True:
                data = response.read1(65536)
                if not data:
                    break
                self.wfile.write(data)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except OSError:
            self.close_connection = True
        finally:
            conn.close()

    def websocket(self, upstream, auth):
        with socket.create_connection((upstream.hostname, upstream.port), timeout=15) as remote:
            headers = {k: v for k, v in self.headers.items() if k.lower() not in {'authorization', 'cookie', 'host'}}
            headers.update(Host=upstream.netloc, Authorization=auth)
            head = self.command + ' ' + self.path + ' HTTP/1.1\r\n' + ''.join(k + ': ' + v + '\r\n' for k, v in headers.items()) + '\r\n'
            remote.sendall(head.encode())
            self.close_connection = True
            remote.settimeout(None)
            self.connection.settimeout(None)
            while True:
                ready, _, _ = select.select([remote, self.connection], [], [], 60)
                if not ready:
                    return
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (self.connection if source is remote else remote).sendall(data)


def main():
    parsed = urllib.parse.urlsplit(config()['console_url'])
    if parsed.hostname != '127.0.0.1':
        raise ValueError('Console must bind to 127.0.0.1')
    server = ThreadingHTTPServer(('127.0.0.1', parsed.port), Handler)
    server.daemon_threads = True
    # Cookies are host-scoped, not port-scoped. Separate concurrent local installations
    # and preserve authentication through observer/UI process restarts.
    server.cookie_name = 'delegate_console_' + str(parsed.port)
    server.cookie = hmac.new((STATE / 'server-password').read_bytes(),
                             config()['console_url'].encode(), hashlib.sha256).hexdigest()
    server.serve_forever()


if __name__ == '__main__':
    main()
