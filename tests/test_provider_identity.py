"""Key-file credential provenance: stat identity, rotation invalidation, fail closed."""
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import bailian_pool
import common
import quota

PROVIDER = 'bailian-token-plan'
SECRET = 'sk-test-only-synthetic-fixture-1-not-a-real-key'


class ProviderIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.config = self.root / 'config.json'
        self.key = self.root / 'aliyun.key'
        self.key.write_text(SECRET)
        os.chmod(self.key, 0o600)
        common.write_json(self.config, {
            'profiles': {'aliyun-max': {'model': PROVIDER + '/qwen3.8-max'}},
            'provider_api_key_files': {PROVIDER: str(self.key)},
        })
        self.patchers = [patch.object(common, 'STATE', self.state),
                         patch.object(common, 'CONFIG', self.config),
                         patch.object(quota, 'STATE', self.state)]
        for item in self.patchers:
            item.start()
        common.init()
        self.now = time.time()

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.tmp.cleanup()

    def test_identity_is_stat_based_and_never_reads_key_contents(self):
        real_open = Path.open
        real_read_text = Path.read_text
        real_read_bytes = Path.read_bytes
        key_str = str(self.key)

        def guard(path_self, *args, **kwargs):
            if str(path_self) == key_str:
                raise AssertionError('credential identity must not open the key file')
            return real_open(path_self, *args, **kwargs)

        def guard_text(path_self, *args, **kwargs):
            if str(path_self) == key_str:
                raise AssertionError('credential identity must not read the key file')
            return real_read_text(path_self, *args, **kwargs)

        def guard_bytes(path_self, *args, **kwargs):
            if str(path_self) == key_str:
                raise AssertionError('credential identity must not read the key file')
            return real_read_bytes(path_self, *args, **kwargs)

        with patch.object(Path, 'open', guard), patch.object(Path, 'read_text', guard_text), \
                patch.object(Path, 'read_bytes', guard_bytes):
            first = common.credential_identity(PROVIDER)
            second = common.credential_identity(PROVIDER)
        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        self.assertNotEqual(first, hashlib.sha256(SECRET.encode()).hexdigest())

    def test_current_file_success_clears_monthly_block(self):
        identity = common.credential_identity(PROVIDER)
        quota.trip(PROVIDER, 'monthly limit reached', reason=quota.MONTHLY_REASON,
                   occurred_at=self.now - 600)
        self.assertIsNotNone(quota.billing_block(PROVIDER))
        # The successful request must have started strictly after the block opened.
        opened = time.time()
        released = quota.observe_model_success(PROVIDER, identity, opened + 1, opened + 2)
        self.assertTrue(released)
        self.assertIsNone(quota.billing_block(PROVIDER))
        stored = json.loads((self.state / 'billing.json').read_text())[PROVIDER]
        self.assertEqual(stored['released'], quota.MODEL_SUCCESS_RELEASE)

    def test_rotated_file_old_inflight_success_cannot_clear_block(self):
        old_identity = common.credential_identity(PROVIDER)
        quota.trip(PROVIDER, 'monthly limit reached', reason=quota.MONTHLY_REASON,
                   occurred_at=self.now - 600)
        # Rotate the key file: the old in-flight identity must no longer release it.
        time.sleep(0.01)
        self.key.write_text(SECRET + '-rotated')
        new_identity = common.credential_identity(PROVIDER)
        self.assertNotEqual(old_identity, new_identity)
        opened = time.time()
        self.assertFalse(quota.observe_model_success(PROVIDER, old_identity, opened + 1, opened + 2))
        stored = json.loads((self.state / 'billing.json').read_text())[PROVIDER]
        self.assertNotIn('cleared_at', stored)
        # The circuit stays bound to the credential that failed; rotation detaches
        # it (existing rotated-key semantics) but never releases it for the old one.
        self.assertIsNone(quota.billing_block(PROVIDER))

    def test_missing_file_fails_closed(self):
        identity = common.credential_identity(PROVIDER)
        self.key.unlink()
        self.assertIsNone(common.credential_identity(PROVIDER))
        quota.trip(PROVIDER, 'monthly limit reached', reason=quota.MONTHLY_REASON,
                   occurred_at=self.now - 600)
        opened = time.time()
        self.assertFalse(quota.observe_model_success(PROVIDER, identity, opened + 1, opened + 2))
        stored = json.loads((self.state / 'billing.json').read_text())[PROVIDER]
        self.assertNotIn('cleared_at', stored)

    def test_auth_store_path_preserved_without_override(self):
        with patch.object(common, 'auth_key', return_value='auth-store-key'):
            expected = hashlib.sha256(b'auth-store-key').hexdigest()
            self.assertEqual(common.credential_identity('kimi-for-coding'), expected)
        # A provider outside the runtime key-file set ignores a stray config entry.
        config = json.loads(self.config.read_text())
        config['provider_api_key_files']['volcengine-agent-plan'] = str(self.key)
        self.config.write_text(json.dumps(config))
        with patch.object(common, 'auth_key', return_value='ark-key'):
            self.assertEqual(common.credential_identity('volcengine-agent-plan'),
                             hashlib.sha256(b'ark-key').hexdigest())

    def test_missing_override_never_uses_old_auth_store(self):
        self.key.unlink()
        with patch.object(common, 'auth_key', return_value='old-auth-store-key') as auth:
            self.assertIsNone(common.credential_identity(PROVIDER))
            auth.assert_not_called()


class PooledProviderIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.config = self.root / 'config.json'
        self.key = self.root / 'seat.key'
        self.key.write_text(SECRET)
        os.chmod(self.key, 0o600)
        self.value = {
            'profiles': {'aliyun-max': {'model': PROVIDER + '/qwen3.8-max'}},
            'bailian_pool': {
                'enabled': True,
                'listen_port': 51530,
                'slots': [
                    {'id': 'seat-1', 'keychain': {
                        'service': 'workerdesk.bailian-token-plan', 'account': 'seat-1'}},
                    {'id': 'seat-2', 'keychain': {
                        'service': 'workerdesk.bailian-token-plan', 'account': 'seat-2'}},
                ],
            },
        }
        common.write_json(self.config, self.value)
        self.patchers = [patch.object(common, 'STATE', self.state),
                         patch.object(common, 'CONFIG', self.config),
                         patch.object(quota, 'STATE', self.state)]
        for item in self.patchers:
            item.start()
        common.init()

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.tmp.cleanup()

    def rewrite(self, value):
        common.write_json(self.config, value)

    def test_keychain_pool_identity_never_reads_a_secret(self):
        with patch.object(bailian_pool.keychain_secrets, 'get',
                          side_effect=AssertionError('identity must not read Keychain')):
            first = common.credential_identity(PROVIDER)
            second = common.credential_identity(PROVIDER)
        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        self.assertNotEqual(first, hashlib.sha256(SECRET.encode()).hexdigest())
        self.assertNotIn(SECRET, first)

    def test_file_slot_identity_uses_stat_and_rotation_changes_it(self):
        value = json.loads(self.config.read_text())
        value['bailian_pool']['slots'] = [{'id': 'seat-1', 'file': str(self.key)}]
        self.rewrite(value)
        real_read_text = Path.read_text
        real_read_bytes = Path.read_bytes
        key_str = str(self.key)

        def guard_text(path_self, *args, **kwargs):
            if str(path_self) == key_str:
                raise AssertionError('pool identity must not read the key file')
            return real_read_text(path_self, *args, **kwargs)

        def guard_bytes(path_self, *args, **kwargs):
            if str(path_self) == key_str:
                raise AssertionError('pool identity must not read the key file')
            return real_read_bytes(path_self, *args, **kwargs)

        with patch.object(Path, 'read_text', guard_text),                 patch.object(Path, 'read_bytes', guard_bytes):
            first = common.credential_identity(PROVIDER)
            second = common.credential_identity(PROVIDER)
        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        time.sleep(0.01)
        self.key.write_text(SECRET + '-rotated')
        self.assertNotEqual(common.credential_identity(PROVIDER), first)

    def test_pool_identity_changes_with_slot_metadata_and_order(self):
        first = common.credential_identity(PROVIDER)
        value = json.loads(self.config.read_text())
        value['bailian_pool']['slots'][0]['keychain']['account'] = 'rotated-account'
        self.rewrite(value)
        self.assertNotEqual(common.credential_identity(PROVIDER), first)
        before_order = common.credential_identity(PROVIDER)
        value = json.loads(self.config.read_text())
        value['bailian_pool']['slots'] = list(reversed(value['bailian_pool']['slots']))
        self.rewrite(value)
        self.assertNotEqual(common.credential_identity(PROVIDER), before_order)

    def test_invalid_pool_configuration_fails_closed(self):
        value = json.loads(self.config.read_text())
        value['bailian_pool']['slots'] = []
        self.rewrite(value)
        self.assertIsNone(common.credential_identity(PROVIDER))


if __name__ == '__main__':
    unittest.main()
