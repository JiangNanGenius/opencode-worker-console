from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import keychain_secrets as keychain


class BoundedKeychainTests(unittest.TestCase):
    def test_only_metadata_in_stdin_and_secret_pipe_never_argv(self):
        secret = b'synthetic-test-value-not-a-live-secret'
        with patch.object(keychain.sys, 'platform', 'darwin'), patch.object(
                keychain.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, secret)) as run:
            self.assertEqual(keychain.get_bounded('test-service', 'test-account'), secret)
        args, kwargs = run.call_args
        self.assertEqual(args[0][:2], ['/usr/bin/python3', '-I'])
        self.assertNotIn(secret.decode(), str(args))
        self.assertEqual(kwargs['input'], b'{"service": "test-service", "account": "test-account"}')
        self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
        self.assertEqual(kwargs['timeout'], 5)
        self.assertNotIn('env', kwargs)

    def test_timeout_and_failed_process_do_not_expose_output(self):
        for error in [subprocess.TimeoutExpired(['safe-helper'], 5, output=b'PRIVATE'),
                      OSError('PRIVATE')]:
            with patch.object(keychain.sys, 'platform', 'darwin'), patch.object(
                    keychain.subprocess, 'run', side_effect=error):
                with self.assertRaises(keychain.KeychainError) as failure:
                    keychain.get_bounded('test-service', 'test-account')
                self.assertNotIn('PRIVATE', str(failure.exception))
        for result in [subprocess.CompletedProcess([], 1, b'PRIVATE'),
                       subprocess.CompletedProcess([], 0, b''),
                       subprocess.CompletedProcess([], 0, b'x' * 65537)]:
            with patch.object(keychain.sys, 'platform', 'darwin'), patch.object(
                    keychain.subprocess, 'run', return_value=result):
                with self.assertRaises(keychain.KeychainError):
                    keychain.get_bounded('test-service', 'test-account')

    def test_invalid_metadata_timeout_and_nonmac_fail_before_process(self):
        with patch.object(keychain.sys, 'platform', 'darwin'), patch.object(keychain.subprocess, 'run') as run:
            for service, account, timeout in [('', 'a', 5), ('s', None, 5), ('s', 'a', True),
                                               ('s', 'a', 0), ('s', 'a', 31)]:
                with self.assertRaises(keychain.KeychainError):
                    keychain.get_bounded(service, account, timeout)
            run.assert_not_called()
        with patch.object(keychain.sys, 'platform', 'linux'), patch.object(keychain.subprocess, 'run') as run:
            with self.assertRaises(keychain.KeychainError):
                keychain.get_bounded('s', 'a')
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
