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
import ipaddress
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import select
import socket
import time
import threading
import urllib.parse
from common import (STATE, api, artifact_dir, config, public_task, read_json, redact,
                    request_cancel, task, tasks)
import console_auth
import console_webauthn
import quota
import service
import management
import delegate
import task_activity
import credentials
import system_status
import host_telemetry
import host_controls
from common import CONFIG, HttpFailure, locked, update, write_json

WEB = Path(__file__).resolve().parent.parent / 'web'

# The login page reuses the existing translation bundle, so it is public too.
PUBLIC_ASSETS = {'login.js': 'text/javascript', 'style.css': 'text/css', 'i18n.js': 'text/javascript',
                 'passkeys.js': 'text/javascript', 'passkeys.css': 'text/css',
                 'worker-desk-icon.svg': 'image/svg+xml', 'worker-desk-icon.png': 'image/png'}
# Authenticated application assets are never served anonymously.
PRIVATE_ASSETS = {'app.js': 'text/javascript', 'manage.js': 'text/javascript', 'i18n.js': 'text/javascript',
                  'setup.js': 'text/javascript', 'task-view.js': 'text/javascript',
                  'host.js': 'text/javascript', 'memory.js': 'text/javascript',
                  'kimi-account-view.js': 'text/javascript', 'voice-settings.js': 'text/javascript'}
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


_HOST_AUX = None

def read_host_aux():
    return {'services': host_controls.services_status(), 'service_health': service_health(),
            'processes': host_controls.top_processes(), 'caffeinate': host_controls.caffeinate_status(),
            'operations': host_controls.reconcile_operations(), 'affected_jobs': host_controls.affected_jobs()}


def start_host_aux():
    global _HOST_AUX
    if _HOST_AUX is not None:
        return
    _HOST_AUX = {'services': [], 'service_health': {}, 'processes': [], 'operations': [],
                 'affected_jobs': [], 'caffeinate': {}}
    def collect_aux():
        global _HOST_AUX
        while True:
            try:
                _HOST_AUX = read_host_aux()
            except Exception:
                pass
            time.sleep(5)
    threading.Thread(target=collect_aux,daemon=True,name='host-service-observer').start()


def quota_credential_refs():
    """Return only console-managed reference metadata, never secret values."""
    allowed = {'volcengine-control-ak', 'volcengine-control-sk',
               'aliyun-control-ak', 'aliyun-control-sk'}
    return {'credentials': [{key: item.get(key) for key in
                             ('name', 'source', 'available', 'registered_at')}
                            for item in credentials.listing().get('credentials', [])
                            if item.get('name') in allowed or
                            re.fullmatch(r'bark-[a-z0-9][a-z0-9_.-]{0,58}', item.get('name', ''))]}


_MEMORY_SESSIONS = {'at': 0.0, 'map': {}}


def memory_session_link(session_id):
    """Verify a memory's session id against real OpenCode sessions before linking.

    A stale migrated id must stay plain provenance text: the console never
    fabricates a session link, and cannot verify one while OpenCode is
    unreachable.
    """
    if not isinstance(session_id, str) or not session_id:
        return None
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', session_id):
        return {'id': session_id, 'verified': False, 'url': None}
    now = time.time()
    if now - _MEMORY_SESSIONS['at'] > 60:
        try:
            result = management.sessions({})
            fresh = {}
            for item in (result or {}).get('sessions') or []:
                sid, directory = item.get('id'), item.get('directory')
                if isinstance(sid, str) and isinstance(directory, str) and directory:
                    fresh[sid] = directory
            _MEMORY_SESSIONS['map'] = fresh
            _MEMORY_SESSIONS['at'] = now
        except Exception:
            return {'id': session_id, 'verified': False, 'url': None}
    directory = _MEMORY_SESSIONS['map'].get(session_id)
    if not isinstance(directory, str) or not directory:
        return {'id': session_id, 'verified': False, 'url': None}
    encoded = base64.urlsafe_b64encode(directory.encode()).decode().rstrip('=')
    return {'id': session_id, 'verified': True,
            'url': '/' + encoded + '/session/' + urllib.parse.quote(session_id, safe='')}


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
        p['recent_activity'] = task_activity.recent_activity_for_task(t)
        import progress
        p['progress'] = progress.snapshot(t['id'], refresh_github=False)
        entries.append(p)
    health = service_health()
    c = config()
    quota_view = quota.view(read_json(STATE / 'quota.json', {}))
    import economics
    guidance = quota.tier_guidance(c, quota_view)
    routing_view = quota.routing_status(c, quota_view)
    economic_view = economics.summary(c, quota_view)
    economic_view['work_pool']['display_projection'] = economics.display_endurance(
        c, economic_view['work_pool'], guidance, routing_view,
        read_json(STATE / 'load-control.json', {}))
    host = system_status.snapshot()
    host['workers'] = {'active': sum(t.get('status') in ('starting', 'running', 'stopping', 'uncertain') for t in entries),
                       'queued': sum(t.get('status') == 'queued' for t in entries)}
    return redact({'tasks': entries, 'quota': quota_view,
                   'tier_guidance': guidance,
                   'routing_status': routing_view,
                   'economics': economic_view,
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
        self.send_header('Referrer-Policy', 'no-referrer')
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
        if getattr(self.server, 'enforce_lan_peer', False):
            try:
                peer = ipaddress.ip_address(self.peer())
                networks = ('127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')
                if not any(peer in ipaddress.ip_network(value) for value in networks):
                    raise ValueError()
            except ValueError:
                self.reply(403, {'error': 'Use the public passkey site'})
                return False
        if not console_auth.host_allowed(self.headers.get('Host', ''), self.origins()):
            self.reply(403, {'error': 'Invalid local host'})
            return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            # An enrollment link deliberately navigates from a LAN origin to
            # the public HTTPS RP. Permit only top-level, read-only entry
            # documents; APIs, upgrades and mutations retain the strict guard.
            entry_navigation = self.command == 'GET' and \
                self.headers.get('Sec-Fetch-Mode') == 'navigate' and \
                self.headers.get('Sec-Fetch-Dest') == 'document' and \
                urllib.parse.urlsplit(self.path).path in ('/', '/console', '/console-login', '/console-passkeys')
            if not entry_navigation:
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
            context = self.auth_context(token)
            return context['username'] if context else None
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
            return self.auth_context(token) is not None
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

    def public_access(self):
        host = console_auth._normalize_host_header(self.headers.get('Host', ''))
        return getattr(self.server, 'listener_role', 'lan') == 'public' or bool(
            host and host[0] == self.passkey_settings()[0])

    def passkey_settings(self):
        return getattr(self.server, 'passkey_settings', None) or console_webauthn.rp_settings(config())

    def auth_context(self, token=None):
        if self.public_access():
            if not console_webauthn.enabled(config()) or not console_webauthn.available() or \
                    self.passkey_settings() != console_webauthn.rp_settings(config()):
                return None
        return console_auth.session_context(token or self.session_token(), self.auth_root(),
                                            require_passkey=self.public_access())

    def passkey_origin(self):
        host = console_auth._normalize_host_header(self.headers.get('Host', ''))
        rp_id, origin = self.passkey_settings()
        return bool(host and host[0] == rp_id and host[1] in (None, 443)
                    and self.headers.get('Origin') == origin)

    # --- auth endpoints ---------------------------------------------------

    def auth_status(self):
        context = self.auth_context()
        enabled = console_webauthn.enabled(config())
        methods = (['passkey'] if enabled else []) if self.public_access() else ['password']
        self.reply(200, {'authenticated': bool(context),
            'username': context['username'] if context else None,
            'auth_method': context['auth_method'] if context else None,
            'allowed_methods': methods, 'passkey_available': enabled and console_webauthn.available(),
            'passkey_enabled': enabled,
            'passkey_origin': self.passkey_settings()[1]})

    def auth_login(self):
        if self.public_access():
            self.reply(403, {'error': 'Use a passkey to sign in through the public site'})
            return
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

    def passkey_action(self, path):
        """Authentication endpoints never issue a session from an enrollment grant."""
        if not console_webauthn.enabled(config()):
            self.reply(404, {'error': 'Passkeys are not enabled'})
            return
        root = self.auth_root()
        try:
            if self.passkey_settings() != console_webauthn.rp_settings(config()):
                self.reply(503, {'error': 'Passkey configuration changed; restart the console'})
                return
            body = self.read_body(MAX_API_BODY)
            if path == '/console-api/auth/enrollment-grant':
                if self.public_access() or not self.require_session():
                    if self.public_access():
                        self.reply(403, {'error': 'Create enrollment links from the LAN console'})
                    return
                retry = console_auth.admit_login_attempt(self.client_identity(), root)
                if retry:
                    self.reply(429, {'error': 'Too many login attempts'}, headers={'Retry-After': str(retry)})
                    return
                user = self.authenticated_user()
                if not console_auth.verify_credentials(user, body.get('password'), root):
                    self.reply(401, {'error': LOGIN_ERROR})
                    return
                console_auth.record_login_success(self.client_identity(), root)
                self.reply(200, {'enrollment_url': console_webauthn.create_grant(root), 'expires_in': 600})
                return
            if not self.passkey_origin():
                self.reply(403, {'error': 'Use the configured HTTPS passkey site'})
                return
            if path.endswith('/authenticate/options'):
                retry = console_auth.admit_login_attempt(self.client_identity(), root)
                if retry:
                    self.reply(429, {'error': 'Too many login attempts'}, headers={'Retry-After': str(retry)})
                    return
                self.reply(200, console_webauthn.authentication_options(root))
            elif path.endswith('/authenticate/verify'):
                token = console_webauthn.authenticate(body, root)
                console_auth.record_login_success(self.client_identity(), root)
                self.reply(200, {'authenticated': True}, headers={'Set-Cookie': self.set_cookie(token)})
            elif path.endswith('/register/options'):
                self.reply(200, console_webauthn.registration_options(root, self.session_token(), body.get('grant')))
            elif path.endswith('/register/verify'):
                self.reply(200, console_webauthn.register(body, root, self.session_token()))
            elif path == '/console-api/auth/passkeys/delete':
                if not self.require_session():
                    return
                self.reply(200, console_webauthn.delete(body.get('id'), self.session_token(), root,
                                                       public=self.public_access()))
            else:
                self.reply(404, {'error': 'Unknown authentication action'})
        except console_auth.AuthStateError:
            self.reply(503, {'error': 'Passkey authentication is unavailable'})
        except (ValueError, TypeError, KeyError):
            self.reply(400, {'error': 'Passkey request failed; sign in again or request a new enrollment link'})

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
        if path == '/console-passkeys':
            page = WEB / 'passkeys.html'
            if page.is_file():
                self.reply(200, page.read_bytes(), 'text/html; charset=utf-8',
                           {'Content-Security-Policy': CSP, 'Referrer-Policy': 'no-referrer'})
            else:
                self.reply(404, {'error': 'Passkey page unavailable'})
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
        if path == '/console-api/auth/passkeys':
            if self.require_session():
                try:
                    self.reply(200, {'passkeys': console_webauthn.listing(self.auth_root())})
                except console_auth.AuthStateError:
                    self.reply(503, {'error': 'Passkey state is unavailable'})
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
        if path == '/console-api/host/snapshot':
            # Cached collector read only; slow acquisition never happens in GET.
            collector = host_telemetry.ensure_collector()
            snapshot = collector.snapshot()
            snapshot.update(_HOST_AUX if _HOST_AUX is not None else read_host_aux())
            self.reply(200, redact({'snapshot': snapshot}))
            return
        if path == '/console-api/host/capabilities':
            self.reply(200, host_controls.capabilities())
            return
        if path == '/console-api/host/operations':
            self.reply(200, {'operations': host_controls.reconcile_operations()})
            return
        if path.startswith('/console-api/host/history'):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            range_name = (query.get('range') or ['15m'])[0]
            try:
                history = host_telemetry.ensure_collector().history(range_name)
            except ValueError as error:
                self.reply(400, {'error': str(error)})
                return
            self.reply(200, redact(history))
            return
        if path in ('/console-api/memory/browse', '/console-api/memory/detail',
                    '/console-api/memory/activity', '/console-api/memory/capacity'):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:
                memory = __import__('memory_manager')
                if path.endswith('/browse'):
                    result = memory.browse(category=query.get('category', [None])[0],
                                           folder=query.get('folder', [None])[0],
                                           project=query.get('project', [None])[0],
                                           page=query.get('page', [1])[0],
                                           page_size=query.get('pageSize', [25])[0],
                                           sort=query.get('sort', ['newest'])[0],
                                           query=query.get('q', [None])[0])
                elif path.endswith('/detail'):
                    result = memory.detail(query.get('id', [None])[0],
                                           scope=query.get('project', [None])[0])
                    if isinstance(result, dict) and result.get('sessionId'):
                        result['session'] = memory_session_link(result['sessionId'])
                elif path.endswith('/activity'):
                    result = memory.activity()
                else:
                    result = memory.capacity()
                self.reply(200, redact(result))
            except (ValueError, TypeError, KeyError) as error:
                self.reply(400, {'error': str(error)})
            except (HttpFailure, OSError, RuntimeError) as error:
                self.reply(503, {'error': 'Local memory service could not complete the request: ' + str(error)})
            return
        if path in ('/console-api/kimi-account/status', '/console-api/kimi-account/login'):
            # Ordinary console session + host/CSRF guards apply. The adapter's
            # output is already normalized and secret-free; redact() is a final
            # belt-and-braces boundary. An unavailable CLI/account service is a
            # 503, never a fabricated zero quota.
            try:
                if path.endswith('/login'):
                    self.reply(200, redact(management.kimi_account_login_status()))
                else:
                    self.reply(200, redact(management.kimi_account_status()))
            except (RuntimeError, ValueError, OSError) as error:
                self.reply(503, {'error': 'Kimi account service unavailable: ' + str(error)})
            return
        if path == '/console-api/local-voice':
            # Read-only status for the installed local Qwen notify skill: catalog,
            # effective default and bounded playback state. A GET never plays audio.
            try:
                local_voice = __import__('local_voice')
                self.reply(200, redact(local_voice.view()))
            except (RuntimeError, OSError) as error:
                self.reply(503, {'error': 'Local voice runtime unavailable: ' + str(error)})
            return
        if path in ('/console-api/models', '/console-api/settings', '/console-api/sessions',
                    '/console-api/workspaces', '/console-api/cleanup', '/console-api/usage-history',
                    '/console-api/credentials', '/console-api/stats', '/console-api/opencode-version',
                    '/console-api/usage-report',
                    '/console-api/memory/status', '/console-api/memory/projects',
                    '/console-api/memory/profile', '/console-api/memory/stats'):
            try:
                memory = __import__('memory_manager')
                fn = {'/console-api/models': management.catalog, '/console-api/settings': management.settings,
                      '/console-api/sessions': management.sessions, '/console-api/workspaces': management.workspaces,
                      '/console-api/cleanup': __import__('cleanup').preview,
                      '/console-api/usage-history':
                          lambda: __import__('usage_ledger').summary(include_entries=True),
                      '/console-api/credentials': quota_credential_refs,
                      '/console-api/stats': lambda: __import__('analytics').summary(
                          days=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                          .get('days', ['30'])[0]),
                      '/console-api/usage-report': lambda: __import__('analytics').usage_report(),
                      '/console-api/opencode-version': lambda: __import__('opencode_update').status(),
                      '/console-api/memory/status': memory.status,
                      '/console-api/memory/projects': memory.projects,
                      '/console-api/memory/profile': memory.profile,
                      '/console-api/memory/stats': memory.stats}[path]
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
                import progress
                payload['progress'] = progress.snapshot(t['id'])
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
        if path.startswith('/console-api/auth/passkey/') or path in (
                '/console-api/auth/enrollment-grant', '/console-api/auth/passkeys/delete'):
            self.passkey_action(path)
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
            if path == '/console-api/host/action':
                user = self.authenticated_user()
                result = host_controls.dispatch(body.get('action'), body.get('params'), user,
                                                peer=self.client_identity())
            elif path == '/console-api/quota':
                result = quota.refresh(force=True)
            elif path == '/console-api/local-voice':
                # Fixed-argv, bounded control over the installed Qwen notify skill.
                # Only catalog voice IDs are accepted; preview text, commands,
                # paths and environment values are never taken from the body.
                # Invalid input is a 400, a missing runtime a 503 (common handler).
                local_voice = __import__('local_voice')
                action = body.get('action')
                if action == 'save':
                    result = local_voice.save(body)
                elif action == 'preview':
                    result = local_voice.preview(body)
                elif action == 'stop':
                    result = local_voice.stop()
                else:
                    raise ValueError('Unknown local voice action')
            elif path == '/console-api/opencode-version':
                action = body.get('action')
                if action not in ('check', 'update'):
                    raise ValueError('Unknown OpenCode version action')
                updater = __import__('opencode_update')
                result = updater.run(check_only=action == 'check', force_check=True)
            elif path == '/console-api/memory':
                memory = __import__('memory_manager')
                action = body.get('action')
                if action == 'list':
                    result = memory.list_memories(body.get('directory'), body.get('page', 1), body.get('limit', 50),
                                                  tag=body.get('tag'))
                elif action == 'search':
                    result = memory.search(body.get('query'), body.get('directory'), body.get('page', 1),
                                           body.get('limit', 20), tag=body.get('tag'))
                elif action == 'add':
                    result = memory.add(body.get('content'), body.get('directory'), body.get('tags'), body.get('type'))
                elif action == 'update':
                    result = memory.update(body.get('id'), body.get('content'))
                elif action == 'pin':
                    memory_id = body.get('id')
                    if not memory_id:
                        raise ValueError('Memory ID is required')
                    result = memory.pin(memory_id, bool(body.get('pinned', True)))
                elif action == 'delete':
                    result = memory.delete(body.get('id'))
                elif action == 'bulk_delete':
                    result = memory.bulk_delete(body.get('ids') or [])
                elif action == 'maintenance_run':
                    result = memory.maintain(force=True)
                else:
                    raise ValueError('Unknown memory action')
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
                # The console exposes only allowlisted built-in references and
                # bounded Bark client names.
                # Values are never accepted or returned; source metadata points
                # to an owner-only local file or a service environment variable.
                if name not in ('volcengine-control-ak', 'volcengine-control-sk',
                                'aliyun-control-ak', 'aliyun-control-sk') and not \
                        re.fullmatch(r'bark-[a-z0-9][a-z0-9_.-]{0,58}', name or ''):
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
            elif path.startswith('/console-api/task/') and path.endswith('/retier'):
                # Same ordinary session + host/CSRF guards as /steer. The body
                # carries the normalized checkpoint inline; the response is the
                # compact tier_change only (never the handoff, never a file path,
                # never a profile/model choice).
                import retier
                result = retier.send(path.split('/')[-2], body.get('tier'), body.get('reason'),
                                     body.get('handoff'), body.get('request_id'))
            elif path == '/console-api/kimi-account/login':
                # Starts the isolated official CLI if needed, then requests one
                # device login. No token, code or account secret is returned by
                # this bridge; only the sanitized device-flow metadata is.
                result = management.kimi_account_login(body.get('region') or 'mainland-cn')
            elif path == '/console-api/kimi-account/bind':
                # Binding is a human decision: only an explicit confirmed=true
                # writes the account/inference identity pair.
                result = management.kimi_account_bind(body.get('confirmed') is True)
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

    @staticmethod
    def safe_upstream_header(name):
        return name.lower() != 'set-cookie' and not name.lower().startswith('access-control-')

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
                if self.safe_upstream_header(k) and k.lower() not in {'transfer-encoding', 'connection', 'content-length'}:
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
            # Sanitize the HTTP upgrade response before switching to raw frames.
            # A read may contain the first frame, which must survive intact.
            remote.settimeout(STREAM_IO_TIMEOUT)
            pending = bytearray()
            deadline = time.monotonic() + 15
            while b'\r\n\r\n' not in pending:
                if len(pending) > 65536 or time.monotonic() >= deadline:
                    self.reply(502, {'error': 'Invalid OpenCode upgrade response'})
                    return
                try:
                    chunk = remote.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    self.reply(502, {'error': 'Incomplete OpenCode upgrade response'})
                    return
                pending.extend(chunk)
            response_head, first_frame = bytes(pending).split(b'\r\n\r\n', 1)
            if len(response_head) > 65536:
                self.reply(502, {'error': 'OpenCode upgrade response is too large'})
                return
            lines = response_head.decode('iso-8859-1').split('\r\n')
            status = lines[0].split(' ', 2)
            if len(status) < 2 or status[0] not in ('HTTP/1.0', 'HTTP/1.1') or not status[1].isdigit():
                self.reply(502, {'error': 'Invalid OpenCode upgrade status'})
                return
            forwarded = [lines[0]]
            for line in lines[1:]:
                if ':' not in line or line[0].isspace():
                    self.reply(502, {'error': 'Invalid OpenCode upgrade headers'})
                    return
                name, _ = line.split(':', 1)
                if self.safe_upstream_header(name):
                    forwarded.append(line)
            if not self.session_valid(token):
                self.reply(401, {'error': 'Authentication required'})
                return
            self.connection.sendall(('\r\n'.join(forwarded) + '\r\n\r\n').encode('iso-8859-1') + first_frame)
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


def build_servers(settings, root=STATE):
    """Bind all listeners before starting background services. Role is server-owned."""
    parsed = urllib.parse.urlsplit(settings['console_url'])
    if parsed.hostname != '127.0.0.1' or not parsed.port:
        raise ValueError('Console URL must be plain HTTP on a loopback host')
    bind = console_auth.console_bind(settings)
    # Validate configured origins/trusted proxies up front so an unsafe list
    # never starts serving.
    allowed_origins = console_auth.origin_tuples(settings)
    trusted_proxies = console_auth.trusted_proxy_networks(settings)
    passkeys = console_webauthn.enabled(settings)
    rp_settings = console_webauthn.rp_settings(settings)
    specs = [('lan', bind, parsed.port, allowed_origins)]
    if passkeys:
        port = settings.get('console_public_port', 51529)
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535 or port == parsed.port:
            raise ValueError('console_public_port must be a distinct port from 1024 to 65535')
        specs[0] = ('lan', bind, parsed.port,
                    [origin for origin in allowed_origins if origin[1] != rp_settings[0]])
        specs.append(('public', '127.0.0.1', port, [('https', rp_settings[0], 443)]))
    servers = []
    try:
        for role, host, port, origins in specs:
            server = ThreadingHTTPServer((host, port), Handler)
            servers.append(server)
            server.daemon_threads = True
            server.listener_role = role
            server.passkey_settings = rp_settings
            server.enforce_lan_peer = passkeys and role == 'lan'
            server.cookie_name = '__Host-workerdesk_session' if role == 'public' else 'delegate_console_session_' + str(parsed.port)
            server.allowed_origins = origins
            server.trusted_proxy_networks = trusted_proxies if role == 'public' else []
            server.auth_root = root
        return servers
    except Exception:
        for server in servers:
            server.server_close()
        raise


def main():
    settings = config()
    servers = build_servers(settings)
    # Cold start creates the documented fresh default only when no account exists;
    # existing credentials and sessions are preserved. Malformed state fails closed.
    console_auth.ensure_default_account(STATE)
    # The shared host collector is owned by the console process; board consumers
    # read its published file, so their presence or absence never affects it.
    try:
        host_telemetry.ensure_collector()
        start_host_aux()
        host_controls._ensure_supervisor()
    except Exception:
        pass
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True, name='console-public-listener').start()
    try:
        servers[0].serve_forever()
    finally:
        for server in servers[1:]:
            server.shutdown()
        for server in servers:
            server.server_close()


if __name__ == '__main__':
    main()
