"""Loopback Alibaba credential-pool proxy: auth, streaming, retry and state.

These tests use a real loopback proxy and a real loopback fake upstream. Keychain
access is replaced with synthetic bytes; no real credential is read.
"""
import gzip
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import bailian_pool
import common


PASSWORD = 'synthetic-local-pool-password-0123456789'
PAYLOAD = b'{"model":"qwen3.8-max","max_tokens":32,"messages":[{"role":"user","content":"hi"}]}'


class FakeUpstream:
    def __init__(self):
        self.requests = []
        self.routes = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, fmt, *args):
                pass

            def finish(self):
                try:
                    super().finish()
                except Exception:
                    pass

            def do_POST(self):
                try:
                    length = int(self.headers.get('Content-Length') or 0)
                except ValueError:
                    length = 0
                body = self.rfile.read(length) if length else b''
                record = {'path': self.path,
                          'headers': {name.lower(): value for name, value in self.headers.items()},
                          'body': body}
                owner.requests.append(record)
                route = owner.routes.get(record['headers'].get('x-api-key'))
                if route is None:
                    route = _json_route(500, {'error': {'message': 'no fake route'}})
                try:
                    route(self)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return 'http://127.0.0.1:%d/apps/anthropic/v1' % self.port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _json_route(status, value, headers=None):
    def route(handler):
        body = json.dumps(value).encode('utf-8')
        handler.send_response(status)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(body)))
        for name, header_value in (headers or {}).items():
            handler.send_header(name, header_value)
        handler.end_headers()
        handler.wfile.write(body)
    return route


def _raw_route(status, body, headers=None):
    def route(handler):
        handler.send_response(status)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(body)))
        for name, header_value in (headers or {}).items():
            handler.send_header(name, header_value)
        handler.end_headers()
        handler.wfile.write(body)
    return route


def _stream_route(chunks, content_type='text/event-stream', status=200):
    def route(handler):
        handler.send_response(status)
        handler.send_header('Content-Type', content_type)
        handler.send_header('Connection', 'close')
        handler.end_headers()
        for chunk in chunks:
            handler.wfile.write(chunk)
            handler.wfile.flush()
        handler.close_connection = True
    return route


def _close_without_response(handler):
    handler.close_connection = True
    try:
        handler.connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    handler.connection.close()


def _redirect_route(handler):
    handler.send_response(302)
    handler.send_header('Location', 'https://evil.example/steal')
    handler.send_header('Content-Length', '0')
    handler.end_headers()


QUOTA = {'type': 'error', 'error': {'type': 'insufficient_quota',
                                    'message': 'The current quota is exhausted'}}
BALANCE = {'error': {'code': 'Arrearage', 'message': 'account is in arrears'}}
AUTH = {'type': 'error', 'error': {'type': 'authentication_error',
                                   'message': 'invalid api key'}}
TEMPORARY = {'type': 'error', 'error': {'type': 'rate_limit_error',
                                        'message': 'Too many requests'}}


class PoolProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.config_path = self.root / 'config.json'
        self.password_file = self.state / 'server-password'
        self.password_file.write_text(PASSWORD)
        os.chmod(self.password_file, 0o600)
        self.keys = {'seat-1': 'fake-seat-one-key-value',
                     'seat-2': 'fake-seat-two-key-value'}
        self.keychain_calls = []
        self.logs = []
        self.upstream = FakeUpstream()
        self.config = {
            'bailian_pool': {
                'enabled': True,
                'listen_port': 51530,
                'request_timeout_seconds': 30,
                'slots': [
                    {'id': 'seat-1', 'keychain': {
                        'service': 'workerdesk.bailian-token-plan', 'account': 'seat-1'}},
                    {'id': 'seat-2', 'keychain': {
                        'service': 'workerdesk.bailian-token-plan', 'account': 'seat-2'}},
                ],
            }
        }
        common.write_json(self.config_path, self.config)

        def get_key(service, account):
            self.keychain_calls.append((service, account))
            return self.keys[account]

        self.patches = [
            patch.object(common, 'STATE', self.state),
            patch.object(common, 'CONFIG', self.config_path),
            patch.object(bailian_pool.providers, 'BAILIAN_BASE_URL', self.upstream.url),
            patch.object(bailian_pool.keychain_secrets, 'get', side_effect=get_key),
            patch.object(bailian_pool, 'log', side_effect=self.logs.append),
        ]
        for item in self.patches:
            item.start()
        common.init()
        self.server = bailian_pool.create_server(self.config, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.upstream.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def request(self, path='/messages', body=PAYLOAD, key=PASSWORD, headers=None):
        request_headers = {'content-type': 'application/json'}
        if key is not None:
            request_headers['x-api-key'] = key
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        connection.request('POST', path, body=body, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        result = (response.status,
                  {name.lower(): value for name, value in response.getheaders()}, data)
        connection.close()
        return result

    def get(self, key=None):
        headers = {}
        if key is not None:
            headers['x-api-key'] = key
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        connection.request('GET', '/health', headers=headers)
        response = connection.getresponse()
        data = response.read()
        result = (response.status,
                  {name.lower(): value for name, value in response.getheaders()}, data)
        connection.close()
        return result

    def stored(self):
        return common.read_json(self.state / 'bailian-pool.json', {})

    def slot(self, slot_id):
        return self.stored().get('slots', {}).get(slot_id, {})

    def test_local_authentication_rejects_missing_and_wrong_values(self):
        for key in (None, 'wrong-password'):
            with self.subTest(key=key):
                status, headers, body = self.request(key=key)
                self.assertEqual(status, 401)
                self.assertEqual(json.loads(body)['error']['type'], 'authentication_error')
        self.assertEqual(self.upstream.requests, [])

    def test_local_authentication_accepts_server_password_and_bearer_form(self):
        self.upstream.routes[self.keys['seat-1']] = _json_route(200, {'ok': True})
        status, _, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'ok': True})
        status, _, body = self.request(key=None, headers={
            'authorization': 'Bearer ' + PASSWORD})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'ok': True})

    def test_streaming_success_relays_exact_bytes_and_strips_local_auth(self):
        chunks = [b'event: message_start\ndata: {"type":"message_start"}\n\n',
                  b'event: content_block_delta\ndata: {"delta":{"text":"hello"}}\n\n',
                  b'event: message_stop\ndata: {"type":"message_stop"}\n\n']
        self.upstream.routes[self.keys['seat-1']] = _stream_route(chunks)
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('content-type'), 'text/event-stream')
        self.assertNotIn('transfer-encoding', headers)
        self.assertEqual(body, b''.join(chunks))
        self.assertEqual(len(self.upstream.requests), 1)
        sent = self.upstream.requests[0]
        self.assertEqual(sent['path'], '/apps/anthropic/v1/messages')
        self.assertEqual(sent['body'], PAYLOAD)
        self.assertEqual(sent['headers'].get('content-length'), str(len(PAYLOAD)))
        self.assertEqual(sent['headers'].get('x-api-key'), 'fake-seat-one-key-value')
        self.assertNotIn('authorization', sent['headers'])
        self.assertNotIn(PASSWORD, json.dumps(sent['headers']))
        state = self.stored()
        self.assertEqual(state.get('active'), 'seat-1')
        self.assertEqual(self.slot('seat-1').get('state'), 'ok')

    def test_confirmed_quota_rejection_retries_second_with_same_bytes(self):
        self.upstream.routes[self.keys['seat-1']] = _json_route(429, QUOTA)
        self.upstream.routes[self.keys['seat-2']] = _json_route(200, {'seat': 'second'})
        status, _, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'seat': 'second'})
        self.assertEqual(len(self.upstream.requests), 2)
        self.assertEqual(self.upstream.requests[0]['body'], PAYLOAD)
        self.assertEqual(self.upstream.requests[1]['body'], PAYLOAD)
        self.assertEqual(self.upstream.requests[0]['headers']['x-api-key'],
                         'fake-seat-one-key-value')
        self.assertEqual(self.upstream.requests[1]['headers']['x-api-key'],
                         'fake-seat-two-key-value')
        self.assertEqual(self.slot('seat-1').get('state'), 'exhausted')
        self.assertEqual(self.slot('seat-1').get('kind'), 'quota')
        self.assertEqual(self.slot('seat-2').get('state'), 'ok')
        self.assertEqual(self.stored().get('active'), 'seat-2')
        # The next request starts at the first eligible slot, so seat-1 is not
        # sent upstream again while the same credential generation is blocked.
        status, _, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(len(self.upstream.requests), 3)
        self.assertEqual(self.upstream.requests[2]['headers']['x-api-key'],
                         'fake-seat-two-key-value')

    def test_balance_rejection_is_classified_and_retried(self):
        self.upstream.routes[self.keys['seat-1']] = _json_route(403, BALANCE)
        self.upstream.routes[self.keys['seat-2']] = _json_route(200, {'seat': 'second'})
        status, _, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(self.slot('seat-1').get('state'), 'exhausted')
        self.assertEqual(self.slot('seat-1').get('kind'), 'balance')

    def test_auth_rejection_with_400_is_classified_and_retried(self):
        self.upstream.routes[self.keys['seat-1']] = _json_route(400, AUTH)
        self.upstream.routes[self.keys['seat-2']] = _json_route(200, {'seat': 'second'})
        status, _, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'seat': 'second'})
        self.assertEqual(self.slot('seat-1').get('state'), 'auth')
        self.assertEqual(self.slot('seat-1').get('kind'), 'auth')

    def test_auth_rejection_is_classified_and_retried(self):
        self.upstream.routes[self.keys['seat-1']] = _json_route(401, AUTH)
        self.upstream.routes[self.keys['seat-2']] = _json_route(200, {'seat': 'second'})
        status, _, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(self.slot('seat-1').get('state'), 'auth')
        self.assertEqual(self.slot('seat-1').get('kind'), 'auth')

    def test_temporary_429_is_relayed_without_retry_or_exhaustion(self):
        self.upstream.routes[self.keys['seat-1']] = _json_route(429, TEMPORARY)
        self.upstream.routes[self.keys['seat-2']] = _json_route(200, {'seat': 'second'})
        status, _, body = self.request()
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body), TEMPORARY)
        self.assertEqual(len(self.upstream.requests), 1)
        self.assertEqual(self.slot('seat-1').get('state'), 'ok')
        self.assertEqual(self.slot('seat-1').get('last_error'), 'temporary_429')
        status, _, _ = self.request()
        self.assertEqual(status, 429)
        self.assertEqual(len(self.upstream.requests), 2)
        self.assertEqual(self.upstream.requests[1]['headers']['x-api-key'],
                         'fake-seat-one-key-value')

    def test_transport_failure_is_not_replayed_on_second_slot(self):
        self.upstream.routes[self.keys['seat-1']] = _close_without_response
        self.upstream.routes[self.keys['seat-2']] = _json_route(200, {'seat': 'second'})
        status, _, body = self.request()
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)['error']['type'], 'api_error')
        self.assertEqual(len(self.upstream.requests), 1)
        self.assertNotIn('fake-seat-two-key-value',
                         [item['headers'].get('x-api-key') for item in self.upstream.requests])
        self.assertEqual(self.slot('seat-1').get('state'), 'ok')
        self.assertEqual(self.slot('seat-1').get('last_error'), 'transport')

    def test_5xx_is_relayed_without_retry(self):
        self.upstream.routes[self.keys['seat-1']] = _json_route(
            500, {'error': {'message': 'upstream failed'}})
        self.upstream.routes[self.keys['seat-2']] = _json_route(200, {'seat': 'second'})
        status, _, body = self.request()
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)['error']['message'], 'upstream failed')
        self.assertEqual(len(self.upstream.requests), 1)
        self.assertEqual(self.slot('seat-1').get('state'), 'ok')
        self.assertEqual(self.slot('seat-1').get('last_error'), 'upstream_error')

    def test_streamed_quota_text_is_not_replayed(self):
        chunks = [b'event: error\ndata: {"type":"error","error":{"message":"insufficient balance"}}\n\n']
        self.upstream.routes[self.keys['seat-1']] = _stream_route(chunks)
        self.upstream.routes[self.keys['seat-2']] = _json_route(200, {'seat': 'second'})
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('content-type'), 'text/event-stream')
        self.assertEqual(body, b''.join(chunks))
        self.assertEqual(len(self.upstream.requests), 1)
        self.assertEqual(self.slot('seat-1').get('state'), 'ok')

    def test_all_slots_exhausted_returns_provider_failure(self):
        self.upstream.routes[self.keys['seat-1']] = _json_route(429, QUOTA)
        self.upstream.routes[self.keys['seat-2']] = _json_route(429, QUOTA)
        status, _, body = self.request()
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body), QUOTA)
        self.assertEqual(len(self.upstream.requests), 2)
        self.assertEqual(self.slot('seat-1').get('state'), 'exhausted')
        self.assertEqual(self.slot('seat-2').get('state'), 'exhausted')
        status, _, body = self.request()
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)['error']['type'], 'rate_limit_error')
        self.assertIn('exhausted', json.loads(body)['error']['message'])
        self.assertEqual(len(self.upstream.requests), 2)

    def test_rotated_credential_generation_resets_exhaustion(self):
        self.upstream.routes[self.keys['seat-1']] = _json_route(429, QUOTA)
        self.upstream.routes[self.keys['seat-2']] = _json_route(429, QUOTA)
        self.request()
        self.assertEqual(self.slot('seat-1').get('state'), 'exhausted')
        old_generation = self.slot('seat-1').get('generation')
        self.keys['seat-1'] = 'fake-seat-one-rotated-key-value'
        self.upstream.routes[self.keys['seat-1']] = _json_route(200, {'seat': 'rotated'})
        status, _, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'seat': 'rotated'})
        self.assertEqual(self.slot('seat-1').get('state'), 'ok')
        self.assertNotEqual(self.slot('seat-1').get('generation'), old_generation)
        self.assertEqual(self.stored().get('active'), 'seat-1')

    def test_state_and_logs_never_contain_plaintext_keys(self):
        self.upstream.routes[self.keys['seat-1']] = _stream_route([b'event: done\ndata: {}\n\n'])
        status, _, _ = self.request()
        self.assertEqual(status, 200)
        state_text = (self.state / 'bailian-pool.json').read_text()
        log_text = '\n'.join(str(item) for item in self.logs)
        for value in (self.keys['seat-1'], self.keys['seat-2'], PASSWORD):
            self.assertNotIn(value, state_text)
            self.assertNotIn(value, log_text)
        snapshot = json.dumps(bailian_pool.status_snapshot(self.config))
        for secret in ('fake-seat-one-key-value', 'fake-seat-two-key-value', PASSWORD):
            self.assertNotIn(secret, snapshot)
        self.assertEqual((self.state / 'bailian-pool.json').stat().st_mode & 0o077, 0)

    def test_error_body_and_headers_are_redacted(self):
        key = self.keys['seat-1']
        body = {'error': {'type': 'invalid_request_error', 'code': 'BadRequest',
                          'message': 'request echoed ' + key, 'echo': key}}
        headers = {'x-api-key': key, 'authorization': 'Bearer ' + key,
                   'proxy-authorization': key, 'x-error-echo': 'key=' + key}
        self.upstream.routes[key] = _json_route(400, body, headers)
        status, response_headers, response_body = self.request()
        self.assertEqual(status, 400)
        text = response_body.decode('utf-8')
        self.assertNotIn(key, text)
        self.assertIn('<redacted>', text)
        self.assertEqual(json.loads(text)['error']['type'], 'invalid_request_error')
        self.assertEqual(response_headers.get('content-length'), str(len(response_body)))
        for name in ('x-api-key', 'authorization', 'proxy-authorization'):
            self.assertNotIn(name, response_headers)
        self.assertNotIn(key, json.dumps(response_headers))
        self.assertNotIn(key, json.dumps(self.stored()))
        self.assertNotIn(key, '\n'.join(str(item) for item in self.logs))

    def test_all_auth_rejection_echoes_are_redacted_in_last_response(self):
        first_key = self.keys['seat-1']
        second_key = self.keys['seat-2']
        for key in (first_key, second_key):
            self.upstream.routes[key] = _json_route(401, {
                'type': 'error',
                'error': {'type': 'authentication_error',
                          'message': 'invalid api key ' + key, 'echo': key},
            }, {'x-api-key': key, 'authorization': 'Bearer ' + key,
                'proxy-authorization': key, 'x-error-echo': key})
        status, response_headers, response_body = self.request()
        self.assertEqual(status, 401)
        text = response_body.decode('utf-8')
        self.assertEqual(response_headers.get('content-length'), str(len(response_body)))
        for key in (first_key, second_key):
            self.assertNotIn(key, text)
            self.assertNotIn(key, json.dumps(response_headers))
            self.assertNotIn(key, json.dumps(self.stored()))
            self.assertNotIn(key, '\n'.join(str(item) for item in self.logs))
        for name in ('x-api-key', 'authorization', 'proxy-authorization'):
            self.assertNotIn(name, response_headers)
        self.assertEqual(self.slot('seat-1').get('state'), 'auth')
        self.assertEqual(self.slot('seat-2').get('state'), 'auth')

    def test_compressed_error_body_is_decoded_redacted_and_length_fixed(self):
        key = self.keys['seat-1']
        body = json.dumps({'error': {'type': 'invalid_request_error',
                                     'message': 'invalid key ' + key,
                                     'echo': key}}).encode('utf-8')
        compressed = gzip.compress(body)
        self.upstream.routes[key] = _raw_route(400, compressed, {
            'Content-Encoding': 'gzip', 'x-api-key': key})
        status, response_headers, response_body = self.request()
        self.assertEqual(status, 400)
        self.assertNotIn(key, response_body.decode('utf-8'))
        self.assertIn('<redacted>', response_body.decode('utf-8'))
        self.assertEqual(json.loads(response_body.decode('utf-8'))['error']['echo'],
                         '<redacted>')
        self.assertNotIn('content-encoding', response_headers)
        self.assertNotIn('x-api-key', response_headers)
        self.assertEqual(response_headers.get('content-length'), str(len(response_body)))

    def test_compressed_quota_rejection_still_retries_second(self):
        key = self.keys['seat-1']
        compressed = gzip.compress(json.dumps(QUOTA).encode('utf-8'))
        self.upstream.routes[key] = _raw_route(429, compressed, {
            'Content-Encoding': 'gzip'})
        self.upstream.routes[self.keys['seat-2']] = _json_route(200, {'seat': 'second'})
        status, _, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'seat': 'second'})
        self.assertEqual(self.slot('seat-1').get('state'), 'exhausted')
        self.assertEqual(self.slot('seat-1').get('kind'), 'quota')

    def test_successful_stream_content_is_not_redacted(self):
        key = self.keys['seat-1']
        chunk = b'event: delta\ndata: {"text":"' + key.encode('utf-8') + b'"}\n\n'
        self.upstream.routes[key] = _stream_route([chunk])
        status, _, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(body, chunk)
        self.assertIn(key, body.decode('utf-8'))
        self.assertNotIn(key, json.dumps(self.stored()))

    def test_health_requires_auth_and_exposes_only_safe_metadata(self):
        status, _, body = self.get()
        self.assertEqual(status, 401)
        status, _, body = self.get(key=PASSWORD)
        self.assertEqual(status, 200)
        value = json.loads(body)
        self.assertTrue(value['enabled'])
        self.assertEqual([slot['id'] for slot in value['slots']], ['seat-1', 'seat-2'])
        self.assertEqual(value['available'], 2)
        for secret in ('fake-seat-one-key-value', 'fake-seat-two-key-value', PASSWORD):
            self.assertNotIn(secret, json.dumps(value))

    def test_unknown_path_and_absolute_url_are_rejected(self):
        status, _, _ = self.request(path='/v1/messages')
        self.assertEqual(status, 404)
        status, _, _ = self.request(path='http://evil.example/messages')
        self.assertEqual(status, 400)
        self.assertEqual(self.upstream.requests, [])

    def test_upstream_redirect_is_refused_without_location(self):
        self.upstream.routes[self.keys['seat-1']] = _redirect_route
        status, headers, body = self.request()
        self.assertEqual(status, 502)
        self.assertNotIn('location', headers)
        self.assertEqual(json.loads(body)['error']['type'], 'api_error')

    def test_upstream_receives_full_json_with_explicit_content_length(self):
        payload = json.dumps({
            'model': 'qwen3.8-max',
            'messages': [{'role': 'user', 'content': '你好 ' * 200}],
        }, ensure_ascii=False).encode('utf-8')
        self.upstream.routes[self.keys['seat-1']] = _json_route(200, {'ok': True})
        status, _, body = self.request(body=payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'ok': True})
        sent = self.upstream.requests[0]
        self.assertEqual(sent['headers'].get('content-length'), str(len(payload)))
        self.assertEqual(sent['headers'].get('transfer-encoding'), None)
        self.assertEqual(sent['body'], payload)
        self.assertEqual(json.loads(sent['body'].decode('utf-8'))['model'], 'qwen3.8-max')

    def test_streaming_flushes_first_chunk_before_upstream_completes(self):
        first = b'event: first\ndata: {"type":"message_start"}\n\n'
        second = b'event: second\ndata: {"type":"message_stop"}\n\n'
        release = threading.Event()

        def route(handler):
            handler.send_response(200)
            handler.send_header('Content-Type', 'text/event-stream')
            handler.send_header('Connection', 'close')
            handler.end_headers()
            handler.wfile.write(first)
            handler.wfile.flush()
            release.wait(3)
            handler.wfile.write(second)
            handler.wfile.flush()
            handler.close_connection = True

        self.upstream.routes[self.keys['seat-1']] = route
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        try:
            connection.request('POST', '/messages', body=PAYLOAD, headers={
                'content-type': 'application/json', 'x-api-key': PASSWORD})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            # If the proxy buffers the upstream stream, this read blocks until
            # release, which is set only after it returns.
            self.assertEqual(response.read(len(first)), first)
            release.set()
            self.assertEqual(response.read(), second)
        finally:
            release.set()
            connection.close()

    def test_query_secret_is_never_logged(self):
        secret = 'query-secret-value-that-must-not-appear'
        self.upstream.routes[self.keys['seat-1']] = _json_route(200, {'ok': True})
        status, _, _ = self.request(path='/messages?token=' + secret)
        self.assertEqual(status, 200)
        self.assertNotIn(secret, '\n'.join(str(item) for item in self.logs))

    def test_auth_error_closes_before_unread_body_can_be_parsed(self):
        secret = 'body-secret-value-that-must-not-be-parsed'
        body = json.dumps({'private': secret, 'messages': []}).encode('utf-8')
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            connection.request('POST', '/messages', body=body, headers={
                'content-type': 'application/json', 'x-api-key': 'wrong-password'})
            response = connection.getresponse()
            payload = response.read()
            self.assertEqual(response.status, 401)
            self.assertEqual(response.getheader('Connection'), 'close')
            self.assertEqual(json.loads(payload)['error']['type'], 'authentication_error')
        finally:
            connection.close()
        self.assertNotIn(secret, '\n'.join(str(item) for item in self.logs))

    def test_late_success_cannot_reopen_same_generation_block(self):
        slot = bailian_pool.pool_settings(self.config)['slots'][0]
        _, generation = bailian_pool._load_key(slot)
        self.assertTrue(bailian_pool._claim(slot, generation))
        bailian_pool._mark(slot, generation, 'exhausted', kind='quota', http_status=429)
        # A success from an older in-flight request must not clear the block.
        bailian_pool._mark(slot, generation, 'ok')
        record = self.slot('seat-1')
        self.assertEqual(record.get('state'), 'exhausted')
        self.assertEqual(record.get('kind'), 'quota')
        self.assertIn('last_success_at', record)
        # A changed generation is the only automatic reset path.
        self.keys['seat-1'] = 'fake-seat-one-new-generation'
        _, rotated = bailian_pool._load_key(slot)
        self.assertNotEqual(rotated, generation)
        self.assertTrue(bailian_pool._claim(slot, rotated))
        self.assertEqual(self.slot('seat-1').get('state'), 'ok')

    def test_concurrent_late_success_does_not_reopen_exhausted_slot(self):
        barrier = threading.Barrier(2)
        counter = {'value': 0}
        lock = threading.Lock()

        def route(handler):
            with lock:
                counter['value'] += 1
                index = counter['value']
            barrier.wait(timeout=3)
            if index == 1:
                _json_route(429, QUOTA)(handler)
            else:
                time.sleep(0.3)
                _json_route(200, {'seat': 'late-success'})(handler)

        self.upstream.routes[self.keys['seat-1']] = route
        self.upstream.routes[self.keys['seat-2']] = _json_route(200, {'seat': 'second'})
        results = {}

        def call(name):
            try:
                results[name] = self.request()
            except Exception as exc:  # surfaced in the assertion below
                results[name] = exc

        first = threading.Thread(target=call, args=('first',))
        second = threading.Thread(target=call, args=('second',))
        first.start()
        second.start()
        first.join(timeout=10)
        second.join(timeout=10)
        for name in ('first', 'second'):
            self.assertNotIsInstance(results.get(name), Exception, results.get(name))
            self.assertEqual(results[name][0], 200)
        self.assertEqual(self.slot('seat-1').get('state'), 'exhausted')
        self.assertEqual(self.slot('seat-1').get('kind'), 'quota')

    def test_count_tokens_and_query_are_forwarded_to_fixed_path(self):
        self.upstream.routes[self.keys['seat-1']] = _json_route(200, {'input_tokens': 7})
        status, _, body = self.request(path='/messages/count_tokens')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'input_tokens': 7})
        self.assertEqual(self.upstream.requests[0]['path'],
                         '/apps/anthropic/v1/messages/count_tokens')
        self.upstream.routes[self.keys['seat-1']] = _json_route(200, {'ok': True})
        status, _, _ = self.request(path='/messages?beta=true')
        self.assertEqual(status, 200)
        self.assertEqual(self.upstream.requests[1]['path'],
                         '/apps/anthropic/v1/messages?beta=true')


if __name__ == '__main__':
    unittest.main()
