"""Focused tests for the Alibaba independent-seat Credits ledger.

Independent seat groups reported by GetSubscriptionStats that share one
SeatRefreshTime are one entitlement for that cycle: their Credits add up, the
minimum of the groups is not the subscription remaining, and TotalSeats is never
a multiplier. Unknown member remaining stays unknown, a different cycle is never
summed, total exhaustion alone blocks Alibaba, and account aggregate telemetry
never releases a circuit bound to a rejected/exhausted inference key.
"""
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import bailian_quota
import common
import host_telemetry
import notifications
import quota
from providers import BAILIAN_PROVIDER as PROVIDER

NOW = 2_000_000_000
END = NOW + 30 * 86400
START = NOW - 86400
PRO = {'limit': 100000.0, 'remaining': 13027.237225}
MAX = {'limit': 217742.0, 'remaining': 217742.0}
COMBINED_REMAINING = PRO['remaining'] + MAX['remaining']
COMBINED_LIMIT = PRO['limit'] + MAX['limit']


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def body(items, start=START, end=END):
    return {'Success': True, 'Data': {
        'SubscriptionStartTime': start * 1000,
        'SubscriptionEndTime': end * 1000,
        'Items': items}}


def item(seat, limit, remaining, refresh=END, total=1, assigned=1):
    row = {'SeatType': seat, 'SeatCredits': limit, 'SeatRefreshTime': refresh * 1000,
           'TotalSeats': total, 'AssignedSeats': assigned}
    if remaining is not None:
        row['SeatRemainingCredits'] = remaining
    return row


def stats(pro_remaining=PRO['remaining'], max_remaining=MAX['remaining'],
          pro_limit=PRO['limit'], max_limit=MAX['limit'], total=1):
    return bailian_quota.normalize_stats(body([
        item('pro', pro_limit, pro_remaining),
        item('max', max_limit, max_remaining, total=total),
    ]), NOW)


def record(value=None, **fields):
    value = dict(value or stats())
    value.update(state='ok', sampled_at=NOW, source=bailian_quota.SOURCE_LIVE)
    value.update(fields)
    return value


class AlibabaSeatLedger(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.patchers = [patch.object(common, 'STATE', self.state),
                         patch.object(quota, 'STATE', self.state),
                         patch.object(quota, 'config', return_value={}),
                         patch.object(quota, 'credential_identity', return_value='inference')]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def allowed(self, value, config=None, complexity='normal'):
        if config is not None:
            with patch.object(quota, 'config', return_value=config):
                return quota.allowed(PROVIDER, {PROVIDER: value}, complexity)
        return quota.allowed(PROVIDER, {PROVIDER: value}, complexity)

    def test_independent_same_cycle_seats_sum_instead_of_minimum(self):
        value = stats()
        self.assertEqual(len(value['windows']), 1)
        window = value['windows'][0]
        self.assertEqual(window['name'], bailian_quota.POOL_WINDOW)
        self.assertEqual(window['limit'], COMBINED_LIMIT)
        self.assertAlmostEqual(window['remaining'], COMBINED_REMAINING, places=6)
        self.assertAlmostEqual(window['remaining_percent'], round(COMBINED_REMAINING / COMBINED_LIMIT * 100, 3))
        # 317742 / 230769.237225 is the subscription total, not the 13% pro bucket.
        self.assertGreater(window['remaining_percent'], 70)
        self.assertLess(window['remaining_percent'], 75)
        self.assertEqual(window['resets_at'], iso(END))
        self.assertEqual(window['duration_minutes'], 44640)
        self.assertEqual(window['seat_count'], 2)
        self.assertEqual(window['seats'], {'total': 2, 'assigned': 2})
        self.assertTrue(window['valid'])
        self.assertTrue(value['available'])
        # Per-seat detail is outside windows, so min-window consumers see 72.63%
        # and can never compute the scarcest single seat by accident.
        groups = value['seat_groups']
        self.assertEqual([g['seat_type'] for g in groups], ['pro', 'max'])
        self.assertAlmostEqual(groups[0]['remaining'], PRO['remaining'])
        self.assertAlmostEqual(groups[0]['remaining_percent'], round(PRO['remaining'] / PRO['limit'] * 100, 3))
        self.assertAlmostEqual(groups[1]['remaining_percent'], 100.0)
        self.assertEqual([w['name'] for w in value['windows']], [bailian_quota.POOL_WINDOW])
        visible = [w['remaining_percent'] for w in value['windows'] if w.get('valid')]
        self.assertEqual(min(visible), window['remaining_percent'])

    def test_total_seats_is_metadata_and_never_a_multiplier(self):
        # One group with TotalSeats=2 already reports the group aggregate.
        single = bailian_quota.normalize_stats(body([
            item('pro', 120000, 60000, total=2)]), NOW)
        self.assertEqual(single['windows'][0]['limit'], 120000)
        self.assertEqual(single['windows'][0]['remaining'], 60000)
        self.assertNotIn('seat_groups', single)
        # The same holds for a member of a combined ledger.
        value = stats(max_limit=120000, max_remaining=60000, total=3)
        window = value['windows'][0]
        self.assertEqual(window['limit'], PRO['limit'] + 120000)
        self.assertAlmostEqual(window['remaining'], PRO['remaining'] + 60000)
        self.assertEqual(window['seats']['total'], 4)

    def test_single_group_keeps_legacy_window_shape(self):
        value = bailian_quota.normalize_stats(body([item('pro', 100000, 80000)]), NOW)
        self.assertEqual(len(value['windows']), 1)
        window = value['windows'][0]
        self.assertEqual(window['name'], 'Credits(pro)')
        self.assertEqual(window['limit'], 100000)
        self.assertEqual(window['remaining'], 80000)
        self.assertEqual(window['remaining_percent'], 80.0)
        self.assertTrue(value['available'])
        self.assertNotIn('seat_groups', value)
        self.assertNotIn('seat_count', window)

    def test_one_exhausted_seat_does_not_block_combined_positive_pool(self):
        value = record(stats(pro_remaining=0))
        self.assertTrue(value['available'])
        self.assertGreater(value['windows'][0]['remaining'], 0)
        view = quota.view({PROVIDER: value})[PROVIDER]
        self.assertEqual(self.allowed(view), (True, 'quota_available'))

    def test_total_exhaustion_alone_blocks_alibaba(self):
        value = record(stats(pro_remaining=0, max_remaining=0))
        self.assertFalse(value['available'])
        self.assertEqual(value['windows'][0]['remaining'], 0)
        view = quota.view({PROVIDER: value})[PROVIDER]
        self.assertEqual(self.allowed(view),
                         (False, 'quota_exhausted_or_account_unavailable'))
        self.assertFalse(bailian_quota.positive_live_proof(value, now=NOW))

    def test_unknown_seat_remaining_never_becomes_partial_or_zero(self):
        value = stats(max_remaining=None)
        window = value['windows'][0]
        # Entitlement stays known; only remaining/percentage are unknown.
        self.assertEqual(window['limit'], COMBINED_LIMIT)
        self.assertIsNone(window['remaining'])
        self.assertIsNone(window['remaining_percent'])
        self.assertFalse(window['valid'])
        self.assertTrue(window['credits_service_degraded'])
        self.assertIsNone(value['available'])
        self.assertIn('credits_service_degraded', value['seat_groups'][1])
        ok, reason = self.allowed(quota.view({PROVIDER: record(value)})[PROVIDER])
        self.assertTrue(ok, reason)
        self.assertEqual(reason, 'quota_unknown')
        self.assertFalse(bailian_quota.positive_live_proof(record(value), now=NOW))

    def test_invalid_member_is_never_hidden_by_a_plausible_sum(self):
        cases = {
            'negative remaining': item('pro', 100000, -10),
            'remaining above limit': item('pro', 100000, 100001),
            'zero limit': item('pro', 0, 0),
        }
        for label, member in cases.items():
            with self.subTest(label=label):
                value = bailian_quota.normalize_stats(body([
                    member, item('max', 217742, 100)]), NOW)
                window = value['windows'][0]
                self.assertFalse(window['valid'])
                self.assertIsNone(window['remaining_percent'])
                self.assertTrue(window['invalid_member'])
                self.assertIsNone(value['available'])
                view = quota.view({PROVIDER: record(value)})[PROVIDER]
                ok, reason = self.allowed(view)
                self.assertTrue(ok, reason)
                self.assertNotEqual(reason, 'quota_available')
                self.assertFalse(bailian_quota.positive_live_proof(record(value), now=NOW))

    def test_different_expiry_groups_are_never_guessed_into_a_sum(self):
        value = bailian_quota.normalize_stats(body([
            item('pro', 100000, 13027.237225),
            item('max', 217742, 217742, refresh=END + 86400),
        ]), NOW)
        self.assertEqual(len(value['windows']), 2)
        self.assertEqual([w['name'] for w in value['windows']], ['Credits(pro)', 'Credits(max)'])
        self.assertNotIn('seat_groups', value)
        self.assertNotIn(COMBINED_LIMIT, [w['limit'] for w in value['windows']])

    def test_shared_expired_cycle_stays_invalid_and_prepaid_blocks(self):
        value = bailian_quota.normalize_stats(body([
            item('pro', 100000, 50000, refresh=NOW - 100),
            item('max', 217742, 217742, refresh=NOW - 100),
        ]), NOW)
        window = value['windows'][0]
        self.assertTrue(window['expired'])
        self.assertFalse(window['valid'])
        self.assertIsNone(value['available'])
        config = {'profiles': {'ali': {'model': 'bailian-token-plan/qwen3.8-max'}},
                  'budget_signals': {PROVIDER: {'mode': 'prepaid'}}}
        self.assertEqual(self.allowed(quota.view({PROVIDER: record(value)})[PROVIDER], config=config),
                         (False, 'quota_exhausted_or_account_unavailable'))

    def test_reported_zero_total_blocks_even_with_an_expired_member(self):
        value = bailian_quota.normalize_stats(body([
            item('pro', 100000, 0, refresh=NOW - 100),
            item('max', 217742, 0),
        ]), NOW)
        window = value['windows'][0]
        self.assertEqual(window['remaining'], 0)
        self.assertTrue(window['expired'])
        self.assertFalse(window['valid'])
        self.assertFalse(value['available'])
        view = quota.view({PROVIDER: record(value)})[PROVIDER]
        self.assertEqual(self.allowed(view),
                         (False, 'quota_exhausted_or_account_unavailable'))

    def test_positive_aggregate_never_clears_a_key_bound_circuit(self):
        block = {'credential': 'inference', 'opened_at': NOW - 100,
                 'reason': quota.BALANCE_REASON, 'message_ids': ['seen'], 'message': 'depleted'}
        common.write_json(self.state / quota.BILLING, {PROVIDER: block})
        pooled = record(stats(), sampled_at=NOW - 1)
        args = {'provider': PROVIDER, 'query_started_at': NOW - 1,
                'inference_identity': 'inference', 'control_identity': 'control',
                'now': NOW, 'value': pooled}
        with patch.object(bailian_quota, 'credential_identity', return_value='control'):
            self.assertFalse(quota.account_api_recovery(**args))
        self.assertNotIn('cleared_at', common.read_json(self.state / quota.BILLING, {})[PROVIDER])
        # A single-seat reading (no seat_groups) keeps the legacy recovery path.
        single = record(bailian_quota.normalize_stats(body([item('pro', 100000, 80000)]), NOW),
                        sampled_at=NOW - 1)
        with patch.object(bailian_quota, 'credential_identity', return_value='control'):
            self.assertTrue(quota.account_api_recovery(**dict(args, value=single)))
        self.assertIn('cleared_at', common.read_json(self.state / quota.BILLING, {})[PROVIDER])
        # A rejected inference key is never cleared by account telemetry at all,
        # pooled or not.
        common.write_json(self.state / quota.BILLING, {PROVIDER: dict(
            block, reason=quota.AUTH_REASON, cleared_at=None)})
        with patch.object(bailian_quota, 'credential_identity', return_value='control'):
            self.assertFalse(quota.account_api_recovery(**args))
            self.assertFalse(quota.account_api_recovery(**dict(args, value=single)))

    def test_view_exposes_combined_window_and_seat_groups_once(self):
        common.write_json(self.state / 'quota.json', {PROVIDER: record(
            stats(), _credential='control', checked_at=NOW)})
        view = quota.view({PROVIDER: record(stats(), _credential='control', checked_at=NOW)})
        value = view[PROVIDER]
        self.assertEqual(len(value['windows']), 1)
        self.assertEqual(value['windows'][0]['name'], bailian_quota.POOL_WINDOW)
        self.assertAlmostEqual(value['windows'][0]['remaining'], COMBINED_REMAINING, places=6)
        self.assertEqual(len(value['seat_groups']), 2)
        self.assertEqual([g['seat_type'] for g in value['seat_groups']], ['pro', 'max'])
        self.assertNotIn('seat_groups', value['windows'][0])
        self.assertNotIn('_credential', value)

    def test_host_small_display_receives_one_combined_credits_row(self):
        row = host_telemetry.normalize_worker(PROVIDER, record(
            stats(), _credential='control', checked_at=NOW, stale=False))
        self.assertEqual(len(row['windows']), 1)
        window = row['windows'][0]
        self.assertAlmostEqual(window['remaining_percent'], 72.6, places=1)
        self.assertEqual(window['unit'], 'credits')
        self.assertAlmostEqual(window['remaining'], round(COMBINED_REMAINING, 3))
        self.assertAlmostEqual(window['limit'], COMBINED_LIMIT)
        self.assertTrue(row['available'])


class AlibabaSeatAlerts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.patchers = [patch.object(common, 'STATE', self.state),
                         patch.object(quota, 'STATE', self.state),
                         patch.object(quota, 'config', return_value={}),
                         patch.object(quota, 'credential_identity', return_value='inference'),
                         patch.object(quota, 'billing_block', return_value=None),
                         patch.object(notifications, 'send', side_effect=self._send)]
        for patcher in self.patchers:
            patcher.start()
        self.settings = notifications.normalize({'enabled': True, 'quota_transitions': True})

    @staticmethod
    def _send(title, body, **kwargs):
        return {'sent': True, 'title': title, 'body': body}

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def observe(self, value):
        value = record(value, stale=False)
        return notifications._observe_alibaba_credits(self.settings, {PROVIDER: value})

    def test_healthy_pool_never_warns_on_the_scarcest_seat(self):
        self.assertEqual(self.observe(stats()), [])

    def test_pool_warning_uses_the_combined_share(self):
        events = self.observe(stats(pro_remaining=4.5, max_remaining=4.5))
        self.assertEqual(len(events), 1)
        self.assertIn('合并后', events[0]['body'])
        self.assertIn('2 个坐席合计', events[0]['body'])

    def test_pool_exhaustion_names_all_seats_and_keeps_the_edge_key(self):
        events = self.observe(stats(pro_remaining=0, max_remaining=0))
        self.assertEqual(len(events), 1)
        self.assertIn('全部坐席', events[0]['title'])
        state = common.read_json(self.state / notifications.STATE_FILE, {})
        self.assertIn('alibaba_credits:inference', state)

    def test_single_seat_keeps_the_legacy_wording(self):
        value = bailian_quota.normalize_stats(body([item('pro', 100000, 0)]), NOW)
        events = self.observe(value)
        self.assertEqual(len(events), 1)
        self.assertIn('阿里云 Credits 已耗尽', events[0]['title'])
        self.assertNotIn('全部坐席', events[0]['title'])

    def test_positive_aggregate_cannot_rearm_a_current_key_circuit(self):
        with patch.object(quota, 'billing_block', return_value={'reason': quota.BALANCE_REASON}):
            self.assertEqual(self.observe(stats()), [])


if __name__ == '__main__':
    unittest.main()
