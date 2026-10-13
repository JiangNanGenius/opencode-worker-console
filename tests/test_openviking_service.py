"""OpenViking service configuration contract: env references only, owner-only files."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import openviking_service


def memory_config(root, **overrides):
    memory = {'enabled': True, 'backend': 'openviking',
              'embedding_api_url': 'https://ark.example/api/plan/v3',
              'embedding_model': 'doubao-embedding-vision',
              'embedding_dimensions': 2048,
              'openviking': {'url': 'http://127.0.0.1:1933',
                             'vlm_model': 'deepseek-flash',
                             'vlm_api_base': 'https://api.deepseek.com'}}
    memory.update(overrides)
    path = root / 'config.json'
    path.write_text(json.dumps({'memory': memory}))
    return path


class RuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = memory_config(self.root)
        self.state = self.root / 'state'
        self.ov_home = self.root / 'ov'
        self.patchers = [patch.object(common, 'CONFIG', self.config),
                         patch.object(common, 'STATE', self.state),
                         patch.object(openviking_service, 'OV_HOME', self.ov_home)]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in self.patchers:
            patcher.stop()
        self.temp.cleanup()

    def test_runtime_config_has_no_secret_values(self):
        cfg = openviking_service.runtime_config()
        self.assertEqual(cfg['embedding']['dense']['api_key'], '${OPENVIKING_ARK_API_KEY}')
        self.assertEqual(cfg['vlm']['api_key'], '${OPENVIKING_DEEPSEEK_API_KEY}')
        self.assertEqual(cfg['embedding']['dense']['api_base'], 'https://ark.example/api/plan/v3')
        self.assertEqual(cfg['embedding']['dense']['dimension'], 2048)
        self.assertEqual(cfg['vlm']['model'], 'deepseek-flash')
        self.assertEqual(cfg['vlm']['api_base'], 'https://api.deepseek.com')
        self.assertEqual(cfg['server']['host'], '127.0.0.1')
        self.assertEqual(cfg['server']['port'], 1933)
        self.assertEqual(cfg['server']['auth_mode'], 'api_key')
        self.assertEqual(cfg['server']['root_api_key'], '${OPENVIKING_ROOT_API_KEY}')
        self.assertNotIn('*', cfg['server']['cors_origins'])
        self.assertTrue(all(origin.startswith('http://127.0.0.1') or origin.startswith('http://localhost')
                            for origin in cfg['server']['cors_origins']))
        self.assertTrue(str(cfg['storage']['workspace']).startswith(str(self.state)))
        self.assertNotIn('/Volumes', json.dumps(cfg))

    def test_write_preserves_extra_fields_and_permissions(self):
        first = openviking_service.write_runtime_config()
        self.assertTrue(Path(first['config']).exists())
        existing = json.loads(Path(first['config']).read_text())
        existing['custom_section'] = {'keep': True}
        existing['vlm']['custom_option'] = 'keep'
        existing['storage']['agfs']['custom_option'] = 'keep'
        Path(first['config']).write_text(json.dumps(existing))
        openviking_service.write_runtime_config()
        merged = json.loads(Path(first['config']).read_text())
        self.assertEqual(merged['custom_section'], {'keep': True})
        self.assertEqual(merged['vlm']['custom_option'], 'keep')
        self.assertEqual(merged['storage']['agfs']['custom_option'], 'keep')
        self.assertEqual(merged['embedding']['dense']['api_key'], '${OPENVIKING_ARK_API_KEY}')
        self.assertEqual(oct(Path(first['config']).stat().st_mode & 0o777), '0o600')
        self.assertEqual(oct(Path(first['cli_config']).stat().st_mode & 0o777), '0o600')
        cli = json.loads(Path(first['cli_config']).read_text())
        self.assertEqual(cli['api_key'], '${OPENVIKING_API_KEY}')
        self.assertNotIn('generated-user-key', json.dumps(cli))
        backups = list(self.ov_home.glob('ov.conf.bak-*'))
        self.assertTrue(backups)
        self.assertEqual(oct(backups[0].stat().st_mode & 0o777), '0o600')

    def test_root_key_is_owner_only_and_env_injected(self):
        key = openviking_service.root_api_key(create=True)
        self.assertTrue(key)
        path = self.state / 'credentials/openviking-root-key'
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')
        with patch.object(openviking_service, 'api_key', return_value='user-key-value'):
            env = openviking_service.service_env()
        self.assertEqual(env['OPENVIKING_ROOT_API_KEY'], key)
        self.assertEqual(env['OPENVIKING_API_KEY'], 'user-key-value')

    def test_bootstrap_stores_user_key_owner_only(self):
        calls = []

        def fake_admin(path, data=None, timeout=30):
            calls.append(path)
            if path.endswith('/key'):
                return {'user_key': 'generated-user-key'}
            return {}

        with patch.object(openviking_service, '_admin_request', side_effect=fake_admin):
            first = openviking_service.bootstrap_key()
            second = openviking_service.bootstrap_key()
        self.assertTrue(first['created'])
        self.assertFalse(second['created'])
        path = openviking_service.api_key_path()
        self.assertEqual(path.read_text(), 'generated-user-key')
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')

    def test_probe_reports_health_and_readiness(self):
        payloads = {'health': {'healthy': True}, 'ready': {'status': 'ready'}}

        def fake_request(url, *args, **kwargs):
            return payloads[url.rsplit('/', 1)[-1]]

        with patch.object(common, 'request', side_effect=fake_request):
            result = openviking_service.probe()
        self.assertTrue(result['healthy'])
        self.assertTrue(result['ready'])

    def test_probe_surfaces_failure_without_raising(self):
        with patch.object(common, 'request', side_effect=common.HttpFailure(None, 'down')):
            result = openviking_service.probe()
        self.assertFalse(result['healthy'])
        self.assertFalse(result['ready'])

    def test_doctor_output_is_redacted(self):
        def fake_run(*args, **kwargs):
            class Done:
                returncode = 0
                stdout = 'Embedding: PASS secretvalue\nAll checks passed\n'
                stderr = ''
            return Done()

        binary = self.root / 'missing-server'
        binary.write_text('#!/bin/sh\n')
        with patch.object(openviking_service, 'venv_bin', return_value=binary), \
             patch.object(openviking_service.subprocess, 'run', side_effect=fake_run), \
             patch.object(openviking_service, '_redact', side_effect=lambda text: text.replace('secretvalue', '<redacted>')):
            result = openviking_service.doctor()
        self.assertTrue(result['passed'])
        self.assertNotIn('secretvalue', json.dumps(result))

    def test_redacts_local_service_keys(self):
        with patch.object(openviking_service, 'api_key', return_value='data-credential'), \
             patch.object(openviking_service, 'root_api_key', return_value='root-credential'):
            self.assertEqual(openviking_service._redact('data-credential root-credential'),
                             '<redacted> <redacted>')

    def test_drain_unavailable_is_not_settled(self):
        with patch.object(openviking_service, 'request', side_effect=ValueError('unavailable')):
            result = openviking_service.drain(timeout=0)
        self.assertFalse(result['settled'])
        self.assertIsNone(result['pending'])

    def test_drain_counts_inflight_work(self):
        def responses(path):
            return {'tasks': [{}]} if 'status=running' in path else {'tasks': []}
        with patch.object(openviking_service, 'request', side_effect=responses):
            result = openviking_service.drain(timeout=0)
        self.assertFalse(result['settled'])
        self.assertEqual(result['running'], 1)

    def test_auth_rejects_dev_server_without_reading_records(self):
        with patch.object(openviking_service, 'probe', return_value={'health': {'auth_mode': 'dev'}}), \
             patch.object(openviking_service, 'request') as request:
            with self.assertRaises(ValueError):
                openviking_service.verify_auth()
        request.assert_not_called()

    def test_listener_must_use_this_runtime_config_and_owner(self):
        import os
        from types import SimpleNamespace
        binary = str(openviking_service.venv_bin('openviking-server'))
        valid = str(os.getuid()) + ' python ' + binary + ' --config ' + str(openviking_service.conf_path())
        for command, expected in ((valid, True),
                                  (valid.replace(str(openviking_service.conf_path()), '/tmp/other.conf'), False),
                                  (valid.replace(binary, '/tmp/openviking-server'), False),
                                  ('99999 ' + valid.split(' ', 1)[1], False)):
            with patch.object(openviking_service, '_alive', return_value=True), \
                 patch.object(openviking_service.subprocess, 'run',
                              return_value=SimpleNamespace(returncode=0, stdout=command.encode())):
                self.assertEqual(openviking_service._verified_pid(123), expected)


if __name__ == '__main__':
    unittest.main()
