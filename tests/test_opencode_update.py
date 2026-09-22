import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import opencode_update


class OpenCodeUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'opencode_binary': '/old/opencode',
                                           'opencode_updates': {'enabled': True, 'check_interval_hours': 6}}))
        self.patchers = [patch.object(common, 'STATE', self.state),
                         patch.object(common, 'CONFIG', self.config),
                         patch.object(opencode_update, 'STATE', self.state),
                         patch.object(opencode_update, 'STATUS', self.state / 'opencode-update.json'),
                         patch.object(opencode_update, 'CONFIG', self.config)]
        for item in self.patchers: item.start()
        common.init()

    def tearDown(self):
        for item in reversed(self.patchers): item.stop()
        self.tmp.cleanup()

    def test_settings_are_normalized_and_validated(self):
        self.assertEqual(opencode_update.settings(), {'enabled': True, 'check_interval_hours': 6})
        self.assertEqual(opencode_update.validate({'enabled': False, 'check_interval_hours': 24}),
                         {'enabled': False, 'check_interval_hours': 24})
        with self.assertRaises(ValueError):
            opencode_update.validate({'enabled': True, 'check_interval_hours': 0})

    def test_check_reports_available_without_installing(self):
        with patch.object(opencode_update, 'current_version', return_value='1.18.30'), \
             patch.object(opencode_update, 'latest_version', return_value='1.18.32'), \
             patch.object(opencode_update, 'install_managed') as install:
            result = opencode_update.run(check_only=True)
        self.assertEqual(result['state'], 'update_available')
        install.assert_not_called()

    def test_apply_defers_while_pool_is_busy(self):
        common.write_json(common.task_path('job-running'), {'id': 'job-running', 'created_at': 1,
                                                            'status': 'running'})
        with patch.object(opencode_update, 'current_version', return_value='1.18.30'), \
             patch.object(opencode_update, 'latest_version', return_value='1.18.32'), \
             patch.object(opencode_update, 'install_managed') as install:
            result = opencode_update.run()
        self.assertEqual(result['state'], 'waiting_for_idle')
        self.assertEqual(result['reason'], 'worker_tasks_active')
        install.assert_not_called()

    def test_apply_switches_binary_when_idle(self):
        versions = iter(['1.18.30', '1.18.32'])
        with patch.object(opencode_update, 'current_version', side_effect=lambda *a: next(versions)), \
             patch.object(opencode_update, 'latest_version', return_value='1.18.32'), \
             patch.object(opencode_update, 'busy_reason', return_value=None), \
             patch.object(opencode_update, 'install_managed', return_value='/managed/1.18.32/opencode'), \
             patch('service.reload_runtime', return_value={'ok': True}):
            result = opencode_update.run()
        self.assertEqual(result['state'], 'updated')
        self.assertEqual(json.loads(self.config.read_text())['opencode_binary'],
                         '/managed/1.18.32/opencode')
        self.assertFalse((self.state / 'maintenance.json').exists())


if __name__ == '__main__': unittest.main()
