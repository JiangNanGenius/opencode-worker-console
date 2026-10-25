"""Offline tests use the actual normalized provider schemas, never real keys."""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import provider_recovery as recovery


class ProviderRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.block = {'credential': 'inference-hash-test', 'opened_at': 1000,
                      'reason': 'billing_error', 'message_ids': ['old-error-id']}
        self.args = dict(captured_inference_identity='inference-hash-test',
                         current_inference_identity='inference-hash-test',
                         query_started_at=1010, query_completed_at=1020, now=1021)
        self.ark_args = dict(self.args, captured_control_identity='control-hash-test',
                             current_control_identity='control-hash-test', official_query=True)
        self.ark = {'state': 'ok', 'sampled_at': 1010, 'available': True,
                    'credential_source': 'credential_reference',
                    'windows': [{'name': name, 'limit': 100, 'remaining': 25,
                                 'remaining_percent': 25, 'valid': True,
                                 'resets_at': '1970-01-01T01:00:00+00:00'}
                                for name in sorted(recovery.AFP_NAMES)]}
        self.kimi_block = dict(self.block, reason='monthly_usage_limit')
        self.kimi = {'state': 'ok', 'source': 'kimi_web_membership', 'sampled_at': 1015,
                     'stale': False, 'available': True, 'account_binding': {'state': 'verified'},
                     'windows': [{'name': 'monthTotal', 'kind': 'month_total', 'limit': 1,
                                  'remaining': .25, 'remaining_percent': 25, 'valid': True,
                                  'expires_at': '1970-01-01T01:00:00Z', 'resets_at': None,
                                  'source': 'kimi_web_membership'}]}

    def ark_check(self, block=None, reading=None, **args):
        return recovery.ark_retry_evidence(self.block if block is None else block,
                                          self.ark if reading is None else reading,
                                          **dict(self.ark_args, **args))

    def kimi_check(self, block=None, reading=None, **args):
        return recovery.kimi_monthly_retry_evidence(
            self.kimi_block if block is None else block,
            self.kimi if reading is None else reading, **dict(self.args, **args))

    def test_ark_distinct_control_and_inference_identities_authorize_retry_only(self):
        result = self.ark_check()
        self.assertEqual(result, {'allowed': True,
                         'evidence_kind': 'ark_official_afp_retry_authorization',
                         'reason': 'fresh_positive_all_afp_windows'})
        self.assertNotIn('hash-test', json.dumps(result))
        self.assertNotIn('healthy', json.dumps(result))

    def test_actual_ark_normalizer_schema_qualifies_without_identity_conflation(self):
        import ark_quota
        value = ark_quota.normalize_afp_usage({name: {
            'Quota': 100, 'Used': 75, 'ResetTime': 3600000}
            for name in recovery.AFP_NAMES})
        value.update(state='ok', sampled_at=1010)
        self.assertTrue(self.ark_check(reading=value)['allowed'])

    def test_actual_kimi_web_normalizer_preserves_expiry_not_inferred_reset(self):
        import kimi_account
        window = kimi_account.normalize_web_total({'subscriptionBalance': {
            'amountUsedRatio': .75, 'expireTime': '1970-01-01T01:00:00Z'}})
        self.assertIsNone(window['resets_at'])
        self.assertTrue(self.kimi_check(reading=dict(self.kimi, windows=[window]))['allowed'])

    def test_ark_cannot_bind_control_hash_to_inference_block(self):
        self.assertFalse(self.ark_check(block=dict(self.block, credential='control-hash-test'))['allowed'])

    def test_ark_all_four_windows_required_including_monthly(self):
        for name in recovery.AFP_NAMES:
            with self.subTest(missing=name):
                value = copy.deepcopy(self.ark)
                value['windows'] = [w for w in value['windows'] if w['name'] != name]
                self.assertFalse(self.ark_check(reading=value)['allowed'])

    def test_ark_duplicate_unknown_or_malformed_windows_refuse(self):
        for windows in [None, [], [None] * 4, self.ark['windows'][:1] * 4,
                        self.ark['windows'] + [{'name': 'overall'}]]:
            with self.subTest(windows=windows):
                self.assertFalse(self.ark_check(reading=dict(self.ark, windows=windows))['allowed'])

    def test_ark_never_releases_cash_auth_or_unknown(self):
        for reason in ['insufficient_balance', 'credential_rejected', None, 'other']:
            with self.subTest(reason=reason):
                self.assertFalse(self.ark_check(block=dict(self.block, reason=reason))['allowed'])

    def test_ark_monthly_only_qualified_for_bounded_retry_with_real_all_windows(self):
        block = dict(self.block, reason='monthly_usage_limit')
        self.assertTrue(self.ark_check(block=block)['allowed'])
        value = copy.deepcopy(self.ark)
        value['windows'] = [w for w in value['windows'] if w['name'] != 'AFPMonthly']
        self.assertFalse(self.ark_check(block=block, reading=value)['allowed'])
        value['windows'].append(dict(self.ark['windows'][0], name='overall'))
        self.assertFalse(self.ark_check(block=block, reading=value)['allowed'])

    def test_ark_requires_official_call_not_names_or_imported_snapshot(self):
        self.assertFalse(self.ark_check(official_query=False)['allowed'])
        self.assertFalse(self.ark_check(prepaid=True)['allowed'])
        self.assertFalse(self.ark_check(prepaid=None)['allowed'])

    def test_current_identities_and_block_binding_must_all_match(self):
        for arg in ['captured_control_identity', 'current_control_identity',
                    'captured_inference_identity', 'current_inference_identity']:
            for value in [None, '', 'rotated-test']:
                with self.subTest(arg=arg, value=value):
                    self.assertFalse(self.ark_check(**{arg: value})['allowed'])
        self.assertFalse(self.ark_check(captured_control_identity=None,
                                       current_control_identity=None)['allowed'])

    def test_query_strictly_after_block_no_future_stale_or_misordered_samples(self):
        invalid = [dict(query_started_at=1000), dict(query_started_at=999),
                   dict(query_completed_at=1009), dict(now=1019), dict(now=1131),
                   dict(query_started_at=True), dict(now=float('nan')),
                   dict(max_age_seconds=0)]
        for args in invalid:
            with self.subTest(args=args):
                self.assertFalse(self.ark_check(**args)['allowed'])
        for sampled in [1000, 1009, 1021, None, True, float('inf')]:
            self.assertFalse(self.ark_check(reading=dict(self.ark, sampled_at=sampled))['allowed'])

    def test_active_block_only_and_no_mutation(self):
        before = copy.deepcopy((self.block, self.ark, self.kimi_block, self.kimi))
        self.assertTrue(self.ark_check()['allowed'])
        self.assertTrue(self.kimi_check()['allowed'])
        self.assertEqual(before, (self.block, self.ark, self.kimi_block, self.kimi))
        self.assertFalse(self.ark_check(block=dict(self.block, cleared_at=1005))['allowed'])

    def test_errors_unknown_zero_and_bad_numbers_refuse(self):
        for key, value in [('state', 'auth_error'), ('state', 'unknown'), ('stale', True),
                           ('stale', 1), ('http_status', 401), ('available', None), ('available', False),
                           ('live_error', {'state': 'auth_error'}), ('error', 'reauth_required')]:
            with self.subTest(key=key, value=value):
                self.assertFalse(self.ark_check(reading=dict(self.ark, **{key: value}))['allowed'])
        for key in ['limit', 'remaining', 'remaining_percent']:
            for value in [0, -1, None, True, float('nan'), float('inf')]:
                reading = copy.deepcopy(self.ark)
                reading['windows'][0][key] = value
                self.assertFalse(self.ark_check(reading=reading)['allowed'])
        reading = copy.deepcopy(self.ark)
        reading['windows'][0]['remaining_percent'] = 100
        self.assertFalse(self.ark_check(reading=reading)['allowed'])

    def test_ark_each_window_and_plan_unexpired(self):
        for expiry in [None, '', 1021, 1020, '1970-01-01T00:17:01', 'invalid']:
            reading = copy.deepcopy(self.ark)
            reading['windows'][0]['resets_at'] = expiry
            self.assertFalse(self.ark_check(reading=reading)['allowed'])
        self.assertFalse(self.ark_check(reading=dict(self.ark, plan={'active': False}))['allowed'])
        self.assertFalse(self.ark_check(reading=dict(self.ark, plan={'ends_at': 1021}))['allowed'])

    def test_kimi_actual_web_month_total_authorizes_retry(self):
        result = self.kimi_check()
        self.assertTrue(result['allowed'])
        self.assertEqual(result['evidence_kind'], 'kimi_verified_monthly_pool_retry_authorization')
        self.assertNotIn('hash-test', json.dumps(result))

    def test_kimi_uses_verified_real_monthly_not_overall_weekly_or_short(self):
        for kind in ['overall', 'weekly', 'short', 'month_code', None]:
            reading = copy.deepcopy(self.kimi)
            reading['windows'][0]['kind'] = kind
            self.assertFalse(self.kimi_check(reading=reading)['allowed'])
        self.assertFalse(self.kimi_check(reading=dict(self.kimi, windows=[]))['allowed'])
        for state in [None, 'unverified', 'mismatch']:
            self.assertFalse(self.kimi_check(reading=dict(self.kimi, account_binding={'state': state}))['allowed'])
        self.assertFalse(self.kimi_check(reading=dict(self.kimi, source='snapshot'))['allowed'])

    def test_kimi_all_existing_month_code_windows_must_be_valid_positive(self):
        value = copy.deepcopy(self.kimi)
        code = dict(value['windows'][0], name='monthCode', kind='month_code', source='kimi_code_account')
        value['windows'].append(code)
        self.assertTrue(self.kimi_check(reading=value)['allowed'])
        for patch in [dict(remaining=0, remaining_percent=0), dict(valid=False),
                      dict(remaining=None), dict(source='snapshot'), dict(expires_at=1021)]:
            reading = copy.deepcopy(value)
            reading['windows'][1].update(patch)
            self.assertFalse(self.kimi_check(reading=reading)['allowed'])

    def test_kimi_ambiguous_monthly_source_and_expiry_refuse(self):
        for patch in [dict(remaining=0, remaining_percent=0), dict(source='snapshot'),
                      dict(name='overall'), dict(expires_at=1021), dict(expires_at=None)]:
            value = copy.deepcopy(self.kimi)
            value['windows'][0].update(patch)
            self.assertFalse(self.kimi_check(reading=value)['allowed'])
        self.assertFalse(self.kimi_check(reading=dict(self.kimi, windows=self.kimi['windows'] * 2))['allowed'])

    def test_kimi_error_auth_rotation_prepaid_and_old_sample_refuse(self):
        self.assertFalse(self.kimi_check(block=dict(self.kimi_block, reason='credential_rejected'))['allowed'])
        self.assertFalse(self.kimi_check(current_inference_identity='rotated-test')['allowed'])
        self.assertFalse(self.kimi_check(prepaid=True)['allowed'])
        for patch in [dict(state='auth_error'), dict(stale=True), dict(sampled_at=1000),
                      dict(error='reauth_required'), dict(account_binding=None)]:
            self.assertFalse(self.kimi_check(reading=dict(self.kimi, **patch))['allowed'])

    def test_malformed_unhashable_names_and_huge_numbers_refuse_without_exception(self):
        value = copy.deepcopy(self.ark)
        value['windows'][0]['name'] = {}
        self.assertFalse(self.ark_check(reading=value)['allowed'])
        value = copy.deepcopy(self.ark)
        value['windows'][0]['remaining'] = 10 ** 1000
        self.assertFalse(self.ark_check(reading=value)['allowed'])

    def test_ark_cannot_accept_kimi_positive_weekly_as_generic_recovery(self):
        self.assertFalse(self.ark_check(reading=self.kimi)['allowed'])
        self.assertFalse(self.kimi_check(reading=self.ark)['allowed'])


if __name__ == '__main__':
    unittest.main()
