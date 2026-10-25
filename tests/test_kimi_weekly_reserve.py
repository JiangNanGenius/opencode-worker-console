"""Four-tier Kimi weekly reserve, account-pool authority and loopback gate HTTP.

The policy tests use one shared helper, ``quota.kimi_request_allowed``, exactly as
the loopback gate and task admission do. The gate tests start the real gate server
with a temporary state directory and assert the HTTP rejection codes; no upstream
request is ever issued for a blocked model.
"""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import delegate
import quota
import routing

NOW = time.time()
WEEK_5H = {'name': 'window_0', 'valid': True, 'limit': 1, 'remaining': 0.9999,
           'remaining_percent': 99.99, 'duration_minutes': 300, 'resets_at': None}
WEEK_7D = {'name': 'usages.limit_7d', 'valid': True, 'limit': 1, 'remaining': 0.8462,
           'remaining_percent': 84.62, 'duration_minutes': 10080, 'resets_at': None}
LEGACY_OVERALL = {'name': 'overall', 'valid': True, 'limit': 1, 'remaining': 0.8462,
                  'remaining_percent': 84.62, 'duration_minutes': 10080, 'resets_at': None}


def sized(window, percent, **extra):
    """One window fixture whose raw remaining and percentage agree."""
    value = dict(window, remaining_percent=percent, remaining=percent / 100.0)
    value.update(extra)
    return value


def profiles(**overrides):
    values = {
        'fallback': {'model': 'deepseek/deepseek-flash', 'variant': 'max', 'label': 'Fallback'},
        'senior-code': {'model': 'kimi-for-coding/kimi-for-coding', 'variant': 'max', 'label': 'K2.8'},
        'deep-research': {'model': 'bailian-token-plan/qwen3.8-max', 'variant': 'max', 'label': 'Qwen Max'},
        'aliyun-flash': {'model': 'bailian-token-plan/qwen3.8-flash', 'variant': 'max', 'label': 'Qwen Flash'},
        'ultra-reasoning': {'model': 'kimi-for-coding/k3.1-pro', 'variant': 'max', 'label': 'K3.1 Pro'},
        'ark-auto': {'model': 'volcengine-agent-plan/ark-code-latest', 'variant': 'max', 'label': 'Ark Auto'},
    }
    values.update(overrides)
    return values


def config(**overrides):
    value = {
        'profiles': profiles(),
        'kimi_weekly_reserve_percent': 30,
        'kimi_reserve_percent': 0,
        'kimi_low_weekly_threshold_percent': 5,
        'kimi_low_weekly_ultra_limit': 1,
        'routing_policy': {
            'fast': [[{'profile': 'fallback', 'weight': 1}]],
            'background': [[{'profile': 'senior-code', 'weight': 1},
                            {'profile': 'aliyun-flash', 'weight': 1}],
                           [{'profile': 'fallback', 'weight': 1}]],
            'deep': [[{'profile': 'deep-research', 'weight': 1}],
                     [{'profile': 'fallback', 'weight': 1}]],
            'ultra': [[{'profile': 'ultra-reasoning', 'weight': 1}]],
        },
    }
    value.update(overrides)
    return value


def account(windows=(), weekly='applicable', binding='verified', sampled_at=None,
            available=True, state='ok'):
    record = {'state': state, 'source': 'kimi_code_account',
              'sampled_at': NOW if sampled_at is None else sampled_at,
              'windows': [dict(window) for window in windows],
              'weekly_applicability': weekly,
              'account_binding': {'state': binding, 'method': 'official_identity_match'},
              'available': available}
    return record


def month_window(percent, kind='month_total', name='monthTotal'):
    return {'name': name, 'kind': kind, 'limit': 1, 'remaining': percent / 100.0,
            'remaining_percent': percent, 'duration_minutes': None,
            'resets_at': None, 'valid': True, 'source': 'kimi_code_account'}


def weekly_window(percent):
    return {'name': 'limit7d', 'kind': 'weekly', 'limit': 1, 'remaining': percent / 100.0,
            'remaining_percent': percent, 'duration_minutes': 10080,
            'resets_at': None, 'valid': True, 'source': 'kimi_code_account'}


def view(windows=None, *, stale=False, available=True, state='ok', account_record=None,
         billing=None):
    record = {'state': state, 'stale': stale, 'sampled_at': NOW, 'available': available,
              'windows': [dict(window) for window in (windows if windows is not None else [])]}
    if account_record is not None:
        record['account'] = account_record
    if billing is not None:
        record['billing'] = billing
        record['billing_reason'] = billing.get('reason')
    return {'kimi-for-coding': record}


class ReservePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state = root / 'state'
        self.state.mkdir()
        self.config_path = root / 'config.json'
        self.config_path.write_text(json.dumps(config()))
        self.patchers = [
            patch.object(common, 'STATE', self.state),
            patch.object(quota, 'STATE', self.state),
            patch.object(common, 'CONFIG', self.config_path),
            patch.object(quota, 'credential_identity', lambda provider: 'cred-' + provider),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()

    def task(self, **overrides):
        value = {'id': 'job-test', 'title': 't', 'status': 'queued', 'mode': 'read',
                 'requested_profile': 'auto', 'tier': 'normal', 'urgency': 'background',
                 'complexity': 'normal', 'source_dir': '/tmp', 'scopes': [], 'targets': [],
                 'resources': []}
        value.update(overrides)
        return value

    def test_explicit_seven_day_window_wins_over_legacy_overall(self):
        both = [WEEK_5H, sized(LEGACY_OVERALL, 70.0), WEEK_7D]
        state = quota.kimi_weekly_state(view(both), config())
        self.assertEqual(state['status'], 'known')
        self.assertEqual(state['remaining_percent'], 84.62)
        self.assertEqual(state['source'], 'telemetry_limit_7d')

    def test_legacy_overall_remains_a_weekly_fallback(self):
        state = quota.kimi_weekly_state(view([WEEK_5H, LEGACY_OVERALL]), config())
        self.assertEqual(state['status'], 'known')
        self.assertEqual(state['source'], 'telemetry_overall')

    def test_five_hour_window_never_counts_as_weekly(self):
        state = quota.kimi_weekly_state(view([WEEK_5H]), config())
        self.assertEqual(state['status'], 'unknown')
        self.assertEqual(quota.kimi_request_allowed('kimi-for-coding/kimi-for-coding',
                                                    config(), view([WEEK_5H]))[0], False)

    def test_reserve_blocks_ordinary_kimi_at_or_below_threshold(self):
        for percent, allowed in ((30.01, True), (30.0, False), (15.38, False)):
            with self.subTest(percent=percent):
                ok, reason = quota.kimi_request_allowed(
                    'kimi-for-coding/kimi-for-coding', config(),
                    view([sized(WEEK_7D, percent)]))
                self.assertEqual(ok, allowed, reason)

    def test_unknown_weekly_fails_closed_for_ordinary_models(self):
        ok, reason = quota.kimi_request_allowed(
            'kimi-for-coding/kimi-for-coding', config(), view([], stale=True))
        self.assertFalse(ok)
        self.assertEqual(reason, 'kimi_weekly_unknown')

    def test_reserved_ultra_model_keeps_the_last_weekly_slice(self):
        low = view([WEEK_5H, sized(WEEK_7D, 12.0)])
        self.assertEqual(quota.kimi_request_allowed('kimi-for-coding/k3.1-pro', config(), low),
                         (True, 'weekly_reserve_beneficiary'))
        self.assertEqual(quota.kimi_request_allowed('k3.1-pro', config(), low)[0], True)

    def test_explicit_profile_cannot_bypass_the_reserve(self):
        c = config()
        q = view([sized(WEEK_7D, 15.38)])
        profile, why = quota.route(self.task(requested_profile='senior-code', tier='normal'), c, q)
        self.assertIsNone(profile)
        self.assertEqual(why, 'kimi_weekly_reserved_for_ultra')
        profile, why = quota.route(self.task(requested_profile='ultra-reasoning',
                                             tier='ultra', complexity='deep'), c, q)
        self.assertEqual(profile, 'ultra-reasoning')

    def test_retired_models_are_not_new_task_entries(self):
        c = config(profiles=profiles(**{'retired-k3': {
            'model': 'kimi-for-coding/k3', 'variant': 'max', 'label': 'K3', 'enabled': True}}))
        ok, reason = quota.kimi_request_allowed('kimi-for-coding/k3', c, view([WEEK_7D]))
        self.assertFalse(ok)
        self.assertEqual(reason, 'retired_model')
        profile, why = quota.route(self.task(requested_profile='retired-k3', tier='normal'), c,
                                   view([WEEK_7D]))
        self.assertIsNone(profile)
        self.assertEqual(why, 'retired_model')

    def test_not_applicable_weekly_allows_ordinary_models(self):
        record = account(windows=[month_window(66.73)], weekly='not_applicable')
        q = view([], stale=True, account_record=record)
        self.assertEqual(quota.kimi_weekly_state(q, config())['status'], 'not_applicable')
        self.assertEqual(quota.kimi_request_allowed('kimi-for-coding/kimi-for-coding',
                                                    config(), q)[0], True)

    def test_account_total_is_never_renamed_into_a_weekly_pool(self):
        # No weekly window exists and the only account pool is monthTotal. That
        # total must not become a simulated weekly percentage.
        record = account(windows=[month_window(66.73)], weekly='not_applicable')
        state = quota.kimi_weekly_state(view([], stale=True, account_record=record), config())
        self.assertEqual(state['status'], 'not_applicable')
        self.assertIsNone(state['remaining_percent'])
        self.assertNotIn('month', str(state.get('source')))

    def test_verified_zero_account_pool_blocks_every_model_including_ultra(self):
        zero = account(windows=[weekly_window(15.38), month_window(0.0)])
        q = view([sized(WEEK_7D, 15.38)], account_record=zero)
        self.assertEqual(quota.kimi_request_allowed('kimi-for-coding/k3.1-pro', config(), q),
                         (False, 'kimi_account_total_exhausted'))
        self.assertEqual(quota.kimi_request_allowed('kimi-for-coding/kimi-for-coding',
                                                    config(), q),
                         (False, 'kimi_account_total_exhausted'))

    def test_unverified_or_missing_month_never_becomes_zero(self):
        unverified = account(windows=[weekly_window(15.38), month_window(0.0)],
                             binding='unverified')
        q = view([sized(WEEK_7D, 84.0)], account_record=unverified)
        self.assertEqual(quota.kimi_request_allowed('kimi-for-coding/k3.1-pro', config(), q)[0],
                         True)
        total = quota.kimi_account_total_state(q)
        self.assertEqual(total['status'], 'unverified')
        self.assertFalse(total['blocks'])
        missing = account(windows=[weekly_window(15.38)])
        total = quota.kimi_account_total_state(view([], account_record=missing))
        self.assertEqual(total['status'], 'not_reported')
        self.assertIsNone(total['remaining_percent'])
        self.assertFalse(total['blocks'])

    def test_base_provider_gate_blocks_reserved_ultra_too(self):
        cases = {
            'empty five-hour window': (view([sized(WEEK_5H, 0.0), WEEK_7D]),
                                       'quota_exhausted_or_account_unavailable'),
            'account unavailable': (view([WEEK_7D], available=False),
                                    'quota_exhausted_or_account_unavailable'),
            'credential rejected': (view([WEEK_7D], state='auth_error'), 'credential_rejected'),
            'control plane rate limited': (view([WEEK_7D], state='rate_limited'),
                                           'quota_endpoint_rate_limited'),
        }
        for label, (q, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(quota.kimi_request_allowed('kimi-for-coding/k3.1-pro',
                                                            config(), q),
                                 (False, expected))
                self.assertEqual(quota.kimi_request_allowed('kimi-for-coding/kimi-for-coding',
                                                            config(), q),
                                 (False, expected))
        # A real durable billing circuit is read from private state, exactly as
        # production admission does; a view overlay alone is not proof.
        quota.trip('kimi-for-coding', 'monthly usage limit', 'm-monthly',
                   reason=quota.MONTHLY_REASON)
        self.assertEqual(quota.kimi_request_allowed('kimi-for-coding/k3.1-pro', config(),
                                                    view([WEEK_7D])),
                         (False, 'provider_billing_blocked'))
        self.assertEqual(quota.kimi_request_allowed('kimi-for-coding/kimi-for-coding', config(),
                                                    view([WEEK_7D])),
                         (False, 'provider_billing_blocked'))

    def test_kimi_monthly_circuit_requires_an_executed_manual_release(self):
        c = config()
        q = view([WEEK_7D])
        quota.trip('kimi-for-coding', 'monthly usage limit', 'm-monthly-2',
                   reason=quota.MONTHLY_REASON)
        self.assertFalse(quota.kimi_request_allowed('kimi-for-coding/kimi-for-coding', c, q)[0])
        task = {'id': 'job-retry', 'title': 'r', 'status': 'queued', 'mode': 'read',
                'requested_profile': 'senior-code', 'tier': 'normal', 'urgency': 'background',
                'complexity': 'normal', 'source_dir': '/tmp', 'scopes': [], 'targets': [],
                'resources': []}
        # Naming a profile is no longer a circuit bypass.
        self.assertEqual(quota.route(task, c, q),
                         (None, 'provider_billing_blocked'))
        self.assertEqual(quota.kimi_request_allowed('kimi-for-coding/k3.1-pro', c, q),
                         (False, 'provider_billing_blocked'))
        # The executable manual release clears the circuit for new attempts.
        self.assertTrue(quota.retry_provider('kimi-for-coding')['released'])
        self.assertEqual(quota.route(task, c, q)[0], 'senior-code')
        self.assertEqual(quota.kimi_request_allowed('kimi-for-coding/k3.1-pro', c, q),
                         (True, 'weekly_reserve_beneficiary'))
        # A genuinely empty short window is never released by that authorization.
        exhausted = view([sized(WEEK_5H, 0.0), WEEK_7D])
        self.assertEqual(quota.kimi_request_allowed('kimi-for-coding/k3.1-pro', c, exhausted),
                         (False, 'quota_exhausted_or_account_unavailable'))

    def test_capability_floor_understands_ultra(self):
        c = config()
        self.assertTrue(quota.meets_capability_floor('ultra-reasoning',
                                                     {'capability_floor': 'ultra'}, c))
        self.assertFalse(quota.meets_capability_floor('deep-research',
                                                      {'capability_floor': 'ultra'}, c))
        self.assertTrue(quota.meets_capability_floor('deep-research',
                                                     {'capability_floor': 'deep'}, c))
        self.assertTrue(quota.meets_capability_floor('ultra-reasoning',
                                                     {'capability_floor': 'normal'}, c))

    def test_ultra_concurrency_limit_needs_a_fresh_low_runway(self):
        far = time.time() + 5 * 86400
        low = view([sized(WEEK_7D, 4.0, resets_at=far)])
        self.assertTrue(quota.kimi_ultra_low_weekly(low, config())[0])
        healthy = view([sized(WEEK_7D, 60.0, resets_at=far)])
        self.assertFalse(quota.kimi_ultra_low_weekly(healthy, config())[0])
        unknown = view([], stale=True)
        self.assertFalse(quota.kimi_ultra_low_weekly(unknown, config())[0])
        self.assertEqual(quota.kimi_ultra_limit({'kimi_low_weekly_k3_limit': 2}), 2)
        self.assertEqual(quota.kimi_ultra_limit({'kimi_low_weekly_ultra_limit': 0}), 0)

    def test_legacy_reserve_percent_folds_in_without_double_counting(self):
        self.assertEqual(quota.kimi_weekly_reserve_percent({'kimi_reserve_percent': 40}),
                         max(30, 40))
        self.assertEqual(quota.kimi_weekly_reserve_percent(
            {'kimi_weekly_reserve_percent': 10, 'kimi_reserve_percent': 0}), 10)
        self.assertEqual(quota.kimi_weekly_reserve_percent(
            {'kimi_weekly_reserve_percent': 0, 'kimi_reserve_percent': 0}), 0)
        # A zero effective reserve disables the reserve but never the unknown
        # fail-closed rule.
        q = view([], stale=True)
        self.assertFalse(quota.kimi_request_allowed(
            'kimi-for-coding/kimi-for-coding',
            {'kimi_weekly_reserve_percent': 0, 'kimi_reserve_percent': 0}, q)[0])



class AccountMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.config_path = self.root / 'config.json'
        self.config_path.write_text(json.dumps(config()))
        self.patchers = [
            patch.object(common, 'STATE', self.state),
            patch.object(quota, 'STATE', self.state),
            patch.object(common, 'CONFIG', self.config_path),
            patch.object(quota, 'credential_identity', lambda provider: 'cred-' + provider),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()

    def test_refresh_merges_only_the_adapter_schema(self):
        import kimi_account
        fetched = account(windows=[weekly_window(15.38), month_window(66.73)],
                          weekly='applicable')
        import quota as quota_module
        with patch.object(quota_module, '_providers_in_use', return_value=['kimi-for-coding']), \
                patch.object(quota_module, 'fetch_one', return_value={
                    'state': 'ok', 'sampled_at': NOW, 'available': True,
                    'windows': [dict(WEEK_5H), dict(WEEK_7D)]}), \
                patch.object(quota_module, 'observe_load', return_value={}), \
                patch.object(kimi_account, 'fetch', return_value=fetched):
            snapshot = quota.refresh(force=True)
        kimi = snapshot['kimi-for-coding']
        self.assertIn('account', kimi)
        self.assertEqual(kimi['account']['state'], 'ok')
        self.assertEqual(len(kimi['account']['windows']), 2)
        self.assertEqual(kimi['weekly_reserve']['status'], 'known')
        self.assertEqual(kimi['weekly_reserve']['remaining_percent'], 15.38)
        self.assertEqual(kimi['weekly_reserve']['reserve_percent'], 30)
        self.assertEqual(kimi['account_total']['status'], 'available')
        self.assertEqual(kimi['account_total']['remaining_percent'], 66.73)
        # The account private record is persisted without any token/credential.
        raw = json.loads((self.state / 'quota.json').read_text())
        text = json.dumps(raw)
        self.assertNotIn('access_token', text)
        self.assertNotIn('refresh_token', text)
        # Enforce the same policy through the merged view exactly as dispatch does.
        self.assertFalse(quota.kimi_request_allowed('kimi-for-coding/kimi-for-coding',
                                                    config(), snapshot)[0])
        self.assertTrue(quota.kimi_request_allowed('kimi-for-coding/k3.1-pro',
                                                   config(), snapshot)[0])

    def test_failed_fetch_retains_the_original_sample_time(self):
        import kimi_account
        stale_sample = NOW - 3600
        previous = {'state': 'ok', 'source': 'kimi_code_account', 'sampled_at': stale_sample,
                    'windows': [weekly_window(15.38)], 'weekly_applicability': 'applicable',
                    'account_binding': {'state': 'verified'}, 'available': True}
        with patch.object(kimi_account, 'fetch', side_effect=RuntimeError('unavailable')):
            kept = quota._kimi_account_snapshot(previous)
        self.assertEqual(kept, previous)
        self.assertEqual(kept['sampled_at'], stale_sample)
        merged = quota.view({'kimi-for-coding': {
            'state': 'ok', 'sampled_at': NOW, 'stale': False, 'available': True,
            'windows': [dict(WEEK_7D)], 'account': kept}})
        self.assertTrue(merged['kimi-for-coding']['account']['stale'])
        # An aging account reading falls back to telemetry instead of being fresh.
        self.assertEqual(quota.kimi_weekly_state(merged, config())['source'],
                         'telemetry_limit_7d')


class GateHTTPTests(unittest.TestCase):
    """The real loopback gate must reject every base-gated model with HTTP 429."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state = root / 'state'
        self.state.mkdir(mode=0o700)
        self.password = 'test-gate-password'
        (self.state / 'server-password').write_text(self.password + '\n')
        (self.state / 'server-password').chmod(0o600)
        self.config_path = root / 'config.json'
        self.config_path.write_text(json.dumps(config(kimi_gate={'enabled': True, 'port': 51531})))
        self.quota_view = None
        self.patchers = [
            patch.object(common, 'STATE', self.state),
            patch.object(quota, 'STATE', self.state),
            patch.object(common, 'CONFIG', self.config_path),
            patch.object(quota, 'credential_identity', lambda provider: 'cred-' + provider),
            # The gate deliberately delegates cache freshness to quota.refresh();
            # this regression stubs only the network sampler, keeping the real
            # gate server and the real shared admission helper in the path.
            patch.object(quota, 'refresh', lambda force=False: self.quota_view),
        ]
        for patcher in self.patchers:
            patcher.start()
        import kimi_gate
        self.gate = kimi_gate
        self.server = kimi_gate.create_server(common.config(), port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        for patcher in reversed(self.patchers):
            patcher.stop()

    def _write_quota(self, **record):
        base = {'state': 'ok', 'sampled_at': time.time(), 'checked_at': time.time(),
                'available': True, 'windows': [dict(WEEK_5H), dict(WEEK_7D)]}
        base.update(record)
        self.quota_view = quota.view({'kimi-for-coding': base})

    def _post(self, model):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            body = json.dumps({'model': model, 'messages': [{'role': 'user', 'content': 'x'}],
                               'stream': False})
            connection.request('POST', '/v1/chat/completions', body=body,
                               headers={'Authorization': 'Bearer ' + self.password,
                                        'Content-Type': 'application/json'})
            response = connection.getresponse()
            payload = json.loads(response.read() or b'{}')
            return response.status, payload
        finally:
            connection.close()

    def test_gate_blocks_reserved_ultra_when_five_hour_window_is_empty(self):
        self._write_quota(windows=[sized(WEEK_5H, 0.0), dict(WEEK_7D)])
        status, payload = self._post('k3.1-pro')
        self.assertEqual(status, 429)
        self.assertEqual(payload['error']['code'],
                         'quota_exhausted_or_account_unavailable')
        # Ordinary Kimi is equally blocked by the base gate, before the reserve.
        status, payload = self._post('kimi-for-coding')
        self.assertEqual(status, 429)
        self.assertEqual(payload['error']['code'],
                         'quota_exhausted_or_account_unavailable')

    def test_gate_blocks_credential_rejection_and_rate_limiting(self):
        self._write_quota(state='auth_error')
        status, payload = self._post('k3.1-pro')
        self.assertEqual((status, payload['error']['code']), (429, 'credential_rejected'))
        self._write_quota(state='rate_limited')
        status, payload = self._post('k3.1-pro')
        self.assertEqual((status, payload['error']['code']), (429, 'quota_endpoint_rate_limited'))

    def test_gate_blocks_unavailable_account_and_billing_circuit(self):
        self._write_quota(available=False)
        status, payload = self._post('k3.1-pro')
        self.assertEqual((status, payload['error']['code']),
                         (429, 'quota_exhausted_or_account_unavailable'))
        self._write_quota()
        quota.trip('kimi-for-coding', 'monthly usage limit', 'm-gate-monthly',
                   reason=quota.MONTHLY_REASON)
        status, payload = self._post('k3.1-pro')
        self.assertEqual((status, payload['error']['code']), (429, 'provider_billing_blocked'))

    def test_gate_applies_weekly_reserve_to_ordinary_kimi_only(self):
        self._write_quota(windows=[dict(WEEK_5H), sized(WEEK_7D, 15.38)])
        status, payload = self._post('kimi-for-coding')
        self.assertEqual(status, 429)
        self.assertEqual(payload['error']['code'], 'kimi_weekly_reserved_for_ultra')
        # The reserved model is not rejected by the gate; it may only proceed
        # through the real upstream, which this test never contacts. Verify the
        # decision function instead so no paid request is issued.
        self.assertEqual(self.gate.preflight('k3.1-pro', common.config()),
                         (True, 'weekly_reserve_beneficiary'))

    def test_gate_rejects_unknown_and_retired_models_without_upstream_calls(self):
        self._write_quota()
        status, payload = self._post('k3')
        self.assertEqual(status, 400)
        self.assertEqual(payload['error']['code'], 'unsupported_model')

    def test_manual_release_reopens_the_gate_but_never_a_true_zero_window(self):
        self._write_quota()
        quota.trip('kimi-for-coding', 'monthly usage limit', 'm-gate-release',
                   reason=quota.MONTHLY_REASON)
        status, payload = self._post('k3.1-pro')
        self.assertEqual((status, payload['error']['code']),
                         (429, 'provider_billing_blocked'))
        self.assertTrue(quota.retry_provider('kimi-for-coding')['released'])
        self.assertEqual(self.gate.preflight('k3.1-pro', common.config()),
                         (True, 'weekly_reserve_beneficiary'))
        self._write_quota(windows=[sized(WEEK_5H, 0.0), dict(WEEK_7D)])
        self.assertEqual(self.gate.preflight('k3.1-pro', common.config()),
                         (False, 'quota_exhausted_or_account_unavailable'))


class UltraDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state = root / 'state'
        self.state.mkdir()
        self.config_path = root / 'config.json'
        self.config_path.write_text(json.dumps(config()))
        self.patchers = [
            patch.object(common, 'STATE', self.state),
            patch.object(delegate, 'STATE', self.state),
            patch.object(quota, 'STATE', self.state),
            patch.object(common, 'CONFIG', self.config_path),
            patch.object(delegate, 'CONFIG', self.config_path),
            patch.object(quota, 'credential_identity', lambda provider: 'cred-' + provider),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()

    def _task(self, **overrides):
        value = {'id': 'job-ultra', 'title': 'u', 'status': 'queued', 'mode': 'read',
                 'requested_profile': 'auto', 'tier': 'ultra', 'urgency': 'background',
                 'complexity': 'deep', 'source_dir': '/tmp', 'owner_thread_id': 'owner',
                 'workspace': 'shared', 'scopes': [], 'targets': [], 'resources': []}
        value.update(overrides)
        return value

    def test_explicit_reserved_profile_obeys_the_same_low_weekly_cap(self):
        c = json.loads(self.config_path.read_text())
        far = time.time() + 5 * 86400
        q = view([dict(WEEK_5H), sized(WEEK_7D, 4.0, resets_at=far)])
        active = [self._task(id='job-a', status='running', profile='ultra-reasoning'),
                  self._task(id='job-b', status='running', profile='ultra-reasoning')]
        waiting = self._task(id='job-explicit', tier='deep', complexity='deep',
                             requested_profile='ultra-reasoning')
        choices = delegate.choose_ready(active + [waiting], c, q)
        entry = next(item for item in choices if item[0]['id'] == 'job-explicit')
        self.assertIsNone(entry[1])
        self.assertTrue(entry[2].startswith('ultra_weekly_concurrency_limited:'))

    def test_low_weekly_caps_ultra_concurrency_without_touching_other_tiers(self):
        c = json.loads(self.config_path.read_text())
        far = time.time() + 5 * 86400
        windows = [dict(WEEK_5H), sized(WEEK_7D, 4.0, resets_at=far)]
        q = view(windows)
        active = [self._task(id='job-a', status='running', profile='ultra-reasoning'),
                  self._task(id='job-b', status='running', profile='ultra-reasoning')]
        waiting = self._task(id='job-c')
        choices = delegate.choose_ready(active + [waiting], c, q)
        entry = next(item for item in choices if item[0]['id'] == 'job-c')
        self.assertIsNone(entry[1])
        self.assertTrue(entry[2].startswith('ultra_weekly_concurrency_limited:'))
        # Ordinary Normal work still dispatches (Qwen Flash peer) even at the
        # same low weekly reading, because the reserve only owns Kimi models.
        normal = self._task(id='job-d', tier='normal', complexity='normal')
        choices = delegate.choose_ready(active + [normal], c, q)
        entry = next(item for item in choices if item[0]['id'] == 'job-d')
        self.assertEqual(entry[1], 'aliyun-flash')


if __name__ == '__main__':
    unittest.main()
