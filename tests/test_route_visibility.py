"""Model-facing projections must not disclose partial automatic-route exhaustion."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import delegate
import diagnostics
import quota
import routing
import worker


SENSITIVE = ('sentinel-a', 'sentinel-b', 'sentinel-c', 'plan-a', 'plan-b',
             'deep-plan', 'ultra-plan', 'monthly', 'quota', 'exhausted',
             'insufficient', 'balance', 'window')


class RouteVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.config = self.root / 'config.json'
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.c = {
            'auto_approve': False, 'kimi_reserve_percent': 0,
            'profiles': {
                'plan-a': {'model': 'sentinel-a/model-a'},
                'plan-b': {'model': 'sentinel-b/model-b'},
                'deep-plan': {'model': 'sentinel-a/deep-model'},
                'ultra-plan': {'model': 'sentinel-c/ultra-model'},
            },
            'routing_policy': {
                'fast': [[{'profile': 'plan-b', 'weight': 1}]],
                'background': [[{'profile': 'plan-a', 'weight': 1}],
                               [{'profile': 'plan-b', 'weight': 1}]],
                'deep': [[{'profile': 'deep-plan', 'weight': 1}]],
                'ultra': [[{'profile': 'ultra-plan', 'weight': 1}]],
            },
        }
        self.config.write_text(json.dumps(self.c))
        self.patchers = [
            patch.object(common, 'STATE', self.state),
            patch.object(common, 'CONFIG', self.config),
            patch.object(delegate, 'STATE', self.state),
            patch.object(delegate, 'CONFIG', self.config),
            patch.object(quota, 'STATE', self.state),
            patch.object(routing, 'STATE', self.state),
            patch.object(worker, 'STATE', self.state),
            patch.object(quota, 'credential_identity', lambda provider: 'cred-' + provider),
            patch.object(routing, 'RETIRED_MODELS', frozenset()),
        ]
        for patcher in self.patchers:
            patcher.start()
        common.init()
        self.q = {
            'sentinel-a': {'state': 'ok', 'stale': False, 'available': True,
                           'sampled_at': time.time(),
                           'windows': [{'remaining_percent': 80}]},
            'sentinel-b': {'state': 'ok', 'stale': False, 'available': True,
                           'sampled_at': time.time(),
                           'windows': [{'remaining_percent': 70}]},
            'sentinel-c': {'state': 'ok', 'stale': False, 'available': True,
                           'sampled_at': time.time(),
                           'windows': [{'remaining_percent': 60}]},
        }
        common.write_json(self.state / 'quota.json', self.q)

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def exhaust_all_providers(self):
        for provider in ('sentinel-a', 'sentinel-b', 'sentinel-c'):
            quota.trip(provider, 'monthly usage limit reached', 'msg-' + provider,
                       reason='monthly_usage_limit')

    def task(self, **overrides):
        error_message = 'sentinel-a monthly usage limit exhausted; quota refreshed next cycle'
        t = {
            'id': 'job-visibility', 'title': 'Bounded task', 'objective': 'Do bounded work',
            'status': 'queued', 'requested_profile': 'auto', 'tier': 'normal',
            'urgency': 'background', 'complexity': 'normal', 'mode': 'read',
            'scopes': [], 'targets': [], 'resources': [], 'commands': [],
            'source_dir': str(self.repo), 'group_id': 'owner', 'group_title': 'owner',
            'created_at': time.time(), 'started_at': time.time(),
            'profile': 'plan-b',
            'reason': 'capacity_wait',
            'queue_reason': 'routing_policy:quota_exhausted_or_account_unavailable',
            'route_reason': ('routing_policy:background:stage1:'
                             'quota_exhausted_or_account_unavailable:baseline_balanced'),
            'route_history': [
                {'from_profile': 'plan-a', 'to_profile': 'plan-b',
                 'from_provider': 'sentinel-a', 'to_provider': 'sentinel-b',
                 'trigger': 'provider_usage_or_rate_limit', 'at': time.time()},
            ],
            'actual_models': ['sentinel-a/model-a'],
            'current_model': 'sentinel-a/model-a', 'current_variant': 'max',
            'fallback_used': True,
            'routing_notice': ('Preferred routing stages were unavailable or exhausted; '
                               'the bridge dispatched the configured fallback model.'),
            'proactive_reroute_levels': [1],
            'excluded_providers': ['sentinel-a'],
            'errors': [
                {'source': 'model', 'code': 'APIError', 'provider': 'sentinel-a',
                 'message': error_message, 'billing': True,
                 'billing_reason': 'monthly_usage_limit', 'message_id': 'msg-cap'},
                {'source': 'bridge', 'code': 'provider_billing_monthly_usage_limit',
                 'message': 'provider billing monthly usage limit',
                 'suggested_action': 'inspect_result_and_session'},
                {'source': 'tool', 'code': 'command_exit_nonzero',
                 'provider': 'sentinel-tool', 'message': 'lint failed'},
            ],
            'recovery': {
                'blocked_reason': 'provider_billing_error',
                'billing_reason': 'monthly_usage_limit',
                'blocked_provider': 'sentinel-a',
                'alternatives': [{'profile': 'plan-b', 'provider': 'sentinel-b'}],
            },
            'last_activity': {'id': 'evt_status', 'type': 'status', 'status': 'queued',
                              'label': 'status',
                              'text': 'routing_policy:quota_exhausted_or_account_unavailable',
                              'time': time.time()},
            'recent_activity': [
                {'id': 'evt_status', 'type': 'status', 'status': 'queued',
                 'label': 'status',
                 'text': 'routing_policy:quota_exhausted_or_account_unavailable',
                 'time': time.time()},
                {'id': 'evt_capacity', 'type': 'error', 'status': 'error',
                 'label': 'APIError', 'text': error_message, 'time': time.time()},
                {'id': 'evt_tool', 'type': 'tool', 'status': 'completed',
                 'label': 'bash', 'text': 'python3 -m unittest', 'time': time.time()},
            ],
        }
        t.update(overrides)
        common.write_json(common.task_path(t['id']), t)
        return common.task(t['id'])

    def assert_private_projection(self, view):
        blob = json.dumps(view, ensure_ascii=False).lower()
        for key in ('profile', 'route_reason', 'route_history', 'actual_models',
                    'fallback_used', 'routing_notice', 'proactive_reroute_levels',
                    'excluded_providers', 'current_model', 'current_variant'):
            self.assertNotIn(key, view)
        self.assertNotIn('queue_reason', view)
        self.assertNotIn('reason', view)
        self.assertNotIn('sentinel-a', blob)
        self.assertNotIn('sentinel-b', blob)
        self.assertNotIn('sentinel-c', blob)
        self.assertNotIn('plan-a', blob)
        self.assertNotIn('plan-b', blob)
        self.assertNotIn('deep-plan', blob)
        self.assertNotIn('ultra-plan', blob)
        self.assertNotIn('monthly', blob)
        self.assertNotIn('quota_exhausted', blob)
        self.assertNotIn('insufficient balance', blob)

    def test_partial_capacity_stop_hides_route_and_quota_but_keeps_tool_error(self):
        # plan-a is unavailable; plan-b remains a legal automatic route.
        self.q['sentinel-a']['available'] = False
        common.write_json(self.state / 'quota.json', self.q)
        t = self.task()
        view = delegate.task_status(t)
        self.assert_private_projection(view)
        self.assertNotIn('recovery', view)
        self.assertEqual(view['errors'], [{'source': 'tool', 'code': 'command_exit_nonzero',
                                           'message': 'lint failed'}])
        self.assertEqual(view['last_activity']['text'], 'Waiting for automatic worker admission')
        self.assertEqual([event['id'] for event in view['recent_activity']],
                         ['evt_status', 'evt_tool'])
        waited = delegate.wait_result(t, 0)
        self.assertTrue(waited['continue_waiting'])
        self.assertFalse(waited.get('attention'))
        self.assertNotIn('recovery', waited)

        # Operator/raw task state retains the truthful provider/route detail.
        raw = common.task(t['id'])
        self.assertEqual(raw['queue_reason'],
                         'routing_policy:quota_exhausted_or_account_unavailable')
        self.assertEqual(raw['recovery']['blocked_provider'], 'sentinel-a')

        legacy_queue = self.task(queue_reason='quota_exhausted_or_account_unavailable')
        legacy_view = delegate.task_status(legacy_queue)
        self.assert_private_projection(legacy_view)

        common.write_json(common.artifact_dir(t['id']) / 'result.json', {
            'status': 'failed', 'reason': 'provider_usage_window_limit',
            'actual_models': ['sentinel-a/model-a'],
            'errors': [{'source': 'model', 'code': 'APIError', 'provider': 'sentinel-a',
                        'message': 'sentinel-a usage window exhausted',
                        'usage_window': True},
                       {'source': 'tool', 'code': 'command_exit_nonzero',
                        'message': 'lint failed'}],
        })
        collected = diagnostics.collect(t['id'])
        self.assert_private_projection(collected['task'])
        self.assertNotIn('recovery', collected)
        self.assertNotIn('reason', collected['result'])
        self.assertEqual(collected['result']['errors'],
                         [{'source': 'tool', 'code': 'command_exit_nonzero',
                           'message': 'lint failed'}])

    def test_total_route_exhaustion_reports_only_generic_blockage(self):
        self.exhaust_all_providers()
        t = self.task()
        self.assertTrue(delegate.wait_ready(t))
        view = delegate.task_status(t)
        self.assert_private_projection(view)
        recovery = view['recovery']
        self.assertTrue(recovery['automatic_routes_exhausted'])
        self.assertEqual(recovery['blocked_reason'], 'automatic_capacity_unavailable')
        self.assertEqual(recovery['suggested_action'], 'wait_for_capacity_recovery')
        self.assertEqual(recovery['alternatives'], [])
        self.assertNotIn('blocked_provider', recovery)
        self.assertNotIn('billing_reason', recovery)
        self.assertNotIn('retry_command', recovery)
        waited = delegate.wait_result(t, 0)
        self.assertTrue(waited['attention'])
        self.assertFalse(waited['continue_waiting'])
        self.assertEqual(waited['next_action'], 'wait_for_capacity_recovery')
        self.assertEqual(waited['recovery'], recovery)

        raw = common.task(t['id'])
        self.assertEqual(raw['queue_reason'],
                         'routing_policy:quota_exhausted_or_account_unavailable')
        self.assertEqual(raw['recovery']['blocked_provider'], 'sentinel-a')

        common.write_json(common.artifact_dir(t['id']) / 'result.json', {
            'status': 'failed', 'reason': 'provider_billing_monthly_usage_limit',
            'actual_models': ['sentinel-a/model-a'],
            'errors': [{'source': 'model', 'code': 'APIError', 'provider': 'sentinel-a',
                        'message': 'sentinel-a monthly usage limit exhausted',
                        'billing': True, 'billing_reason': 'monthly_usage_limit'},
                       {'source': 'bridge', 'code': 'provider_billing_monthly_usage_limit',
                        'message': 'provider billing monthly usage limit'},
                       {'source': 'tool', 'code': 'command_exit_nonzero',
                        'provider': 'sentinel-tool', 'message': 'lint failed'}],
            'worker_report': {'summary': 'bounded evidence'},
        })
        collected = diagnostics.collect(t['id'])
        self.assert_private_projection(collected['task'])
        self.assertEqual(collected['recovery'], recovery)
        self.assertEqual(collected['task']['recovery'], recovery)
        self.assertNotIn('reason', collected['result'])
        self.assertNotIn('actual_models', collected['result'])
        self.assertEqual(collected['result']['errors'],
                         [{'source': 'tool', 'code': 'command_exit_nonzero',
                           'message': 'lint failed'}])

    def test_requested_tier_exhausted_but_fast_normal_service_usable_is_waiting_only(self):
        # Deep (sentinel-a) and Ultra (sentinel-c) are exhausted; Fast/Normal
        # (sentinel-b) is still usable. This task must not be told the whole
        # automatic service is exhausted, nor which tier/provider stopped.
        quota.trip('sentinel-a', 'monthly usage limit reached', 'msg-a',
                   reason='monthly_usage_limit')
        quota.trip('sentinel-c', 'monthly usage limit reached', 'msg-c',
                   reason='monthly_usage_limit')
        t = self.task(tier='deep', profile='deep-plan',
                      queue_reason='routing_policy:quota_exhausted_or_account_unavailable')
        self.assertFalse(delegate.wait_ready(t))
        view = delegate.task_status(t)
        self.assertEqual(view['tier'], 'deep')
        self.assertNotIn('recovery', view)
        blob = json.dumps(view).lower()
        for value in ('sentinel-a', 'sentinel-c', 'deep-plan', 'monthly', 'quota_exhausted'):
            self.assertNotIn(value, blob)
        waited = delegate.wait_result(t, 0)
        self.assertTrue(waited['continue_waiting'])
        self.assertFalse(waited.get('attention'))
        self.assertEqual(waited['next_action'], 'call_wait_again')
        self.assertNotIn('recovery', waited)

    def test_unknown_or_stale_quota_is_not_whole_service_exhaustion(self):
        for value in self.q.values():
            value.update(available=False, stale=True)
        common.write_json(self.state / 'quota.json', self.q)
        t = self.task(queue_reason='routing_policy:quota_exhausted_or_account_unavailable')
        view = delegate.task_status(t)
        self.assertNotIn('recovery', view)
        self.assertNotIn('sentinel-a', json.dumps(view).lower())

    def test_auth_only_all_routes_blocked_preserves_actionable_auth(self):
        for value in self.q.values():
            value.update(state='auth_error', available=True)
        common.write_json(self.state / 'quota.json', self.q)
        t = self.task(status='failed', reason='provider_authentication_blocked',
                      queue_reason=None,
                      errors=[{'source': 'model', 'code': 'APIError',
                               'provider': 'sentinel-a', 'message': 'credential rejected',
                               'auth': True, 'auth_reason': 'credential_rejected'}],
                      last_activity=None, recent_activity=None)
        view = delegate.task_status(t)
        self.assertEqual(view['reason'], 'provider_authentication_blocked')
        self.assertIn('auth', view['errors'][0])
        blob = json.dumps(view).lower()
        self.assertNotIn('automatic_routes_exhausted', blob)
        self.assertNotIn('automatic_capacity_unavailable', blob)

    def test_manual_exclusion_is_not_whole_service_exhaustion(self):
        t = self.task(excluded_providers=['sentinel-a', 'sentinel-b', 'sentinel-c'])
        view = delegate.task_status(t)
        self.assertNotIn('recovery', view)
        self.assertNotIn('sentinel-a', json.dumps(view).lower())

    def test_policy_disabled_or_unconfigured_tier_is_not_whole_service_exhaustion(self):
        self.c['profiles']['plan-b']['enabled'] = False
        self.config.write_text(json.dumps(self.c))
        quota.trip('sentinel-a', 'monthly usage limit reached', 'msg-a',
                   reason='monthly_usage_limit')
        quota.trip('sentinel-c', 'monthly usage limit reached', 'msg-c',
                   reason='monthly_usage_limit')
        t = self.task(queue_reason='routing_policy:quota_exhausted_or_account_unavailable')
        view = delegate.task_status(t)
        self.assertNotIn('recovery', view)
        # An unconfigured Ultra tier is not a proven exhaustion either.
        self.c['routing_policy'].pop('ultra')
        self.c['profiles']['plan-b']['enabled'] = True
        self.config.write_text(json.dumps(self.c))
        self.exhaust_all_providers()
        t = self.task(queue_reason='routing_policy:quota_exhausted_or_account_unavailable')
        view = delegate.task_status(t)
        self.assertNotIn('recovery', view)

    def test_total_exhaustion_retains_requested_tier_and_capability_floor(self):
        self.exhaust_all_providers()
        t = self.task(tier='deep', capability_floor='deep',
                      capability_reason='Deep reasoning is required for this outcome')
        view = delegate.task_status(t)
        self.assertEqual(view['tier'], 'deep')
        self.assertEqual(view['capability_floor'], 'deep')
        self.assertEqual(view['capability_reason'],
                         'Deep reasoning is required for this outcome')
        self.assertTrue(view['recovery']['automatic_routes_exhausted'])
        blob = json.dumps(view).lower()
        for value in ('deep-plan', 'ultra-plan', 'sentinel-a', 'sentinel-b', 'sentinel-c'):
            self.assertNotIn(value, blob)

    def test_explicit_profile_keeps_operator_actionability(self):
        quota.trip('sentinel-a', 'monthly usage limit reached', 'msg-bill',
                   reason='monthly_usage_limit')
        t = self.task(requested_profile='plan-a', profile='plan-a', status='failed',
                      reason='provider_billing_monthly_usage_limit')
        view = delegate.task_status(t)
        self.assertEqual(view['requested_profile'], 'plan-a')
        self.assertEqual(view['profile'], 'plan-a')  # persisted operator field
        self.assertEqual(view['current_model'], 'sentinel-a/model-a')
        self.assertEqual(view['reason'], 'provider_billing_monthly_usage_limit')
        self.assertEqual(view['recovery']['blocked_provider'], 'sentinel-a')
        self.assertIn('monthly_usage_limit', view['recovery']['billing_reason'])
        waited = delegate.wait_result(t, 0)
        self.assertEqual(waited['next_action'], 'resubmit_same_tier_auto')

    def test_auth_failure_remains_actionable_and_is_not_generic_capacity(self):
        t = self.task(status='failed', reason='provider_authentication_blocked',
                      queue_reason=None,
                      errors=[{'source': 'model', 'code': 'APIError',
                               'provider': 'sentinel-a', 'message': 'credential rejected',
                               'auth': True, 'auth_reason': 'credential_rejected'}],
                      recovery=None, route_history=None, actual_models=None,
                      current_model=None, fallback_used=None, routing_notice=None,
                      excluded_providers=None, last_activity=None, recent_activity=None)
        view = delegate.task_status(t)
        self.assertEqual(view['reason'], 'provider_authentication_blocked')
        self.assertEqual(view['errors'], [{'source': 'model', 'code': 'APIError',
                                           'message': 'credential rejected', 'auth': True,
                                           'auth_reason': 'credential_rejected'}])
        self.assertNotIn('recovery', view)

    def test_capacity_continuation_prompt_omits_trigger_and_provider(self):
        t = self.task(status='running')
        text = worker.prompt(t, {
            'reason': 'provider_usage_or_rate_limit',
            'previous_provider': 'sentinel-a',
            'selected_profile': 'plan-b',
            'selected_provider': 'sentinel-b',
        })
        self.assertIn('Continue only', text)
        self.assertIn('This is not a new task', text)
        for value in ('provider_usage_or_rate_limit', 'sentinel-a', 'sentinel-b',
                      'plan-b'):
            self.assertNotIn(value, text)
        # Non-capacity transition evidence retains its actionable semantics.
        auth_text = worker.prompt(t, {
            'reason': 'provider_authentication_blocked',
            'previous_provider': 'sentinel-a',
        })
        self.assertIn('provider_authentication_blocked', auth_text)
        self.assertIn('sentinel-a', auth_text)

    def test_submit_projection_is_private_even_when_a_route_is_queued(self):
        result = delegate.submit({
            'directory': str(self.repo), 'objective': 'bounded task', 'mode': 'write',
            'scopes': ['a.txt'], 'profile': 'auto', 'tier': 'normal',
        })
        for key in ('profile', 'route_reason', 'route_history', 'actual_models',
                    'current_model', 'current_variant', 'fallback_used',
                    'routing_notice', 'excluded_providers', 'reason',
                    'queue_reason', 'recovery'):
            self.assertNotIn(key, result)


if __name__ == '__main__':
    unittest.main()
