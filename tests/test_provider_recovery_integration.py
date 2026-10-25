import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import quota
import provider_recovery


class ProviderRecoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        for obj, name, value in ((quota, 'STATE', self.state), (common, 'STATE', self.state)):
            p = patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in [('credential_identity', 'inference-test'),
                            ('_telemetry_identity', 'control-test'),
                            ('provider_is_prepaid', False)]:
            p = patch.object(quota, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        self.block = {'credential': 'inference-test', 'opened_at': 1000,
                      'reason': quota.MONTHLY_REASON, 'message_ids': ['seen-test']}
        self.ark = {'state': 'ok', 'available': True, 'sampled_at': 1311,
                    'windows': [{'name': name, 'limit': 100, 'remaining': 25,
                                 'remaining_percent': 25, 'valid': True,
                                 'resets_at': '1970-01-02T00:00:00Z'}
                                for name in sorted(provider_recovery.AFP_NAMES)]}
        self.kimi = {'state': 'ok', 'source': 'kimi_web_membership',
                     'available': True, 'sampled_at': 1311,
                     'account_binding': {'state': 'verified'},
                     'windows': [{'name': 'monthTotal', 'kind': 'month_total',
                                  'source': 'kimi_web_membership', 'limit': 1,
                                  'remaining': .25, 'remaining_percent': 25,
                                  'valid': True, 'expires_at': '1970-01-02T00:00:00Z'}]}

    def save(self, provider, block=None):
        common.write_json(self.state / quota.BILLING, {provider: self.block if block is None else block})

    def stored(self, provider):
        return common.read_json(self.state / quota.BILLING, {})[provider]

    def apply(self, provider, reading=None, start=1310, end=1312):
        return quota.apply_provider_retry_evidence(
            provider, self.ark if reading is None else reading,
            start, end, 'inference-test', 'control-test', now=end)

    def test_ark_distinct_identity_recovery_is_bounded_authorization(self):
        self.save(quota.ARK_PROVIDER)
        self.assertTrue(self.apply(quota.ARK_PROVIDER))
        block = self.stored(quota.ARK_PROVIDER)
        self.assertEqual(block['message_ids'], ['seen-test'])
        self.assertEqual(block['credential'], 'inference-test')
        self.assertEqual(block['cleared_at'], 1312)
        self.assertEqual(block['recovery_retry_count'], 1)
        self.assertEqual(block['recovery_retry_after'], 1912)
        self.assertIn('retry_authorization', block['released'])
        self.assertFalse(self.apply(quota.ARK_PROVIDER))

    def test_kimi_verified_month_total_recovers_but_weekly_does_not(self):
        self.save(quota.KIMI_PROVIDER)
        weekly = dict(self.kimi, windows=[dict(self.kimi['windows'][0], name='overall', kind='weekly')])
        self.assertFalse(self.apply(quota.KIMI_PROVIDER, weekly))
        self.assertTrue(self.apply(quota.KIMI_PROVIDER, self.kimi))

    def test_real_rejection_keeps_retry_backoff_and_stale_message_cannot_retrip(self):
        self.save(quota.ARK_PROVIDER)
        self.assertTrue(self.apply(quota.ARK_PROVIDER))
        with patch.object(quota.time, 'time', return_value=1320):
            quota.trip(quota.ARK_PROVIDER, message_id='new-test', occurred_at=1319,
                       reason=quota.MONTHLY_REASON)
        block = self.stored(quota.ARK_PROVIDER)
        self.assertEqual(block['recovery_retry_count'], 1)
        self.assertEqual(block['recovery_retry_after'], 1912)
        reading = copy.deepcopy(self.ark)
        reading['sampled_at'] = 1711
        self.assertFalse(self.apply(quota.ARK_PROVIDER, reading, 1710, 1712))
        reading['sampled_at'] = 1921
        self.assertTrue(self.apply(quota.ARK_PROVIDER, reading, 1920, 1922))
        self.assertEqual(self.stored(quota.ARK_PROVIDER)['recovery_retry_after'], 3122)
        with patch.object(quota.time, 'time', return_value=1923):
            quota.trip(quota.ARK_PROVIDER, message_id='seen-test', occurred_at=1001)
        self.assertEqual(self.stored(quota.ARK_PROVIDER)['cleared_at'], 1922)

    def test_initial_cooldown_and_newer_block_win(self):
        self.save(quota.ARK_PROVIDER, dict(self.block, opened_at=1200))
        self.assertFalse(self.apply(quota.ARK_PROVIDER))
        self.save(quota.ARK_PROVIDER, dict(self.block, opened_at=1311))
        self.assertFalse(self.apply(quota.ARK_PROVIDER))

    def test_auth_never_cleared_by_generic_or_specific_quota_evidence(self):
        self.save(quota.ARK_PROVIDER, dict(self.block, reason=quota.AUTH_REASON))
        self.assertFalse(quota.clear(quota.ARK_PROVIDER, sampled_since=1310, identity='inference-test'))
        self.assertFalse(self.apply(quota.ARK_PROVIDER))

    def test_current_inference_and_control_rotation_refuse_old_proof(self):
        for attr in ['credential_identity', '_telemetry_identity']:
            self.save(quota.ARK_PROVIDER)
            with patch.object(quota, attr, return_value='rotated-test'):
                self.assertFalse(self.apply(quota.ARK_PROVIDER))

    def test_nonofficial_kimi_unknown_and_zero_preserve_monthly_block(self):
        for changes in [{'account_binding': {'state': 'mismatch'}}, {'state': 'unknown'},
                        {'windows': [dict(self.kimi['windows'][0], remaining=0, remaining_percent=0)]}]:
            self.save(quota.KIMI_PROVIDER)
            self.assertFalse(self.apply(quota.KIMI_PROVIDER, dict(self.kimi, **changes)))
            self.assertNotIn('cleared_at', self.stored(quota.KIMI_PROVIDER))

    def test_refresh_uses_inference_binding_for_ark_not_control_hash(self):
        self.save(quota.ARK_PROVIDER)
        reading = dict(self.ark, sampled_at=1312)
        with patch.object(quota.time, 'time', return_value=1312), \
                patch.object(quota, '_providers_in_use', return_value=[quota.ARK_PROVIDER]), \
                patch.object(quota, 'fetch_one', return_value=reading), \
                patch.object(quota, 'scheduled_release', return_value=False), \
                patch.object(quota, 'config', return_value={}), \
                patch.object(quota, '_record_history'), patch.object(quota, 'observe_load'), \
                patch.object(quota, 'view', side_effect=lambda value: value):
            quota.refresh(force=True)
        self.assertEqual(self.stored(quota.ARK_PROVIDER)['credential'], 'inference-test')
        self.assertEqual(self.stored(quota.ARK_PROVIDER)['released'],
                         'ark_official_afp_retry_authorization')

    def test_refresh_monthly_hook_uses_real_account_after_window_fetch(self):
        self.save(quota.KIMI_PROVIDER)
        account = dict(self.kimi, sampled_at=1312)
        with patch.object(quota.time, 'time', return_value=1312), \
                patch.object(quota, '_providers_in_use', return_value=[quota.KIMI_PROVIDER]), \
                patch.object(quota, 'fetch_one', return_value={
                    'state': 'ok', 'available': True, 'sampled_at': 1312, 'windows': []}), \
                patch.object(quota, '_kimi_account_snapshot', return_value=account), \
                patch.object(quota, 'scheduled_release', return_value=False), \
                patch.object(quota, 'config', return_value={}), \
                patch.object(quota, '_record_history'), patch.object(quota, 'observe_load'), \
                patch.object(quota, 'view', side_effect=lambda value: value):
            quota.refresh(force=True)
        self.assertEqual(self.stored(quota.KIMI_PROVIDER)['released'],
                         'kimi_verified_monthly_pool_retry_authorization')

    def test_account_cleaning_never_launders_stale_or_missing_provenance(self):
        for changes in [{'stale': True}, {'live_error': {'state': 'failed'}},
                        {'http_status': 503}, {'source': None}, {'sampled_at': None},
                        {'sampled_at': float('nan')}]:
            self.save(quota.KIMI_PROVIDER)
            raw = dict(self.kimi, **changes)
            with patch.object(quota.time, 'time', return_value=1312):
                cleaned = quota._clean_account_record(raw)
            self.assertFalse(self.apply(quota.KIMI_PROVIDER, cleaned), changes)

    def test_retrip_preserves_watermark_against_unseen_old_message(self):
        self.save(quota.ARK_PROVIDER)
        self.assertTrue(self.apply(quota.ARK_PROVIDER))
        with patch.object(quota.time, 'time', return_value=1320):
            quota.trip(quota.ARK_PROVIDER, message_id='genuine-after-test',
                       occurred_at=1319, reason=quota.MONTHLY_REASON)
        before = self.stored(quota.ARK_PROVIDER)
        self.assertEqual(before['recovery_watermark'], 1312)
        with patch.object(quota.time, 'time', return_value=1321):
            quota.trip(quota.ARK_PROVIDER, message_id='unseen-but-old-test',
                       occurred_at=1002, reason=quota.MONTHLY_REASON)
        self.assertEqual(self.stored(quota.ARK_PROVIDER), before)

    def test_backoff_caps_and_decays_after_six_hours_without_attempt(self):
        self.save(quota.ARK_PROVIDER)
        self.assertTrue(self.apply(quota.ARK_PROVIDER))
        for index in range(6):
            previous = self.stored(quota.ARK_PROVIDER)
            at = previous['cleared_at'] + 1
            with patch.object(quota.time, 'time', return_value=at):
                quota.trip(quota.ARK_PROVIDER, message_id='backoff-test-' + str(index),
                           occurred_at=at, reason=quota.MONTHLY_REASON)
            due = previous['recovery_retry_after'] + 2
            reading = dict(self.ark, sampled_at=due - 1)
            self.assertTrue(self.apply(quota.ARK_PROVIDER, reading, due - 1, due))
        current = self.stored(quota.ARK_PROVIDER)
        self.assertEqual(current['recovery_retry_after'] - current['cleared_at'], 3600)
        with patch.object(quota.time, 'time', return_value=current['cleared_at'] + 1):
            quota.trip(quota.ARK_PROVIDER, message_id='decay-test',
                       occurred_at=current['cleared_at'] + 1, reason=quota.MONTHLY_REASON)
        due = current['cleared_at'] + 21602
        self.assertTrue(self.apply(quota.ARK_PROVIDER, dict(self.ark, sampled_at=due - 1), due - 1, due))
        self.assertEqual(self.stored(quota.ARK_PROVIDER)['recovery_retry_count'], 1)


if __name__ == '__main__':
    unittest.main()
