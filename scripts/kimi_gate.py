#!/usr/bin/env python3
"""Authenticated loopback admission gate for native Kimi Coding requests.

Inference bytes and streaming chunks are relayed unchanged to the single fixed
official endpoint. A new request is admitted before any upstream work starts;
an admitted stream is never rechecked, interrupted or replayed by this gate.
"""
import hmac
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import socket
import time
import urllib.parse

import common

UPSTREAM_HOST = 'api.kimi.com'
UPSTREAM_PREFIX = '/coding/v1'
MODELS = frozenset(('kimi-for-coding', 'k3.1-pro'))
DEFAULT_PORT = 51531
MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
MAX_MODELS_BYTES = 1024 * 1024
REQUEST_TIMEOUT = 900
CLIENT_TIMEOUT = 30
# A native Ultra stream can outlive the 30-minute demand cooldown. While chunks
# are flowing, refresh the bounded activity stamp at most once a minute; the
# 900-second socket inactivity timeout bounds any silent gap, so the cooldown is
# never allowed to expire in the middle of a live native Ultra stream.
NATIVE_HEARTBEAT_SECONDS = 60
NATIVE_ULTRA_MODEL = 'k3.1-pro'


def settings(c):
    block = c.get('kimi_gate') if isinstance(c, dict) else None
    if block is None:
        return None
    if not isinstance(block, dict):
        raise ValueError('kimi_gate must be an object')
    enabled = block.get('enabled', False)
    if not isinstance(enabled, bool):
        raise ValueError('kimi_gate.enabled must be boolean')
    if not enabled:
        return None
    if set(block) - {'enabled', 'port'}:
        raise ValueError('kimi_gate has unsupported settings')
    port = block.get('port', DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('kimi_gate.port must be an integer from 1 to 65535')
    return {'port': port}


def _password():
    path = common.STATE / 'server-password'
    stat = path.stat()
    if path.is_symlink() or not path.is_file() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError('Local gate credential unavailable')
    value = path.read_text().strip()
    if not value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError('Local gate credential unavailable')
    return value


def _snapshot():
    """Use identity-aware shared caching; never fabricate refresh timestamps."""
    import quota
    # refresh owns the 60-second cache and checks credential generations even
    # on cache hits. A gate-local age shortcut could use another account's
    # otherwise recent record after credentials rotate. An unexpected refresh
    # failure must propagate to the preflight's 503 handler: an old display view
    # deliberately strips credential identities and cannot prove applicability.
    return quota.refresh()


def preflight(model, c):
    import quota
    return quota.kimi_request_allowed(model, c, _snapshot())


def _track_native_activity(model, identity):
    """Bounded native Ultra evidence; False when it could not be verified.

    Only the reserved native Ultra model participates; ordinary Kimi traffic is
    unaffected. A failed write is remembered in the process-local uncertainty
    guard so later Gate admissions cannot mistake failed bookkeeping for quiet
    demand. This helper never raises.
    """
    if model != NATIVE_ULTRA_MODEL:
        return True
    try:
        import kimi_reserve
        if kimi_reserve.record_native_activity(identity):
            kimi_reserve.note_local_native_activity()
            return True
    except Exception:
        pass
    try:
        import kimi_reserve
        kimi_reserve.note_local_uncertainty()
    except Exception:
        pass
    return False


def _native_identity(key):
    """The exact demand identity of this admission from its captured key.

    ``common.credential_identity`` hashes the same Kimi inference key with
    SHA-256 and no prefix/NUL since Kimi is not a key-file provider. Deriving it
    from the already-pinned admission key avoids re-reading the credential store
    after the admission compare, so a rotation can never reassign this request to
    another account.
    """
    return hashlib.sha256(key.encode()).hexdigest()


class RequestError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


class GateHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'KimiGate/1'

    def setup(self):
        super().setup()
        self.connection.settimeout(CLIENT_TIMEOUT)

    def log_message(self, *args):
        pass  # Never log headers, request targets, credentials or prompt bodies.

    def do_GET(self):
        self._dispatch('GET')

    def do_POST(self):
        self._dispatch('POST')

    def do_HEAD(self):
        self._dispatch('HEAD')

    def _dispatch(self, method):
        self._response_started = False
        self.close_connection = True
        try:
            self._authorize()
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
                raise RequestError(400, 'invalid_request_error', 'Only fixed gate paths are accepted')
            path = parsed.path
            if path == '/health':
                if method != 'GET':
                    raise RequestError(405, 'invalid_request_error', 'Method not allowed')
                if settings(common.config()) is None:
                    raise RequestError(503, 'gate_unavailable', 'Kimi gate is disabled')
                self._json(200, {'healthy': True, 'service': 'kimi_gate'})
                return
            if (method, path) not in {('GET', '/v1/models'), ('POST', '/v1/chat/completions')}:
                raise RequestError(404, 'not_found_error', 'Unknown gate path')
            c = common.config()
            if settings(c) is None:
                raise RequestError(503, 'gate_unavailable', 'Kimi gate is disabled')
            body = None
            admitted_model = None
            admitted_identity = None
            # Pin one credential to this admission. A rotation during quota
            # sampling must not send a new key using the old key's allowance.
            key = self._inference_key()
            if method == 'POST':
                body = self._body()
                try:
                    value = json.loads(body)
                except (ValueError, UnicodeError):
                    raise RequestError(400, 'invalid_request_error', 'Invalid JSON request') from None
                if (not isinstance(value, dict) or not isinstance(value.get('model'), str)
                        or value['model'] not in MODELS):
                    raise RequestError(400, 'unsupported_model', 'Unsupported Kimi model')
                try:
                    allowed, reason = preflight(value['model'], c)
                except Exception:
                    raise RequestError(503, 'quota_preflight_unavailable', 'Kimi quota preflight unavailable') from None
                if not allowed:
                    # The stable reason is diagnostic metadata, never arbitrary
                    # upstream text; all policy rejections are genuine admission
                    # limits, so the worker can safely select another route.
                    safe = reason if isinstance(reason, str) and re.fullmatch(r'[a-z0-9_]{1,100}', reason) else 'quota_unavailable'
                    raise RequestError(429, safe, 'Kimi quota admission limit: ' + safe)
                if not hmac.compare_digest(key.encode(), self._inference_key().encode()):
                    raise RequestError(503, 'inference_credential_changed', 'Kimi inference credential changed during admission')
                admitted_model = value['model']
                if admitted_model == NATIVE_ULTRA_MODEL:
                    # A native Ultra request must carry verified demand evidence
                    # before any upstream work: otherwise a fresh quiet snapshot
                    # could release ordinary Kimi admission while raw Ultra is
                    # actually running. Fail closed here instead.
                    admitted_identity = _native_identity(key)
                    if not _track_native_activity(admitted_model, admitted_identity):
                        raise RequestError(503, 'kimi_demand_unavailable',
                                           'Kimi native Ultra demand tracking unavailable')
            try:
                self._proxy(method, path.removeprefix('/v1'), body, key,
                            heartbeat_identity=admitted_identity)
            finally:
                if admitted_model == NATIVE_ULTRA_MODEL:
                    # The stream already started: preserve delivery, but a failed
                    # end/heartbeat write leaves the local uncertainty guard set.
                    _track_native_activity(admitted_model, admitted_identity)
        except RequestError as error:
            if not self._response_started:
                self._error(error.status, error.code, error.message)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            if not self._response_started:
                self._error(504, 'gate_timeout', 'Kimi request timed out')
        except Exception:
            if not self._response_started:
                self._error(503, 'gate_unavailable', 'Kimi gate unavailable')

    def _authorize(self):
        fields = self.headers.get_all('Authorization') or []
        value = fields[0] if len(fields) == 1 else ''
        if not value.startswith('Bearer '):
            raise RequestError(401, 'authentication_error', 'Invalid local gate credential')
        try:
            valid = hmac.compare_digest(value[7:].encode(), _password().encode())
        except (OSError, ValueError, UnicodeError):
            valid = False
        if not valid:
            raise RequestError(401, 'authentication_error', 'Invalid local gate credential')

    def _body(self):
        if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Encoding'):
            raise RequestError(400, 'invalid_request_error', 'Encoded request bodies are not accepted')
        lengths = self.headers.get_all('Content-Length') or []
        if len(lengths) != 1 or not re.fullmatch(r'[0-9]+', lengths[0]):
            raise RequestError(411, 'invalid_request_error', 'One Content-Length is required')
        length = int(lengths[0])
        if not 0 < length <= MAX_REQUEST_BYTES:
            raise RequestError(413, 'invalid_request_error', 'Invalid request body size')
        value = self.rfile.read(length)
        if len(value) != length:
            raise RequestError(400, 'invalid_request_error', 'Incomplete request body')
        return value

    def _inference_key(self):
        key = common.auth_key('kimi-for-coding')
        if not isinstance(key, str) or not key or any(ord(c) < 32 or ord(c) == 127 for c in key):
            raise RequestError(503, 'inference_credential_unavailable', 'Kimi inference credential unavailable')
        return key

    def _proxy(self, method, suffix, body, key, heartbeat_identity=None):
        headers = {'Authorization': 'Bearer ' + key, 'Accept-Encoding': 'identity',
                   'Accept': 'application/json, text/event-stream', 'User-Agent': 'opencode/1.19.4'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        connection = http.client.HTTPSConnection(UPSTREAM_HOST, timeout=REQUEST_TIMEOUT)
        response = None
        try:
            # No redirects, endpoint supplied by caller, or retry after an
            # uncertain transport result. Exactly one upstream request.
            connection.request(method, UPSTREAM_PREFIX + suffix, body=body, headers=headers)
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                self._upstream_error(response, key)
                return
            if suffix == '/models':
                raw = response.read(MAX_MODELS_BYTES + 1)
                if len(raw) > MAX_MODELS_BYTES:
                    raise RequestError(502, 'invalid_upstream_response', 'Kimi model response too large')
                value = json.loads(raw)
                if not isinstance(value, dict) or not isinstance(value.get('data'), list):
                    raise RequestError(502, 'invalid_upstream_response', 'Invalid Kimi model response')
                value['data'] = [item for item in value['data'] if isinstance(item, dict) and item.get('id') in MODELS]
                self._json(response.status, value)
                return
            self.send_response(response.status)
            content_type = response.getheader('Content-Type') or 'application/json'
            if '\r' in content_type or '\n' in content_type:
                content_type = 'application/json'
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self._response_started = True
            last_heartbeat = time.time()
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                if heartbeat_identity is not None and \
                        time.time() - last_heartbeat >= NATIVE_HEARTBEAT_SECONDS:
                    # At most one bounded write per minute while the stream is
                    # actually flowing; the captured admission identity is kept.
                    last_heartbeat = time.time()
                    _track_native_activity(NATIVE_ULTRA_MODEL, heartbeat_identity)
        finally:
            if response is not None:
                response.close()
            connection.close()

    def _upstream_error(self, response, key):
        raw = response.read(MAX_ERROR_BYTES + 1)
        try:
            value = json.loads(raw[:MAX_ERROR_BYTES])
            error = value.get('error', {}) if isinstance(value, dict) else {}
            message = error.get('message') if isinstance(error, dict) else None
            code = error.get('code') if isinstance(error, dict) else None
            kind = error.get('type') if isinstance(error, dict) else None
            if not isinstance(message, str):
                message = 'Kimi upstream returned HTTP ' + str(response.status)
            representations = []
            for secret in (key, _password()):
                for representation in common.secret_representations(secret):
                    representations.append(representation)
                    message = message.replace(representation, '<redacted>')
            message = common.redact(message)[:4096]
            safe_code = code if isinstance(code, str) and re.fullmatch(r'[a-zA-Z0-9_]{1,80}', code) else 'upstream_error'
            safe_kind = kind if isinstance(kind, str) and re.fullmatch(r'[a-zA-Z0-9_]{1,80}', kind) else 'api_error'
            # A text code could itself be an echoed key. Apply the same known
            # secret replacement before emitting even allowlisted metadata.
            if any(secret in field for secret in representations for field in (safe_code, safe_kind)):
                safe_code, safe_kind = 'upstream_error', 'api_error'
        except Exception:
            message, safe_code, safe_kind = 'Kimi upstream returned HTTP ' + str(response.status), 'upstream_error', 'api_error'
        self._error(response.status, safe_code, message, safe_kind)

    def _json(self, status, value):
        raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self._response_started = True
        if self.command != 'HEAD':
            self.wfile.write(raw)
            self.wfile.flush()

    def _error(self, status, code, message, kind=None):
        if kind is None:
            kind = ('authentication_error' if status == 401 else 'rate_limit_error' if status == 429
                    else 'invalid_request_error' if status in (400, 404, 405, 411, 413) else 'api_error')
        self._json(status, {'error': {'code': code, 'type': kind, 'message': message}})


def create_server(c, port=None, host='127.0.0.1'):
    value = settings(c)
    if value is None:
        raise ValueError('kimi_gate is disabled')
    if host != '127.0.0.1':
        raise ValueError('Kimi gate must bind loopback')
    bind_port = value['port'] if port is None else port
    if isinstance(bind_port, bool) or not isinstance(bind_port, int) or not 0 <= bind_port <= 65535:
        raise ValueError('Invalid Kimi gate port')
    server = ThreadingHTTPServer((host, bind_port), GateHandler)
    server.daemon_threads = True
    return server


def wait_ready(port, password=None, timeout=10):
    try:
        _origin_port = settings({'kimi_gate': {'enabled': True, 'port': port}})['port']
        password = _password() if password is None else password
    except (ValueError, OSError):
        return False
    deadline = time.monotonic() + max(0, timeout)
    while time.monotonic() < deadline:
        connection = http.client.HTTPConnection('127.0.0.1', _origin_port, timeout=1)
        try:
            connection.request('GET', '/health', headers={'Authorization': 'Bearer ' + password})
            response = connection.getresponse()
            raw = response.read(8192)
            if response.status == 200 and json.loads(raw).get('service') == 'kimi_gate':
                return True
        except Exception:
            pass
        finally:
            connection.close()
        time.sleep(.1)
    return False


def main():
    server = create_server(common.config())
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
