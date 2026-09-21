#!/usr/bin/env python3
"""Local task console and same-origin authenticated OpenCode web gateway.

Every console API, native OpenCode HTTP path and WebSocket upgrade requires a
server-side session created through the explicit ``POST /console-api/auth/login``
flow. There is no automatic cookie bootstrap and the previous deterministic HMAC
cookie is not accepted. The login page and the small static assets it needs are
public; everything else is gated before any backend Basic credential is injected.
"""
import base64
import http.client
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import select
import socket
import time
import urllib.parse
from common import (STATE, api, artifact_dir, config, public_task, read_json, redact,
                    request_cancel, task, tasks)
import console_auth
import quota
import service
import management
import delegate
import task_activity
import credentials
import system_status
from common import CONFIG, HttpFailure, locked, update, write_json

WEB = Path(__file__).resolve().parent.parent / 'web'

# The login page reuses the existing translation bundle, so it is public too.
PUBLIC_ASSETS = {'login.js': 'text/javascript', 'style.css': 'text/css', 'i18n.js': 'text/javascript',
                 'worker-desk-icon.svg': 'image/svg+xml', 'worker-desk-icon.png': 'image/png'}
# Authenticated application assets are never served anonymously.
PRIVATE_ASSETS = {'app.js': 'text/javascript', 'manage.js': 'text/javascript', 'i18n.js': 'text/javascript',
                  'setup.js': 'text/javascript', 'task-view.js': 'text/javascript'}
MAX_LOGIN_BODY = 4096
MAX_API_BODY = 65536
# Long-lived streams and WebSockets re-check the server-side session this often
# so logout, expiry or password rotation terminates an already-open connection.
STREAM_AUTH_INTERVAL = 30
# Every long-lived socket read and write is additionally bounded by this
# timeout. An idle upstream or a client that stops reading can therefore delay
# a revocation check by at most one interval plus one I/O timeout, instead of
# parking the thread on a 60-second read or an unbounded write. The observable
# upper bound is conservative (STREAM_AUTH_INTERVAL + STREAM_IO_TIMEOUT), not
# an exact STREAM_AUTH_INTERVAL.
STREAM_IO_TIMEOUT = 5
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; form-action 'self'")
LOGIN_ERROR = 'Invalid username or password'


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


def quota_credential_refs():
    """Return only console-managed reference metadata, never secret values."""
    allowed = {'volcengine-control-ak', 'volcengine-control-sk', 'bark-endpoint'}
    return {'credentials': [{key: item.get(key) for key in
                             ('name', 'source', 'available', 'registered_at')}
                            for item in credentials.listing().get('credentials', [])
                            if item.get('name') in allowed]}


def state():
    entries = []
    for t in reversed(tasks()):
        p = public_task(t)
        p['group_id'] = t.get('group_id') or t['source_dir']
        p['group_title'] = t.get('group_title') or Path(t['source_dir']).name
        p['session_url'] = session_url(t)
        # Cheap per-task usage only: cached live sample or retained snapshot.
        # Global refresh never fetches sessions and never scans history.
        p['usage'] = task_activity.usage_for_task(t)
        entries.append(p)
    health = service_health()
    c = config()
    quota_view = quota.view(read_json(STATE / 'quota.json', {}))
    import economics
    host = system_status.snapshot()
    host['workers'] = {'active': sum(t.get('status') in ('starting', 'running', 'stopping', 'uncertain') for t in entries),
                       'queued': sum(t.get('status') == 'queued' for t in entries)}
    return redact({'tasks': entries, 'quota': quota_view,
                   'tier_guidance': quota.tier_guidance(c, quota_view),
                   'routing_status': quota.routing_status(c, quota_view),
                   'economics': economics.summary(c, quota_view),
                   'pool_healthy': all(health.values()), 'services': health,
                   'host': host,
                   'profiles': {k: {'label': v.get('label', v['model']), 'model': v['model']} for k, v in c['profiles'].items()},
                   'updated_at': time.time(), 'max_parallel_per_owner': management.settings()['max_parallel_per_owner']})


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    # Bound request/header/body reads so a slow client cannot hold a thread
    # forever. Long-lived WebSocket and streaming responses clear this timeout.
    timeout = 30

    def log_message(self, *_):
        pass  # No URLs, cookies, usernames, prompts or credentials in access logs.

    # --- response helpers -------------------------------------------------

    def reply(self, status, content, mime='application/json', headers=None):
        raw = content if isinstance(content, bytes) else json.dumps(content, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('X-Frame-Options', 'DENY')
        if self.command not in ('GET', 'HEAD'):
            # Authentication/guard responses may precede reading a request body.
            # Do not let unread bytes become the next keep-alive request line.
            self.close_connection = True
            self.send_header('Connection', 'close')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def redirect(self, location):
        self.reply(302, b'', 'text/plain; charset=utf-8', {'Location': location})

    # --- request guards and session --------------------------------------

    def auth_root(self):
        return getattr(self.server, 'auth_root', None)

    def origins(self):
        cached = getattr(self.server, 'allowed_origins', None)
        if cached is None:
            cached = console_auth.origin_tuples(config())
            self.server.allowed_origins = cached
        return cached

    def trusted_proxies(self):
        cached = getattr(self.server, 'trusted_proxy_networks', None)
        if cached is None:
            cached = console_auth.trusted_proxy_networks(config())
            self.server.trusted_proxy_networks = cached
        return cached

    def guard(self):
        """Host and CSRF checks applied to every request, public or not.

        These checks deliberately never consult forwarded headers: only the
        actual Host/Origin/Sec-Fetch-Site values from the client are evaluated.
        Network-level client identity (used for login throttle buckets) is a
        separate decision handled by ``client_identity``.
        """
        if not console_auth.host_allowed(self.headers.get('Host', ''), self.origins()):
            self.reply(403, {'error': 'Invalid local host'})
            return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self.reply(403, {'error': 'Cross-origin request rejected'})
            return False
        origin = self.headers.get('Origin')
        if origin is not None:
            # The Origin must both be allowed and match the actual request Host,
            # so one allowed origin cannot address a different allowed host.
            if not console_auth.origin_allowed(origin, self.origins()) or \
                    not console_auth.origin_matches_host(origin, self.headers.get('Host', '')):
                self.reply(403, {'error': 'Cross-origin request rejected'})
                return False
        return True

    def session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
        except Exception:
            return None
        morsel = cookie.get(self.cookie_name())
        return morsel.value if morsel is not None else None

    def authenticated_user(self):
        token = self.session_token()
        if not token:
            return None
        try:
            return console_auth.session_username(token, self.auth_root())
        except Exception:
            return None

    def require_session(self, redirect_html=False):
        user = self.authenticated_user()
        if user:
            return True
        if redirect_html:
            self.redirect('/console-login')
        else:
            self.reply(401, {'error': 'Authentication required'})
        return False

    def cookie_name(self):
        return getattr(self.server, 'cookie_name', 'delegate_console_session')

    def set_cookie(self, token):
        attributes = ['Path=/', 'HttpOnly', 'SameSite=Strict',
                      'Max-Age=' + str(console_auth.SESSION_TTL_SECONDS)]
        if console_auth.secure_for_host(self.headers.get('Host', ''), self.origins()):
            attributes.append('Secure')
        return self.cookie_name() + '=' + token + '; ' + '; '.join(attributes)

    def clear_cookie(self):
        attributes = ['Path=/', 'HttpOnly', 'SameSite=Strict', 'Max-Age=0']
        if console_auth.secure_for_host(self.headers.get('Host', ''), self.origins()):
            attributes.append('Secure')
        return self.cookie_name() + '=; ' + '; '.join(attributes)

    def session_valid(self, token):
        if not token:
            return False
        try:
            return console_auth.session_username(token, self.auth_root()) is not None
        except Exception:
            return False

    def wants_html(self):
        return 'text/html' in self.headers.get('Accept', '').lower()

    def peer(self):
        return self.client_address[0] if self.client_address else 'unknown'

    def client_identity(self):
        """Throttle identity: socket peer unless an allowlisted proxy forwarded one."""
        return console_auth.client_identity(self.peer(), self.headers.get('X-Forwarded-For'),
                                            self.trusted_proxies())

    # --- auth endpoints ---------------------------------------------------

    def auth_status(self):
        user = self.authenticated_user()
        if user:
            self.reply(200, {'authenticated': True, 'username': user})
        else:
            self.reply(200, {'authenticated': False})

    def auth_login(self):
        root = self.auth_root()
        peer = self.client_identity()
        # Reserve the attempt atomically before hashing so a concurrent burst
        # cannot each pass a stale check and exceed the cap.
        try:
            retry = console_auth.admit_login_attempt(peer, root)
        except Exception:
            # The throttle store is unavailable: fail closed. Admitting the
            # attempt with no reservation would allow an unbounded guess burst.
            self.reply(503, {'error': 'Console authentication is unavailable'})
            return
        if retry:
            self.reply(429, {'error': 'Too many login attempts'}, headers={'Retry-After': str(retry)})
            return
        try:
            body = self.read_body(MAX_LOGIN_BODY)
            username, password = body.get('username'), body.get('password')
            if not isinstance(username, str) or not isinstance(password, str) or \
                    len(username) > 256 or len(password) > console_auth.MAX_PASSWORD_BYTES:
                raise ValueError('Invalid login body')
        except ValueError:
            self.reply(400, {'error': 'Invalid login request'})
            return
        try:
            token = console_auth.authenticate(username, password, root)
        except console_auth.AuthStateError:
            # Fail closed on malformed local state; never fall back to a default.
            self.reply(503, {'error': 'Console authentication is unavailable'})
            return
        if token is None:
            self.reply(401, {'error': LOGIN_ERROR})
            return
        try:
            console_auth.record_login_success(peer, root)
        except Exception:
            pass
        self.reply(200, {'authenticated': True, 'username': username},
                   headers={'Set-Cookie': self.set_cookie(token)})

    def auth_logout(self):
        token = self.session_token()
        cleared = {'Set-Cookie': self.clear_cookie()}
        if token:
            try:
                console_auth.revoke_session(token, self.auth_root())
            except Exception:
                # A failed server-side revocation must never be reported as a
                # successful logout. Clear the browser cookie anyway so the
                # client stops sending the token; the caller sees 503.
                self.reply(503, {'error': 'Console authentication is unavailable'}, headers=cleared)
                return
        self.reply(200, {'authenticated': False}, headers=cleared)

    # --- static assets ----------------------------------------------------

    def serve_login(self):
        page = WEB / 'login.html'
        if not page.is_file():
            self.reply(404, {'error': 'Login page unavailable'})
            return
        self.reply(200, page.read_bytes(), 'text/html; charset=utf-8',
                   {'Content-Security-Policy': CSP})

    def serve_public_asset(self, name):
        if name not in PUBLIC_ASSETS:
            self.reply(404, {'error': 'Unknown console asset'})
            return
        path = WEB / name
        if not path.is_file():
            self.reply(404, {'error': 'Unknown console asset'})
            return
        self.reply(200, path.read_bytes(), PUBLIC_ASSETS[name])

    def serve_private_asset(self, name):
        if name not in PRIVATE_ASSETS:
            self.reply(404, {'error': 'Unknown console asset'})
            return
        path = WEB / name
        if not path.is_file():
            self.reply(404, {'error': 'Unknown console asset'})
            return
        self.reply(200, path.read_bytes(), PRIVATE_ASSETS[name])

    def serve_console(self):
        page = WEB / 'index.html'
        if not page.is_file():
            self.reply(404, {'error': 'Console page unavailable'})
            return
        self.reply(200, page.read_bytes(), 'text/html; charset=utf-8',
                   {'Content-Security-Policy': CSP})

    # --- routing ----------------------------------------------------------

    def do_GET(self):
        if not self.guard():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == '/console-login':
            self.serve_login()
            return
        if path.startswith('/console-assets/'):
            # Only a bare allowlisted filename is served; traversal never matches.
            name = path[len('/console-assets/'):]
            if name in PUBLIC_ASSETS:
                self.serve_public_asset(name)
                return
            if name not in PRIVATE_ASSETS:
                self.reply(404, {'error': 'Unknown console asset'})
                return
            if not self.require_session():
                return
            self.serve_private_asset(name)
            return
        if path == '/console-api/auth/status':
            self.auth_status()
            return
        if path == '/console':
            if not self.require_session(redirect_html=self.wants_html()):
                return
            self.serve_console()
            return
        if path.startswith('/console-api/'):
            if not self.require_session():
                return
            self.console_get(path)
            return
        # Native OpenCode path or WebSocket upgrade: gate before Basic auth.
        # Upgrades always get a JSON 401 rather than an HTML login redirect.
        upgrade = self.headers.get('Upgrade', '').lower() == 'websocket'
        if not self.require_session(redirect_html=self.wants_html() and not upgrade):
            return
        self.proxy()

    def console_get(self, path):
        if path in ('/console-api/models', '/console-api/settings', '/console-api/sessions',
                    '/console-api/workspaces', '/console-api/cleanup', '/console-api/usage-history',
                    '/console-api/credentials', '/console-api/stats'):
            try:
                fn = {'/console-api/models': management.catalog, '/console-api/settings': management.settings,
                      '/console-api/sessions': management.sessions, '/console-api/workspaces': management.workspaces,
                      '/console-api/cleanup': __import__('cleanup').preview,
                      '/console-api/usage-history':
                          lambda: __import__('usage_ledger').summary(include_entries=True),
                      '/console-api/credentials': quota_credential_refs,
                      '/console-api/stats': lambda: __import__('analytics').summary()}[path]
                self.reply(200, redact(fn()))
            except (ValueError, HttpFailure, credentials.CredentialError) as e:
                self.reply(503, {'error': str(e)})
        elif path == '/console-api/state':
            self.reply(200, state())
        elif path.startswith('/console-api/task/') and '/event/' in path:
            try:
                tail = path[len('/console-api/task/'):]
                task_id, event_id = tail.split('/event/', 1)
                t = task(urllib.parse.unquote(task_id))
                text = task_activity.full_event_text(t, urllib.parse.unquote(event_id))
                self.reply(200, redact({'text': text}))
            except ValueError as error:
                self.reply(404, {'error': str(error)})
        elif path.startswith('/console-api/task/'):
            try:
                t = task(path.rsplit('/', 1)[1])
                result = read_json(artifact_dir(t['id']) / 'result.json', {})
                payload = {'task': public_task(t), 'objective': t['objective'],
                           'acceptance': t['acceptance'], 'report': result.get('worker_report'),
                           'changes': result.get('changes'), 'errors': t.get('errors', result.get('errors', [])),
                           'pending': result.get('pending', []), 'session_url': session_url(t)}
                # Live activity/usage for in-flight tasks; retained evidence
                # (never the network) for closed or completed jobs.
                payload.update(task_activity.snapshot(t))
                self.reply(200, redact(payload))
            except ValueError:
                self.reply(404, {'error': 'Task not found'})
        else:
            self.reply(404, {'error': 'Unknown console action'})

    def read_body(self, max_size=MAX_API_BODY):
        try:
            size = int(self.headers.get('Content-Length', 0))
        except (TypeError, ValueError):
            raise ValueError('Invalid request body') from None
        if size < 0 or size > max_size:
            raise ValueError('Request body too large')
        try:
            body = json.loads(self.rfile.read(size) or b'{}')
        except (json.JSONDecodeError, UnicodeError):
            raise ValueError('Invalid JSON body')
        if not isinstance(body, dict):
            raise ValueError('JSON object required')
        return body

    def do_POST(self):
        if not self.guard():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == '/console-api/auth/login':
            self.auth_login()
            return
        if path == '/console-api/auth/logout':
            self.auth_logout()
            return
        if path.startswith('/console-api/auth/'):
            self.reply(404, {'error': 'Unknown console action'})
            return
        if not self.require_session():
            return
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
            elif path == '/console-api/credentials':
                action = body.get('action')
                name = body.get('name')
                # The console exposes only fixed references used by built-in adapters.
                # Values are never accepted or returned; source metadata points
                # to an owner-only local file or a service environment variable.
                if name not in ('volcengine-control-ak', 'volcengine-control-sk', 'bark-endpoint'):
                    raise ValueError('Unsupported credential reference')
                if action == 'register':
                    registered = credentials.register(name, file=body.get('file'), env=body.get('env'))
                    result = {key: registered.get(key) for key in
                              ('name', 'source', 'available', 'registered_at')}
                elif action == 'remove':
                    result = credentials.remove(name)
                else:
                    raise ValueError('Unknown credential action')
            elif path == '/console-api/cleanup':
                import cleanup
                result = cleanup.run(apply=body.get('apply') is True, force=False)
            elif path.startswith('/console-api/task/') and path.endswith('/steer'):
                import steering
                result = steering.send(path.split('/')[-2], body.get('text'), body.get('request_id'))
            elif path == '/console-api/tasks':
                result = delegate.submit(body)
            elif path == '/console-api/tasks/manage':
                result = management.delete_tasks(body)
            elif path == '/console-api/sessions':
                result = management.create_session(body)
            elif path.startswith('/console-api/session/'):
                result = management.update_session(path.rsplit('/', 1)[1], body)
            elif path.startswith('/console-api/task/') and path.endswith('/cancel'):
                t = task(path.split('/')[-2])
                result = public_task(request_cancel(t['id'], 'user_requested', source='console'))
            else:
                self.reply(404, {'error': 'Unknown console action'})
                return
            self.reply(200, redact(result))
        except (ValueError, KeyError, TypeError, credentials.CredentialError) as e:
            self.reply(400, {'error': str(e)})
        except (HttpFailure, RuntimeError, OSError) as e:
            self.reply(503, {'error': 'Local service could not apply the request: ' + str(e)})

    def do_PATCH(self):
        if not self.guard():
            return
        if not self.require_session():
            return
        self.proxy()

    def do_DELETE(self):
        if not self.guard():
            return
        if not self.require_session():
            return
        self.proxy()

    # --- native gateway proxy --------------------------------------------

    def upstream(self):
        """Validated loopback upstream; never connect anywhere else."""
        parsed = urllib.parse.urlsplit(config()['server_url'])
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port or \
                parsed.username or parsed.password or parsed.path not in ('', '/'):
            return None
        return parsed

    def proxy(self):
        upstream = self.upstream()
        if upstream is None:
            self.reply(502, {'error': 'OpenCode upstream is not a loopback address'})
            return
        pw = (STATE / 'server-password').read_text().strip()
        auth = 'Basic ' + base64.b64encode(('opencode:' + pw).encode()).decode()
        token = self.session_token()
        if self.headers.get('Upgrade', '').lower() == 'websocket':
            return self.websocket(upstream, auth, token)
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
            # getresponse() may release conn.sock for a close-delimited body, so
            # capture the live socket before it is handed to the response.
            upstream_socket = conn.sock
            response = conn.getresponse()
            if upstream_socket is None:
                raw = getattr(getattr(response, 'fp', None), 'raw', None)
                upstream_socket = getattr(raw, '_sock', None)
            self.send_response(response.status)
            for k, v in response.getheaders():
                if k.lower() not in {'transfer-encoding', 'connection', 'set-cookie', 'access-control-allow-origin', 'content-length'}:
                    self.send_header(k, v)
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            # A finite write timeout bounds a stalled client. The upstream read
            # is not allowed to block indefinitely either: any prefetched bytes
            # are drained first, otherwise a short bounded select() poll waits
            # for more data. read1() still performs http.client's chunked
            # decoding unchanged.
            self.connection.settimeout(STREAM_IO_TIMEOUT)
            if upstream_socket is not None:
                upstream_socket.settimeout(STREAM_IO_TIMEOUT)
            last_check = time.time()
            read_poll = STREAM_IO_TIMEOUT
            while True:
                if response.length == 0:
                    break  # Includes 204/304 and explicitly empty keep-alive responses.
                now = time.time()
                if now - last_check >= STREAM_AUTH_INTERVAL:
                    if not self.session_valid(token):
                        break
                    last_check = now
                # http.client's BufferedReader may have prefetched body bytes
                # (or chunk framing) while parsing the response headers, so
                # socket select readiness alone is not sufficient: probe the
                # buffer so a complete small/keep-alive body is forwarded
                # immediately. peek() may perform one raw read, so probe while
                # the upstream socket is non-blocking; otherwise an idle stream
                # would block here and latch a read timeout.
                buffered = False
                if response.fp is not None and upstream_socket is not None:
                    try:
                        upstream_socket.setblocking(False)
                        buffered = bool(response.fp.peek(0))
                    except BlockingIOError:
                        buffered = False
                    except (ValueError, OSError):
                        break
                    finally:
                        upstream_socket.settimeout(STREAM_IO_TIMEOUT)
                if not buffered and upstream_socket is not None:
                    try:
                        ready, _, _ = select.select([upstream_socket], [], [], read_poll)
                    except (OSError, ValueError):
                        break
                    if not ready:
                        continue
                try:
                    data = response.read1(65536)
                except (TimeoutError, socket.timeout, http.client.IncompleteRead):
                    break
                except OSError:
                    break
                if not data:
                    break
                try:
                    self.wfile.write(data)
                    self.wfile.flush()
                except (TimeoutError, socket.timeout, BrokenPipeError, ConnectionResetError, OSError):
                    break
                # A known-length body is complete once its remaining length is
                # exhausted; do not keep the proxy thread parked on an idle
                # keep-alive upstream.
                remaining = getattr(response, 'length', None)
                if remaining is not None and remaining <= 0:
                    break
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except OSError:
            self.close_connection = True
        finally:
            conn.close()

    def websocket(self, upstream, auth, token):
        with socket.create_connection((upstream.hostname, upstream.port), timeout=15) as remote:
            headers = {k: v for k, v in self.headers.items() if k.lower() not in {'authorization', 'cookie', 'host'}}
            headers.update(Host=upstream.netloc, Authorization=auth)
            head = self.command + ' ' + self.path + ' HTTP/1.1\r\n' + ''.join(k + ': ' + v + '\r\n' for k, v in headers.items()) + '\r\n'
            remote.sendall(head.encode())
            self.close_connection = True
            # Finite timeouts also bound a stalled WebSocket send/receive so a
            # peer that stops reading cannot block this thread indefinitely.
            remote.settimeout(STREAM_IO_TIMEOUT)
            self.connection.settimeout(STREAM_IO_TIMEOUT)
            last_check = time.time()
            try:
                while True:
                    ready, _, _ = select.select([remote, self.connection], [], [], STREAM_AUTH_INTERVAL)
                    if time.time() - last_check >= STREAM_AUTH_INTERVAL:
                        # Re-check the live session; logout/expiry/rotation closes here.
                        if not self.session_valid(token):
                            return
                        last_check = time.time()
                    if not ready:
                        continue
                    for source in ready:
                        try:
                            data = source.recv(65536)
                        except (TimeoutError, socket.timeout):
                            continue
                        if not data:
                            return
                        try:
                            (self.connection if source is remote else remote).sendall(data)
                        except (TimeoutError, socket.timeout, BrokenPipeError, ConnectionResetError):
                            return
            except OSError:
                return


def main():
    parsed = urllib.parse.urlsplit(config()['console_url'])
    if parsed.hostname != '127.0.0.1' or not parsed.port:
        raise ValueError('Console URL must be plain HTTP on a loopback host')
    bind = console_auth.console_bind(config())
    # Validate configured origins/trusted proxies up front so an unsafe list
    # never starts serving.
    allowed_origins = console_auth.origin_tuples(config())
    trusted_proxies = console_auth.trusted_proxy_networks(config())
    # Cold start creates the documented fresh default only when no account exists;
    # existing credentials and sessions are preserved. Malformed state fails closed.
    console_auth.ensure_default_account(STATE)
    server = ThreadingHTTPServer((bind, parsed.port), Handler)
    server.daemon_threads = True
    # Cookies are host-scoped, not port-scoped. Separate concurrent local installations
    # and preserve server-side sessions through observer/UI process restarts.
    server.cookie_name = 'delegate_console_session_' + str(parsed.port)
    server.allowed_origins = allowed_origins
    server.trusted_proxy_networks = trusted_proxies
    server.auth_root = STATE
    server.serve_forever()


if __name__ == '__main__':
    main()
