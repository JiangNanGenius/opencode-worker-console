"""Console HTTP integration for the memory bridge: auth, native browse/detail,
capability gating and the confirmed maintenance run (isolated server fixture)."""
import http.client
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


class MemoryHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name).resolve()
        cls.state = root / 'state'
        (cls.state / 'tasks').mkdir(parents=True)
        cls.server = console.ThreadingHTTPServer(('127.0.0.1', 0), console.Handler)
        cls.port = cls.server.server_address[1]
        cls.server.daemon_threads = True
        cls.server.auth_root = cls.state
        cls.server.cookie_name = 'delegate_console_session_' + str(cls.port)
        config_path = root / 'config.json'
        config_path.write_text(json.dumps({
            'profiles': {'worker': {'model': 'acme/test'}},
            'server_url': 'http://127.0.0.1:1',
            'console_url': 'http://127.0.0.1:{}'.format(cls.port),
            'max_parallel_per_owner': 4, 'kimi_reserve_percent': 0,
        }))
        console_auth.set_user('admin', 'api-password', cls.state)
        cls.config_patcher = patch.object(common, 'CONFIG', config_path)
        cls.config_patcher.start()
        cls.state_patcher = patch.object(common, 'STATE', cls.state)
        cls.state_patcher.start()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.state_patcher.stop()
        cls.config_patcher.stop()
        cls.tmp.cleanup()

    def _login(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        conn.request('POST', '/console-api/auth/login',
                     json.dumps({'username': 'admin', 'password': 'api-password'}),
                     {'Content-Type': 'application/json'})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        cookie = response.getheader('Set-Cookie').split(';')[0]
        conn.close()
        return cookie.split('=', 1)[1]

    def _request(self, method, path, token=None, body=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Cookie'] = 'delegate_console_session_{}={}'.format(self.port, token)
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, (json.loads(raw) if raw else {})

    def test_memory_routes_require_a_session(self):
        for path in ('/console-api/memory/browse', '/console-api/memory/detail?id=x',
                     '/console-api/memory/activity', '/console-api/memory/capacity'):
            status, _ = self._request('GET', path)
            self.assertEqual(status, 401, path)

    def test_native_browse_returns_pagination_and_navigation(self):
        token = self._login()
        payload = {'items': [{'id': 'viking://user/default/memories/experiences/a.md'}],
                   'total': 7, 'page': 2, 'pageSize': 25, 'totalPages': 1,
                   'categories': [{'name': 'experiences', 'count': 7}],
                   'directories': [], 'projects': [], 'totalRecords': 7}
        import memory_manager
        with patch.object(memory_manager, 'browse', return_value=payload) as browse:
            status, body = self._request(
                'GET', '/console-api/memory/browse?category=experiences&project=opencode_project_x&page=2', token)
        self.assertEqual(status, 200)
        self.assertEqual(body['total'], 7)
        self.assertEqual(browse.call_args.kwargs['category'], 'experiences')
        self.assertEqual(browse.call_args.kwargs['project'], 'opencode_project_x')
        self.assertEqual(browse.call_args.kwargs['page'], '2')

    def test_invalid_detail_uri_is_a_bad_request(self):
        token = self._login()
        import memory_manager
        with patch.object(memory_manager, 'detail', side_effect=ValueError('Memory URI must stay inside the memory root')):
            status, body = self._request('GET', '/console-api/memory/detail?id=viking://user/default/sessions/x.md', token)
        self.assertEqual(status, 400)
        self.assertIn('memory root', body['error'])

    def test_detail_adds_only_verified_session_links(self):
        token = self._login()
        import memory_manager
        console._MEMORY_SESSIONS.update(at=0.0, map={})
        with patch.object(memory_manager, 'detail', return_value={'id': 'mem', 'sessionId': 'ses_1'}), \
             patch.object(console.management, 'sessions',
                          return_value={'sessions': [{'id': 'ses_1', 'directory': '/tmp/project'}]}):
            status, body = self._request('GET', '/console-api/memory/detail?id=mem', token)
        self.assertEqual(status, 200)
        self.assertTrue(body['session']['verified'])
        self.assertTrue(body['session']['url'].startswith('/'))

    def test_legacy_backend_capability_gate_returns_400(self):
        token = self._login()
        import memory_manager
        with patch.object(memory_manager, 'capacity',
                          side_effect=ValueError('Memory capacity requires the OpenViking backend')):
            status, body = self._request('GET', '/console-api/memory/capacity', token)
        self.assertEqual(status, 400)
        self.assertIn('OpenViking', body['error'])

    def test_maintenance_run_is_confirmed_and_forced(self):
        token = self._login()
        import memory_manager
        with patch.object(memory_manager, 'maintain', return_value={'enabled': True, 'deleted': 0}) as maintain:
            status, body = self._request('POST', '/console-api/memory', token,
                                         {'action': 'maintenance_run'})
        self.assertEqual(status, 200)
        self.assertTrue(body['enabled'])
        maintain.assert_called_once_with(force=True)


if __name__ == '__main__':
    unittest.main()
