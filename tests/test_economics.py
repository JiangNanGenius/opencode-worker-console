import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import economics


class EconomicsTests(unittest.TestCase):
    def test_default_unit_and_baseline_formula_match_plan_math(self):
        result = economics.summary({}, {})
        self.assertEqual(result['afp_eq_per_cny'], 500.0)
        self.assertEqual(result['kimi_plan_afp_eq'], 349500.0)
        self.assertAlmostEqual(result['baseline_ark_afp_per_m']['deep'], 1000 / 3)
        self.assertEqual(result['baseline_ark_afp_per_m']['normal'], 125.0)
        self.assertEqual(result['baseline_ark_afp_per_m']['small'], 50.0)

    def test_live_monthly_ark_usage_is_authoritative_and_costed(self):
        quota = {'volcengine-agent-plan': {'windows': [
            {'name': 'AFPMonthly', 'valid': True, 'limit': 500000, 'remaining': 499750,
             'resets_at': '2026-10-20T00:00:00+00:00'}]}}
        monthly = economics.summary({}, quota)['ark_monthly']
        self.assertEqual(monthly['used'], 250)
        self.assertEqual(monthly['used_cny'], 0.5)
        self.assertEqual(monthly['remaining_cny'], 999.5)

    def test_work_pool_fits_providers_by_observed_runtime(self):
        quota = {
            'deepseek': {'state': 'ok', 'stale': False, 'available': True, 'balances': [
                {'currency': 'CNY', 'remaining': 50.0,
                 'consumption_estimate': {'rate_balance_per_hour': 2.0, 'observed_capacity': 60.0}},
                {'currency': 'USD', 'remaining': 5.0}], 'windows': []},
            'kimi-for-coding': {'state': 'ok', 'stale': False, 'available': False, 'windows': [
                {'name': 'overall', 'valid': True, 'remaining_percent': 0,
                 'duration_minutes': 10080,
                 'consumption_estimate': {'rate_percent_per_hour': 0.6}}]},
            'volcengine-agent-plan': {'state': 'ok', 'stale': False, 'windows': [
                {'name': 'AFPWeekly', 'valid': True, 'remaining_percent': 44,
                 'duration_minutes': 10080,
                 'consumption_estimate': {'rate_percent_per_hour': 0.8}}]}}
        pool = economics.work_pool({}, quota)
        self.assertEqual(pool['unit'], 'fitted-hours')
        self.assertEqual(pool['components']['balance']['amount'], 25.0)
        self.assertEqual(pool['components']['balance']['capacity'], 30.0)
        self.assertEqual(pool['components']['kimi']['amount'], 0.0)
        self.assertAlmostEqual(pool['components']['kimi']['capacity'], 166.667)
        self.assertEqual(pool['components']['plan']['amount'], 55.0)
        self.assertEqual(pool['components']['plan']['capacity'], 125.0)
        self.assertEqual(pool['total'], 80.0)
        self.assertAlmostEqual(pool['remaining_percent'], 24.87, places=2)
        self.assertEqual(pool['components']['balance']['excluded_currencies'], ['USD'])
        self.assertTrue(pool['normalization']['kimi_included'])
        self.assertTrue(pool['normalization']['notes']['weights_use_observed_burn'])
        self.assertLess(pool['components']['balance']['capacity'] / pool['capacity'], .1)

        plans = economics.work_pool({}, quota, include_payg_balance=False)
        self.assertEqual(plans['components']['balance']['amount'], 25.0)
        self.assertEqual(plans['total'], 55.0)
        self.assertAlmostEqual(plans['capacity'], 291.667, places=3)
        self.assertAlmostEqual(plans['remaining_percent'], 18.857, places=3)
        self.assertFalse(plans['normalization']['payg_balance_included'])

        # A Kimi reset immediately restores its fitted full runtime to the pool.
        quota['kimi-for-coding']['available'] = True
        quota['kimi-for-coding']['windows'][0]['remaining_percent'] = 100
        reset = economics.work_pool({}, quota)
        self.assertEqual(reset['components']['kimi']['remaining_percent'], 100.0)
        self.assertGreater(reset['remaining_percent'], 75)

    def test_work_pool_handles_missing_zero_and_stale_sources(self):
        # No sources at all: both components missing, total zero but incomplete.
        empty = economics.work_pool({}, {})
        self.assertEqual(empty['total'], 0.0)
        self.assertEqual(empty['capacity'], 0)
        self.assertIsNone(empty['remaining_percent'])
        self.assertFalse(empty['complete'])
        self.assertEqual(empty['components']['balance']['status'], 'missing')
        self.assertEqual(empty['components']['plan']['status'], 'missing')

        # Zero balance is a real zero; a missing plan keeps the pool partial.
        zero_balance = {
            'deepseek': {'state': 'ok', 'stale': False, 'available': False,
                         'balances': [{'currency': 'CNY', 'remaining': 0}], 'windows': []}}
        pool = economics.work_pool({}, zero_balance)
        self.assertIsNone(pool['components']['balance']['amount'])
        self.assertEqual(pool['components']['balance']['status'], 'unavailable')
        self.assertEqual(pool['components']['plan']['status'], 'missing')
        self.assertEqual(pool['total'], 0.0)
        self.assertFalse(pool['complete'])

        stale = {'deepseek': {'state': 'ok', 'stale': True, 'available': True,
                              'balances': [{'currency': 'CNY', 'remaining': 1.0,
                                            'consumption_estimate': {'rate_balance_per_hour': .1,
                                                                     'observed_capacity': 2.0}}],
                              'windows': []}}
        pool = economics.work_pool({}, stale)
        self.assertEqual(pool['components']['balance']['status'], 'stale')
        self.assertTrue(pool['complete'])

    def test_work_pool_ignores_price_settings_and_uses_window_prior_at_reset(self):
        quota = {'volcengine-agent-plan': {'state': 'ok', 'stale': False, 'windows': [
            {'name': 'AFPFiveHour', 'valid': True, 'remaining_percent': 100,
             'duration_minutes': 300}]}}
        cheap = economics.work_pool({'economics': {'afp_cny_per_unit': .001}}, quota)
        expensive = economics.work_pool({'economics': {'afp_cny_per_unit': .02,
                                                        'kimi_plan_cny': 9999}}, quota)
        self.assertEqual(cheap, expensive)
        self.assertEqual(cheap['components']['plan']['amount'], 5.0)
        self.assertEqual(cheap['components']['plan']['capacity'], 5.0)
        self.assertEqual(cheap['components']['plan']['fit_source'], 'window_prior')

        # Idle-inclusive burn may predict longer than the quota cycle, but a
        # weekly refill cannot contribute more than one week to this meter.
        quota['volcengine-agent-plan']['windows'][0].update(
            name='AFPWeekly', duration_minutes=10080,
            consumption_estimate={'rate_percent_per_hour': .1})
        capped = economics.work_pool({}, quota)
        self.assertEqual(capped['components']['plan']['capacity'], 168.0)

    def test_work_pool_forecasts_independent_provider_refills(self):
        now = datetime(2026, 9, 21, tzinfo=timezone.utc).timestamp()
        def reset(hours):
            return datetime.fromtimestamp(now + hours * 3600, timezone.utc).isoformat()
        quota = {
            'deepseek': {'available': True, 'balances': [{'currency': 'CNY', 'remaining': 25,
                'consumption_estimate': {'rate_balance_per_hour': 1, 'observed_capacity': 30}}]},
            'kimi-for-coding': {'available': False, 'windows': [{'name': 'overall', 'valid': True,
                'remaining_percent': 0, 'duration_minutes': 10080, 'resets_at': reset(20),
                'consumption_estimate': {'rate_percent_per_hour': .5}}]},
            'volcengine-agent-plan': {'available': True, 'windows': [{'name': 'AFPWeekly', 'valid': True,
                'remaining_percent': 44, 'duration_minutes': 10080, 'resets_at': reset(40),
                'consumption_estimate': {'rate_percent_per_hour': 1}}]},
        }
        pool = economics.work_pool({}, quota, now=now)
        self.assertEqual([item['provider'] for item in pool['refills']], ['kimi', 'plan'])
        self.assertEqual(pool['refills'][0]['hours_until'], 20)
        self.assertAlmostEqual(pool['refills'][0]['projected_remaining_percent'], 66.107, places=3)
        self.assertEqual(pool['refills'][1]['hours_until'], 40)
        self.assertAlmostEqual(pool['refills'][1]['projected_remaining_percent'], 83.221, places=3)

    def test_validation_rejects_unknown_negative_or_missing_values(self):
        valid = dict(economics.DEFAULTS)
        self.assertEqual(economics.validate(valid), valid)
        for bad in ({}, dict(valid, extra=1), dict(valid, afp_cny_per_unit=-1),
                    dict(valid, kimi_plan_cny=True)):
            with self.assertRaises(ValueError):
                economics.validate(bad)

    def test_legacy_economics_payload_receives_current_deepseek_defaults(self):
        legacy = {key: economics.DEFAULTS[key] for key in (
            'afp_cny_per_unit', 'kimi_plan_cny', 'ark_auto_afp_per_m',
            'ark_evolving_afp_per_m', 'ark_k3_afp_per_m')}
        saved = economics.validate(legacy)
        self.assertEqual(saved['deepseek_flash_input_cny_per_m'], 2.0)
        self.assertEqual(saved['deepseek_offpeak_multiplier'], 0.5)
        with self.assertRaises(ValueError):
            economics.validate(dict(legacy, deepseek_offpeak_multiplier=1.1))

    def test_deepseek_cost_uses_token_classes_and_beijing_price_band(self):
        # Monday 10:00 Beijing is peak; 20:00 is off-peak.
        peak = datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc).timestamp()
        offpeak = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc).timestamp()
        usage = {'input': 1_000_000, 'cache_read': 1_000_000,
                 'cache_write': 0, 'output': 750_000, 'reasoning': 250_000}
        peak_cost = economics.deepseek_usage_cost(usage, 'deepseek/deepseek-flash', peak)
        offpeak_cost = economics.deepseek_usage_cost(usage, 'deepseek/deepseek-flash', offpeak)
        self.assertTrue(peak_cost['peak'])
        self.assertAlmostEqual(peak_cost['cost_cny'], 10.04)
        self.assertFalse(offpeak_cost['peak'])
        self.assertAlmostEqual(offpeak_cost['cost_cny'], 5.02)


if __name__ == '__main__':
    unittest.main()
