import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import console_auth
import delegate
import install
import setup_wizard


class FakeTTY(io.StringIO):
    """A StringIO that looks like a terminal and feeds scripted input lines."""

    def __init__(self, lines=()):
        super().__init__()
        self._lines = list(lines)

    def isatty(self):
        return True

    def readline(self):
        if not self._lines:
            return ''  # EOF
        return self._lines.pop(0) + '\n'


class WizardTests(unittest.TestCase):
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
        (root / 'SKILL.md').write_text('skill\n')
        (root / 'README.md').write_text('# guide en\n')
        (root / 'README.zh-CN.md').write_text('# guide zh\n')
        (root / 'LICENSE').write_text('license\n')
        (root / 'SECURITY.md').write_text('security\n')
        (root / 'CONTRIBUTING.md').write_text('contributing\n')
        for name in ('agents', 'scripts', 'references', 'web'):
            (root / name).mkdir(exist_ok=True)
        (root / 'scripts' / 'service.py').write_text('# service\n')
        (root / 'scripts' / 'delegate.py').write_text('# delegate\n')
        (root / 'scripts' / 'common.py').write_text('# common\n')

    def run_wizard(self, lang, lines, no_start=False):
        stdout = FakeTTY()
        stdin = FakeTTY(lines)
        setup_wizard.run(lang, no_start=no_start, stdin=stdin, stdout=stdout)
        return stdout.getvalue()

    def assert_untouched(self):
        self.assertFalse(self.config.exists())
        self.assertFalse(self.install_dir.exists())
        self.assertFalse(self.run_mock.called)

    def existing_config(self):
        existing = {'version': 1, 'server_url': 'http://127.0.0.1:41234',
                    'console_url': 'http://127.0.0.1:41235', 'opencode_binary': '/old/opencode',
                    'max_parallel': 5, 'kimi_reserve_percent': 10,
                    'profiles': {'fast-code': {'model': 'acme/one', 'label': 'Mine'}}}
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(existing))
        return existing

    def test_non_tty_rejected_with_actionable_flags_before_any_changes(self):
        with self.assertRaises(SystemExit) as error:
            setup_wizard.run('en', stdin=io.StringIO(), stdout=FakeTTY())
        message = str(error.exception)
        self.assertIn('--model', message)
        self.assertIn('--preset', message)
        self.assertIn('No changes were made', message)
        self.assert_untouched()
        with self.assertRaises(SystemExit):
            setup_wizard.run('zh-CN', stdin=FakeTTY(), stdout=io.StringIO())
        self.assert_untouched()

    def test_fresh_install_choose_preset_completes(self):
        out = self.run_wizard('en', ['1', '', 'n', 'y', 'y'])
        c = json.loads(self.config.read_text())
        self.assertEqual(c['profiles'], install.PRESETS['deepseek-kimi'])
        self.assertEqual(c['opencode_binary'], '/usr/bin/opencode')
        wrapper = self.home / '.local' / 'bin' / 'delegate-opencode'
        self.assertTrue(wrapper.exists())
        self.assertTrue(any('start' in call.args[0] for call in self.run_mock.call_args_list))
        self.assertIn('setup wizard', out)
        self.assertIn('Review the installation plan', out)
        # The resulting command and console URL are reported from the installer JSON.
        self.assertIn('Command: ' + str(wrapper), out)
        self.assertIn('Console: http://127.0.0.1:', out)
        self.assertIn('"command"', out)
        self.assertIn('"console_url"', out)
        self.assertIn('opencode auth login', out)
        # Guides ship with the runtime so ../README.md links stay valid.
        self.assertEqual((self.install_dir / 'README.md').read_text(), '# guide en\n')
        self.assertEqual((self.install_dir / 'README.zh-CN.md').read_text(), '# guide zh\n')
        self.assertEqual((self.install_dir / 'LICENSE').read_text(), 'license\n')
        self.assertEqual((self.install_dir / 'SECURITY.md').read_text(), 'security\n')
        self.assertEqual((self.install_dir / 'CONTRIBUTING.md').read_text(), 'contributing\n')
        # Password skipped: the post-install reminder documents the local
        # admin/admin default exactly once.
        self.assertEqual(out.count('Password setup skipped'), 1)
        self.assertEqual(out.count('admin/admin'), 1)
        self.assertTrue(console_auth.verify_credentials('admin', 'admin', self.state))

    def test_fresh_install_choose_ark_agent_plan_preset(self):
        self.run_wizard('en', ['3', '', 'n', 'y', 'y'])
        c = json.loads(self.config.read_text())
        self.assertEqual(c['profiles'], install.PRESETS['ark-agent-plan'])
        self.assertEqual(c['routing_policy'], install.DEFAULT_ROUTING_POLICY)

    def test_fresh_install_custom_model_validates_and_retries(self):
        out = self.run_wizard('en', ['2', 'not a provider!', 'x', 'acme', 'worker-7', '', 'n', 'y', 'y'])
        self.assertIn('Invalid model choice', out)
        c = json.loads(self.config.read_text())
        for name in install.PROFILE_NAMES:
            self.assertEqual(c['profiles'][name], {'model': 'acme/worker-7', 'label': 'worker-7'})

    def test_fresh_install_validates_opencode_path(self):
        out = self.run_wizard('en', ['1', str(self.root / 'missing'), '', 'n', 'y', 'y'])
        self.assertIn('not an executable file', out)
        binary = self.root / 'bin' / 'opencode'
        binary.parent.mkdir(exist_ok=True)
        binary.write_text('#!/bin/sh\n')
        binary.chmod(0o755)
        self.config.unlink()
        shutil.rmtree(self.install_dir)
        self.run_mock.reset_mock()
        self.run_wizard('en', ['1', str(binary), 'n', 'y', 'y'])
        c = json.loads(self.config.read_text())
        self.assertEqual(c['opencode_binary'], str(binary.resolve()))

    def test_cancel_at_model_choice_makes_no_changes(self):
        with self.assertRaises(setup_wizard.Cancelled) as error:
            self.run_wizard('en', ['4'])
        self.assertIn('no changes', str(error.exception))
        self.assert_untouched()

    def test_eof_cancels_without_changes(self):
        with self.assertRaises(setup_wizard.Cancelled):
            self.run_wizard('en', [])
        self.assert_untouched()

    def test_declining_review_makes_no_changes_or_downloads(self):
        with self.assertRaises(setup_wizard.Cancelled):
            self.run_wizard('en', ['1', '', 'n', 'y', 'n'])
        self.assert_untouched()

    def test_existing_install_keep_current_upgrades_and_preserves_everything(self):
        existing = self.existing_config()
        out = self.run_wizard('en', ['1', 'y', 'y'])
        c = json.loads(self.config.read_text())
        self.assertEqual(c['profiles'], existing['profiles'])
        self.assertEqual(c['opencode_binary'], '/old/opencode')
        self.assertEqual(c['max_parallel'], 5)
        self.assertEqual(c['kimi_reserve_percent'], 10)
        self.assertIn('keep the existing configuration', out)
        self.assertIn('web console (Models & routing)', out)

    def test_existing_install_has_no_replace_option_and_never_changes_models(self):
        existing = self.existing_config()
        # Option 2 used to be "replace models"; it must now cancel instead, and
        # model changes route to the web console.
        with self.assertRaises(setup_wizard.Cancelled):
            self.run_wizard('en', ['2'])
        self.assertEqual(json.loads(self.config.read_text()), existing)
        self.assertFalse(self.install_dir.exists())
        self.assertFalse(self.run_mock.called)
        # Cancelling leaves profiles intact even after a prior upgrade.
        out = self.run_wizard('en', ['1', 'y', 'y'])
        self.assertEqual(json.loads(self.config.read_text())['profiles'], existing['profiles'])
        self.assertIn('web console (Models & routing)', out)

    def test_existing_install_never_offers_password_reset(self):
        self.existing_config()
        out = self.run_wizard('en', ['1', 'y', 'y'])
        self.assertIn('never resets', out)
        self.assertNotIn('Repeat the password', out)

    def test_password_set_via_getpass_and_never_printed(self):
        stdout = FakeTTY()
        with patch.object(setup_wizard.getpass, 'getpass', side_effect=['s3cret-pw', 's3cret-pw']) as gp:
            setup_wizard.run('en', stdin=FakeTTY(['1', '', 'y', 'y', 'y']), stdout=stdout)
        out = stdout.getvalue()
        self.assertEqual(gp.call_count, 2)
        self.assertNotIn('s3cret-pw', out)
        self.assertTrue(console_auth.verify_credentials('admin', 's3cret-pw', self.state))
        self.assertFalse(console_auth.verify_credentials('admin', 'admin', self.state))
        self.assertIn('password updated', out)
        # No admin/admin reminder when a password was actually set.
        self.assertNotIn('Password setup skipped', out)

    def test_password_mismatch_then_skip_falls_back_to_default(self):
        stdout = FakeTTY()
        with patch.object(setup_wizard.getpass, 'getpass',
                          side_effect=['first-pw', 'other-pw', '']):
            setup_wizard.run('en', stdin=FakeTTY(['1', '', 'y', 'y', 'y']), stdout=stdout)
        out = stdout.getvalue()
        self.assertIn('do not match', out)
        # Blank-after-mismatch is also a skip path: reminder shown once post-install.
        self.assertEqual(out.count('Password setup skipped'), 1)
        self.assertTrue(console_auth.verify_credentials('admin', 'admin', self.state))

    def test_password_never_passed_to_installer_argv(self):
        with patch.object(setup_wizard.install, 'main') as main_mock:
            with patch.object(setup_wizard.console_auth, 'set_user') as set_user:
                stdout = FakeTTY()
                with patch.object(setup_wizard.getpass, 'getpass', side_effect=['s3cret-pw', 's3cret-pw']):
                    setup_wizard.run('en', stdin=FakeTTY(['1', '', 'y', 'y', 'y']), stdout=stdout)
        argv = main_mock.call_args.args[0]
        self.assertIn('--preset', argv)
        self.assertNotIn('s3cret-pw', json.dumps(argv))
        self.assertNotIn('s3cret-pw', stdout.getvalue())
        set_user.assert_called_once_with('admin', 's3cret-pw', self.state)

    def test_no_start_flag_skips_launch_question(self):
        out = self.run_wizard('en', ['1', '', 'n', 'y'], no_start=True)
        self.assertIn('do not start (--no-start)', out)
        self.assertFalse(any('start' in call.args[0] for call in self.run_mock.call_args_list))
        self.assertTrue(self.config.exists())

    def test_chinese_prompts_full_flow(self):
        stdout = FakeTTY()
        setup_wizard.run('zh-CN', stdin=FakeTTY(['1', '', 'n', 'y', 'y']), stdout=stdout)
        out = stdout.getvalue()
        self.assertIn('安装向导', out)
        self.assertIn('请确认安装计划', out)
        self.assertIn('安装完成', out)
        self.assertIn('提供商登录由 OpenCode 管理', out)
        self.assertIn('命令：', out)
        self.assertTrue(self.config.exists())

    def test_chinese_existing_upgrade_mentions_web_console(self):
        self.existing_config()
        out = self.run_wizard('zh-CN', ['1', 'y', 'y'])
        self.assertIn('网页控制台（模型与调度）', out)

    def test_chinese_cancel_message(self):
        with self.assertRaises(setup_wizard.Cancelled) as error:
            self.run_wizard('zh-CN', [])
        self.assertIn('未做任何更改', str(error.exception))

    def test_unsupported_language_refused(self):
        with self.assertRaises(SystemExit):
            setup_wizard.run('fr', stdin=FakeTTY(['1']), stdout=FakeTTY())

    def test_existing_unreadable_config_refused_before_changes(self):
        self.config.parent.mkdir(parents=True)
        self.config.write_text('{ not json')
        with self.assertRaises(SystemExit) as error:
            self.run_wizard('en', [])
        self.assertIn('not valid JSON', str(error.exception))
        self.assertFalse(self.install_dir.exists())
        self.assertFalse(self.run_mock.called)


class DelegateSetupCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_documents_installed_wizard_path_without_tty(self):
        with patch.object(delegate.sys, 'stdin', io.StringIO()), \
                patch.object(delegate.sys, 'stdout', io.StringIO()), \
                patch.object(delegate.subprocess, 'call') as call_mock:
            result = delegate.setup_wizard_command('zh-CN')
        self.assertFalse(call_mock.called)
        self.assertIn('--wizard --lang zh-CN', result['command'])
        self.assertTrue(result['command'].split()[1].endswith('scripts/install.py'))
        self.assertIn('--model provider/model', result['noninteractive'])
        self.assertIn('no changes were made', result['setup'])

    def test_opens_installed_wizard_on_tty(self):
        with patch.object(delegate.sys, 'stdin', FakeTTY()), \
                patch.object(delegate.sys, 'stdout', FakeTTY()), \
                patch.dict(os.environ, {'LC_ALL': 'zh_CN.UTF-8'}), \
                patch.object(delegate.subprocess, 'call', return_value=0) as call_mock:
            result = delegate.setup_wizard_command()
        self.assertEqual(result, {'wizard': 'completed', 'lang': 'zh-CN', 'exit_code': 0})
        args = call_mock.call_args.args[0]
        self.assertEqual(args[0], sys.executable)
        self.assertTrue(args[1].endswith('scripts/install.py'))
        self.assertEqual(args[2:], ['--wizard', '--lang', 'zh-CN'])

    def test_wizard_abortion_reported(self):
        with patch.object(delegate.sys, 'stdin', FakeTTY()), \
                patch.object(delegate.sys, 'stdout', FakeTTY()), \
                patch.object(delegate.subprocess, 'call', return_value=2):
            result = delegate.setup_wizard_command('en')
        self.assertEqual(result['wizard'], 'cancelled')
        self.assertEqual(result['exit_code'], 2)

    def test_setup_propagates_child_failure_and_cancellation(self):
        for code, status in ((0, 'completed'), (1, 'failed'), (2, 'cancelled')):
            with self.subTest(code=code), \
                    patch.object(delegate.sys, 'stdin', FakeTTY()), \
                    patch.object(delegate.sys, 'stdout', FakeTTY()) as output, \
                    patch.object(delegate.sys, 'argv', ['delegate.py', 'setup', '--lang', 'en']), \
                    patch.object(delegate.subprocess, 'call', return_value=code), \
                    patch.object(delegate, 'init') as initialize:
                self.assertEqual(delegate.main(), code)
                self.assertEqual(json.loads(output.getvalue())['wizard'], status)
                initialize.assert_not_called()

    def test_lang_parameter_wins_over_locale(self):
        with patch.object(delegate.sys, 'stdin', io.StringIO()), \
                patch.object(delegate.sys, 'stdout', io.StringIO()), \
                patch.dict(os.environ, {'LC_ALL': 'zh_CN.UTF-8'}):
            result = delegate.setup_wizard_command('en')
        self.assertIn('--lang en', result['command'])

    def test_delegate_main_setup_before_init_creates_no_state_dirs(self):
        # Non-TTY `delegate-opencode setup` must not create any state directories.
        state = Path(self.tmp.name) / 'unused-state'
        buffer = io.StringIO()
        with patch.object(common, 'STATE', state), \
                patch.object(delegate, 'STATE', state), \
                patch.object(delegate.sys, 'stdin', io.StringIO()), \
                patch.object(delegate.sys, 'stdout', buffer), \
                patch.object(delegate.sys, 'argv', ['delegate.py', 'setup']), \
                patch.object(delegate.subprocess, 'call') as call_mock:
            delegate.main()
        self.assertFalse(call_mock.called)
        self.assertFalse(state.exists())
        result = json.loads(buffer.getvalue())
        self.assertIn('--wizard', result['command'])


if __name__ == '__main__':
    unittest.main()
