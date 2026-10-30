"""Display-only hiding for fresh allowances that cannot recover without payment."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import allowance_visibility

NOW = 1_790_000_000.0  # Fixed synthetic time; no live account timestamp.


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def team_plan(remaining=0.0, sampled_at=None, reset=None):
    reset = reset if reset is not None else iso(NOW + 17 * 86400)
    return {
        'state': 'ok', 'source': 'openapi_live', 'plan_edition': 'team',
        'sampled_at': sampled_at if sampled_at is not None else NOW,
        'available': remaining > 0,
        'windows': [{'name': 'Credits(max)', 'seat_type': 'max', 'limit': 250000.0,
                     'remaining': remaining, 'remaining_percent': remaining / 250000.0 * 100,
                     'duration_minutes': 44640.0, 'resets_at': reset, 'valid': True}],
        'balances': [],
        'subscription': {'start_at': iso(NOW - 14 * 86400), 'end_at': reset},
    }


def personal_plan(percent=0.0, sampled_at=None, reset=None):
    reset = reset if reset is not None else iso(NOW + 27 * 86400)
    return {
        'state': 'ok', 'source': 'personal_console', 'plan_edition': 'personal',
        'sampled_at': sampled_at if sampled_at is not None else NOW,
        'available': percent > 0,
        'windows': [{'name': 'Monthly', 'unit': 'percent',
                     'remaining_percent': percent, 'duration_minutes': 45111.23,
                     'resets_at': reset, 'valid': True}],
        'balances': [],
        'subscription': {'start_at': iso(NOW - 4 * 86400), 'end_at': reset},
        'addon': {'known': True, 'unit': 'credits', 'remaining': 0.0, 'limit': 40000.0,
                  'used': 40000.0, 'active_count': 2, 'remaining_percent': 0.0,
                  'nearest_expires_at': iso(NOW + 29 * 86400)},
    }


class TerminalAllowanceTests(unittest.TestCase):
    def test_live_alibaba_snapshot_hides_both_plans(self):
        # Representative provider shape: both windows reset at their paid-term end,
        # so neither reset is an inside-term recovery.
        self.assertTrue(allowance_visibility.terminal_exhausted(team_plan(), 'aliyun-team', NOW))
        self.assertTrue(allowance_visibility.terminal_exhausted(personal_plan(), 'aliyun-personal', NOW))

    def test_one_live_plan_keeps_only_the_other_plan_visible(self):
        self.assertTrue(allowance_visibility.terminal_exhausted(team_plan(), 'aliyun-team', NOW))
        self.assertFalse(allowance_visibility.terminal_exhausted(
            personal_plan(97.48), 'aliyun-personal', NOW))

    def test_replenishment_reappears(self):
        self.assertFalse(allowance_visibility.terminal_exhausted(
            team_plan(250000.0), 'aliyun-team', NOW))
        self.assertFalse(allowance_visibility.terminal_exhausted(
            personal_plan(0.01), 'aliyun-personal', NOW))

    def test_window_resetting_inside_the_paid_term_is_recoverable(self):
        plan = team_plan()
        plan['windows'] = [
            {'name': 'limit5h', 'valid': True, 'remaining': 0, 'remaining_percent': 0,
             'resets_at': iso(NOW + 300)},
            {'name': 'limit7d', 'valid': True, 'remaining': 0, 'remaining_percent': 0,
             'resets_at': iso(NOW + 3 * 86400)},
        ]
        self.assertFalse(allowance_visibility.terminal_exhausted(plan, 'aliyun-team', NOW))

    def test_ark_empty_monthly_pool_hides_positive_short_cycle_guards(self):
        end = iso(NOW + 20 * 86400)
        plan = {'state': 'ok', 'sampled_at': NOW, 'available': False, 'balances': [],
                'plan': {'type': 'Medium', 'active': True, 'ends_at': end},
                'windows': [
                    {'name': 'AFPFiveHour', 'valid': True, 'remaining': 9000,
                     'remaining_percent': 90, 'resets_at': iso(NOW + 300)},
                    {'name': 'AFPDaily', 'valid': True, 'remaining': 40000,
                     'remaining_percent': 80, 'resets_at': iso(NOW + 86400)},
                    {'name': 'AFPMonthly', 'valid': True, 'remaining': 0,
                     'remaining_percent': 0, 'resets_at': end}]}
        self.assertTrue(allowance_visibility.terminal_exhausted(
            plan, 'volcengine-agent-plan', NOW))

        # A monthly reset inside the paid term is recoverable by waiting.
        plan['windows'][2]['resets_at'] = iso(NOW + 10 * 86400)
        self.assertFalse(allowance_visibility.terminal_exhausted(
            plan, 'volcengine-agent-plan', NOW))

        # An empty 5-hour/daily guard beside a live monthly pool is temporary.
        plan['windows'][2].update(remaining=50000, remaining_percent=50)
        plan['windows'][1].update(remaining=0, remaining_percent=0)
        self.assertFalse(allowance_visibility.terminal_exhausted(
            plan, 'volcengine-agent-plan', NOW))

    def test_nonempty_live_error_never_hides(self):
        plan = team_plan()
        plan['live_error'] = 'Retained reading after the query failed'
        self.assertFalse(allowance_visibility.terminal_exhausted(plan, 'aliyun-team', NOW))
        plan['live_error'] = {}
        self.assertTrue(allowance_visibility.terminal_exhausted(plan, 'aliyun-team', NOW))

    def test_kimi_short_and_weekly_zero_without_paid_end_is_retained(self):
        kimi = {'state': 'ok', 'sampled_at': NOW, 'available': False, 'balances': [],
                'windows': [
                    {'name': 'window_0', 'valid': True, 'remaining': 0,
                     'remaining_percent': 0, 'resets_at': iso(NOW + 300)},
                    {'name': 'overall', 'valid': True, 'remaining': 0,
                     'remaining_percent': 0, 'resets_at': iso(NOW + 3 * 86400)}]}
        self.assertFalse(allowance_visibility.terminal_exhausted(kimi, 'kimi-for-coding', NOW))

    def test_unknown_missing_or_stale_data_is_never_hidden(self):
        no_subscription = team_plan()
        no_subscription.pop('subscription')
        self.assertFalse(allowance_visibility.terminal_exhausted(
            no_subscription, 'aliyun-team', NOW))

        self.assertFalse(allowance_visibility.terminal_exhausted(
            team_plan(sampled_at=NOW - allowance_visibility.FRESH_SECONDS - 1),
            'aliyun-team', NOW))

        stale = team_plan()
        stale['stale'] = True
        self.assertFalse(allowance_visibility.terminal_exhausted(stale, 'aliyun-team', NOW))

        failed = team_plan()
        failed['live_error'] = {'state': 'unavailable'}
        self.assertFalse(allowance_visibility.terminal_exhausted(failed, 'aliyun-team', NOW))

        unknown_amount = team_plan()
        unknown_amount['windows'][0]['remaining'] = None
        self.assertFalse(allowance_visibility.terminal_exhausted(
            unknown_amount, 'aliyun-team', NOW))

        unknown_percent = personal_plan()
        unknown_percent['windows'][0]['remaining_percent'] = None
        self.assertFalse(allowance_visibility.terminal_exhausted(
            unknown_percent, 'aliyun-personal', NOW))

        unknown_addon = personal_plan()
        unknown_addon['addon']['known'] = False
        self.assertFalse(allowance_visibility.terminal_exhausted(
            unknown_addon, 'aliyun-personal', NOW))

        missing_addon = personal_plan()
        missing_addon.pop('addon')
        self.assertFalse(allowance_visibility.terminal_exhausted(
            missing_addon, 'aliyun-personal', NOW))

        stale_snapshot = team_plan()
        stale_snapshot['snapshot'] = {'freshness': 'stale', 'captured_at': iso(NOW)}
        self.assertFalse(allowance_visibility.terminal_exhausted(
            stale_snapshot, 'aliyun-team', NOW))

    def test_deepseek_zero_cash_requires_top_up_and_is_hidden(self):
        value = {'state': 'ok', 'sampled_at': NOW, 'available': False, 'windows': [],
                 'balances': [{'currency': 'CNY', 'remaining': 0.0}]}
        self.assertTrue(allowance_visibility.terminal_exhausted(value, 'deepseek', NOW))
        self.assertFalse(allowance_visibility.terminal_exhausted(
            dict(value, balances=[{'currency': 'CNY', 'remaining': 0.01}]),
            'deepseek', NOW))
        self.assertFalse(allowance_visibility.terminal_exhausted(
            dict(value, balances=[]), 'deepseek', NOW))
        self.assertFalse(allowance_visibility.terminal_exhausted(
            dict(value, balances=[{'currency': 'CNY'}]), 'deepseek', NOW))
        self.assertFalse(allowance_visibility.terminal_exhausted(
            dict(value, stale=True), 'deepseek', NOW))

    def test_a_positive_sibling_window_keeps_the_plan_visible(self):
        plan = team_plan()
        plan['windows'].append({'name': 'Credits(max-standby)', 'valid': True,
                                'remaining': 1, 'remaining_percent': 0.001,
                                'resets_at': iso(NOW + 17 * 86400)})
        self.assertFalse(allowance_visibility.terminal_exhausted(plan, 'aliyun-team', NOW))

    def test_nonzero_paid_end_and_malformed_windows_are_conservative(self):
        expired = team_plan()
        expired['subscription']['end_at'] = iso(NOW - 60)
        expired['windows'][0]['resets_at'] = iso(NOW - 60)
        # The plan already ended: recovery still needs renewal, and every empty
        # window is at the end, so this is terminal rather than an unknown zero.
        self.assertTrue(allowance_visibility.terminal_exhausted(expired, 'aliyun-team', NOW))

        missing_reset = team_plan()
        missing_reset['windows'][0]['resets_at'] = None
        self.assertFalse(allowance_visibility.terminal_exhausted(
            missing_reset, 'aliyun-team', NOW))

        expired_window = team_plan()
        expired_window['windows'][0]['expired'] = True
        self.assertFalse(allowance_visibility.terminal_exhausted(
            expired_window, 'aliyun-team', NOW))


if __name__ == '__main__':
    unittest.main()
