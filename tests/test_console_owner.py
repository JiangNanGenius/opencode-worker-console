"""Console contract for configurations without deprecated global worker limits."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import console
import quota
import service
import bootstrap
import management


class ConsoleOwnerTests(unittest.TestCase):
    def test_services_do_not_inherit_starting_codex_conversation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / 'config.json'
            config_path.write_text(json.dumps({
                'opencode_binary': '/fake/opencode', 'console_url': 'http://127.0.0.1:1',
            }))
            with patch.dict(os.environ, {'CODEX_THREAD_ID': 'conversation-a'}), \
                 patch.object(common, 'CONFIG', config_path), \
                 patch.object(common, 'STATE', root), \
                 patch.object(service, 'STATE', root), \
                 patch.object(service, 'alive', return_value=False), \
                 patch.object(service, 'identity', return_value='test-process'), \
                 patch.object(service, 'api', return_value={'healthy': True}), \
                 patch.object(bootstrap, 'ensure_opencode', return_value='/fake/opencode'), \
                 patch.object(management, 'sync_worker_permissions'), \
                 patch.object(service.subprocess, 'Popen') as launch:
                launch.return_value.pid = 123
                service.start()
                self.assertEqual(launch.call_count, 3)
                for call in launch.call_args_list:
                    self.assertNotIn('CODEX_THREAD_ID', call.kwargs['env'])
                self.assertEqual(os.environ['CODEX_THREAD_ID'], 'conversation-a')

    def test_state_works_with_only_owner_limit_and_no_legacy_caps(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / 'config.json'
            config_path.write_text(json.dumps({
                'profiles': {'senior-code': {'model': 'example/model'}},
                'max_parallel_per_owner': 4, 'kimi_reserve_percent': 0,
                'max_steps': 80,
            }))
            with patch.object(common, 'CONFIG', config_path), \
                 patch.object(common, 'STATE', root), \
                 patch.object(console, 'STATE', root), \
                 patch.object(quota, 'STATE', root), \
                 patch.object(console, 'service_health', return_value={'pool': True, 'server': True}):
                result = console.state()
            self.assertEqual(result['max_parallel_per_owner'], 4)
            self.assertNotIn('max_parallel', result)
            self.assertNotIn('provider_limits', result)
            self.assertTrue(result['pool_healthy'])
            self.assertEqual(result['tasks'], [])
