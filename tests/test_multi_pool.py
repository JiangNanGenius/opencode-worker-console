"""Focused tests for the generalized multi-pool routing curve and manual snapshots.

Covers the review cases: unknown telemetry on one pool must not erase a known
severely scarce peer, a depleted pool collapses to zero and returns after real
recovery, pay-as-you-go balances are never compared as subscription runways,
and manual Alibaba-style snapshots keep ordering, freshness and depletion-latch
semantics honest.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import quota
import routing


def three_pool_profiles():
    return {
        'kimi-k3': {'model': 'kimi-for-coding/k3', 'enabled': True},
        'ali-max': {'model': 'bailian-token-plan/qwen3.8-max', 'enabled': True},
        'ark-k3': {'model': 'volcengine-agent-plan/kimi-k3', 'enabled': True},
        'fallback': {'model': 'deepseek/deepseek-flash', 'enabled': True},
    }


def stage(*weights):
    names = ('kimi-k3', 'ali-max', 'ark-k3')[:len(weights)]
    return [{'profile': name, 'weight': weight} for name, weight in zip(names, weights)]


def pick(*pairs):
    return [{'profile': name, 'weight': weight} for name, weight in pairs]


class ScaledCurveTests(unittest.TestCase):
    def providers(self):
        return {'kimi-k3': 'kimi-for-coding', 'ali-max': 'bailian-token-plan',
                'ark-k3': 'volcengine-agent-plan', 'fallback': 'deepseek'}

    def test_unknown_provider_keeps_prior_without_erasing_known_scarcity(self):
        entries = pick(('ali-max', 4), ('ark-k3', 1))
        effective, reason, info = routing.dynamics(
            entries, self.providers(), adaptive={'curve': {'strength': 2}},
            runway_override={'bailian-token-plan': None, 'volcengine-agent-plan': 0.184})
        weights = {entry['profile']: entry['weight'] for entry in effective}
        self.assertGreater(weights['ali-max'], 90)
        self.assertLess(weights.get('ark-k3', 0), 5)
        self.assertIsNone(info['ali-max']['runway'])
        self.assertAlmostEqual(info['ark-k3']['runway'], 0.184)

    def test_fully_unknown_stage_keeps_baseline(self):
        entries = stage(5, 4, 1)
        effective, reason, _ = routing.dynamics(
            entries, self.providers(), adaptive={'curve': {'strength': 2}},
            runway_override={p: None for p in ('kimi-for-coding', 'bailian-token-plan',
                                               'volcengine-agent-plan')})
        self.assertEqual(reason, 'telemetry_unknown')
        self.assertEqual([entry['weight'] for entry in effective], [5, 4, 1])

    def test_unknown_peer_does_not_restore_zero_runway_pool(self):
        effective, _, _ = routing.dynamics(
            pick(('ali-max', 4), ('ark-k3', 1)), self.providers(),
            adaptive={'curve': {'strength': 2}},
            runway_override={'bailian-token-plan': None, 'volcengine-agent-plan': 0})
        self.assertEqual([e['profile'] for e in effective], ['ali-max'])

    def test_depleted_pool_drops_and_recovers_to_baseline(self):
        entries = stage(5, 4, 1)
        overrides = {'kimi-for-coding': 1.0, 'bailian-token-plan': 1.0, 'volcengine-agent-plan': 0.0}
        effective, reason, _ = routing.dynamics(
            entries, self.providers(), adaptive={'curve': {'strength': 2}}, runway_override=overrides)
        names = [entry['profile'] for entry in effective]
        self.assertNotIn('ark-k3', names)
        weights = {entry['profile']: entry['weight'] for entry in effective}
        self.assertGreater(weights['kimi-k3'], weights['ali-max'])
        # Real recovery returns the pool to its configured baseline share.
        overrides['volcengine-agent-plan'] = 1.0
        effective, reason, _ = routing.dynamics(
            entries, self.providers(), adaptive={'curve': {'strength': 2}}, runway_override=overrides)
        self.assertEqual(reason, 'runway_scaled_50_40_10')
        self.assertEqual([entry['weight'] for entry in effective], [50, 40, 10])

    def test_healthy_and_scarce_mix_returns_kimi_while_ark_collapses(self):
        entries = stage(5, 4, 1)
        effective, _, _ = routing.dynamics(
            entries, self.providers(), adaptive={'curve': {'strength': 2}},
            runway_override={'kimi-for-coding': 1.0, 'bailian-token-plan': 0.784,
                             'volcengine-agent-plan': 0.05})
        weights = {entry['profile']: entry['weight'] for entry in effective}
        self.assertGreater(weights['kimi-k3'], weights['ali-max'])
        self.assertTrue('ark-k3' not in weights or weights['ark-k3'] <= 1)

    def test_payg_balance_unknown_keeps_prior_in_mixed_stage(self):
        entries = pick(('fallback', 6), ('ali-max', 3))
        effective, reason, _ = routing.dynamics(
            entries, self.providers(), adaptive={'curve': {'strength': 2}},
            runway_override={'deepseek': None, 'bailian-token-plan': 0.5})
        weights = {entry['profile']: entry['weight'] for entry in effective}
        # Baseline is 6:3; an unknown payg balance keeps that prior while the
        # half-pace subscription pool gives up part of its share.
        self.assertGreater(weights['fallback'], 80)
        self.assertLess(weights['ali-max'], 20)

    def test_curve_record_validation_and_ladder_compatibility(self):
        profiles = three_pool_profiles()
        policy = {'deep': [stage(5, 4, 1)]}
        value = {'deep': {'0': {'curve': {'strength': 2}}}}
        self.assertEqual(routing.validate_dynamics(value, policy, profiles), value)
        with self.assertRaises(ValueError):
            routing.validate_dynamics({'deep': {'0': {'ladder': [[5, 4], [1, 1]]}}}, policy, profiles)
        with self.assertRaises(ValueError):
            routing.validate_dynamics({'deep': {'0': {'curve': {'strength': 0}}}}, policy, profiles)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.config = self.root / 'config.json'
        common.write_json(self.config, {
            'profiles': {'ali-max': {'model': 'bailian-token-plan/qwen3.8-max'},
                         'fallback': {'model': 'deepseek/deepseek-flash'}}})
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

    def iso(self, offset):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(self.now + offset, tz=timezone.utc).isoformat()

    def payload(self, remaining, captured, reset_offset=30 * 86400, limit=100000):
        return {'remaining': remaining, 'limit': limit, 'resets_at': self.iso(reset_offset),
                'captured_at': self.iso(captured), 'source': 'console snapshot'}

    def test_import_view_and_freshness(self):
        result = quota.import_snapshot('bailian-token-plan', self.payload(99926.94, 0), now=self.now)
        self.assertTrue(result['imported'])
        view = quota.view(common.read_json(self.state / 'quota.json', {}))
        record = view['bailian-token-plan']
        self.assertFalse(record['stale'])
        self.assertEqual(record['snapshot']['freshness'], 'fresh')
        self.assertTrue(record['available'])
        window = record['windows'][0]
        self.assertEqual(window['name'], 'CreditsPool')
        self.assertAlmostEqual(window['remaining_percent'], 99.927, places=2)

    def test_out_of_order_positive_import_cannot_erase_depletion(self):
        quota.import_snapshot('bailian-token-plan', self.payload(0, 0), now=self.now)
        view = quota.view(common.read_json(self.state / 'quota.json', {}))
        self.assertFalse(view['bailian-token-plan']['available'])
        with self.assertRaises(ValueError):
            # An older positive reading must not release a newer depletion latch.
            quota.import_snapshot('bailian-token-plan', self.payload(50000, -3600), now=self.now)
        view = quota.view(common.read_json(self.state / 'quota.json', {}))
        self.assertFalse(view['bailian-token-plan']['available'])
        self.assertFalse(quota.allowed('bailian-token-plan', view, 'normal')[0])

    def test_newer_positive_import_releases_depletion(self):
        quota.import_snapshot('bailian-token-plan', self.payload(0, 0), now=self.now)
        result = quota.import_snapshot('bailian-token-plan', self.payload(25000, 60), now=self.now + 60)
        self.assertTrue(result['imported'])
        self.assertTrue(result['recovery'])
        view = quota.view(common.read_json(self.state / 'quota.json', {}))
        self.assertTrue(view['bailian-token-plan']['available'])

    def test_expired_period_positive_reading_never_clears_depletion(self):
        quota.trip('bailian-token-plan', 'monthly limit reached', reason=quota.MONTHLY_REASON,
                   occurred_at=self.now - 60)
        quota.import_snapshot('bailian-token-plan',
                              self.payload(0, 0, reset_offset=30), now=self.now)
        self.assertFalse(quota.view(common.read_json(self.state / 'quota.json', {}))['bailian-token-plan']['available'])
        # A later-captured positive reading whose own period already expired is
        # history only: it must not erase the latch or release the circuit.
        result = quota.import_snapshot('bailian-token-plan',
                                       self.payload(50000, 60, reset_offset=30), now=self.now + 60)
        self.assertTrue(result['imported'])
        self.assertFalse(result['recovery'])
        with patch.object(quota.time, 'time', return_value=self.now + 600):
            view = quota.view(common.read_json(self.state / 'quota.json', {}))
            self.assertFalse(view['bailian-token-plan']['available'])
            self.assertTrue(view['bailian-token-plan']['snapshot']['expired'])
            self.assertIsNotNone(view['bailian-token-plan']['snapshot']['depleted_at'])
            self.assertFalse(quota.allowed('bailian-token-plan', view, 'normal')[0])
        self.assertIsNotNone(quota.billing_block('bailian-token-plan'))

    def test_stale_positive_reading_never_clears_depletion(self):
        quota.import_snapshot('bailian-token-plan', self.payload(0, 0), now=self.now)
        # Fresh for only five minutes; imported an hour later it is already stale.
        payload = self.payload(50000, 120, reset_offset=7 * 86400)
        payload['fresh_for_minutes'] = 5
        result = quota.import_snapshot('bailian-token-plan', payload, now=self.now + 3600)
        self.assertFalse(result['recovery'])
        with patch.object(quota.time, 'time', return_value=self.now + 3600):
            view = quota.view(common.read_json(self.state / 'quota.json', {}))
            self.assertFalse(view['bailian-token-plan']['available'])
            self.assertTrue(view['bailian-token-plan']['stale'])
            self.assertIsNotNone(view['bailian-token-plan']['snapshot']['depleted_at'])

    def test_period_expiring_between_capture_and_import_cannot_release(self):
        quota.import_snapshot('bailian-token-plan', self.payload(0, 0), now=self.now)
        quota.trip('bailian-token-plan', 'monthly limit reached', reason=quota.MONTHLY_REASON,
                   occurred_at=self.now)
        payload = self.payload(50000, 60, reset_offset=120)
        result = quota.import_snapshot('bailian-token-plan', payload, now=self.now + 180)
        self.assertFalse(result['recovery'])
        stored = quota.snapshot_store()['bailian-token-plan']
        self.assertIsNotNone(stored['depleted_at'])
        self.assertIsNotNone(quota.billing_block('bailian-token-plan'))

    def test_reset_clock_marks_snapshot_unknown_not_healthy(self):
        quota.import_snapshot('bailian-token-plan', self.payload(99900, 0, reset_offset=600), now=self.now)
        # One hour later the reading's reset has passed: old-period data is
        # stale and must not represent the new period as healthy allowance.
        with patch.object(quota.time, 'time', return_value=self.now + 3600):
            view = quota.view(common.read_json(self.state / 'quota.json', {}))
            record = view['bailian-token-plan']
            self.assertIsNone(record['available'])
            self.assertTrue(record['stale'])
            self.assertFalse(record['windows'][0]['valid'])
            ok, why = quota.allowed('bailian-token-plan', view, 'normal')
        self.assertTrue(ok)
        self.assertIn(why, ('quota_unknown', 'quota_available'))

    def test_ancient_positive_snapshot_is_stale(self):
        quota.import_snapshot('bailian-token-plan', self.payload(99900, -3 * 86400), now=self.now)
        view = quota.view(common.read_json(self.state / 'quota.json', {}))
        self.assertTrue(view['bailian-token-plan']['stale'])
        self.assertEqual(view['bailian-token-plan']['snapshot']['freshness'], 'stale')

    def test_identical_reimport_is_idempotent(self):
        first = quota.import_snapshot('bailian-token-plan', self.payload(1234, 0), now=self.now)
        second = quota.import_snapshot('bailian-token-plan', self.payload(1234, 0), now=self.now + 30)
        self.assertTrue(first['imported'])
        self.assertFalse(second['imported'])
        self.assertEqual(second['reason'], 'identical_snapshot')

    def test_positive_snapshot_releases_monthly_block_with_newer_evidence(self):
        quota.trip('bailian-token-plan', 'monthly limit reached', reason=quota.MONTHLY_REASON,
                   occurred_at=self.now - 7200)
        # Evidence captured before the block opened cannot release it.
        quota.import_snapshot('bailian-token-plan', self.payload(1000, -4 * 3600), now=self.now)
        self.assertIsNotNone(quota.billing_block('bailian-token-plan'))
        # Newer positive evidence (captured after the block opened) releases it.
        quota.import_snapshot('bailian-token-plan', self.payload(1000, 30), now=self.now + 60)
        block = quota.billing_block('bailian-token-plan')
        self.assertIsNone(block)
        released = common.read_json(self.state / 'billing.json')['bailian-token-plan']
        self.assertEqual(released['released'], quota.SNAPSHOT_RELEASE)

    def test_live_telemetry_provider_rejects_snapshot(self):
        with self.assertRaises(ValueError):
            quota.import_snapshot('deepseek', self.payload(1000, 0), now=self.now)

    def test_replenishment_is_observed_for_conservation(self):
        quota.import_snapshot('bailian-token-plan', self.payload(100, 0), now=self.now)
        first = quota._plan_window_observation(
            {'profiles': {'ali-max': {'model': 'bailian-token-plan/qwen3.8-max'}},
             'routing_policy': {'background': [[{'profile': 'ali-max', 'weight': 1}]]}},
            quota.view(common.read_json(self.state / 'quota.json', {})))
        quota.import_snapshot('bailian-token-plan', self.payload(60000, 3600), now=self.now + 3600)
        second = quota._plan_window_observation(
            {'profiles': {'ali-max': {'model': 'bailian-token-plan/qwen3.8-max'}},
             'routing_policy': {'background': [[{'profile': 'ali-max', 'weight': 1}]]}},
            quota.view(common.read_json(self.state / 'quota.json', {})))
        self.assertTrue(quota._observed_replenishment(first, second))


class ProviderAndPresetTests(unittest.TestCase):
    def test_bailian_provider_declares_only_fixed_models(self):
        import providers
        provider = providers.bailian_provider()
        self.assertEqual(set(provider['models']), {'qwen3.8-max', 'qwen3.8-flash'})
        for model in provider['models'].values():
            self.assertEqual(model['variants']['max']['effort'], 'xhigh')
            self.assertEqual(model['limit']['context'], 983616)
        self.assertNotIn('apiKey', provider['options'])
        with_key = providers.bailian_provider('/private/path/aliyun.key')
        self.assertEqual(with_key['options']['apiKey'], '{file:/private/path/aliyun.key}')
        for bad in ('relative.key', '/tmp/{injected}', '/tmp/with space', '/tmp/x\ny'):
            self.assertNotIn('apiKey', providers.bailian_provider(bad)['options'])

    def test_three_plan_preset_has_no_auto_and_fast_stays_deepseek(self):
        import install
        profiles = install.PRESETS['three-plan']
        self.assertNotIn('auto', profiles)
        policy = install.THREE_PLAN_ROUTING_POLICY
        routing.validate_policy(policy, profiles)
        routing.validate_dynamics(install.THREE_PLAN_ROUTING_DYNAMICS, policy, profiles)
        self.assertIn('1', install.THREE_PLAN_ROUTING_DYNAMICS['deep'])
        self.assertEqual(policy['fast'], [[{'profile': 'fallback', 'weight': 1}]])
        self.assertEqual(policy['fast'][0][0]['profile'], 'fallback')
        self.assertNotIn('aliyun-flash', [entry['profile'] for stage in policy['fast'] for entry in stage])
        # Deep is Qwen3.8 Max, Normal is K2.8 + Qwen3.8 Flash, Ultra is the
        # reserved K3.1 Pro stage; the retired K3 pair is absent.
        deep = [entry['profile'] for stage in policy['deep'] for entry in stage]
        self.assertIn('deep-research', deep)
        self.assertNotIn('ark-k3', deep)
        background = [entry['profile'] for stage in policy['background'] for entry in stage]
        self.assertIn('senior-code', background)
        self.assertIn('aliyun-flash', background)
        self.assertNotIn('ark-evolving', background)
        self.assertEqual(policy['ultra'], [[{'profile': install.KIMI_ULTRA_PROFILE,
                                             'weight': 1}]])
        self.assertEqual(profiles['deep-research']['model'], 'bailian-token-plan/qwen3.8-max')
        self.assertNotIn('kimi-k3', json.dumps(profiles))
        for name, profile in profiles.items():
            self.assertNotIn('/auto', profile['model'])


if __name__ == '__main__':
    unittest.main()
