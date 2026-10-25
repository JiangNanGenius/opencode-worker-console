"""Isolated auth runtime selection and prepare-before-stop failure boundaries."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import console_runtime as runtime
import install
import service


ENABLED = {'console_passkey': {'enabled': True}}


class RuntimeTests(unittest.TestCase):
    def test_legacy_does_not_require_uv_or_managed_environment(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(runtime, 'smoke') as smoke:
            selected = runtime.interpreters({}, Path(temp))
            self.assertEqual(selected['worker_python'], sys.executable)
            self.assertEqual(selected['console_python'], sys.executable)
            smoke.assert_not_called()
            self.assertEqual(list(Path(temp).iterdir()), [])
        for value in ({}, {'console_passkey': True}, {'console_passkey': {'enabled': 'true'}}):
            self.assertFalse(runtime.passkey_enabled(value))

    def test_missing_or_stale_runtime_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            with self.assertRaisesRegex(RuntimeError, 'missing or stale'):
                runtime.interpreters(ENABLED, state)
            (state / 'runtime').mkdir()
            runtime._write(state / 'runtime' / 'interpreters.json', {
                'worker_python': sys.executable, 'lock_sha256': 'old',
                'console_python': '/some/old/venv/bin/python'})
            with self.assertRaisesRegex(RuntimeError, 'missing or stale'):
                runtime.interpreters(ENABLED, state)

    def test_registry_keeps_workers_pinned_when_caller_is_console(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            (state / 'runtime').mkdir()
            digest = runtime.lock_digest()
            console = state / 'runtime' / 'console-auth' / digest / 'venv' / 'bin' / 'python'
            original = sys.executable
            runtime._write(state / 'runtime' / 'interpreters.json', {
                'worker_python': original, 'lock_sha256': digest, 'console_python': str(console)})
            with patch.object(runtime.sys, 'executable', str(console)), \
                 patch.object(runtime, 'smoke', return_value={'webauthn': '3.0.1'}):
                selected = runtime.interpreters(ENABLED, state)
                self.assertEqual(selected, {'worker_python': original, 'console_python': str(console)})
                # Disabling passkeys from a 3.12 caller also retains the worker pin.
                self.assertEqual(runtime.interpreters({}, state)['console_python'], original)
            with patch.object(runtime, 'smoke', side_effect=subprocess.CalledProcessError(1, 'python')):
                with self.assertRaisesRegex(RuntimeError, 'failed verification'):
                    runtime.interpreters(ENABLED, state)

    def test_failed_dependency_install_keeps_previous_registry(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            root = state / 'runtime'
            root.mkdir()
            previous = {'worker_python': sys.executable, 'console_python': '/old/runtime/python',
                        'lock_sha256': 'previous'}
            registry = root / 'interpreters.json'
            runtime._write(registry, previous)
            calls = []
            def run(argv, **kwargs):
                calls.append((argv, kwargs))
                if 'sync' in argv:
                    raise subprocess.CalledProcessError(1, argv)
                return types.SimpleNamespace(returncode=0)
            with patch.object(runtime, '_version', return_value=(3, 12)), \
                 patch.object(runtime.subprocess, 'run', side_effect=run):
                with self.assertRaises(subprocess.CalledProcessError):
                    runtime.prepare_runtime(state, python=sys.executable, uv=sys.executable)
            self.assertEqual(json.loads(registry.read_text()), previous)
            self.assertFalse((root / 'console-auth' / runtime.lock_digest()).exists())
            sync, options = calls[-1]
            self.assertIn('--require-hashes', sync)
            self.assertIn('--only-binary', sync)
            self.assertEqual(options['env']['UV_PYTHON_DOWNLOADS'], 'never')

    def test_prepare_publishes_only_after_smoke_and_reuses_ready_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            registry = state / 'runtime' / 'interpreters.json'
            def smoke(*args):
                self.assertFalse(registry.exists())
                return {'webauthn': '3.0.1'}
            with patch.object(runtime, '_version', return_value=(3, 12)), \
                 patch.object(runtime.subprocess, 'run'), \
                 patch.object(runtime, 'smoke', side_effect=smoke):
                result = runtime.prepare_runtime(state, python=sys.executable, uv=sys.executable)
            self.assertEqual(json.loads(registry.read_text()), result)
            self.assertEqual(registry.stat().st_mode & 0o777, 0o600)
            with patch.object(runtime, '_version', return_value=(3, 12)), \
                 patch.object(runtime.subprocess, 'run') as run, \
                 patch.object(runtime, 'smoke', return_value={'webauthn': '3.0.1'}):
                self.assertEqual(runtime.prepare_runtime(state, python=sys.executable), result)
                run.assert_not_called()

    def test_service_start_uses_pinned_worker_for_server_and_pool(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            (state / 'logs').mkdir()
            c = dict(ENABLED, opencode_binary='/fake/opencode', console_url='http://localhost:51528')
            selected = {'worker_python': '/original/python3.9', 'console_python': '/isolated/python3.12'}
            bootstrap = types.SimpleNamespace(ensure_opencode=lambda path: path)
            management = types.SimpleNamespace(sync_worker_permissions=lambda: None)
            with patch.object(service, 'STATE', state), patch.object(service, 'init'), \
                 patch.object(service, 'config', return_value=c), \
                 patch.object(service, 'identity', return_value='identity'), \
                 patch.object(service, 'api', return_value={'healthy': True}), \
                 patch.object(service, 'alive', return_value=False), \
                 patch.object(runtime, 'interpreters', return_value=selected), \
                 patch.object(service.sys, 'executable', '/isolated/python3.12'), \
                 patch.object(service.subprocess, 'Popen') as launch, \
                 patch.dict(sys.modules, {'bootstrap': bootstrap, 'management': management}):
                launch.return_value.pid = 42
                service.start()
            self.assertEqual([call.args[0][0] for call in launch.call_args_list],
                             ['/original/python3.9', '/original/python3.9', '/isolated/python3.12'])

    def test_installer_runtime_failure_does_not_stop_or_copy_deployment(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            with patch.object(install, 'STATE', state), \
                 patch.object(install, 'CONFIG', state / 'config.json'), \
                 patch.object(install, 'check_platform'), patch.object(install, 'detect_opencode'), \
                 patch.object(install, 'refuse_active_tasks'), \
                 patch.object(install, 'load_or_build_config', return_value=(ENABLED, False)), \
                 patch.object(install, 'ensure_password'), \
                 patch('console_auth.ensure_default_account'), \
                 patch.object(runtime, 'prepare_runtime', side_effect=RuntimeError('dependency failure')), \
                 patch.object(install, 'stop_installed_services') as stop, \
                 patch.object(install, 'copy_package') as copy, \
                 patch.object(install, 'backup_existing') as backup, \
                 redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'dependency failure'):
                    install.main(['--no-start'])
            stop.assert_not_called()
            copy.assert_not_called()
            backup.assert_not_called()


if __name__ == '__main__':
    unittest.main()
