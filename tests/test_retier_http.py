"""Retier's actual HTTP boundary, using only temporary accounts and task state."""
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import console
import console_auth
import retier


class RetierHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        config_path = self.root / 'config.json'
        cfg = {'profiles': {'worker': {'model': 'synthetic/model'}}}
        config_path.write_text(json.dumps(cfg))
        for target, name, value in [(common, 'STATE', self.state),
                                    (common, 'CONFIG', config_path),
                                    (console, 'STATE', self.state)]:
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        common.init()
        common.write_json(common.task_path('job-http'), {
            'id': 'job-http', 'status': 'queued', 'tier': 'deep',
            'requested_profile': 'auto', 'directory': str(self.root),
            'scopes': ['output.txt'], 'workspace_mode': 'shared'})
        routing = patch.object(retier, '_route_for', return_value=('worker', 'test', {}))
        routing.start()
        self.addCleanup(routing.stop)
        console_auth.set_user('test-user', 'synthetic-test-password', self.state)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), console.Handler)
        self.server.daemon_threads = True
        self.origin = 'http://127.0.0.1:' + str(self.server.server_port)
        self.server.allowed_origins = console_auth.origin_tuples({'console_url': self.origin})
        self.server.trusted_proxy_networks = []
        self.server.auth_root = self.state
        self.server.cookie_name = 'retier_test_session'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        code, _, headers = self.post('/console-api/auth/login', {
            'username': 'test-user', 'password': 'synthetic-test-password'}, authenticated=False)
        self.assertEqual(code, 200)
        self.cookie = headers['set-cookie'].split(';')[0]

    def post(self, path, body, authenticated=True, **headers):
        if authenticated:
            headers['Cookie'] = self.cookie
        headers.setdefault('Content-Type', 'application/json')
        c = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            c.request('POST', path, json.dumps(body).encode(), headers)
            response = c.getresponse()
            return response.status, json.loads(response.read()), {
                k.lower(): v for k, v in response.getheaders()}
        finally:
            c.close()

    def payload(self):
        return {'tier': 'fast', 'reason': 'Investigation resolved the uncertainty',
                'handoff': {'done': 'Rules verified', 'remaining': 'Write output.txt',
                            'do_not_repeat': 'Do not repeat the investigation'},
                'request_id': 'http-phase-2'}

    def test_authentication_and_origin_guards_precede_retier(self):
        with patch.object(retier, 'send', wraps=retier.send) as send:
            for authenticated, headers, expected in [
                (False, {}, 401),
                (True, {'Origin': 'https://untrusted.example'}, 403),
                (True, {'Host': 'untrusted.example'}, 403),
            ]:
                code, _, _ = self.post('/console-api/task/job-http/retier', self.payload(),
                                       authenticated=authenticated, **headers)
                self.assertEqual(code, expected)
            send.assert_not_called()
        self.assertEqual(common.task('job-http')['tier'], 'deep')

    def test_authenticated_request_applies_same_queued_task_and_replays_compactly(self):
        path = '/console-api/task/job-http/retier'
        code, result, _ = self.post(path, self.payload(), Origin=self.origin)
        self.assertEqual(code, 200)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['from_tier'], 'deep')
        self.assertEqual(result['to_tier'], 'fast')
        self.assertLessEqual(set(result), set(retier.COMPACT_KEYS))
        self.assertNotIn('Write output.txt', json.dumps(result))
        saved = common.task('job-http')
        self.assertEqual(saved['id'], 'job-http')
        self.assertEqual(saved['tier'], 'fast')
        self.assertEqual(saved['scopes'], ['output.txt'])
        self.assertIn('Write output.txt', retier.dispatch_note(saved))
        code, replay, _ = self.post(path, self.payload())
        self.assertEqual(code, 200)
        self.assertEqual(replay, result)
        self.assertEqual(len(common.task('job-http')['tier_history']), 1)

    def test_invalid_checkpoint_returns_400_without_mutation(self):
        payload = self.payload()
        payload['handoff'] = {'remaining': []}
        code, result, _ = self.post('/console-api/task/job-http/retier', payload)
        self.assertEqual(code, 400)
        self.assertIn('remaining', result['error'])
        self.assertEqual(common.task('job-http')['tier'], 'deep')
        self.assertNotIn('tier_history', common.task('job-http'))


if __name__ == '__main__':
    unittest.main()
