import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ark_quota


class ArkQuotaTests(unittest.TestCase):
    def test_canonical_query_and_signature_are_deterministic_without_secret_output(self):
        query = ark_quota.canonical_query({'Version': '2024-01-01', 'Action': 'GetAFPUsage'})
        self.assertEqual(query, 'Action=GetAFPUsage&Version=2024-01-01')
        headers = ark_quota.sign('AK_TEST', 'SK_TEST', query, b'{}',
                                 datetime(2026, 9, 20, tzinfo=timezone.utc))
        self.assertIn('Credential=AK_TEST/', headers['Authorization'])
        self.assertNotIn('SK_TEST', json.dumps(headers))
        self.assertEqual(ark_quota._redact_auth(headers)['Authorization'], '<redacted>')

    def test_normalization_keeps_four_windows_and_marks_zero_unavailable(self):
        result = {'PlanType': 'Max'}
        for index, (name, _) in enumerate(ark_quota.AFP_WINDOWS):
            result[name] = {'Quota': 1000, 'Used': 1000 if index == 0 else 100,
                            'SubscribeTime': 1700000000000, 'ResetTime': 1700010000000}
        value = ark_quota.normalize_afp_usage(result)
        self.assertEqual(len(value['windows']), 4)
        self.assertFalse(value['available'])
        self.assertEqual(value['plan']['type'], 'Max')
        self.assertEqual(value['windows'][0]['remaining'], 0)

    def test_environment_pair_is_used_only_when_no_reference_is_registered(self):
        with patch('credentials._load_registry', return_value={}), patch.dict(
                os.environ, {'VOLC_ACCESSKEY': 'ak-env', 'VOLC_SECRETKEY': 'sk-env'}, clear=False):
            self.assertEqual(ark_quota.resolve_credentials(), ('ak-env', 'sk-env', 'environment'))
        broken = {'volcengine-control-ak': {'source': 'file', 'path': '/missing'}}
        with patch('credentials._load_registry', return_value=broken), patch.dict(
                os.environ, {'VOLC_ACCESSKEY': 'ak-env', 'VOLC_SECRETKEY': 'sk-env'}, clear=False):
            self.assertEqual(ark_quota.resolve_credentials(), (None, None, None))

    def test_fetch_returns_only_safe_metadata(self):
        usage = {'Result': {'PlanType': 'Max', 'AFPMonthly': {'Quota': 500000, 'Used': 12,
                  'SubscribeTime': 1700000000000, 'ResetTime': 1800000000000}}}
        plan = {'Result': {'PlanType': 'Max', 'Status': 'Running', 'AutoRenew': False,
                          'StartTime': '2026-09-20T09:12:11Z', 'EndTime': '2026-10-20T15:59:59Z'}}
        with patch.object(ark_quota, 'resolve_credentials', return_value=('ak-secret', 'sk-secret', 'credential_reference')), \
             patch.object(ark_quota, 'call', side_effect=[usage, plan]):
            result = ark_quota.fetch()
        raw = json.dumps(result)
        self.assertEqual(result['state'], 'ok')
        self.assertEqual(result['credential_source'], 'credential_reference')
        self.assertNotIn('ak-secret', raw)
        self.assertNotIn('sk-secret', raw)
        self.assertTrue(result['plan']['active'])


if __name__ == '__main__':
    unittest.main()
