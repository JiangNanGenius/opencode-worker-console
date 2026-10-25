"""Prepaid credit-seat policy, key-specific recovery and Alibaba alert edges."""
import json
import math
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import bailian_quota
import common
import diagnostics
import economics
import notifications
import quota
import routing
import worker
from providers import BAILIAN_PROVIDER

NOW = 2_000_000_000


def iso(offset):
    return datetime.fromtimestamp(NOW + offset, tz=timezone.utc).isoformat()


def record(percent, hours_left, *, available=True, state='ok', stale=False,
           duration=43200, rate=None):
    value = {'state': state, 'available': available, 'stale': stale,
             'sampled_at': NOW,
             'windows': [{'valid': True, 'remaining_percent': percent,
                          'remaining': percent, 'limit': 100,
                          'duration_minutes': duration,
                          'resets_at': iso(hours_left * 3600)}]}
    if rate is not None:
        value['windows'][0]['consumption_estimate'] = {'rate_percent_per_hour': rate,
                                                       'hours': percent / rate}
    return value


def config(*, prepaid=True):
    c = {
        'profiles': {
            'deep-research': {'model': 'kimi-for-coding/k3', 'enabled': True},
            'aliyun-max': {'model': BAILIAN_PROVIDER + '/qwen3.8-max', 'enabled': True},
            'senior-code': {'model': 'kimi-for-coding/kimi-for-coding', 'enabled': True},
            'aliyun-flash': {'model': BAILIAN_PROVIDER + '/qwen3.8-flash', 'enabled': True},
            'ark-evolving': {'model': 'volcengine-agent-plan/doubao-seed-evolving', 'enabled': True},
            'ark-k3': {'model': 'volcengine-agent-plan/kimi-k3', 'enabled': True},
            'fallback': {'model': 'deepseek/deepseek-flash', 'enabled': True},
        },
        'routing_policy': {
            'fast': [[{'profile': 'fallback', 'weight': 1}]],
            'background': [[{'profile': 'senior-code', 'weight': 4},
                            {'profile': 'aliyun-flash', 'weight': 4},
                            {'profile': 'ark-evolving', 'weight': 2}]],
            'deep': [[{'profile': 'deep-research', 'weight': 5},
                      {'profile': 'aliyun-max', 'weight': 4},
                      {'profile': 'ark-k3', 'weight': 1}]],
        },
        'routing_dynamics': {'deep': {'0': {'curve': {'strength': 4}}},
                             'background': {'0': {'curve': {'strength': 4}}}},
        'quota_spillover': {'enabled': True, 'profile': 'fallback',
                            'tiers': ['background'], 'max_share_percent': 33,
                            'level2_runway_percent': 25},
        'fast_bias_runway_percent': 38,
    }
    if prepaid:
        c['budget_signals'] = {BAILIAN_PROVIDER: {'mode': 'prepaid'}}
    return c


def depleted_peer():
    return record(4, 700, duration=10080)


class PrepaidValidationTests(unittest.TestCase):
    def test_prepaid_mode_validates_without_extra_fields(self):
        profiles = config()['profiles']
        value = quota.validate_budget_signals(
            {BAILIAN_PROVIDER: {'mode': 'prepaid'}}, profiles)
        self.assertEqual(value, {BAILIAN_PROVIDER: {'mode': 'prepaid'}})
        self.assertEqual(quota.prepaid_providers(config()), {BAILIAN_PROVIDER})
        # Extra fields are ignored for a mode that has no settings of its own.
        self.assertEqual(quota.validate_budget_signals(
            {BAILIAN_PROVIDER: {'mode': 'prepaid', 'remaining_percent': 5}}, profiles),
            {BAILIAN_PROVIDER: {'mode': 'prepaid'}})
        with self.assertRaises(ValueError):
            quota.validate_budget_signals(
                {BAILIAN_PROVIDER: {'mode': 'unknown'}}, profiles)


class PrepaidControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / 'state'
        self.c = config()
        self.patchers = [patch.object(common, 'STATE', self.state),
                         patch.object(quota, 'STATE', self.state),
                         patch.object(quota, 'config', return_value=self.c),
                         patch.object(quota, 'credential_identity', return_value='inference')]
        for item in self.patchers:
            item.start()
        common.init()

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.tmp.cleanup()

    def test_depleted_renewable_peers_do_not_squeeze_or_downgrade_prepaid(self):
        q = {'kimi-for-coding': depleted_peer(),
             'volcengine-agent-plan': depleted_peer(),
             BAILIAN_PROVIDER: record(72, 1000, rate=.1)}
        with patch.object(quota.time, 'time', return_value=NOW):
            guidance = quota.tier_guidance(self.c, q, _raw=True)
        self.assertEqual(guidance['prepaid_providers'], [BAILIAN_PROVIDER])
        self.assertEqual(guidance['provider_runway'][BAILIAN_PROVIDER], 1.0)
        self.assertEqual(guidance['provider_conservation_level'][BAILIAN_PROVIDER], 0)
        self.assertLess(guidance['provider_runway']['kimi-for-coding'], 1.0)
        # Other plans still protect their own quotas.
        self.assertGreater(guidance['provider_conservation_level']['kimi-for-coding'], 0)
        # Weight attenuation follows the same neutral signal.
        override = quota._dynamic_runway_override(
            'deep', {'kimi': 'kimi-for-coding', 'ali': BAILIAN_PROVIDER}, q, guidance)
        self.assertEqual(override[BAILIAN_PROVIDER], 1.0)
        self.assertLess(override['kimi-for-coding'], 1.0)
        entries = [{'profile': 'ali', 'weight': 4}, {'profile': 'kimi', 'weight': 5}]
        effective, reason, _ = routing.dynamics(
            entries, {'ali': BAILIAN_PROVIDER, 'kimi': 'kimi-for-coding'},
            adaptive={'curve': {'strength': 4}}, runway_override=override)
        weights = {entry['profile']: entry['weight'] for entry in effective}
        self.assertGreater(weights['ali'], entries[0]['weight'])
        # Global Level 2 must not replace Alibaba Max with Flash.
        stage = self.c['routing_policy']['deep'][0]
        conserved = quota._conserved_deep_stage(
            self.c, q, stage, {'conservation_level': 2,
                               'prepaid_providers': [BAILIAN_PROVIDER]})
        self.assertEqual([entry['profile'] for entry in conserved],
                         ['deep-research', 'aliyun-max', 'ark-k3'])
        # A Normal stage with usable prepaid capacity is not rerouted to Fast.
        self.assertFalse(quota._level2_conservation_active(
            'background', self.c['quota_spillover'], {'conservation_level': 2},
            prepaid_available=True))

    def test_control_query_auth_and_rate_errors_are_not_inference_failures(self):
        positive = record(72, 1000)
        positive.update(state='auth_error', stale=True,
                        live_error={'state': 'auth_error'})
        q = {BAILIAN_PROVIDER: positive}
        self.assertTrue(quota.allowed(BAILIAN_PROVIDER, q, 'normal', prepaid=True)[0])
        self.assertFalse(quota.allowed(BAILIAN_PROVIDER, q, 'normal', prepaid=False)[0])
        positive.update(state='rate_limited')
        self.assertTrue(quota.allowed(BAILIAN_PROVIDER, q, 'normal', prepaid=True)[0])
        # Confirmed zero, expiry and an inference circuit remain hard gates.
        zero = record(0, 1000, available=False)
        self.assertFalse(quota.allowed(BAILIAN_PROVIDER, {BAILIAN_PROVIDER: zero},
                                       'normal', prepaid=True)[0])
        expired = {'state': 'ok', 'available': None, 'stale': False,
                   'windows': [{'valid': False, 'expired': True, 'remaining_percent': None,
                                'remaining': 0, 'limit': 100,
                                'resets_at': iso(-3600)}]}
        self.assertFalse(quota.allowed(BAILIAN_PROVIDER, {BAILIAN_PROVIDER: expired},
                                       'normal', prepaid=True)[0])
        quota.trip(BAILIAN_PROVIDER, 'Insufficient Balance', 'm-402',
                   reason=quota.BALANCE_REASON)
        self.assertFalse(quota.allowed(BAILIAN_PROVIDER, q, 'normal', prepaid=True)[0])

    def test_view_keeps_real_estimates_and_reset_but_not_calendar_pace(self):
        q = {BAILIAN_PROVIDER: record(72, 1000, rate=.5)}
        prepaid = quota.view(q)[BAILIAN_PROVIDER]
        with patch.object(quota, 'config', return_value=config(prepaid=False)):
            normal = quota.view(q)[BAILIAN_PROVIDER]
        self.assertTrue(prepaid['prepaid'])
        self.assertFalse(normal.get('prepaid', False))
        window = prepaid['windows'][0]
        self.assertEqual(window['resets_at'], iso(1000 * 3600))
        self.assertAlmostEqual(window['remaining_percent'], 72)
        # The real observed/prior runtime estimate and reset survive unchanged.
        self.assertAlmostEqual(window['capacity_forecast']['hours'],
                               normal['windows'][0]['capacity_forecast']['hours'])
        self.assertEqual(window['capacity_forecast']['resets_at'],
                         normal['windows'][0]['capacity_forecast']['resets_at'])
        # Only the calendar pace ratio is neutralized for the refillable pool.
        self.assertEqual(window['capacity_forecast']['runway'], 1.0)
        self.assertEqual(prepaid['capacity_forecast']['runway'], 1.0)
        self.assertNotEqual(normal['capacity_forecast']['runway'], 1.0)

    def test_work_pool_display_includes_prepaid_control_can_exclude_it(self):
        q = {'kimi-for-coding': record(50, 1000, duration=10080, rate=.1),
             BAILIAN_PROVIDER: record(72, 1000, rate=.1),
             'volcengine-agent-plan': record(40, 1000, duration=10080, rate=.1)}
        display = economics.work_pool(self.c, q, now=NOW)
        control = economics.work_pool(self.c, q, now=NOW, include_payg_balance=False,
                                      exclude_providers={BAILIAN_PROVIDER})
        self.assertIsNotNone(display['components'][BAILIAN_PROVIDER]['capacity'])
        self.assertGreater(display['total'], control['total'])
        self.assertEqual(control['complete'], True)


class PrepaidRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / 'state'
        self.c = config()
        self.patchers = [patch.object(common, 'STATE', self.state),
                         patch.object(quota, 'STATE', self.state),
                         patch.object(quota, 'config', return_value=self.c),
                         patch.object(quota, 'credential_identity', return_value='inference'),
                         patch.object(bailian_quota, 'credential_identity', return_value='control')]
        for item in self.patchers:
            item.start()
        common.init()

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.tmp.cleanup()

    def positive_value(self):
        return {'state': 'ok', 'source': 'openapi_live', 'available': True,
                'sampled_at': NOW - 1,
                'windows': [{'valid': True, 'remaining': 80, 'limit': 100,
                             'resets_at': iso(1000)}]}

    def test_positive_aggregate_never_clears_prepaid_current_key_block(self):
        quota.trip(BAILIAN_PROVIDER, 'Insufficient Balance', reason=quota.BALANCE_REASON)
        self.assertIsNotNone(quota.billing_block(BAILIAN_PROVIDER))
        self.assertFalse(quota.account_api_recovery(
            BAILIAN_PROVIDER, NOW - 1, 'inference', 'control',
            now=NOW, value=self.positive_value()))
        self.assertIsNotNone(quota.billing_block(BAILIAN_PROVIDER))
        self.assertEqual(quota._unrecovered_provider(
            BAILIAN_PROVIDER, quota.BALANCE_REASON, self.c), BAILIAN_PROVIDER)

    def test_non_prepaid_monthly_api_recovery_is_still_available(self):
        non_prepaid = config(prepaid=False)
        with patch.object(quota, 'config', return_value=non_prepaid), \
                patch.object(quota, 'credential_identity', return_value='inference'):
            quota.trip(BAILIAN_PROVIDER, 'monthly limit reached', 'm-1',
                       reason=quota.MONTHLY_REASON)
            self.assertTrue(quota.account_api_recovery(
                BAILIAN_PROVIDER, NOW - 1, 'inference', 'control',
                now=NOW, value=self.positive_value()))

    def test_current_key_success_recovers_auth_and_prepaid_balance(self):
        quota.trip(BAILIAN_PROVIDER, 'Insufficient Balance', 'm-b',
                   reason=quota.BALANCE_REASON)
        opened = quota.billing_block(BAILIAN_PROVIDER)['opened_at']
        self.assertTrue(quota.observe_model_success(
            BAILIAN_PROVIDER, 'inference', opened + 1, opened + 2))
        self.assertIsNone(quota.billing_block(BAILIAN_PROVIDER))
        self.assertEqual(quota._unrecovered_provider(
            BAILIAN_PROVIDER, quota.BALANCE_REASON, self.c), None)

        quota.trip(BAILIAN_PROVIDER, '401 invalid key', 'm-a',
                   reason=quota.AUTH_REASON)
        opened = quota.billing_block(BAILIAN_PROVIDER)['opened_at']
        self.assertTrue(quota.observe_model_success(
            BAILIAN_PROVIDER, 'inference', opened + 1, opened + 2))
        non_prepaid = config(prepaid=False)
        quota.trip(BAILIAN_PROVIDER, 'Insufficient Balance', 'm-b2',
                   reason=quota.BALANCE_REASON)
        opened = quota.billing_block(BAILIAN_PROVIDER)['opened_at']
        with patch.object(quota, 'config', return_value=non_prepaid):
            self.assertFalse(quota.observe_model_success(
                BAILIAN_PROVIDER, 'inference', opened + 1, opened + 2))
            self.assertIsNotNone(quota.billing_block(BAILIAN_PROVIDER))
            # Auth rejection is credential-specific even outside prepaid mode.
            quota.trip(BAILIAN_PROVIDER, '401 invalid key', 'm-a2',
                       reason=quota.AUTH_REASON)
            opened = quota.billing_block(BAILIAN_PROVIDER)['opened_at']
            self.assertTrue(quota.observe_model_success(
                BAILIAN_PROVIDER, 'inference', opened + 1, opened + 2))
            self.assertIsNone(quota.billing_block(BAILIAN_PROVIDER))

    def test_key_rotation_detaches_but_control_rotation_never_clears(self):
        quota.trip(BAILIAN_PROVIDER, 'Insufficient Balance', reason=quota.BALANCE_REASON)
        with patch.object(quota, 'credential_identity', return_value='new-key'):
            self.assertIsNone(quota.billing_block(BAILIAN_PROVIDER))
            self.assertIsNone(quota._unrecovered_provider(
                BAILIAN_PROVIDER, quota.BALANCE_REASON, self.c))
        # Rotating the separate quota-query AK/SK pair is not inference recovery.
        with patch('bailian_quota.credential_identity', return_value='control-2'):
            self.assertFalse(quota.account_api_recovery(
                BAILIAN_PROVIDER, NOW - 1, 'inference', 'control-2',
                now=NOW, value=self.positive_value()))
        with patch.object(quota, 'credential_identity', return_value='inference'):
            self.assertIsNotNone(quota.billing_block(BAILIAN_PROVIDER))

    def test_snapshot_display_anchor_does_not_release_prepaid_block(self):
        quota.trip(BAILIAN_PROVIDER, 'Insufficient Balance', reason=quota.BALANCE_REASON)
        result = quota.import_snapshot(BAILIAN_PROVIDER, {
            'remaining': 50000, 'limit': 100000,
            'captured_at': iso(1), 'resets_at': iso(30 * 86400),
            'source': 'console snapshot'}, now=NOW + 2)
        self.assertTrue(result['imported'])
        self.assertFalse(result['recovery'])
        self.assertIsNotNone(quota.billing_block(BAILIAN_PROVIDER))

    def test_explicit_operator_retry_still_releases(self):
        quota.trip(BAILIAN_PROVIDER, 'Insufficient Balance', reason=quota.BALANCE_REASON)
        self.assertTrue(quota.retry_provider(BAILIAN_PROVIDER)['released'])
        self.assertIsNone(quota.billing_block(BAILIAN_PROVIDER))


class PrepaidNotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / 'state'
        self.identity = 'opaque-identity-1'
        self.patchers = [patch.object(common, 'STATE', self.state),
                         patch.object(quota, 'STATE', self.state),
                         patch.object(quota, 'credential_identity',
                                      return_value=self.identity)]
        for item in self.patchers:
            item.start()
        common.init()
        self.settings = {'notifications': {'enabled': True, 'quota_transitions': True,
                                           'credentials': ['bark-endpoint']}}

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.tmp.cleanup()

    def snapshot(self, percent, available=True, state='ok', stale=False):
        return {BAILIAN_PROVIDER: {
            'state': state, 'stale': stale, 'available': available,
            'windows': [{'valid': True, 'remaining_percent': percent,
                         'remaining': percent, 'limit': 100,
                         'resets_at': iso(30 * 86400)}]}}

    def test_low_warning_exhaustion_dedup_and_new_key_rearm(self):
        with patch.object(notifications.quota, 'credential_identity',
                          return_value=self.identity), \
                patch.object(notifications, 'send', return_value={'sent': True}) as send:
            settings = notifications.normalize(self.settings['notifications'])
            notifications._observe_alibaba_credits(settings, self.snapshot(12))
            self.assertEqual(send.call_count, 0)
            notifications._observe_alibaba_credits(settings, self.snapshot(10))
            self.assertEqual(send.call_count, 1)
            notifications._observe_alibaba_credits(settings, self.snapshot(9))
            self.assertEqual(send.call_count, 1)
            notifications._observe_alibaba_credits(settings, self.snapshot(0, available=False))
            self.assertEqual(send.call_count, 2)
            notifications._observe_alibaba_credits(settings, self.snapshot(0, available=False))
            self.assertEqual(send.call_count, 2)
            body = send.call_args.args[1]
            self.assertIn('坐席', body)
            self.assertIn('Token Plan 推理 API Key', body)
            self.assertNotIn(self.identity, body)
            self.assertNotIn(self.identity, send.call_args.args[0])
            # Healthy refill rearms the same key's next crossing.
            notifications._observe_alibaba_credits(settings, self.snapshot(50))
            notifications._observe_alibaba_credits(settings, self.snapshot(8))
            self.assertEqual(send.call_count, 3)
        with patch.object(notifications.quota, 'credential_identity',
                          return_value='opaque-identity-2'), \
                patch.object(notifications, 'send', return_value={'sent': True}) as send:
            notifications._observe_alibaba_credits(
                notifications.normalize(self.settings['notifications']),
                self.snapshot(0, available=False))
            self.assertEqual(send.call_count, 1)

    def test_observe_quota_integrates_the_alibaba_credit_edge(self):
        cfg = {'notifications': self.settings['notifications'],
               'profiles': {'aliyun-max': {'model': BAILIAN_PROVIDER + '/qwen3.8-max'}}}
        with patch.object(notifications.quota, 'credential_identity',
                          return_value=self.identity), \
                patch.object(notifications.quota, 'tier_guidance', return_value={}), \
                patch.object(notifications.quota, 'route', return_value=('aliyun-max', 'ok')), \
                patch.object(notifications, 'send', return_value={'sent': True}) as send:
            notifications.observe_quota(cfg, self.snapshot(10))
            self.assertEqual(send.call_count, 1)
            notifications.observe_quota(cfg, self.snapshot(9))
            self.assertEqual(send.call_count, 1)

    def test_direct_402_and_401_actions_are_distinct_and_deduped(self):
        with patch.object(notifications.quota, 'credential_identity',
                          return_value=self.identity), \
                patch.object(notifications, 'send', return_value={'sent': True}) as send:
            notifications.observe_provider_failure(
                self.settings, BAILIAN_PROVIDER, quota.BALANCE_REASON)
            self.assertEqual(send.call_count, 1)
            self.assertIn('坐席', send.call_args.args[1])
            notifications.observe_provider_failure(
                self.settings, BAILIAN_PROVIDER, quota.BALANCE_REASON)
            self.assertEqual(send.call_count, 1)
            notifications.observe_provider_failure(
                self.settings, BAILIAN_PROVIDER, quota.AUTH_REASON)
            self.assertEqual(send.call_count, 2)
            auth_body = send.call_args.args[1]
            self.assertIn('推理 Key', auth_body)
            self.assertIn('控制面 AK/SK', auth_body)
            self.assertNotIn(self.identity, auth_body)
            notifications.observe_provider_failure(
                self.settings, BAILIAN_PROVIDER, quota.AUTH_REASON)
            self.assertEqual(send.call_count, 2)
            # Native 429 and generic billing errors never ask for a seat/key.
            for reason in (quota.WINDOW_REASON, quota.GENERIC_REASON):
                self.assertEqual(notifications.observe_provider_failure(
                    self.settings, BAILIAN_PROVIDER, reason), [])
            self.assertEqual(send.call_count, 2)
            # Other providers are out of scope.
            self.assertEqual(notifications.observe_provider_failure(
                self.settings, 'kimi-for-coding', quota.BALANCE_REASON), [])
            self.assertEqual(send.call_count, 2)

    def test_positive_telemetry_cannot_rearm_an_active_key_failure(self):
        settings = notifications.normalize(self.settings['notifications'])
        with patch.object(notifications, 'send', return_value={'sent': True}) as send:
            quota.trip(BAILIAN_PROVIDER, 'Insufficient Balance', 'm-402',
                       reason=quota.BALANCE_REASON)
            notifications.observe_provider_failure(
                self.settings, BAILIAN_PROVIDER, quota.BALANCE_REASON)
            self.assertEqual(send.call_count, 1)
            # Another seat's healthy aggregate is not proof for this key and must
            # not reset the alert edge.
            self.assertEqual(notifications._observe_alibaba_credits(
                settings, self.snapshot(80)), [])
            notifications.observe_provider_failure(
                self.settings, BAILIAN_PROVIDER, quota.BALANCE_REASON)
            self.assertEqual(send.call_count, 1)
            # The same rule holds while the inference key is rejected.
            quota.trip(BAILIAN_PROVIDER, '401 invalid key', 'm-401',
                       reason=quota.AUTH_REASON)
            self.assertEqual(notifications._observe_alibaba_credits(
                settings, self.snapshot(80)), [])
            notifications.observe_provider_failure(
                self.settings, BAILIAN_PROVIDER, quota.AUTH_REASON)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(notifications._observe_alibaba_credits(
                settings, self.snapshot(80)), [])
            notifications.observe_provider_failure(
                self.settings, BAILIAN_PROVIDER, quota.AUTH_REASON)
            self.assertEqual(send.call_count, 2)

    def test_control_plane_failure_is_not_an_inference_alert(self):
        with patch.object(notifications.quota, 'credential_identity',
                          return_value=self.identity), \
                patch.object(notifications, 'send', return_value={'sent': True}) as send:
            snapshot = self.snapshot(72, state='auth_error', stale=True)
            self.assertEqual(notifications._observe_alibaba_credits(
                notifications.normalize(self.settings['notifications']), snapshot), [])
            self.assertEqual(notifications.observe_provider_failure(
                self.settings, BAILIAN_PROVIDER, quota.WINDOW_REASON), [])
            send.assert_not_called()
        disabled = {'notifications': {'enabled': True, 'quota_transitions': False}}
        with patch.object(notifications, 'send') as send:
            notifications.observe_provider_failure(
                disabled, BAILIAN_PROVIDER, quota.BALANCE_REASON)
            send.assert_not_called()


class PrepaidSingleProviderAuthTests(unittest.TestCase):
    def test_model_401_is_auth_not_billing_or_window(self):
        message = {'info': {'id': 'm-401', 'role': 'assistant', 'providerID': BAILIAN_PROVIDER,
                            'error': {'name': 'APIError', 'data': {'statusCode': 401,
                                                                    'message': 'invalid api key'}}},
                   'parts': []}
        error = diagnostics.from_messages([message])[0]
        self.assertTrue(error.get('auth'))
        self.assertEqual(error.get('auth_reason'), quota.AUTH_REASON)
        self.assertNotIn('billing', error)
        self.assertNotIn('usage_window', error)
        self.assertEqual(error['suggested_action'], 'check_provider_credentials')
        message['info']['error']['data']['statusCode'] = 403
        self.assertFalse(diagnostics.from_messages([message])[0].get('auth', False))
        # A 401 from another provider keeps its prior generic classification.
        message['info']['providerID'] = 'kimi-for-coding'
        message['info']['error']['data']['statusCode'] = 401
        self.assertFalse(diagnostics.from_messages([message])[0].get('auth', False))


class PrepaidWorkerHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / 'state'
        self.c = config()
        self.patchers = [patch.object(common, 'STATE', self.state),
                         patch.object(worker, 'STATE', self.state),
                         patch.object(worker, 'config', return_value=self.c),
                         patch.object(worker, 'credential_identity', return_value='dispatch-key'),
                         patch.object(quota, 'STATE', self.state),
                         patch.object(quota, 'credential_identity', return_value='dispatch-key')]
        for item in self.patchers:
            item.start()
        common.init()
        self.task = {'profile': 'aliyun-max',
                     '_billing_dispatch': {'provider': BAILIAN_PROVIDER,
                                           'identity': 'dispatch-key', 'at': 1}}

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.tmp.cleanup()

    def test_402_direct_hook_uses_dispatch_identity_and_survives_notification_error(self):
        with patch.object(notifications, 'observe_provider_failure',
                          side_effect=RuntimeError('offline')) as hook:
            self.assertTrue(worker.record_billing_errors(self.task, [{
                'source': 'model', 'provider': BAILIAN_PROVIDER, 'billing': True,
                'billing_reason': quota.BALANCE_REASON, 'message': 'Insufficient Balance',
                'message_id': 'm-402'}]))
        hook.assert_called_once()
        self.assertEqual(hook.call_args.args[1], BAILIAN_PROVIDER)
        self.assertEqual(hook.call_args.args[2], quota.BALANCE_REASON)
        self.assertEqual(hook.call_args.kwargs['identity'], 'dispatch-key')
        self.assertEqual(quota.billing_block(BAILIAN_PROVIDER)['credential'],
                         'dispatch-key')

    def test_explicit_stale_identity_cannot_write_a_new_key_block(self):
        with patch.object(quota, 'credential_identity', return_value='new-key'):
            quota.trip(BAILIAN_PROVIDER, 'Insufficient Balance', 'm-direct-old',
                       reason=quota.BALANCE_REASON, identity='old-key')
        self.assertIsNone(quota.billing_block(BAILIAN_PROVIDER))
        self.assertNotIn(BAILIAN_PROVIDER,
                         common.read_json(quota.STATE / quota.BILLING, {}))

    def test_stale_dispatch_key_failure_never_trips_or_alerts_new_key(self):
        stale = {'profile': 'aliyun-max',
                 '_billing_dispatch': {'provider': BAILIAN_PROVIDER,
                                       'identity': 'old-key', 'at': 1}}
        with patch.object(worker, 'credential_identity', return_value='new-key'), \
                patch.object(quota, 'credential_identity', return_value='new-key'), \
                patch.object(notifications, 'observe_provider_failure') as hook:
            self.assertFalse(worker.record_billing_errors(stale, [{
                'source': 'model', 'provider': BAILIAN_PROVIDER, 'billing': True,
                'billing_reason': quota.BALANCE_REASON, 'message': 'Insufficient Balance',
                'message_id': 'm-old-402'}]))
            self.assertFalse(worker.record_auth_errors(stale, [{
                'source': 'model', 'provider': BAILIAN_PROVIDER, 'auth': True,
                'auth_reason': quota.AUTH_REASON, 'message': '401 old key',
                'message_id': 'm-old-401'}]))
        hook.assert_not_called()
        self.assertIsNone(quota.billing_block(BAILIAN_PROVIDER))
        self.assertNotIn(BAILIAN_PROVIDER,
                         common.read_json(quota.STATE / quota.BILLING, {}))

    def test_legacy_task_without_provenance_still_uses_current_key(self):
        legacy = {'profile': 'aliyun-max'}
        with patch.object(worker, 'credential_identity', return_value='current-key'), \
                patch.object(quota, 'credential_identity', return_value='current-key'), \
                patch.object(notifications, 'observe_provider_failure') as hook:
            self.assertTrue(worker.record_billing_errors(legacy, [{
                'source': 'model', 'provider': BAILIAN_PROVIDER, 'billing': True,
                'billing_reason': quota.BALANCE_REASON, 'message': 'Insufficient Balance',
                'message_id': 'm-legacy-402'}]))
        hook.assert_called_once()
        self.assertEqual(hook.call_args.kwargs['identity'], 'current-key')
        stored = common.read_json(quota.STATE / quota.BILLING, {})[BAILIAN_PROVIDER]
        self.assertEqual(stored['credential'], 'current-key')

    def test_native_429_never_calls_the_seat_hook(self):
        with patch.object(notifications, 'observe_provider_failure') as hook:
            worker.record_billing_errors(self.task, [{
                'source': 'model', 'provider': BAILIAN_PROVIDER, 'usage_window': True,
                'usage_window_reason': quota.WINDOW_REASON, 'message': '429',
                'message_id': 'm-429'}])
        hook.assert_not_called()
        self.assertIsNone(quota.billing_block(BAILIAN_PROVIDER))

    def test_auth_hook_is_alibaba_only(self):
        with patch.object(notifications, 'observe_provider_failure') as hook:
            self.assertFalse(worker.record_auth_errors(self.task, [{
                'source': 'model', 'provider': 'kimi-for-coding', 'auth': True,
                'auth_reason': quota.AUTH_REASON, 'message': '401',
                'message_id': 'm-kimi-401'}]))
        hook.assert_not_called()
        self.assertIsNone(quota.billing_block('kimi-for-coding'))
        with patch.object(notifications, 'observe_provider_failure') as hook:
            self.assertTrue(worker.record_auth_errors(self.task, [{
                'source': 'model', 'provider': BAILIAN_PROVIDER, 'auth': True,
                'auth_reason': quota.AUTH_REASON, 'message': '401',
                'message_id': 'm-ali-401'}]))
        hook.assert_called_once()
        self.assertEqual(hook.call_args.args[2], quota.AUTH_REASON)
        block = quota.billing_block(BAILIAN_PROVIDER)
        self.assertEqual(block['reason'], quota.AUTH_REASON)


if __name__ == '__main__':
    unittest.main()
