"""Console HTTP integration for host endpoints: auth, origin, cache-only GET,
action validation and legacy compatibility. Runs a real server on an
ephemeral loopback port with isolated state.
"""
import base64
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
import host_telemetry


class HostApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name).resolve()
        cls.state = root / 'state'
        (cls.state / 'tasks').mkdir(parents=True)
        # Bind first so the config can name the real console origin.
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
        common.write_json(cls.state / 'tasks' / 'job-1.json',
                          {'id': 'job-1', 'status': 'running', 'title': 'Live', 'created_at': 1,
                           'started_at': 1, 'updated_at': 2, 'source_dir': '/tmp/proj', 'directory': '/tmp/proj'})
        console_auth.set_user('admin', 'api-password', cls.state)
        cls.config_patcher = patch.object(common, 'CONFIG', config_path)
        cls.config_patcher.start()
        cls.collector = host_telemetry.Collector(state_dir=cls.state, codex_root=cls.state / 'no-codex')
        cls.collector.latest_system = {'sampled_at': __import__('time').time(), 'cpu_percent': 25.0, 'gpu_percent': None,
                                       'memory_percent': 50.0, 'memory_pressure_percent': None,
                                       'net_in_bps': None, 'net_out_bps': None, 'disk_total_bps': None,
                                       'disks': [], 'fans': None, 'temp_cpu_c': None, 'temp_gpu_c': None,
                                       'thermal_pressure': None, 'fan_control_mode': None,
                                       'fan_active_profile': None, 'fan_safety_engaged': None,
                                       'uptime_hours': 1.0, 'load_average': [1.0, 1.0, 1.0]}
        cls.collector.latest_tasks = {'codex': {'state': 'unavailable', 'sampled_at': 1000.0, 'total': 0,
                                                'active': 0, 'items': []},
                                      'opencode': {'state': 'ok', 'sampled_at': __import__('time').time(), 'total': 1,
                                                   'active': 1, 'items': [{'id': 'job-1', 'title': 'Live',
                                                                           'status': 'running', 'model': 'test'}]}}
        cls.collector.latest_quotas = [{'id': 'codex', 'state': 'unavailable'}]
        collector_patcher = patch.object(host_telemetry, 'ensure_collector', return_value=cls.collector)
        collector_patcher.start()
        cls.collector_patcher = collector_patcher
        collector_ref_patcher = patch.object(host_telemetry, '_COLLECTOR', cls.collector)
        collector_ref_patcher.start()
        cls.collector_ref_patcher = collector_ref_patcher
        cls.state_patcher = patch.object(common, 'STATE', cls.state)
        cls.state_patcher.start()
        cls.ht_state_patcher = patch.object(host_telemetry, 'STATE', cls.state)
        cls.ht_state_patcher.start()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.ht_state_patcher.stop()
        cls.state_patcher.stop()
        cls.collector_ref_patcher.stop()
        cls.collector_patcher.stop()
        cls.config_patcher.stop()
        cls.tmp.cleanup()

    def _headers(self, token=None, origin=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Cookie'] = 'delegate_console_session_{}={}'.format(self.port, token)
        if origin:
            headers['Origin'] = origin
        return headers

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

    def _get(self, path, token=None, origin=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        conn.request('GET', path, headers=self._headers(token, origin))
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, (json.loads(body) if body else {})

    def _get_raw(self, path, token=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        conn.request('GET', path, headers=self._headers(token))
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, body

    def _post(self, path, body, token=None, origin=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        conn.request('POST', path, json.dumps(body), self._headers(token, origin))
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, (json.loads(raw) if raw else {})

    def test_host_endpoints_require_session(self):
        for path in ('/console-api/host/snapshot', '/console-api/host/history?range=15m',
                     '/console-api/host/capabilities', '/console-api/host/operations'):
            status, _ = self._get(path)
            self.assertEqual(status, 401, path)

    def test_snapshot_is_cached_and_get_never_acquires(self):
        token = self._login()
        with patch.object(host_telemetry.Collector, 'collect_host_once',
                          side_effect=AssertionError('GET must not acquire')):
            status, body = self._get('/console-api/host/snapshot', token=token)
        self.assertEqual(status, 200)
        snapshot = body['snapshot']
        self.assertGreater(snapshot['sampled_at'], 1e6)
        self.assertFalse(snapshot['stale'])
        self.assertEqual(snapshot['affected_jobs'], ['job-1'])
        self.assertEqual(len(snapshot['services']), 3)
        self.assertIn('pool', snapshot['service_health'])
        self.assertEqual(snapshot['tasks']['opencode']['items'][0]['id'], 'job-1')

    def test_history_validates_range_and_reads_store(self):
        token = self._login()
        status, body = self._get('/console-api/host/history?range=15m', token=token)
        self.assertEqual(status, 200)
        self.assertEqual(body['range'], '15m')
        self.assertIn('cpu_percent', body['series'])
        status, body = self._get('/console-api/host/history?range=100y', token=token)
        self.assertEqual(status, 400)

    def test_capabilities_and_operations(self):
        token = self._login()
        status, caps = self._get('/console-api/host/capabilities', token=token)
        self.assertEqual(status, 200)
        self.assertIn('fans', caps)
        status, body = self._get('/console-api/host/operations', token=token)
        self.assertEqual(status, 200)
        self.assertEqual(body['operations'], [])

    def test_action_endpoint_validation_and_routing(self):
        token = self._login()
        status, body = self._post('/console-api/host/action', {'action': 'nope.nothing'}, token=token)
        self.assertEqual(status, 400)
        status, body = self._post('/console-api/host/action', {}, token=token)
        self.assertEqual(status, 400)
        with patch('host_controls.dispatch', return_value={'ok': True}) as dispatch:
            status, body = self._post('/console-api/host/action',
                                      {'action': 'fan.profile', 'params': {'name': 'Cool'}}, token=token)
        self.assertEqual(status, 200)
        self.assertEqual(body, {'ok': True})
        args = dispatch.call_args.args
        self.assertEqual(args[0], 'fan.profile')
        self.assertEqual(args[2], 'admin')

    def test_origin_guard_applies_to_host_api(self):
        token = self._login()
        status, _ = self._get('/console-api/host/snapshot', token=token,
                              origin='https://evil.example')
        self.assertEqual(status, 403)
        status, _ = self._post('/console-api/host/action', {'action': 'x'}, token=token,
                               origin='https://evil.example')
        self.assertEqual(status, 403)

    def test_host_asset_requires_session_and_legacy_state_unaffected(self):
        status, _ = self._get_raw('/console-assets/host.js')
        self.assertEqual(status, 401)
        token = self._login()
        status, body = self._get_raw('/console-assets/host.js', token=token)
        self.assertEqual(status, 200)
        self.assertIn(b'Host overview', body)
        status, state = self._get('/console-api/state', token=token)
        self.assertEqual(status, 200)
        self.assertIn('cpu_percent', state['host'])  # legacy system_status shape
        self.assertIn('tasks', state)
        self.assertNotIn('affected_jobs', state)     # host extras stay in host APIs


if __name__ == '__main__':
    unittest.main()
