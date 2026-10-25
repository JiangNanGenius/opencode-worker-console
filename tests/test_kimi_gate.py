"""Real loopback gate contract; fixed upstream is a deterministic fake."""
import io
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import kimi_gate
import quota


class Response:
    def __init__(self, status=200, body=b'{"ok":true}', headers=None):
        self.status = status
        self.body = io.BytesIO(body)
        self.headers = headers or {'Content-Type': 'application/json'}

    def read(self, n=-1):
        return self.body.read(n)

    def read1(self, n=-1):
        return self.body.read(n)

    def getheader(self, name):
        return next((v for k, v in self.headers.items() if k.lower() == name.lower()), None)

    def close(self):
        pass


class SettingsTests(unittest.TestCase):
    def test_disabled_and_enabled(self):
        self.assertIsNone(kimi_gate.settings({}))
        self.assertIsNone(kimi_gate.settings({'kimi_gate': {'enabled': False}}))
        self.assertEqual(kimi_gate.settings({'kimi_gate': {'enabled': True}}), {'port': 51531})

    def test_invalid_configuration(self):
        for block in ([], True, {'enabled': 'true'}, {'enabled': True, 'port': True},
                      {'enabled': True, 'port': 0}, {'enabled': True, 'port': 65536},
                      {'enabled': True, 'upstream': 'https://other.invalid'}):
            with self.subTest(block=block), self.assertRaises(ValueError):
                kimi_gate.settings({'kimi_gate': block})


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.password = 'local-only-test-credential'
        self.key = 'sk-test-only-synthetic-fixture-1-not-a-real-key'
        password = self.root / 'server-password'
        password.write_text(self.password)
        password.chmod(0o600)
        self.config = {'kimi_gate': {'enabled': True, 'port': 51531}}
        self.upstream = Mock()
        self.upstream.getresponse.return_value = Response()
        self.real_preflight = kimi_gate.preflight
        self.patches = [patch.object(common, 'STATE', self.root),
                        patch.object(common, 'config', return_value=self.config),
                        patch.object(common, 'auth_key', return_value=self.key),
                        patch.object(common, 'redact', side_effect=lambda v: v),
                        patch.object(kimi_gate, 'preflight', return_value=(True, 'quota_available')),
                        patch.object(kimi_gate.http.client, 'HTTPSConnection', return_value=self.upstream)]
        self.mocks = [p.start() for p in self.patches]
        self.check = self.mocks[-2]
        self.transport = self.mocks[-1]
        self.server = kimi_gate.create_server(self.config, port=0)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def request(self, path='/v1/chat/completions', method='POST', value=None, raw=None, headers=None):
        body = raw if raw is not None else json.dumps(value or {'model': 'k3.1-pro', 'stream': False}).encode()
        h = {'Authorization': 'Bearer ' + self.password}
        if headers:
            h.update(headers)
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            client.request(method, path, body if method == 'POST' else None, h)
            result = client.getresponse()
            return result.status, dict(result.getheaders()), result.read()
        finally:
            client.close()

    def test_health_is_authenticated(self):
        status, headers, body = self.request('/health', 'GET')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['healthy'])
        self.assertTrue(kimi_gate.wait_ready(self.port, self.password, timeout=1))
        self.assertFalse(kimi_gate.wait_ready(self.port, 'wrong', timeout=.12))
        self.transport.assert_not_called()

    def test_invalid_auth_never_reaches_preflight_or_upstream(self):
        for auth in ('Bearer wrong', 'Basic ' + self.password, ''):
            with self.subTest(auth=auth):
                status, _headers, body = self.request(headers={'Authorization': auth})
                self.assertEqual(status, 401)
                self.assertEqual(json.loads(body)['error']['type'], 'authentication_error')
                self.assertNotIn(self.password.encode(), body)
        self.check.assert_not_called()
        self.transport.assert_not_called()

    def test_private_password_file_required(self):
        (self.root / 'server-password').chmod(0o644)
        self.assertEqual(self.request()[0], 401)
        self.transport.assert_not_called()

    def test_paths_queries_absolute_urls_and_methods_rejected(self):
        for path in ('http://other.invalid/v1/chat/completions', '/v1/chat/completions?token=x',
                     '/v1/../chat/completions', '/v1/messages', '//other.invalid/v1/models',
                     '/v1/chat/completions#fragment'):
            with self.subTest(path=path):
                self.assertIn(self.request(path)[0], (400, 404))
        self.assertEqual(self.request('/v1/chat/completions', 'GET')[0], 404)
        self.check.assert_not_called()
        self.transport.assert_not_called()

    def test_old_and_unknown_models_rejected(self):
        for model in ('k3', 'k3-256k', 'kimi-k3', 'k3.1-pro-highspeed', 'other', None, {}, []):
            with self.subTest(model=model):
                self.assertEqual(self.request(value={'model': model})[0], 400)
        self.check.assert_not_called()
        self.transport.assert_not_called()

    def test_json_object_and_uncompressed_body_required(self):
        for raw in (b'not-json', b'[]', b'null'):
            self.assertEqual(self.request(raw=raw)[0], 400)
        self.assertEqual(self.request(headers={'Content-Encoding': 'gzip'})[0], 400)
        self.transport.assert_not_called()

    def test_body_limit_rejects_before_parsing(self):
        with patch.object(kimi_gate, 'MAX_REQUEST_BYTES', 2):
            self.assertEqual(self.request()[0], 413)
        self.transport.assert_not_called()

    def test_reserve_rejection_429_precedes_upstream(self):
        self.check.return_value = (False, 'reserve_kimi_for_ultra')
        status, _headers, body = self.request(value={'model': 'kimi-for-coding'})
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)['error']['code'], 'reserve_kimi_for_ultra')
        self.assertEqual(json.loads(body)['error']['type'], 'rate_limit_error')
        self.check.assert_called_once_with('kimi-for-coding', self.config)
        self.transport.assert_not_called()

    def test_real_helper_rejects_provider_constraints_on_each_request(self):
        base = {'state': 'ok', 'stale': False, 'available': True,
                'sampled_at': time.time(), 'windows': [
                    {'name': 'limit7d', 'duration_minutes': 10080,
                     'valid': True, 'remaining': 80, 'remaining_percent': 80}]}
        constraints = [
            ('short_window_empty', {'windows': base['windows'] + [
                {'name': 'limit5h', 'duration_minutes': 300, 'valid': True,
                 'remaining': 0, 'remaining_percent': 0}]}),
            ('account_unavailable', {'available': False}),
            ('auth_error', {'state': 'auth_error'}),
            ('rate_limited', {'state': 'rate_limited'}),
        ]
        for model in ('k3.1-pro', 'kimi-for-coding'):
            for label, fields in constraints:
                with self.subTest(model=model, constraint=label):
                    q = {'kimi-for-coding': dict(base, **fields)}
                    with patch.object(kimi_gate, 'preflight', self.real_preflight), \
                            patch.object(kimi_gate, '_snapshot', return_value=q), \
                            patch.object(quota, 'billing_block', return_value=None):
                        status, _headers, _body = self.request(value={'model': model})
                    self.assertEqual(status, 429)
        self.transport.assert_not_called()

    def test_preflight_unexpected_error_is_503_and_not_echoed(self):
        self.check.side_effect = RuntimeError(self.key)
        status, _headers, body = self.request()
        self.assertEqual(status, 503)
        self.assertNotIn(self.key.encode(), body)
        self.transport.assert_not_called()

    def test_key_rotation_during_preflight_never_dispatches_or_retries(self):
        old = self.key
        rotated = 'sk-test-only-synthetic-fixture-2-not-a-real-key'
        key_reader = self.mocks[2]
        def rotate(model, config):
            key_reader.return_value = rotated
            return True, 'weekly_reserve_beneficiary'
        self.check.side_effect = rotate
        status, _headers, raw = self.request()
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw)['error']['code'], 'inference_credential_changed')
        self.assertNotIn(old.encode(), raw)
        self.assertNotIn(rotated.encode(), raw)
        self.assertEqual(key_reader.call_count, 2)
        self.check.assert_called_once()
        self.transport.assert_not_called()

    def test_raw_request_bytes_and_fixed_endpoint(self):
        raw = b'{ "model":"k3.1-pro", "reasoning_effort":"max", "stream":false, "messages":[] }'
        status, headers, body = self.request(raw=raw, headers={'Cookie': 'never-forward', 'X-Other': 'never-forward'})
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"ok":true}')
        self.transport.assert_called_once_with('api.kimi.com', timeout=900)
        args, kwargs = self.upstream.request.call_args
        self.assertEqual(args, ('POST', '/coding/v1/chat/completions'))
        self.assertEqual(kwargs['body'], raw)
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer ' + self.key)
        self.assertNotIn('Cookie', kwargs['headers'])
        self.assertNotIn('X-Other', kwargs['headers'])
        self.assertNotIn(self.password, repr(kwargs))

    def test_stream_is_exact_and_no_sensitive_headers_relayed(self):
        stream = b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n\ndata: [DONE]\n\n'
        self.upstream.getresponse.return_value = Response(body=stream, headers={
            'Content-Type': 'text/event-stream', 'Set-Cookie': 'secret-cookie',
            'Authorization': 'secret', 'X-Other': 'not-forwarded'})
        status, headers, body = self.request(value={'model': 'k3.1-pro', 'stream': True})
        self.assertEqual(status, 200)
        self.assertEqual(body, stream)
        self.assertEqual(headers['Content-Type'], 'text/event-stream')
        self.assertNotIn('Set-Cookie', headers)
        self.assertNotIn('Authorization', headers)
        self.check.assert_called_once()
        self.upstream.request.assert_called_once()

    def test_upstream_error_status_and_billing_message_retained_secrets_redacted(self):
        raw = json.dumps({'error': {'type': 'billing_error', 'code': 'monthly_usage_limit',
                                   'message': 'Monthly quota exhausted; ' + self.key + ' ' + self.password}}).encode()
        self.upstream.getresponse.return_value = Response(402, raw)
        status, _headers, body = self.request()
        value = json.loads(body)['error']
        self.assertEqual(status, 402)
        self.assertEqual(value['code'], 'monthly_usage_limit')
        self.assertIn('Monthly quota exhausted', value['message'])
        self.assertNotIn(self.key.encode(), body)
        self.assertNotIn(self.password.encode(), body)
        self.upstream.request.assert_called_once()

    def test_upstream_redirect_not_followed_or_location_relayed(self):
        self.upstream.getresponse.return_value = Response(302, b'', {'Location': 'https://other.invalid'})
        status, headers, _body = self.request()
        self.assertEqual(status, 302)
        self.assertNotIn('Location', headers)
        self.upstream.request.assert_called_once()

    def test_unknown_transport_result_is_not_retried(self):
        self.upstream.getresponse.side_effect = OSError(self.key)
        status, _headers, body = self.request()
        self.assertEqual(status, 503)
        self.assertNotIn(self.key.encode(), body)
        self.upstream.request.assert_called_once()

    def test_models_response_cannot_reintroduce_k3(self):
        self.upstream.getresponse.return_value = Response(body=json.dumps({'data': [
            {'id': 'k3'}, {'id': 'k3.1-pro'}, {'id': 'kimi-for-coding'}, {'id': 'k3.1-pro-highspeed'}]}).encode())
        status, _headers, raw = self.request('/v1/models', 'GET')
        self.assertEqual(status, 200)
        self.assertEqual([v['id'] for v in json.loads(raw)['data']], ['k3.1-pro', 'kimi-for-coding'])
        self.check.assert_not_called()

    def test_non_loopback_bind_rejected(self):
        with self.assertRaises(ValueError):
            kimi_gate.create_server(self.config, host='0.0.0.0')


class SnapshotTests(unittest.TestCase):
    def test_recent_cached_attempt_still_uses_identity_aware_refresh(self):
        cached = {'kimi-for-coding': {'checked_at': time.time(), 'sampled_at': time.time() - 1200}}
        with patch.object(common, 'read_json', return_value=cached), \
                patch.object(quota, 'view', return_value={'retained': True}) as view, \
                patch.object(quota, 'refresh', return_value={'identity_checked': True}) as refresh:
            self.assertEqual(kimi_gate._snapshot(), {'identity_checked': True})
            view.assert_not_called()
            refresh.assert_called_once_with()

    def test_missing_or_old_cache_refreshes(self):
        for cached in ({}, {'kimi-for-coding': {'checked_at': time.time() - 61}},
                       {'kimi-for-coding': {'checked_at': True}}):
            with self.subTest(cached=cached), patch.object(common, 'read_json', return_value=cached), \
                    patch.object(quota, 'refresh', return_value={'fresh': True}) as refresh:
                self.assertEqual(kimi_gate._snapshot(), {'fresh': True})
                refresh.assert_called_once()

    def test_refresh_failure_never_uses_old_account_view(self):
        cached = {'kimi-for-coding': {'sampled_at': 1, 'stale': True}}
        with patch.object(common, 'read_json', return_value=cached) as read, \
                patch.object(quota, 'refresh', side_effect=RuntimeError('private')), \
                patch.object(quota, 'view', return_value=cached) as view:
            with self.assertRaises(RuntimeError):
                kimi_gate._snapshot()
            view.assert_not_called()
            read.assert_not_called()

    def test_shared_authoritative_helper_receives_model_and_current_config(self):
        with patch.object(kimi_gate, '_snapshot', return_value={'q': True}), \
                patch.object(quota, 'kimi_request_allowed', create=True, return_value=(False, 'monthly_empty')) as helper:
            self.assertEqual(kimi_gate.preflight('k3.1-pro', {'c': True}), (False, 'monthly_empty'))
            helper.assert_called_once_with('k3.1-pro', {'c': True}, {'q': True})


if __name__ == '__main__':
    unittest.main()
