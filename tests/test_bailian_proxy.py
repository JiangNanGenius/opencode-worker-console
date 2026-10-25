"""Pool configuration, runtime overlay and service lifecycle contracts."""
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import bailian_pool
import common
import console_runtime
import providers
import server
import service


PROFILE = {'ali-max': {'model': providers.BAILIAN_PROVIDER + '/qwen3.8-max'}}
POOL = {
    'enabled': True,
    'listen_port': 51530,
    'request_timeout_seconds': 60,
    'slots': [
        {'id': 'seat-1', 'keychain': {'service': 'workerdesk.bailian-token-plan',
                                      'account': 'seat-1'}},
        {'id': 'seat-2', 'keychain': {'service': 'workerdesk.bailian-token-plan',
                                      'account': 'seat-2'}},
    ],
}


class ConfigValidationTests(unittest.TestCase):
    def test_disabled_pool_ignores_slot_schema(self):
        self.assertIsNone(bailian_pool.pool_settings({
            'bailian_pool': {'enabled': False, 'slots': 'not-a-list'}}))
        self.assertIsNone(bailian_pool.pool_settings({'profiles': PROFILE}))

    def test_valid_keychain_pool_normalizes_metadata_only(self):
        settings = bailian_pool.pool_settings({'bailian_pool': POOL})
        self.assertEqual(settings['port'], 51530)
        self.assertEqual([slot['id'] for slot in settings['slots']], ['seat-1', 'seat-2'])
        self.assertEqual(settings['slots'][0]['source'], 'keychain')
        self.assertNotIn('key', settings['slots'][0])
        self.assertNotIn('secret', json.dumps(settings))

    def test_file_reference_compatibility_is_absolute_only(self):
        config = {'bailian_pool': {'enabled': True, 'slots': [
            {'id': 'legacy', 'file': '/private/path/seat.key'}]}}
        settings = bailian_pool.pool_settings(config)
        self.assertEqual(settings['slots'][0]['source'], 'file')
        for bad in ('relative.key', '/tmp/{file}', '/tmp/with space', '/tmp/x\n'):
            with self.subTest(bad=bad):
                broken = {'bailian_pool': {'enabled': True, 'slots': [
                    {'id': 'legacy', 'file': bad}]}}
                with self.assertRaises(ValueError):
                    bailian_pool.pool_settings(broken)

    def test_slot_must_have_exactly_one_reference(self):
        for slot in ({'id': 'both', 'keychain': {'service': 's', 'account': 'a'},
                      'file': '/private/key'},
                     {'id': 'neither'}):
            with self.subTest(slot=slot):
                with self.assertRaises(ValueError):
                    bailian_pool.pool_settings({'bailian_pool': {
                        'enabled': True, 'slots': [slot]}})

    def test_plaintext_key_setting_is_rejected_without_echo(self):
        secret = 'sk-test-only-synthetic-fixture-1-not-a-real-key'
        config = {'bailian_pool': {'enabled': True, 'slots': [
            {'id': 'seat-1', 'key': secret,
             'keychain': {'service': 's', 'account': 'a'}}]}}
        with self.assertRaises(ValueError) as raised:
            bailian_pool.pool_settings(config)
        self.assertIn('key', str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))

    def test_duplicate_ids_and_bad_bounds_are_rejected(self):
        duplicate = {'bailian_pool': {'enabled': True, 'slots': [
            {'id': 'same', 'keychain': {'service': 's', 'account': 'a'}},
            {'id': 'same', 'keychain': {'service': 's', 'account': 'b'}}]}}
        with self.assertRaises(ValueError):
            bailian_pool.pool_settings(duplicate)
        for block in ({'enabled': True, 'listen_port': 0, 'slots': POOL['slots']},
                      {'enabled': True, 'listen_port': 65536, 'slots': POOL['slots']},
                      {'enabled': True, 'request_timeout_seconds': 0, 'slots': POOL['slots']},
                      {'enabled': True, 'slots': []}):
            with self.subTest(block=block):
                with self.assertRaises(ValueError):
                    bailian_pool.pool_settings({'bailian_pool': block})


class RuntimeOverlayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        (self.state / 'server-password').write_text('synthetic-server-password')
        self.patches = [patch.object(server, 'STATE', self.state),
                        patch.object(common, 'STATE', self.state)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_disabled_pool_preserves_old_provider_api_key_files(self):
        config = {'profiles': PROFILE,
                  'provider_api_key_files': {providers.BAILIAN_PROVIDER: '/private/old.key'}}
        provider = server.runtime_overlay(config)['provider'][providers.BAILIAN_PROVIDER]
        self.assertEqual(provider['options']['baseURL'], providers.BAILIAN_BASE_URL)
        self.assertEqual(provider['options']['apiKey'], '{file:/private/old.key}')

    def test_enabled_pool_uses_loopback_and_server_password(self):
        config = {'profiles': PROFILE,
                  'bailian_pool': POOL,
                  'provider_api_key_files': {providers.BAILIAN_PROVIDER: '/private/old.key'}}
        provider = server.runtime_overlay(config)['provider'][providers.BAILIAN_PROVIDER]
        self.assertEqual(provider['options']['baseURL'], 'http://127.0.0.1:51530')
        self.assertEqual(provider['options']['apiKey'],
                         '{file:' + str(self.state / 'server-password') + '}')
        self.assertNotIn('/private/old.key', json.dumps(provider))

    def test_bad_pool_config_rejects_overlay_before_start(self):
        config = {'profiles': PROFILE,
                  'bailian_pool': {'enabled': True, 'slots': []}}
        with self.assertRaises(ValueError):
            server.runtime_overlay(config)

    def test_provider_only_accepts_the_local_loopback_base_url(self):
        for value in ('http://127.0.0.1:51530', 'http://127.0.0.1:1',
                      'http://127.0.0.1:65535'):
            with self.subTest(value=value):
                self.assertEqual(providers.bailian_provider(
                    '/private/key', base_url=value)['options']['baseURL'], value)
        for value in ('https://evil.example', 'http://localhost:51530',
                      'http://127.0.0.1:0', 'http://127.0.0.1:65536',
                      'http://127.0.0.1:51530/path', 'http://127.0.0.1:51530?x=1'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    providers.bailian_provider('/private/key', base_url=value)


class ServiceLifecycleTests(unittest.TestCase):
    def _start(self, config):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = Path(temp.name)
        (state / 'logs').mkdir()
        (state / 'server-password').write_text('synthetic-server-password')
        calls = []
        selected = {'worker_python': '/worker/python3.9',
                    'console_python': '/console/python3.12'}
        bootstrap = types.SimpleNamespace(ensure_opencode=lambda path: path)
        management = types.SimpleNamespace(sync_worker_permissions=lambda: None)

        def launch(argv, **kwargs):
            calls.append(argv)
            return types.SimpleNamespace(pid=100 + len(calls))

        with patch.object(service, 'STATE', state), patch.object(common, 'STATE', state), \
                patch.object(service, 'init'), patch.object(service, 'config', return_value=config), \
                patch.object(service, 'identity', return_value='identity'), \
                patch.object(service, 'api', return_value={'healthy': True}), \
                patch.object(service, 'alive', return_value=False), \
                patch.object(console_runtime, 'interpreters', return_value=selected), \
                patch.object(bailian_pool, 'wait_ready', return_value=True) as wait_ready, \
                patch.object(service.subprocess, 'Popen', side_effect=launch), \
                patch.dict(sys.modules, {'bootstrap': bootstrap, 'management': management}):
            result = service.start()
        return result, calls, wait_ready

    def test_service_starts_pool_before_server_and_waits_for_readiness(self):
        config = {'profiles': PROFILE, 'bailian_pool': dict(POOL),
                  'memory': {'enabled': False}, 'opencode_binary': '/fake/opencode'}
        result, calls, wait_ready = self._start(config)
        scripts = [Path(argv[1]).name for argv in calls]
        self.assertEqual(scripts, ['bailian_pool.py', 'server.py', 'delegate.py'])
        self.assertEqual(calls[0][0], '/worker/python3.9')
        wait_ready.assert_called_once_with(51530, 'synthetic-server-password', timeout=10)
        self.assertIn('bailian_pool', result['services'])

    def test_service_rejects_invalid_pool_before_launch(self):
        config = {'profiles': PROFILE,
                  'bailian_pool': {'enabled': True, 'slots': []},
                  'memory': {'enabled': False}, 'opencode_binary': '/fake/opencode'}
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with patch.object(service, 'STATE', Path(temp.name)), \
                patch.object(common, 'STATE', Path(temp.name)), \
                patch.object(service, 'config', return_value=config), \
                patch.object(service, 'init'), \
                patch.object(console_runtime, 'interpreters') as interpreters, \
                patch.object(service.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, 'Invalid bailian_pool'):
                service.start()
        interpreters.assert_not_called()
        launch.assert_not_called()

    def test_service_stop_includes_bailian_pool(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = Path(temp.name)
        record = {'pid': 123, 'identity': 'identity'}
        calls = []
        live = {'value': True}

        def alive(entry):
            return entry is record and live['value']

        def kill(pid, sig):
            calls.append((pid, sig))
            live['value'] = False

        with patch.object(service, 'STATE', state), patch.object(common, 'STATE', state), \
                patch.object(service, 'tasks', return_value=[]), \
                patch.object(service, 'read_json', return_value={'bailian_pool': record}), \
                patch.object(service, 'alive', side_effect=alive), \
                patch.object(service, 'config', return_value={'memory': {'enabled': False}}), \
                patch.object(service.os, 'kill', side_effect=kill), \
                patch.object(service.time, 'sleep', return_value=None):
            service.stop()
        self.assertEqual(calls, [(123, service.signal.SIGTERM)])


if __name__ == '__main__':
    unittest.main()
