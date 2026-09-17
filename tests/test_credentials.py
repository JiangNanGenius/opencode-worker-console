import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import credentials
import delegate
import diagnostics
import steering
import transcript


CLI = Path(__file__).resolve().parents[1] / 'scripts' / 'delegate.py'
CHILD = [sys.executable, '-c']


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / 'state'
        self.config = self.root / 'config.json'
        self.patches = [
            patch.object(common, 'STATE', self.state),
            patch.object(common, 'CONFIG', self.config),
            patch.object(delegate, 'STATE', self.state),
            patch.object(delegate, 'CONFIG', self.config),
            patch.dict(os.environ, {'XDG_DATA_HOME': str(self.root / 'data')}),
        ]
        for item in self.patches:
            item.start()
        common.init()
        common.write_json(self.config, {'profiles': {}})
        self.secret = 'synthetic-secret-value-123'
        self.secret_file = self.root / 'private' / 'secret.txt'
        self.secret_file.parent.mkdir(mode=0o700)
        self._write_secret(self.secret)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def _write_secret(self, value, path=None):
        path = Path(path or self.secret_file)
        path.write_text(value)
        path.chmod(0o600)
        return path

    def cli(self, *args, timeout=60, env=None):
        environment = dict(os.environ, DELEGATE_STATE=str(self.state),
                          DELEGATE_CONFIG=str(self.config))
        if env:
            environment.update(env)
        return subprocess.run([sys.executable, str(CLI), *args], env=environment,
                              capture_output=True, text=True, timeout=timeout)

    # Registration, listing, removal, and metadata safety ---------------------

    def test_register_stores_metadata_only_and_list_is_safe(self):
        run = self.cli('credential', 'register', 'demo', '--file', str(self.secret_file))
        self.assertEqual(run.returncode, 0, run.stderr)
        registered = json.loads(run.stdout)
        self.assertEqual(registered['name'], 'demo')
        self.assertEqual(registered['source'], 'file')
        self.assertNotIn(self.secret, run.stdout)
        registry = (self.state / 'credentials' / 'registry.json').read_text()
        self.assertNotIn(self.secret, registry)
        self.assertEqual(json.loads(registry)['credentials']['demo']['path'], str(self.secret_file))

        listed = self.cli('credential', 'list')
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertNotIn(self.secret, listed.stdout)
        entry = json.loads(listed.stdout)['credentials'][0]
        self.assertEqual((entry['name'], entry['source'], entry['available']), ('demo', 'file', True))

        removed = self.cli('credential', 'remove', 'demo')
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(json.loads(removed.stdout)['removed'], 'demo')
        self.assertFalse((self.state / 'credentials' / 'registry.json').read_text().count(self.secret))
        self.assertEqual(self.secret_file.read_text(), self.secret)
        self.assertEqual(json.loads(self.cli('credential', 'list').stdout)['credentials'], [])

    def test_register_rejects_unsafe_file_references(self):
        cases = []
        cases.append(str(self.root / 'does-not-exist.txt'))
        cases.append(str(self.root))
        loose = self._write_secret('loose-value-123', self.root / 'loose.txt')
        loose.chmod(0o644)
        cases.append(str(loose))
        empty = self._write_secret('', self.root / 'empty.txt')
        cases.append(str(empty))
        big = self.root / 'big.txt'
        big.write_text('x' * (credentials.MAX_CREDENTIAL_BYTES + 1))
        big.chmod(0o600)
        cases.append(str(big))
        for path in cases:
            with self.assertRaises(credentials.CredentialError):
                credentials.register('bad', file=path)
        link = self.root / 'link.txt'
        link.symlink_to(self.secret_file)
        with self.assertRaises(credentials.CredentialError):
            credentials.register('bad', file=str(link))
        with self.assertRaises(credentials.CredentialError):
            credentials.register('bad', file='relative/path.txt')
        with self.assertRaises(credentials.CredentialError):
            credentials.register('bad-name!', file=str(self.secret_file))
        with self.assertRaises(credentials.CredentialError):
            credentials.register('both', file=str(self.secret_file), env='A')
        with self.assertRaises(credentials.CredentialError):
            credentials.register('neither')

    def test_register_rejects_git_tracked_file(self):
        try:
            subprocess.run(['git', '--version'], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True)
        except (OSError, subprocess.SubprocessError):
            self.skipTest('git is unavailable')
        repo = self.root / 'repo'
        repo.mkdir()
        for args in (['init', '-q'], ['config', 'user.name', 'Test'],
                     ['config', 'user.email', 'test@example.invalid']):
            subprocess.run(['git', '-C', str(repo), *args], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tracked = self._write_secret('tracked-value-123', repo / 'tracked.txt')
        subprocess.run(['git', '-C', str(repo), 'add', 'tracked.txt'], check=True)
        subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'fixture'], check=True)
        with self.assertRaises(credentials.CredentialError):
            credentials.register('tracked', file=str(tracked))
        untracked = self._write_secret('untracked-value-123', repo / 'untracked.txt')
        credentials.register('untracked', file=str(untracked))

    def test_env_reference_is_metadata_and_resolves_in_process(self):
        with patch.dict(os.environ, {'DELEGATE_TEST_SECRET': 'synthetic-env-value-123'}):
            entry = credentials.register('envdemo', env='DELEGATE_TEST_SECRET')
            self.assertEqual(entry['source'], 'env')
            self.assertTrue(entry['available'])
            self.assertNotIn('synthetic-env-value-123', json.dumps(entry))
            code, result = credentials.run(['INJECTED=envdemo'], 10,
                                           CHILD + ['import os; print(os.environ["INJECTED"])'])
            self.assertEqual(code, 0)
            self.assertNotIn('synthetic-env-value-123', json.dumps(result))
            self.assertIn('<redacted>', result['stdout'])
        os.environ.pop('DELEGATE_TEST_SECRET', None)
        self.assertFalse(credentials.listing()['credentials'][0]['available'])
        with self.assertRaises(credentials.CredentialError):
            credentials.run(['INJECTED=envdemo'], 10, CHILD + ['pass'])

    def test_register_env_does_not_require_present_value(self):
        entry = credentials.register('latervar', env='DELEGATE_LATER_SECRET')
        self.assertFalse(entry['available'])

    # Runner semantics -------------------------------------------------------

    def test_run_success_redacts_literal_and_encodings(self):
        special = 'sp ace"q\\b+slash/value'
        self._write_secret(special, self.root / 'special.txt')
        credentials.register('special', file=str(self.root / 'special.txt'))
        script = (
            'import os, base64, json, urllib.parse, sys\n'
            's = os.environ["INJECTED"]\n'
            'forms = [s, urllib.parse.quote(s, safe=""), json.dumps(s)[1:-1],\n'
            '         base64.b64encode(s.encode()).decode(),\n'
            '         base64.urlsafe_b64encode(s.encode()).decode()]\n'
            'sys.stdout.write(" ".join(forms))\n'
            'sys.stderr.write("stderr " + s)\n'
        )
        code, result = credentials.run(['INJECTED=special'], 20, CHILD + [script])
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['exit_code'], 0)
        self.assertFalse(result['output_suppressed'])
        forms = [special, urllib.parse.quote(special, safe=''),
                 json.dumps(special)[1:-1], base64.b64encode(special.encode()).decode(),
                 base64.urlsafe_b64encode(special.encode()).decode()]
        for form in forms:
            self.assertNotIn(form, result['stdout'])
            self.assertNotIn(form, result['stderr'])
        self.assertIn('<redacted>', result['stdout'])
        self.assertIn('<redacted>', result['stderr'])
        self.assertNotIn(special, os.environ.values())

    def test_run_redacts_basic_credential_shape(self):
        basic_secret = 'user:pass-value-123'
        self._write_secret(basic_secret, self.root / 'basic.txt')
        credentials.register('basic', file=str(self.root / 'basic.txt'))
        encoded = base64.b64encode(basic_secret.encode()).decode()
        code, result = credentials.run(['INJECTED=basic'], 20,
                                       CHILD + ['import os; print(os.environ["INJECTED"])'])
        self.assertEqual(code, 0)
        self.assertNotIn(basic_secret, json.dumps(result))
        _, basic_result = credentials.run(['INJECTED=basic'], 20,
                                          CHILD + ['import base64, os; print(base64.b64encode(os.environ["INJECTED"].encode()).decode())'])
        self.assertNotIn(encoded, json.dumps(basic_result))

    def test_run_short_secret_and_split_writes_are_redacted(self):
        self._write_secret('pw', self.root / 'short.txt')
        credentials.register('short', file=str(self.root / 'short.txt'))
        code, result = credentials.run(['INJECTED=short'], 20, CHILD + ['import os; print(os.environ["INJECTED"])'])
        self.assertEqual(code, 0)
        self.assertNotIn('pw', result['stdout'])
        script = (
            'import os, sys, time\n'
            'half = len(os.environ["INJECTED"]) // 2\n'
            's = os.environ["INJECTED"]\n'
            'sys.stdout.write(s[:half]); sys.stdout.flush(); time.sleep(0.3)\n'
            'sys.stdout.write(s[half:])\n'
        )
        code, split = credentials.run(['INJECTED=short'], 20, CHILD + [script])
        self.assertEqual(code, 0)
        self.assertNotIn('pw', split['stdout'])

    def test_run_preserves_nonzero_exit_and_sanitizes(self):
        credentials.register('demo', file=str(self.secret_file))
        code, result = credentials.run(
            ['INJECTED=demo'], 20,
            CHILD + ['import os, sys; sys.stderr.write(os.environ["INJECTED"]); sys.exit(3)'])
        self.assertEqual(result['status'], 'nonzero')
        self.assertEqual(result['exit_code'], 3)
        self.assertEqual(code, 3)
        self.assertNotIn(self.secret, json.dumps(result))

    def test_run_rejects_secret_and_encodings_in_argv(self):
        credentials.register('demo', file=str(self.secret_file))
        for encoded in common.secret_representations(self.secret):
            with self.assertRaises(credentials.CredentialError):
                credentials.run(['INJECTED=demo'], 10, CHILD + ['print("ok")', encoded])

    def test_run_launch_failure_cannot_leak_secret(self):
        credentials.register('demo', file=str(self.secret_file))
        with self.assertRaises(credentials.CredentialError) as caught:
            credentials.run(['INJECTED=demo'], 10, ['/nonexistent/definitely-not-a-real-binary'])
        self.assertNotIn(self.secret, str(caught.exception))

    def test_timeout_suppresses_output_and_kills_process_group(self):
        credentials.register('demo', file=str(self.secret_file))
        pidfile = self.root / 'pids.txt'
        script = (
            'import os, subprocess, sys, time\n'
            'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n'
            'open(os.environ["PIDFILE"], "w").write(str(os.getpid()) + " " + str(child.pid))\n'
            'sys.stdout.write(os.environ["INJECTED"]); sys.stdout.flush()\n'
            'time.sleep(30)\n'
        )
        with patch.dict(os.environ, {'PIDFILE': str(pidfile)}):
            code, result = credentials.run(['INJECTED=demo'], 0.5, CHILD + [script])
        self.assertEqual(code, credentials.EXIT_TIMEOUT)
        self.assertTrue(result['timed_out'])
        self.assertTrue(result['output_suppressed'])
        self.assertEqual((result['stdout'], result['stderr']), ('', ''))
        self.assertNotIn(self.secret, json.dumps(result))
        pids = [int(p) for p in pidfile.read_text().split()]
        deadline = time.time() + 5
        while time.time() < deadline and any(_alive(pid) for pid in pids):
            time.sleep(0.1)
        self.assertFalse(any(_alive(pid) for pid in pids), 'process group survived timeout')

    def test_parent_exit_with_pipe_holding_grandchild_is_bounded(self):
        credentials.register('demo', file=str(self.secret_file))
        pidfile = self.root / 'pids2.txt'
        script = (
            'import os, subprocess, sys\n'
            'shadow = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n'
            'open(os.environ["PIDFILE"], "w").write(str(shadow.pid))\n'
            'sys.stdout.write(os.environ["INJECTED"]); sys.stdout.flush()\n'
        )
        with patch.dict(os.environ, {'PIDFILE': str(pidfile)}):
            began = time.monotonic()
            code, result = credentials.run(['INJECTED=demo'], 0.6, CHILD + [script])
            elapsed = time.monotonic() - began
        self.assertLess(elapsed, 10, 'runner waited on a descendant that inherited the pipes')
        self.assertEqual(code, credentials.EXIT_TIMEOUT)
        self.assertTrue(result['output_suppressed'])
        self.assertEqual((result['stdout'], result['stderr']), ('', ''))
        self.assertNotIn(self.secret, json.dumps(result))
        pids = [int(p) for p in pidfile.read_text().split()]
        deadline = time.time() + 5
        while time.time() < deadline and any(_alive(pid) for pid in pids):
            time.sleep(0.1)
        self.assertFalse(any(_alive(pid) for pid in pids), 'descendant survived process-group kill')

    def test_file_read_decoding_and_spacing(self):
        invalid = self.root / 'invalid.txt'
        invalid.write_bytes(b'\xff\xfe\xfd')
        invalid.chmod(0o600)
        with self.assertRaises(credentials.CredentialError):
            credentials._read_file_value(str(invalid))
        nul = self.root / 'nul.txt'
        nul.write_bytes(b'abc\x00def')
        nul.chmod(0o600)
        with self.assertRaises(credentials.CredentialError):
            credentials._read_file_value(str(nul))
        spaced = self.root / 'spaced.txt'
        spaced.write_text('  padded-secret-123  \n')
        spaced.chmod(0o600)
        self.assertEqual(credentials._read_file_value(str(spaced)), '  padded-secret-123  ')
        crlf = self.root / 'crlf.txt'
        crlf.write_bytes(b'value\r\n')
        crlf.chmod(0o600)
        self.assertEqual(credentials._read_file_value(str(crlf)), 'value')
        only_newline = self.root / 'onlynl.txt'
        only_newline.write_text('\n')
        only_newline.chmod(0o600)
        with self.assertRaises(credentials.CredentialError):
            credentials._read_file_value(str(only_newline))

    def test_file_read_rechecks_owner_only_permissions_on_opened_fd(self):
        path = self._write_secret('synthetic-secret-value-123')
        path.chmod(0o644)
        with patch.object(credentials, '_file_metadata', return_value=(path, None)):
            with self.assertRaises(credentials.CredentialError):
                credentials._read_file_value(str(path))

    def test_registered_file_permission_drift_fails_closed(self):
        credentials.register('demo', file=str(self.secret_file))
        self.secret_file.chmod(0o644)
        with self.assertRaises(credentials.CredentialError):
            credentials.run(['INJECTED=demo'], 10, CHILD + ['pass'])
        self.secret_file.chmod(0o600)

    def test_run_preserves_significant_spaces(self):
        spaced = self.root / 'spaced.txt'
        spaced.write_text('  padded-secret-123  \n')
        spaced.chmod(0o600)
        credentials.register('spaced', file=str(spaced))
        code, result = credentials.run(
            ['INJECTED=spaced'], 20,
            CHILD + ['import os, sys; sys.stdout.write("[" + os.environ["INJECTED"] + "]")'])
        self.assertEqual(code, 0)
        # If the value had been stripped, the spaces would remain outside the marker.
        self.assertEqual(result['stdout'], '[<redacted>]')
        self.assertNotIn('padded-secret-123', json.dumps(result))
        self.assertNotIn('  <redacted>  ', json.dumps(result))

    def test_output_limit_suppresses_buffers(self):
        credentials.register('demo', file=str(self.secret_file))
        script = (
            'import os, sys\n'
            's = os.environ["INJECTED"]\n'
            'for _ in range(8):\n'
            '    sys.stdout.write("x" * 65536)\n'
            '    sys.stdout.write(s)\n'
        )
        code, result = credentials.run(['INJECTED=demo'], 30, CHILD + [script])
        self.assertEqual(code, credentials.EXIT_OUTPUT_LIMIT)
        self.assertEqual(result['status'], 'output_limit')
        self.assertTrue(result['truncated'])
        self.assertTrue(result['output_suppressed'])
        self.assertEqual((result['stdout'], result['stderr']), ('', ''))
        self.assertGreater(result['stdout_bytes'], credentials.MAX_OUTPUT_BYTES)
        self.assertNotIn(self.secret, json.dumps(result))

    def test_run_parameter_validation_and_unknown_references(self):
        credentials.register('demo', file=str(self.secret_file))
        with self.assertRaises(credentials.CredentialError):
            credentials.run([], 10, CHILD + ['pass'])
        with self.assertRaises(credentials.CredentialError):
            credentials.run(['INJECTED=nope'], 10, CHILD + ['pass'])
        with self.assertRaises(credentials.CredentialError):
            credentials.run(['1INVALID=demo'], 10, CHILD + ['pass'])
        with self.assertRaises(credentials.CredentialError):
            credentials.run(['A=demo', 'A=demo'], 10, CHILD + ['pass'])
        with self.assertRaises(credentials.CredentialError):
            credentials.run(['INJECTED=demo'], 0, CHILD + ['pass'])
        with self.assertRaises(credentials.CredentialError):
            credentials.run(['INJECTED=demo'], 10, [])
        os.remove(self.secret_file)
        with self.assertRaises(credentials.CredentialError) as caught:
            credentials.run(['INJECTED=demo'], 10, CHILD + ['pass'])
        self.assertNotIn(str(self.secret_file), str(caught.exception))

    # Integration with existing redaction and guards -------------------------

    def test_common_redact_covers_registered_values_encodings_and_keys(self):
        credentials.register('demo', file=str(self.secret_file))
        cleaned = common.redact('value=' + self.secret)
        self.assertNotIn(self.secret, cleaned)
        for representation in common.secret_representations(self.secret):
            self.assertNotIn(representation, cleaned)
        nested = common.redact({'prefix-' + self.secret: [self.secret]})
        self.assertNotIn(self.secret, json.dumps(nested))
        self.assertIn('<redacted>', json.dumps(nested))

    def test_common_redact_covers_server_password_basic_blob(self):
        (self.state / 'server-password').write_text('synthetic-server-password-123')
        blob = base64.b64encode(b'opencode:synthetic-server-password-123').decode()
        cleaned = common.redact({'Authorization': 'Basic ' + blob})
        self.assertNotIn(blob, json.dumps(cleaned))

    def test_submit_and_steer_reject_registered_secret(self):
        credentials.register('demo', file=str(self.secret_file))
        repo = self.root / 'repo'
        repo.mkdir()
        spec = {'directory': str(repo), 'objective': 'please use ' + self.secret, 'mode': 'read'}
        with self.assertRaises(ValueError):
            delegate.submit(spec)
        with self.assertRaises(ValueError):
            steering.send('job-anything', 'use ' + self.secret)

    def test_collect_and_transcript_redact_registered_secret(self):
        credentials.register('demo', file=str(self.secret_file))
        common.write_json(common.task_path('job-test'), {
            'id': 'job-test', 'title': 'Fixture', 'directory': str(self.root),
            'session_id': 'ses_test', 'status': 'completed'})
        common.write_json(common.artifact_dir('job-test') / 'result.json',
                          {'status': 'completed', 'errors': [{'message': 'leak ' + self.secret}]})
        common.write_json(common.artifact_dir('job-test') / 'messages.json',
                          [{'info': {'id': 'msg_1', 'role': 'assistant'},
                            'parts': [{'type': 'text', 'text': 'value ' + self.secret}]}])
        collected = diagnostics.collect('job-test')
        self.assertNotIn(self.secret, json.dumps(collected))
        saved = transcript.read('job-test', saved=True)
        self.assertNotIn(self.secret, json.dumps(saved))
        self.assertIn('<redacted>', json.dumps(saved))

    # Real CLI subprocess boundaries -----------------------------------------

    def test_cli_run_success_nonzero_and_timeout_exit_codes(self):
        register = self.cli('credential', 'register', 'demo', '--file', str(self.secret_file))
        self.assertEqual(register.returncode, 0, register.stderr)
        ok = self.cli('credential', 'run', '--use', 'INJECTED=demo', '--timeout', '20', '--',
                      sys.executable, '-c', 'import os; print(os.environ["INJECTED"])')
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertNotIn(self.secret, ok.stdout)
        self.assertIn('<redacted>', json.loads(ok.stdout)['stdout'])
        bad = self.cli('credential', 'run', '--use', 'INJECTED=demo', '--timeout', '20', '--',
                       sys.executable, '-c', 'import sys; sys.exit(4)')
        self.assertEqual(bad.returncode, 4, bad.stderr)
        slow = self.cli('credential', 'run', '--use', 'INJECTED=demo', '--timeout', '0.5', '--',
                        sys.executable, '-c', 'import os,time; print(os.environ["INJECTED"]); time.sleep(30)')
        self.assertEqual(slow.returncode, credentials.EXIT_TIMEOUT, slow.stderr)
        self.assertTrue(json.loads(slow.stdout)['output_suppressed'])
        self.assertNotIn(self.secret, slow.stdout)

    def test_cli_requires_delimiter_and_valid_actions(self):
        self.cli('credential', 'register', 'demo', '--file', str(self.secret_file))
        missing = self.cli('credential', 'run', '--use', 'INJECTED=demo')
        self.assertNotEqual(missing.returncode, 0)
        unknown = self.cli('credential', 'get', 'demo')
        self.assertNotEqual(unknown.returncode, 0)
        immediate = self.cli('credential', 'run', '--use', 'INJECTED=demo', '--timeout', '5',
                             '--', sys.executable, '-c', 'import os; print(os.environ["INJECTED"])',
                             timeout=10)
        self.assertEqual(immediate.returncode, 0, immediate.stderr)
        self.assertNotIn(self.secret, immediate.stdout)


if __name__ == '__main__':
    unittest.main()
