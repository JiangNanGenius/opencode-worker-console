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
import console_auth
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

    def test_state_and_settings_do_not_expose_network_or_auth_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 'state'
            state.mkdir()
            config_path = root / 'config.json'
            config_path.write_text(json.dumps({
                'profiles': {'senior-code': {'model': 'example/model'}},
                'server_url': 'http://127.0.0.1:1', 'console_url': 'http://127.0.0.1:2',
                'console_bind': '0.0.0.0', 'console_allowed_origins': ['https://desk.example.test'],
                'max_parallel_per_owner': 4, 'kimi_reserve_percent': 0, 'max_steps': 80,
            }))
            console_auth.set_user('admin', 'synthetic-secret', state)
            with patch.object(common, 'CONFIG', config_path), \
                 patch.object(common, 'STATE', state), \
                 patch.object(console, 'STATE', state), \
                 patch.object(quota, 'STATE', state), \
                 patch.object(console, 'service_health', return_value={'pool': True, 'server': True}):
                blob = json.dumps({'state': console.state(), 'settings': management.settings()})
            for secret in ('console_bind', 'console_allowed_origins', 'desk.example.test',
                           'synthetic-secret', 'console-auth', 'pbkdf2', 'salt', 'hash'):
                self.assertNotIn(secret, blob)

    def test_console_settings_and_ui_have_no_step_or_time_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / 'config.json'
            config_path.write_text(json.dumps({
                'profiles': {'senior-code': {'model': 'example/model'}},
                'max_parallel_per_owner': 4, 'kimi_reserve_percent': 0, 'max_steps': 80,
            }))
            with patch.object(common, 'CONFIG', config_path):
                result = management.settings()
        # A legacy max_steps: 80 on disk is neither exported nor defaulted back.
        self.assertNotIn('max_steps', result)
        self.assertNotIn('max_steps', json.dumps(result))
        project = Path(__file__).resolve().parents[1]
        index = (project / 'web' / 'index.html').read_text()
        manage = (project / 'web' / 'manage.js').read_text()
        i18n = (project / 'web' / 'i18n.js').read_text()
        for source in (index, manage):
            self.assertNotIn('max-steps', source)
            self.assertNotIn('max_steps', source)
            self.assertNotIn('maxSteps', source)
            self.assertNotIn('timeout', source.lower())
            self.assertNotIn('deadline', source.lower())
        self.assertNotIn('routing.maxSteps', i18n)
        self.assertNotIn('timeout', i18n.lower())

    def test_console_exposes_safe_task_management_and_resilient_rendering(self):
        project = Path(__file__).resolve().parents[1]
        index = (project / 'web' / 'index.html').read_text()
        app = (project / 'web' / 'app.js').read_text()
        server = (project / 'scripts' / 'console.py').read_text()
        self.assertIn('/console-api/tasks/manage', server)
        self.assertIn('id="select-visible-tasks"', index)
        self.assertIn('id="delete-selected-tasks"', index)
        self.assertIn('id="clear-completed-tasks"', index)
        self.assertIn("action:'clear_finished'", app)
        self.assertNotIn('id="economics-card"', index)
        self.assertNotIn('renderEconomics', app)
        self.assertIn("['tasks',renderTasks]", app)
        self.assertLess(app.index("['tasks',renderTasks]"), app.index("['quota',renderQuota]"))
        self.assertIn("if(name==='tasks')", app)
        # The detail panel must stay in the document until a task expands; moving it
        # into a detached row during startup made later getElementById calls return null.
        setup = app[app.index("const detailPanel = $('detail')"):app.index('function renderTasks')]
        self.assertNotIn('inlineDetailCell.append(detailPanel)', setup)
        self.assertIn('if(expanded){inlineDetailCell.append(detailPanel)', app)
