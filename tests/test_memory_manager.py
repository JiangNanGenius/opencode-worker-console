import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import memory_manager


class MemoryManagerTests(unittest.TestCase):
    def _config(self, root, enabled=True):
        path = root / 'config.json'
        path.write_text(json.dumps({'memory': {'enabled': enabled}}))
        return path

    def test_settings_are_loopback_and_reserve_is_bounded(self):
        value = memory_manager.validate({
            'enabled': True,
            'api_url': 'http://127.0.0.1:4747',
            'embedding_api_url': 'https://ark.example/v3',
            'embedding_model': 'embedding',
            'embedding_dimensions': 2048,
            'capture_profile': 'provider/model',
            'reserve': {'minimum_percent': .25, 'maximum_percent': 3,
                        'headroom_multiplier': 1.5},
        })
        self.assertEqual(value['reserve']['minimum_percent'], .25)
        with self.assertRaises(ValueError):
            memory_manager.validate({**value, 'api_url': 'http://192.168.1.2:4747'})

    def test_project_tag_is_stable_without_git(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = memory_manager.project_info(root)
            second = memory_manager.project_info(root)
        self.assertEqual(first, second)
        self.assertTrue(first['tag'].startswith('opencode_project_'))
        self.assertEqual(first['projectName'], root.name)

    def test_crud_uses_scoped_loopback_api(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = self._config(root)
            calls = []

            def request(url, method='GET', data=None, **kwargs):
                calls.append((url, method, data, kwargs))
                return {'success': True, 'data': {'items': []}}

            with patch.object(common, 'CONFIG', config_path), \
                 patch.object(common, 'STATE', root / 'state'), \
                 patch.object(memory_manager, '_api_token', return_value='local-token'), \
                 patch.object(common, 'request', side_effect=request):
                memory_manager.list_memories(root)
                memory_manager.search('needle', root)
                memory_manager.add('durable fact', root, ['verified'])
                memory_manager.update('mem_123', 'updated fact')
                memory_manager.delete('mem_123')
                memory_manager.bulk_delete(['mem_123', 'mem_456'])
            self.assertTrue(all(url.startswith('http://127.0.0.1:4747/api/') for url, _, _, _ in calls))
            self.assertEqual([method for _, method, _, _ in calls],
                             ['GET', 'GET', 'POST', 'PUT', 'DELETE', 'POST'])
            self.assertIn('containerTag', calls[2][2])
            self.assertTrue(all(call[3]['headers']['X-Opencode-Mem-Token'] == 'local-token'
                                for call in calls))

    def test_configure_writes_owner_only_secret_and_file_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = self._config(root)
            home = root / 'home'
            home.mkdir()
            with patch.object(common, 'CONFIG', config_path), \
                 patch.object(common, 'STATE', root / 'state'), \
                 patch.object(common, 'auth_key', return_value='secret-not-returned'), \
                 patch.object(memory_manager, 'install_plugin', return_value={'version': '2.26.0'}), \
                 patch.object(memory_manager, '_api_token', return_value='local-token'), \
                 patch.object(Path, 'home', return_value=home):
                result = memory_manager.configure()
            payload = json.loads((home / '.config/opencode/opencode-mem.jsonc').read_text())
            secret = root / 'state/credentials/opencode-mem-ark-key'
            self.assertTrue(result['configured'])
            self.assertEqual(payload['embeddingApiKey'], 'file://' + str(secret))
            self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('secret-not-returned', json.dumps(result))

    def test_api_token_is_created_owner_only_and_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            with patch.object(Path, 'home', return_value=home):
                first = memory_manager._api_token(create=True)
                second = memory_manager._api_token()
            token_file = home / '.opencode-mem/.auth-token'
            self.assertEqual(first, second)
            self.assertEqual(len(first), 64)
            self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)

    def test_plugin_install_can_defer_to_opencode_without_npm(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(memory_manager, '_plugin_dir', return_value=Path(temp) / 'plugin'), \
             patch.object(memory_manager.shutil, 'which', return_value=None):
            value = memory_manager.install_plugin()
        self.assertFalse(value['installed'])
        self.assertEqual(value['managed_by'], 'opencode')

    def test_projects_include_fresh_task_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 'state'
            workspace = root / 'project'
            (state / 'tasks').mkdir(parents=True)
            workspace.mkdir()
            (state / 'tasks/task-1.json').write_text(json.dumps({
                'id': 'task-1', 'directory': str(workspace), 'created_at': 1
            }))
            with patch.object(common, 'STATE', state), \
                 patch.object(memory_manager, '_request', return_value={'project': []}):
                rows = memory_manager.projects()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['projectPath'], str(workspace.resolve()))


if __name__ == '__main__':
    unittest.main()
