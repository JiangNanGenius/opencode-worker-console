import copy
import json
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import host_telemetry as ht


class KimiHostQuotaTests(unittest.TestCase):
    def record(self):
        now = time.time()
        return {'state': 'ok', 'sampled_at': now, 'available': True,
                'windows': [{'name': 'window_0', 'duration_minutes': 300,
                             'remaining_percent': 100, 'valid': True},
                            {'name': 'overall', 'duration_minutes': 10080,
                             'remaining_percent': 15.4, 'valid': True}],
                'account': {'state': 'ok', 'sampled_at': now,
                            'account_binding': {'state': 'verified'},
                            'account': {'nickname': 'PRIVATE', 'userId': 'PRIVATE-ID'},
                            'windows': [
                                {'name': 'limit5h', 'kind': 'short', 'duration_minutes': 300,
                                 'remaining_percent': 99.99, 'valid': True,
                                 'source': 'kimi_code_account'},
                                {'name': 'limit7d', 'kind': 'weekly', 'duration_minutes': 10080,
                                 'remaining_percent': 15.38, 'valid': True,
                                 'source': 'kimi_code_account'},
                                {'name': 'monthTotal', 'kind': 'month_total',
                                 'remaining_percent': 66.73, 'valid': True,
                                 'remaining': .6673, 'limit': 1,
                                 'expires_at': now + 86400, 'resets_at': None,
                                 'source': 'kimi_web_membership'}]}}

    def test_raw_cache_account_windows_replace_duplicates_and_keep_total(self):
        record = self.record()
        original = copy.deepcopy(record)
        result = ht.normalize_worker('kimi-for-coding', record)
        self.assertEqual(len(result['windows']), 3)
        by_kind = {row['kind']: row for row in result['windows']}
        self.assertEqual(by_kind['weekly']['remaining_percent'], 15.38)
        self.assertEqual(by_kind['short']['remaining_percent'], 99.99)
        self.assertEqual(by_kind['month_total']['remaining_percent'], 66.73)
        self.assertEqual(by_kind['month_total']['name'], 'monthTotal')
        self.assertEqual(by_kind['month_total']['source'], 'kimi_web_membership')
        self.assertEqual(by_kind['month_total']['expires_at'], record['account']['windows'][-1]['expires_at'])
        self.assertEqual(by_kind['month_total']['resets_at'], 0)
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(record, original)

    def test_quota_view_shape_accepts_explicit_fresh_account(self):
        record = self.record()
        record['stale'] = False
        record['account']['stale'] = False
        record['account_total'] = {'authority': 'verified', 'remaining_percent': 66.73}
        result = ht.normalize_worker('kimi-for-coding', record)
        self.assertEqual(next(row for row in result['windows'] if row['kind'] == 'month_total')['remaining_percent'], 66.73)

    def test_unverified_mismatched_or_old_account_does_not_override_api(self):
        for state in ('unverified', 'mismatch'):
            record = self.record()
            record['account']['account_binding']['state'] = state
            result = ht.normalize_worker('kimi-for-coding', record)
            self.assertEqual(len(result['windows']), 2)
            self.assertFalse(any(row.get('kind') == 'month_total' for row in result['windows']))
        for field, value in [('sampled_at', time.time() - 901), ('sampled_at', time.time() + 60),
                             ('stale', True), ('state', 'unavailable')]:
            record = self.record()
            record['account'][field] = value
            self.assertEqual(len(ht.normalize_worker('kimi-for-coding', record)['windows']), 2)

    def test_expired_or_invalid_month_total_is_not_current(self):
        for updates in [{'expires_at': time.time() - 1}, {'expired': True},
                        {'remaining_percent': None}, {'remaining_percent': True},
                        {'remaining_percent': float('nan')}]:
            record = self.record()
            record['account']['windows'][-1].update(updates)
            result = ht.normalize_worker('kimi-for-coding', record)
            self.assertFalse(any(row.get('kind') == 'month_total' for row in result['windows']))

    def test_real_monthly_zero_overrides_positive_api_availability(self):
        record = self.record()
        record['account']['windows'][-1].update(remaining=0, remaining_percent=0)
        result = ht.normalize_worker('kimi-for-coding', record)
        self.assertFalse(result['available'])
        self.assertEqual(next(row for row in result['windows'] if row['kind'] == 'month_total')['remaining'], 0)

    def test_missing_total_never_becomes_zero(self):
        record = self.record()
        record['account']['windows'].pop()
        result = ht.normalize_worker('kimi-for-coding', record)
        self.assertTrue(result['available'])
        self.assertEqual(len(result['windows']), 2)

    def test_non_kimi_provider_is_unchanged(self):
        record = self.record()
        before = ht.normalize_worker('bailian-token-plan', record)
        del record['account']
        after = ht.normalize_worker('bailian-token-plan', record)
        self.assertEqual(before, after)
        self.assertNotIn('kind', before['windows'][0])


if __name__ == '__main__':
    unittest.main()
