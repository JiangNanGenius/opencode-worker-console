"""Plan-scoped admission must never attach Team Credits to Personal inference."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import bailian_quota
import common
import management
import quota
from providers import BAILIAN_PROVIDER as PROVIDER


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class PersonalPlanAccounting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.configuration = self.root / 'config.json'
        self.now = time.time()
        self.c = {'profiles': {'max': {'model': PROVIDER + '/qwen3.8-max'}},
                  'bailian_plan_edition': 'personal', 'max_parallel_per_owner': 4,
                  'kimi_reserve_percent': 0,
                  'routing': {'fast': 'max', 'background': 'max', 'deep': 'max'},
                  'bailian_pool': {'enabled': True, 'slots': [{'id': 'personal', 'keychain':
                      {'service': 'test-only', 'account': 'personal'}}]},
                  'budget_signals': {PROVIDER: {'mode': 'prepaid'}}}
        self.patchers = [patch.object(common, 'STATE', self.state),
                         patch.object(quota, 'STATE', self.state),
                         patch.object(common, 'CONFIG', self.configuration),
                         patch.object(common, 'credential_identity', return_value='current-inference'),
                         patch.object(quota, 'credential_identity', return_value='current-inference'),
                         patch.object(quota, 'billing_block', return_value=None),
                         patch('bailian_pool.recovery_available', return_value={'configured': False})]
        for item in self.patchers:
            item.start()
        common.init()
        common.write_json(self.configuration, self.c)

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.tmp.cleanup()

    def record(self, remaining, personal=False):
        value = {'state': 'ok', 'source': bailian_quota.SOURCE_LIVE,
                 'sampled_at': self.now, 'available': remaining > 0,
                 'windows': [{'name': 'Credits(pool)', 'limit': 100, 'remaining': remaining,
                              'remaining_percent': remaining, 'valid': True,
                              'duration_minutes': 43200, 'resets_at': iso(self.now + 86400)}],
                 'balances': [], '_credential': 'old-team-control'}
        if personal:
            value['windows'] = [{'name': 'Monthly', 'unit': 'percent',
                                 'remaining_percent': remaining, 'valid': True,
                                 'duration_minutes': 43200, 'resets_at': iso(self.now + 86400)}]
            value.update(plan_edition='personal',
                         _plan_identity=bailian_quota.credential_identity(),
                         _credential=bailian_quota.credential_identity(),
                         source='personal_console')
        return value

    def snapshot(self, remaining):
        return {'limit': 100, 'remaining': remaining, 'captured_at': iso(self.now),
                'resets_at': iso(self.now + 86400), 'fresh_for_minutes': 60,
                'depleted_at': iso(self.now) if remaining == 0 else None}

    def view(self, record):
        return quota.view({PROVIDER: record})

    def test_team_zero_does_not_block_personal_before_refresh(self):
        q = self.view(self.record(0))
        self.assertEqual(q[PROVIDER]['plan_edition'], 'personal')
        self.assertEqual(q[PROVIDER]['windows'], [])
        self.assertIsNone(q[PROVIDER]['available'])
        self.assertTrue(quota.allowed(PROVIDER, q, 'normal')[0])

    def test_team_positive_is_not_personal_balance(self):
        q = self.view(self.record(85))
        self.assertEqual(q[PROVIDER]['state'], 'unknown')
        self.assertEqual(q[PROVIDER]['windows'], [])
        self.assertEqual(q[PROVIDER]['capacity_forecast']['windows'], [])

    def test_legacy_team_snapshot_cannot_reappear(self):
        common.write_json(self.state / quota.SNAPSHOTS, {PROVIDER: self.snapshot(0)})
        for value in ({}, {PROVIDER: self.record(0)}, {PROVIDER: bailian_quota.unknown_record()}):
            q = quota.view(value)
            self.assertEqual(q[PROVIDER]['windows'], [])
            self.assertTrue(quota.allowed(PROVIDER, q, 'normal')[0])
        self.assertIn(PROVIDER, quota.snapshot_store())  # History was not erased.

    def test_refresh_never_queries_or_revives_team_source(self):
        common.write_json(self.state / 'quota.json', {PROVIDER: self.record(0)})
        common.write_json(self.state / quota.SNAPSHOTS, {PROVIDER: self.snapshot(0)})
        with patch.object(bailian_quota, '_call', side_effect=AssertionError('Team API called')):
            for _ in range(2):
                q = quota.refresh(force=True)
                self.assertEqual(q[PROVIDER]['plan_edition'], 'personal')
                self.assertEqual(q[PROVIDER]['windows'], [])
                self.assertTrue(quota.allowed(PROVIDER, q, 'normal')[0])

    def test_same_personal_zero_stays_enforced(self):
        q = self.view(self.record(0, personal=True))
        self.assertEqual(q[PROVIDER]['windows'][0]['remaining_percent'], 0)
        self.assertNotIn('remaining', q[PROVIDER]['windows'][0])
        self.assertFalse(quota.allowed(PROVIDER, q, 'normal')[0])
        self.assertNotIn('_plan_identity', q[PROVIDER])
        self.assertNotIn('_credential', q[PROVIDER])

    def test_foreign_personal_binding_is_unknown(self):
        value = self.record(0, personal=True)
        value['_plan_identity'] = 'previous-personal-key'
        q = self.view(value)
        self.assertEqual(q[PROVIDER]['windows'], [])
        self.assertTrue(quota.allowed(PROVIDER, q, 'normal')[0])

    def test_same_personal_zero_survives_quota_session_failure(self):
        old = self.record(0, personal=True)
        common.write_json(self.state / 'quota.json', {PROVIDER: old})
        with patch('bailian_personal.fetch', return_value=bailian_quota.unknown_record()):
            q = quota.refresh(force=True)
        self.assertEqual(q[PROVIDER]['sampled_at'], old['sampled_at'])
        self.assertEqual(q[PROVIDER]['windows'][0]['remaining_percent'], 0)
        self.assertFalse(quota.allowed(PROVIDER, q, 'normal')[0])
        self.assertTrue(q[PROVIDER]['stale'])

    def test_current_personal_auth_circuit_is_not_bypassed(self):
        block = {'reason': quota.AUTH_REASON, 'opened_at': self.now, 'message': 'Rejected'}
        with patch.object(quota, 'billing_block', return_value=block):
            q = self.view(self.record(90, personal=True))
            self.assertFalse(quota.allowed(PROVIDER, q, 'normal')[0])

    def test_old_history_is_not_personal_burn_rate(self):
        common.write_json(self.state / quota.HISTORY,
                          {PROVIDER: {'_credential': 'old-team-control',
                                      'samples': [{'time': self.now - 60, 'windows': {}}]}})
        with patch.object(quota, 'consumption_estimate', return_value=None) as estimate:
            self.view(self.record(75, personal=True))
        self.assertEqual(estimate.call_args.args[1], [])

    def test_new_personal_snapshot_gets_binding_not_team_depletion(self):
        common.write_json(self.state / quota.SNAPSHOTS, {PROVIDER: self.snapshot(0)})
        common.write_json(self.state / 'quota.json', {PROVIDER: self.record(0)})
        payload = self.snapshot(80)
        payload.pop('depleted_at')
        payload['captured_at'] = iso(self.now + 1)
        result = quota.import_snapshot(PROVIDER, payload, now=self.now + 1)
        self.assertTrue(result['imported'])
        raw = common.read_json(self.state / 'quota.json')[PROVIDER]
        self.assertEqual(raw['plan_edition'], 'personal')
        self.assertIsNone(raw['snapshot']['depleted_at'])
        self.assertTrue(quota.allowed(PROVIDER, quota.view({PROVIDER: raw}), 'normal')[0])

    def test_newer_personal_live_sample_wins_over_manual_anchor(self):
        live = self.record(80, personal=True)
        common.write_json(self.state / 'quota.json', {PROVIDER: live})
        payload = self.snapshot(0)
        payload.pop('depleted_at')
        payload['captured_at'] = iso(self.now - 5)
        quota.import_snapshot(PROVIDER, payload, now=self.now)
        cached = common.read_json(self.state / 'quota.json')[PROVIDER]
        self.assertEqual(cached['source'], 'personal_console')
        self.assertEqual(quota.view({PROVIDER: cached})[PROVIDER]['windows'][0]['remaining_percent'], 80)

    def test_edition_changes_telemetry_identity_and_control_epoch(self):
        personal = bailian_quota.credential_identity()
        old_signature = quota._control_policy(self.c)
        self.c['bailian_plan_edition'] = 'team'
        common.write_json(self.configuration, self.c)
        with patch('aliyun_openapi.credential_identity', return_value='team-control'):
            self.assertEqual(bailian_quota.credential_identity(), 'team-control')
            self.assertNotEqual(personal, bailian_quota.credential_identity())
        self.assertNotEqual(old_signature, quota._control_policy(self.c))

    def test_default_team_keeps_existing_zero_semantics(self):
        self.c.pop('bailian_plan_edition')
        common.write_json(self.configuration, self.c)
        q = self.view(self.record(0))
        self.assertEqual(q[PROVIDER]['windows'][0]['remaining'], 0)
        self.assertFalse(quota.allowed(PROVIDER, q, 'normal')[0])

    def test_management_validates_and_exports_plan_enum(self):
        self.assertEqual(management.settings()['bailian_plan_edition'], 'personal')
        for edition in ('team', 'personal'):
            body = dict(self.c, bailian_plan_edition=edition)
            self.assertEqual(management._validate_settings(body)['bailian_plan_edition'], edition)
        for edition in (None, True, 'enterprise', {}, []):
            with self.subTest(edition=edition), self.assertRaises(ValueError):
                management._validate_settings(dict(self.c, bailian_plan_edition=edition))


if __name__ == '__main__':
    unittest.main()
