"""Spending floor, native tool-round gate and coordinator handoff contracts."""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import deepseek_gate
import kimi_gate
import quota
import server
from test_kimi_gate import Response


class BalanceFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.c = {'deepseek_min_balance_cny': 20,
                  'deepseek_gate': {'enabled': True},
                  'profiles': {'fallback': {'model': 'deepseek/deepseek-flash'}},
                  'routing': {'fast': 'fallback', 'background': 'fallback'}}
        for target, name, value in ((common, 'STATE', self.state),
                                    (quota, 'STATE', self.state)):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(quota, '_telemetry_identity', return_value='fixture-identity')
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(quota, 'config', return_value=self.c)
        p.start()
        self.addCleanup(p.stop)
        self.q = self.sample(21)

    def sample(self, amount, **extra):
        v = dict(state='ok', stale=False, available=True, sampled_at=time.time(),
                 windows=[], balances=[{'currency': 'CNY', 'remaining': amount}],
                 _credential='fixture-identity')
        v.update(extra)
        q = {'deepseek': v}
        common.write_json(self.state / 'quota.json', q)
        return q



class BalanceTests(BalanceFixture, unittest.TestCase):
    def test_boundary_and_recharge(self):
        for balance, expected in ((20.01, True), (20, False), (19.99, False), (0, False), (30, True)):
            with self.subTest(balance=balance):
                self.assertEqual(quota.allowed('deepseek', self.sample(balance), 'normal', c=self.c)[0], expected)

    def test_stale_unknown_wrong_currency_and_rotated_account_fail_closed(self):
        for extra in ({'stale': True}, {'sampled_at': time.time() - 91},
                      {'sampled_at': time.time() + 100}, {'state': 'unknown'},
                      {'balances': [{'currency': 'USD', 'remaining': 999}]},
                      {'balances': []}, {'_credential': 'rotated'},
                      {'balances': [{'currency': 'CNY', 'remaining': float('nan')}]},
                      {'balances': [{'currency': 'CNY', 'remaining': True}]}):
            with self.subTest(extra=extra):
                self.assertFalse(quota.deepseek_balance_allowed(self.c, self.sample(99, **extra))[0])

    def test_explicit_profile_and_monthly_retry_cannot_bypass_floor(self):
        q = self.sample(17.13)
        for t in ({'tier': 'fast', 'requested_profile': 'auto'},
                  {'tier': 'normal', 'requested_profile': 'fallback'}):
            self.assertIsNone(quota.route(t, self.c, q)[0])
        self.assertFalse(quota.allowed('deepseek', q, 'normal', retry_monthly=True, prepaid=True, c=self.c)[0])

    def test_below_floor_is_proven_budget_stop_but_unknown_is_not(self):
        q = self.sample(17.13)
        self.assertTrue(quota.is_capacity_exhaustion_reason('deepseek_balance_below_floor', 'deepseek', q=q))
        q = self.sample(17.13, stale=True)
        self.assertFalse(quota.is_capacity_exhaustion_reason('deepseek_balance_below_floor', 'deepseek', q=q))
        self.assertFalse(quota.is_capacity_exhaustion_reason('deepseek_balance_unknown', 'deepseek', q=q))

    def test_disabled_floor_keeps_legacy_admission(self):
        self.assertTrue(quota.allowed('deepseek', {}, 'normal', c={})[0])

    def test_invalid_floor_never_unlocks(self):
        for floor in (-1, True, '20', float('nan'), float('inf')):
            self.assertFalse(quota.deepseek_balance_allowed({'deepseek_min_balance_cny': floor}, self.q)[0])

    def test_runtime_requires_gate_and_uses_only_local_password_reference(self):
        with self.assertRaises(ValueError):
            server.runtime_overlay(dict(self.c, deepseek_gate={'enabled': False}))
        overlay = server.runtime_overlay(self.c)
        options = overlay['provider']['deepseek']['options']
        self.assertEqual(options['baseURL'], 'http://127.0.0.1:51532/v1')
        self.assertTrue(options['apiKey'].startswith('{file:'))

    def test_gate_settings(self):
        self.assertEqual(deepseek_gate.settings(self.c), {'port': 51532})
        for block in (True, [], {'enabled': 'true'}, {'enabled': True, 'port': 0},
                      {'enabled': True, 'upstream': 'https://other.invalid'}):
            with self.subTest(block=block), self.assertRaises(ValueError):
                deepseek_gate.settings({'deepseek_gate': block})


class NativeRoundTests(BalanceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.password = 'fixture-local-only'
        self.key = 'fixture-upstream-only'
        password = self.state / 'server-password'
        password.write_text(self.password)
        password.chmod(0o600)
        self.upstream = Mock()
        self.upstream.getresponse.side_effect = lambda: Response()
        for p in (patch.object(common, 'config', return_value=self.c),
                  patch.object(common, 'auth_key', return_value=self.key),
                  patch.object(quota, 'refresh', side_effect=lambda: self.q),
                  patch.object(kimi_gate.http.client, 'HTTPSConnection', return_value=self.upstream)):
            p.start()
            self.addCleanup(p.stop)
        self.http = deepseek_gate.create_server(self.c, port=0)
        thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)

    def request(self, model='deepseek-flash', password=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=3)
        try:
            conn.request('POST', '/v1/chat/completions', json.dumps({'model': model}),
                         {'Authorization': 'Bearer ' + (password or self.password)})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_next_tool_round_is_blocked_then_recharge_recovers(self):
        self.assertEqual(self.request()[0], 200)
        self.q = self.sample(20)
        status, body = self.request()
        self.assertEqual(status, 429)
        self.assertEqual(body['error']['code'], 'deepseek_balance_below_floor')
        self.assertEqual(self.upstream.request.call_count, 1)
        self.q = self.sample(21)
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.upstream.request.call_count, 2)
        args, kwargs = self.upstream.request.call_args
        self.assertEqual(args, ('POST', '/v1/chat/completions'))
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer ' + self.key)

    def test_unknown_balance_and_unsupported_model_never_reach_upstream(self):
        self.q = self.sample(99, stale=True)
        self.assertEqual(self.request()[0], 429)
        self.assertEqual(self.request('k3.1-pro')[0], 400)
        self.assertEqual(self.request(password='wrong')[0], 401)
        self.upstream.request.assert_not_called()

    def test_authenticated_health(self):
        self.assertTrue(deepseek_gate.wait_ready(self.http.server_port, self.password, timeout=1))
        self.upstream.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
