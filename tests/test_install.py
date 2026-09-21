import io
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import console_auth
import install


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = self.root / 'src'
        self.install_dir = self.root / 'home' / '.codex' / 'skills' / 'delegate-opencode'
        self.state = self.root / 'state'
        self.config = self.root / 'config' / 'delegate-pool.json'
        self.home = self.root / 'home'
        self.home.mkdir()
        self.make_package(self.src)
        self.run_mock = patch('subprocess.run').start()
        self.addCleanup(patch.stopall)
        for target, value in [('ROOT', self.src), ('INSTALL', self.install_dir),
                              ('STATE', self.state), ('CONFIG', self.config)]:
            patch.object(install, target, value).start()
        patch.dict(os.environ, {'HOME': str(self.home)}).start()
        patch.object(install.shutil, 'which', return_value='/usr/bin/opencode').start()

    def tearDown(self):
        self.tmp.cleanup()

    def make_package(self, root):
        root.mkdir(parents=True, exist_ok=True)
        for name, text in [('SKILL.md', 'skill\n'), ('README.md', 'readme en\n'),
                           ('README.zh-CN.md', 'readme zh\n'), ('LICENSE', 'license\n'),
                           ('SECURITY.md', 'security\n'), ('CONTRIBUTING.md', 'contributing\n')]:
            (root / name).write_text(text)
        for name in ('agents', 'scripts', 'references', 'web'):
            (root / name).mkdir(exist_ok=True)
        (root / 'scripts' / 'service.py').write_text('# service\n')
        (root / 'scripts' / 'delegate.py').write_text('# delegate\n')
        (root / 'scripts' / 'common.py').write_text('# common\n')

    def run_install(self, *argv):
        output = io.StringIO()
        with redirect_stdout(output):
            install.main(list(argv))
        return output.getvalue()

    def started(self):
        return any('start' in call.args[0] for call in self.run_mock.call_args_list)

    def test_windows_unsupported_due_fcntl(self):
        with patch.object(install.sys, 'platform', 'win32'):
            with self.assertRaises(SystemExit) as error:
                install.main(['--model', 'acme/worker'])
        self.assertIn('fcntl', str(error.exception))

    def test_fresh_install_requires_explicit_model_choice(self):
        with self.assertRaises(SystemExit) as error:
            install.main([])
        self.assertIn('--model', str(error.exception))
        self.assertFalse(self.config.exists())
        self.assertFalse(self.run_mock.called)

    def test_model_and_preset_conflict(self):
        with self.assertRaises(SystemExit):
            install.main(['--model', 'acme/worker', '--preset', 'deepseek-kimi'])
        self.assertFalse(self.config.exists())

    def test_model_requires_provider_slash_form(self):
        with self.assertRaises(SystemExit) as error:
            install.main(['--model', 'no-provider-separator'])
        self.assertIn('provider/model', str(error.exception))

    def test_fresh_install_with_model_maps_all_profiles(self):
        out = self.run_install('--model', 'acme/worker-1')
        c = json.loads(self.config.read_text())
        for name in install.PROFILE_NAMES:
            self.assertEqual(c['profiles'][name], {'model': 'acme/worker-1', 'label': 'worker-1'})
        self.assertEqual(c['max_parallel_per_owner'], 4)
        self.assertEqual(c['kimi_reserve_percent'], 0)
        self.assertNotIn('max_steps', c)
        for legacy in ('max_parallel', 'max_kimi_parallel', 'provider_limits'):
            self.assertNotIn(legacy, c)
        self.assertEqual(c['opencode_binary'], '/usr/bin/opencode')
        self.assertTrue(c['server_url'].startswith('http://127.0.0.1:'))
        self.assertTrue(c['console_url'].startswith('http://127.0.0.1:'))
        self.assertNotEqual(c['server_url'], c['console_url'])
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        password = self.state / 'server-password'
        self.assertTrue(password.exists())
        self.assertEqual(stat.S_IMODE(password.stat().st_mode), 0o600)
        self.assertNotIn(password.read_text(), out)
        self.assertEqual((self.install_dir / 'SKILL.md').read_text(), 'skill\n')
        for guide in ('README.md', 'README.zh-CN.md', 'LICENSE', 'SECURITY.md', 'CONTRIBUTING.md'):
            self.assertEqual((self.install_dir / guide).read_text(), self.src.joinpath(guide).read_text())
        self.assertEqual((self.install_dir / 'scripts' / 'common.py').read_text(), '# common\n')
        wrapper = self.home / '.local' / 'bin' / 'delegate-opencode'
        self.assertTrue(wrapper.exists())
        self.assertIn('delegate.py', wrapper.read_text())
        self.assertNotIn('DELEGATE_', wrapper.read_text())
        self.assertTrue(self.started())
        summary = json.loads(out)
        self.assertEqual(summary['server_url'], c['server_url'])
        self.assertTrue(summary['fresh'])

    def test_preset_deepseek_kimi_keeps_known_mappings(self):
        self.run_install('--preset', 'deepseek-kimi')
        c = json.loads(self.config.read_text())
        self.assertEqual(c['profiles'], install.PRESETS['deepseek-kimi'])
        for spec in c['profiles'].values():
            self.assertEqual(spec['variant'], 'max')
            self.assertTrue(spec['label'])
        self.assertEqual(c['max_parallel_per_owner'], 4)
        self.assertEqual(c['kimi_reserve_percent'], 0)
        for legacy in ('max_parallel', 'max_kimi_parallel', 'provider_limits'):
            self.assertNotIn(legacy, c)

    def test_ark_agent_plan_preset_installs_complete_weighted_ladder(self):
        self.run_install('--preset', 'ark-agent-plan')
        c = json.loads(self.config.read_text())
        self.assertEqual(c['profiles'], install.PRESETS['ark-agent-plan'])
        self.assertEqual(c['routing_policy'], install.DEFAULT_ROUTING_POLICY)
        self.assertEqual(c['routing_dynamics'], install.DEFAULT_ROUTING_DYNAMICS)
        self.assertEqual(c['quota_spillover'], install.DEFAULT_QUOTA_SPILLOVER)
        self.assertEqual(c['routing_policy']['deep'][0], [
            {'profile': 'deep-research', 'weight': 2}, {'profile': 'ark-k3', 'weight': 1}])
        self.assertEqual(c['routing_policy']['background'][0], [
            {'profile': 'senior-code', 'weight': 1}, {'profile': 'ark-evolving', 'weight': 1}])
        self.assertEqual(c['routing_policy']['fast'][0], [{'profile': 'ark-auto', 'weight': 1}])
        self.assertNotIn('ark-deepseek', c['profiles'])

    def test_existing_config_unchanged_except_missing_defaults(self):
        existing = {'version': 1, 'server_url': 'http://127.0.0.1:41234',
                    'opencode_binary': '/old/opencode', 'max_parallel': 5, 'max_kimi_parallel': 2,
                    'kimi_reserve_percent': 10, 'profiles': {'fallback': {'model': 'acme/one', 'label': 'Mine'}}}
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(existing))
        self.run_install()
        c = json.loads(self.config.read_text())
        self.assertEqual(c['profiles'], existing['profiles'])
        # Legacy global/provider caps stay on disk but are inert; the new
        # per-owner cap defaults to 4 and custom reserve values are preserved.
        self.assertEqual(c['max_parallel'], 5)
        self.assertEqual(c['max_kimi_parallel'], 2)
        self.assertEqual(c['max_parallel_per_owner'], 4)
        self.assertEqual(c['kimi_reserve_percent'], 10)
        self.assertEqual(c['opencode_binary'], '/old/opencode')
        self.assertNotIn('max_steps', c)
        self.assertTrue(c['console_url'].startswith('http://127.0.0.1:'))
        self.assertNotEqual(c['console_url'], c['server_url'])

    def test_upgrade_renames_legacy_fast_code_profile_and_all_routes(self):
        existing = {
            'version': 1,
            'server_url': 'http://127.0.0.1:41234',
            'console_url': 'http://127.0.0.1:41235',
            'opencode_binary': '/old/opencode',
            'profiles': {
                'fast-code': {
                    'model': 'deepseek/deepseek-flash',
                    'label': 'DeepSeek V4.1 Flash',
                },
                'senior-code': {'model': 'acme/normal', 'label': 'Normal'},
            },
            'routing': {'fast': 'fast-code', 'background': 'senior-code', 'deep': 'fast-code'},
            'routing_policy': {
                'fast': [[{'profile': 'senior-code', 'weight': 1}],
                         [{'profile': 'fast-code', 'weight': 1}]],
            },
        }
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(existing))
        self.run_install()
        c = json.loads(self.config.read_text())
        self.assertNotIn('fast-code', c['profiles'])
        self.assertEqual(c['profiles']['fallback']['label'], 'Fallback · DeepSeek V4.1 Flash')
        self.assertEqual(c['routing']['fast'], 'fallback')
        self.assertEqual(c['routing']['deep'], 'fallback')
        self.assertEqual(c['routing_policy']['fast'][1][0]['profile'], 'fallback')

    def test_legacy_max_steps_is_dropped_on_update(self):
        existing = {'version': 1, 'server_url': 'http://127.0.0.1:41234',
                    'console_url': 'http://127.0.0.1:41235', 'opencode_binary': '/old/opencode',
                    'max_steps': 80, 'profiles': {'fallback': {'model': 'acme/one', 'label': 'Mine'}}}
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(existing))
        self.run_install()
        c = json.loads(self.config.read_text())
        self.assertNotIn('max_steps', c)
        self.assertEqual(c['profiles'], existing['profiles'])
        self.assertEqual(c['max_parallel_per_owner'], 4)

    def test_legacy_provider_limits_remained_inert_on_upgrade(self):
        existing = {'version': 1, 'server_url': 'http://127.0.0.1:41234',
                    'console_url': 'http://127.0.0.1:41235', 'opencode_binary': '/old/opencode',
                    'max_parallel': 2, 'provider_limits': {'kimi-for-coding': 1},
                    'profiles': {'fallback': {'model': 'kimi-for-coding/k2', 'label': 'Kimi'}}}
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(existing))
        self.run_install()
        c = json.loads(self.config.read_text())
        self.assertEqual(c['provider_limits'], {'kimi-for-coding': 1})
        self.assertEqual(c['max_parallel'], 2)
        self.assertEqual(c['max_parallel_per_owner'], 4)
        self.assertEqual(c['profiles'], existing['profiles'])

    def test_fresh_install_sets_console_network_defaults_and_admin_login(self):
        self.run_install('--model', 'acme/worker')
        c = json.loads(self.config.read_text())
        self.assertEqual(c['console_bind'], '127.0.0.1')
        self.assertEqual(c['console_allowed_origins'], [c['console_url']])
        self.assertEqual(c['console_trusted_proxies'], [])
        self.assertTrue(c['console_url'].startswith('http://127.0.0.1:'))
        account = self.state / 'console-auth' / 'account.json'
        self.assertTrue(account.exists())
        self.assertEqual(stat.S_IMODE(account.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(account.parent.stat().st_mode), 0o700)
        self.assertTrue(console_auth.verify_credentials('admin', 'admin', self.state))
        # A later install must not reset the provisioned credentials.
        first = account.read_bytes()
        self.run_install()
        self.assertEqual(account.read_bytes(), first)

    def test_existing_console_network_config_is_preserved(self):
        existing = {'version': 1, 'server_url': 'http://127.0.0.1:41234',
                    'console_url': 'http://127.0.0.1:41235', 'opencode_binary': '/old/opencode',
                    'console_bind': '0.0.0.0', 'console_allowed_origins': ['https://desk.example.test'],
                    'console_trusted_proxies': ['127.0.0.1', '10.0.0.0/8'],
                    'profiles': {'fallback': {'model': 'acme/one', 'label': 'Mine'}}}
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(existing))
        self.run_install()
        c = json.loads(self.config.read_text())
        self.assertEqual(c['console_bind'], '0.0.0.0')
        self.assertEqual(c['console_allowed_origins'], ['https://desk.example.test'])
        self.assertEqual(c['console_trusted_proxies'], ['127.0.0.1', '10.0.0.0/8'])
        # server_url and console_url remain pinned to loopback.
        self.assertTrue(c['server_url'].startswith('http://127.0.0.1:'))
        self.assertTrue(c['console_url'].startswith('http://127.0.0.1:'))

    def test_invalid_console_origin_refuses_install(self):
        existing = {'version': 1, 'server_url': 'http://127.0.0.1:41234',
                    'console_url': 'http://127.0.0.1:41235', 'opencode_binary': '/old/opencode',
                    'console_allowed_origins': ['https://*.example.test'],
                    'profiles': {'fallback': {'model': 'acme/one'}}}
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(existing))
        with self.assertRaises(SystemExit):
            install.main([])

    def test_active_task_refuses_update(self):
        tasks = self.state / 'tasks'
        tasks.mkdir(parents=True)
        (tasks / 'job-1.json').write_text(json.dumps({'id': 'job-1', 'status': 'running'}))
        with self.assertRaises(SystemExit) as error:
            install.main(['--model', 'acme/worker'])
        self.assertIn('Active workers', str(error.exception))
        self.assertFalse(self.config.exists())
        self.assertFalse(self.run_mock.called)

    def test_update_backs_up_stops_then_starts(self):
        self.run_install('--model', 'acme/worker')
        (self.install_dir / 'marker.txt').write_text('v2\n')
        before = len(self.run_mock.call_args_list)
        self.run_install('--model', 'acme/worker')
        backups = list((self.state / 'releases').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'marker.txt').read_text(), 'v2\n')
        calls = [' '.join(map(str, call.args[0])) for call in self.run_mock.call_args_list[before:]]
        stop_at = next(i for i, cmd in enumerate(calls) if ' stop' in cmd)
        start_at = next(i for i, cmd in enumerate(calls) if ' start' in cmd)
        self.assertLess(stop_at, start_at)

    def test_no_copy_recursion_when_source_equals_target(self):
        self.make_package(self.install_dir)
        with patch.object(install, 'ROOT', self.install_dir), \
                patch.object(install.shutil, 'copytree', wraps=shutil.copytree) as copytree:
            self.run_install('--model', 'acme/worker')
        self.assertEqual(copytree.call_count, 0)
        self.assertTrue(self.config.exists())
        self.assertTrue((self.home / '.local' / 'bin' / 'delegate-opencode').exists())

    def test_wrapper_exports_env_overrides(self):
        overrides = {'DELEGATE_CONFIG': '/iso lated/c.json', 'DELEGATE_STATE': '/isolated/state',
                     'DELEGATE_INSTALL': '/isolated/skill'}
        with patch.dict(os.environ, overrides):
            self.run_install('--model', 'acme/worker')
        wrapper = (self.home / '.local' / 'bin' / 'delegate-opencode').read_text()
        self.assertIn("export DELEGATE_CONFIG='/iso lated/c.json'", wrapper)
        self.assertIn('export DELEGATE_STATE=/isolated/state', wrapper)
        self.assertIn('export DELEGATE_INSTALL=/isolated/skill', wrapper)

    def test_no_start_skips_service_start(self):
        self.run_install('--model', 'acme/worker', '--no-start')
        self.assertFalse(self.started())
        self.assertTrue((self.home / '.local' / 'bin' / 'delegate-opencode').exists())
        self.assertTrue(self.config.exists())

    def test_stop_command_uses_installed_delegate(self):
        self.make_package(self.install_dir)
        self.run_mock.return_value.stdout = '{"services": "stopped"}'
        out = self.run_install('--stop')
        self.assertIn('stopped', out)
        self.assertIn('stop', self.run_mock.call_args.args[0])
        self.assertFalse(self.config.exists())

    def test_stop_requires_existing_deployment(self):
        with self.assertRaises(SystemExit):
            install.main(['--stop'])
        self.assertFalse(self.run_mock.called)

    def test_urls_must_be_distinct_loopback(self):
        base = {'version': 1, 'opencode_binary': '/x',
                'profiles': {'fallback': {'model': 'acme/worker'}}}
        for server, console in [('http://127.0.0.1:49999', 'http://127.0.0.1:49999'),
                                ('http://127.0.0.1:49999', 'http://example.com:49998'),
                                ('http://127.0.0.1:49999', 'https://127.0.0.1:49998')]:
            record = dict(base, server_url=server, console_url=console)
            self.config.parent.mkdir(parents=True, exist_ok=True)
            self.config.write_text(json.dumps(record))
            with self.assertRaises(SystemExit):
                install.main([])
            self.assertFalse(self.started())

    def test_opencode_detection_and_executable_check(self):
        with patch.object(install.shutil, 'which', return_value=None), patch('bootstrap.ensure_opencode', side_effect=RuntimeError('Unavailable')):
            with self.assertRaises(RuntimeError):
                install.main(['--model', 'acme/worker'])
        binary = self.root / 'bin' / 'opencode'
        binary.parent.mkdir()
        binary.write_text('#!/bin/sh\n')
        with self.assertRaises(SystemExit):
            install.main(['--model', 'acme/worker', '--opencode', str(binary)])
        binary.chmod(0o755)
        self.run_install('--model', 'acme/worker', '--opencode', str(binary))
        c = json.loads(self.config.read_text())
        self.assertEqual(c['opencode_binary'], str(binary.resolve()))

    def test_existing_configured_binary_reused_before_path_and_bootstrap(self):
        binary = self.root / 'custom' / 'opencode'
        binary.parent.mkdir()
        binary.write_text('#!/bin/sh\n')
        binary.chmod(0o755)
        existing = {'version': 1, 'server_url': 'http://127.0.0.1:41234',
                    'console_url': 'http://127.0.0.1:41235',
                    'opencode_binary': str(binary),
                    'profiles': {'fallback': {'model': 'acme/one', 'label': 'Mine'}}}
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(existing))
        # Neither PATH nor bootstrap may be consulted while the saved binary is valid.
        with patch.object(install.shutil, 'which', return_value=None) as which, \
                patch('bootstrap.ensure_opencode', side_effect=AssertionError('must not bootstrap')):
            self.run_install()
        self.assertFalse(which.called)
        c = json.loads(self.config.read_text())
        # The saved value is preserved (never rewritten on upgrade).
        self.assertEqual(c['opencode_binary'], str(binary))
        self.assertTrue(self.started())

    def test_invalid_saved_binary_falls_back_to_path(self):
        existing = {'version': 1, 'server_url': 'http://127.0.0.1:41234',
                    'console_url': 'http://127.0.0.1:41235',
                    'opencode_binary': str(self.root / 'gone' / 'opencode'),
                    'profiles': {'fallback': {'model': 'acme/one', 'label': 'Mine'}}}
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(existing))
        self.run_install()
        c = json.loads(self.config.read_text())
        # Invalid saved path falls back to PATH; the configured value is not clobbered.
        self.assertTrue(self.started())
        self.assertEqual(c['opencode_binary'], str(self.root / 'gone' / 'opencode'))

    def test_wizard_requires_explicit_language(self):
        with self.assertRaises(SystemExit) as error:
            install.main(['--wizard'])
        self.assertIn('--lang', str(error.exception))
        self.assertFalse(self.run_mock.called)
        with self.assertRaises(SystemExit) as error:
            install.main(['--lang', 'en'])
        self.assertIn('--wizard', str(error.exception))
        self.assertFalse(self.run_mock.called)

    def test_wizard_rejects_conflicting_flags(self):
        for argv in (['--wizard', '--lang', 'en', '--model', 'acme/worker'],
                     ['--wizard', '--lang', 'en', '--preset', 'deepseek-kimi'],
                     ['--wizard', '--lang', 'en', '--opencode', '/x/opencode'],
                     ['--wizard', '--lang', 'en', '--stop'],
                     ['--wizard', '--lang', 'en', '--live']):
            with self.assertRaises(SystemExit) as error:
                install.main(list(argv))
            self.assertIn('--wizard cannot be combined', str(error.exception))
        self.assertFalse(self.run_mock.called)
        self.assertFalse(self.config.exists())

    def test_wizard_rejects_unattended_invocation_before_changes(self):
        import setup_wizard
        with patch.object(setup_wizard.sys, 'stdin', io.StringIO()), \
                patch.object(setup_wizard.sys, 'stdout', io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                install.main(['--wizard', '--lang', 'en'])
        message = str(error.exception)
        self.assertIn('--model', message)
        self.assertIn('No changes were made', message)
        self.assertFalse(self.run_mock.called)
        self.assertFalse(self.config.exists())

    def test_wizard_delegates_with_language_and_no_start(self):
        import setup_wizard
        with patch.object(setup_wizard, 'run') as run_mock:
            install.main(['--wizard', '--lang', 'zh-CN', '--no-start'])
        run_mock.assert_called_once_with('zh-CN', no_start=True)

    def test_wizard_cancel_reports_exit_code_two(self):
        import setup_wizard
        with patch.object(setup_wizard, 'run', side_effect=setup_wizard.Cancelled('cancelled test')):
            with self.assertRaises(SystemExit) as error:
                install.main(['--wizard', '--lang', 'en'])
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(self.config.exists())


if __name__ == '__main__':
    unittest.main()
