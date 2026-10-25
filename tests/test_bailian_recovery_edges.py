import copy
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import quota
import bailian_quota
from providers import BAILIAN_PROVIDER as PROVIDER


class BailianRecoveryEdges(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.now = 2_000_000_000
        self.patchers = [patch.object(common, 'STATE', self.state),
                         patch.object(quota, 'STATE', self.state),
                         patch.object(quota, 'config', return_value={}),
                         patch.object(quota, 'credential_identity', return_value='inference'),
                         patch.object(bailian_quota, 'credential_identity', return_value='control')]
        for item in self.patchers:
            item.start()
        self.value = {'state': 'ok', 'source': 'openapi_live', 'available': True,
                      'sampled_at': self.now - 1,
                      'windows': [{'valid': True, 'remaining': 80, 'limit': 100,
                                   'resets_at': datetime.fromtimestamp(self.now + 1000, timezone.utc).isoformat()}]}
        self.block = {'credential': 'inference', 'opened_at': self.now - 100,
                      'reason': quota.MONTHLY_REASON, 'message_ids': ['seen'], 'message': 'depleted'}
        common.write_json(self.state / quota.BILLING, {PROVIDER: self.block})

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    def recover(self, value=None, **overrides):
        args = {'provider': PROVIDER, 'query_started_at': self.now - 1,
                'inference_identity': 'inference', 'control_identity': 'control',
                'now': self.now, 'value': self.value if value is None else value}
        args.update(overrides)
        return quota.account_api_recovery(**args)

    def test_consecutive_failures_keep_last_live_reading_and_original_time(self):
        prior = dict(self.value, sampled_at=self.now - 500)
        failure = {'state': 'unavailable', 'attempted_at': self.now}
        first = quota._bailian_display(failure, prior, None, self.now)
        second = quota._bailian_display(failure, first, None, self.now + 10)
        self.assertEqual(second['sampled_at'], prior['sampled_at'])
        self.assertEqual(second['windows'], prior['windows'])
        self.assertEqual(second['state'], 'unavailable')

    def test_positive_current_api_releases_only_matching_bailian_block(self):
        self.assertTrue(self.recover())
        saved = common.read_json(self.state / quota.BILLING, {})[PROVIDER]
        self.assertEqual(saved['released'], quota.API_RELEASE)
        self.assertEqual(saved['message_ids'], ['seen'])
        self.assertEqual(saved['opened_at'], self.block['opened_at'])
        self.assertFalse(self.recover(provider='kimi-for-coding'))

    def test_stale_expired_unknown_or_partial_evidence_never_recovers(self):
        variants = []
        for field, value in (('state', 'unavailable'), ('source', 'manual_snapshot'),
                             ('available', None), ('live_error', {'state': 'auth_error'})):
            changed = copy.deepcopy(self.value); changed[field] = value; variants.append(changed)
        for remaining, valid, reset in ((0, True, self.now + 10), (80, False, self.now + 10),
                                         (80, True, self.now - 1), (None, False, self.now + 10)):
            changed = copy.deepcopy(self.value)
            changed['windows'][0].update(remaining=remaining, valid=valid,
                resets_at=datetime.fromtimestamp(reset, timezone.utc).isoformat())
            variants.append(changed)
        changed = copy.deepcopy(self.value); changed['windows'].append({'valid': False})
        variants.append(changed)
        for value in variants:
            with self.subTest(value=value):
                self.assertFalse(self.recover(value=value))

    def test_rotation_and_query_time_guards(self):
        self.assertFalse(self.recover(inference_identity='rotated'))
        self.assertFalse(self.recover(control_identity='rotated'))
        self.assertFalse(self.recover(query_started_at=self.block['opened_at']))
        self.assertFalse(self.recover(now=self.now - 2))

    def test_complete_cycle_duration_is_stable_and_null_credits_are_unknown(self):
        start = self.now - 86400
        end = self.now + 29 * 86400
        body = {'Success': True, 'Data': {'SubscriptionStartTime': start * 1000,
            'SubscriptionEndTime': end * 1000, 'Items': [{'SeatType': 'pro',
            'SeatCredits': 100, 'SeatRemainingCredits': 80, 'SeatRefreshTime': end * 1000}]}}
        first = bailian_quota.normalize_stats(body, self.now)
        later = bailian_quota.normalize_stats(body, self.now + 3600)
        self.assertEqual(first['windows'][0]['duration_minutes'], 43200)
        self.assertEqual(first['windows'][0]['duration_minutes'], later['windows'][0]['duration_minutes'])
        body['Data']['Items'].append({'SeatType': 'other', 'SeatCredits': 100,
            'SeatRemainingCredits': None, 'SeatRefreshTime': end * 1000})
        self.assertIsNone(bailian_quota.normalize_stats(body, self.now)['available'])

    def test_refresh_preserves_live_source_through_repeated_failures(self):
        failure = {'state': 'unavailable', 'attempted_at': self.now}
        with patch.object(quota, '_providers_in_use', return_value=[PROVIDER]), \
                patch.object(quota, '_telemetry_identity', return_value='control'), \
                patch.object(quota, 'fetch_one', side_effect=[self.value, failure, failure]), \
                patch.object(quota, 'scheduled_release'), patch.object(quota, 'observe_load'), \
                patch.object(quota.time, 'time', return_value=self.now):
            good = quota.refresh(force=True)[PROVIDER]
            bad = quota.refresh(force=True)[PROVIDER]
            repeated = quota.refresh(force=True)[PROVIDER]
        self.assertFalse(good['stale'])
        self.assertTrue(bad['stale'])
        self.assertTrue(repeated['stale'])
        self.assertEqual(repeated['sampled_at'], good['sampled_at'])
        self.assertEqual(repeated['source'], 'openapi_live')
        self.assertEqual(repeated['windows'][0]['remaining'], 80)

    def test_older_manual_snapshot_cannot_replace_newer_live_data_or_release_block(self):
        current = dict(self.value, available=False)
        current['windows'] = [dict(self.value['windows'][0], remaining=0)]
        common.write_json(self.state / 'quota.json', {PROVIDER: current})
        iso = lambda seconds: datetime.fromtimestamp(seconds, timezone.utc).isoformat()
        result = quota.import_snapshot(PROVIDER, {'remaining': 50, 'limit': 100,
            'captured_at': iso(self.now - 10), 'resets_at': iso(self.now + 1000),
            'source': 'manual'}, now=self.now)
        self.assertFalse(result['recovery'])
        cached = common.read_json(self.state / 'quota.json', {})[PROVIDER]
        self.assertEqual(cached['source'], 'openapi_live')
        self.assertEqual(cached['windows'][0]['remaining'], 0)
        self.assertNotIn('cleared_at', common.read_json(self.state / quota.BILLING, {})[PROVIDER])

    def test_control_rotation_during_fetch_discards_the_inflight_sample(self):
        with patch.object(quota, '_providers_in_use', return_value=[PROVIDER]), \
                patch.object(quota, '_telemetry_identity', side_effect=['control', 'rotated']), \
                patch.object(quota, 'fetch_one', return_value=self.value), \
                patch.object(quota, 'scheduled_release'), patch.object(quota, 'observe_load'), \
                patch.object(quota.time, 'time', return_value=self.now):
            result = quota.refresh(force=True)[PROVIDER]
        self.assertEqual(result['billing']['telemetry_state'], 'unknown')
        self.assertNotIn('sampled_at', result)
        self.assertTrue(result['stale'])
        self.assertNotIn('cleared_at', common.read_json(self.state / quota.BILLING, {})[PROVIDER])


if __name__ == '__main__':
    unittest.main()
