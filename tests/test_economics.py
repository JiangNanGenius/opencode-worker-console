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

    def test_work_pool_adds_only_normalized_components(self):
        quota = {
            'deepseek': {'state': 'ok', 'stale': False, 'available': True, 'balances': [
                {'currency': 'CNY', 'remaining': 2.0},
                {'currency': 'USD', 'remaining': 5.0}], 'windows': []},
            'volcengine-agent-plan': {'state': 'ok', 'stale': False, 'windows': [
                {'name': 'AFPMonthly', 'valid': True, 'limit': 1000, 'remaining': 400,
                 'resets_at': '2026-10-20T00:00:00+00:00'}]}}
        pool = economics.work_pool({}, quota)
        # 2 CNY * 500 AFP-eq/CNY = 1000; plus 400 authoritative live AFP.
        self.assertEqual(pool['components']['balance']['amount'], 1000.0)
        self.assertEqual(pool['components']['balance']['source_amount'], 2.0)
        self.assertEqual(pool['components']['plan']['amount'], 400.0)
        self.assertEqual(pool['total'], 1400.0)
        self.assertTrue(pool['complete'])
        # Raw USD is never silently converted/summed; it is flagged as excluded.
        self.assertEqual(pool['components']['balance']['excluded_currencies'], ['USD'])
        self.assertFalse(pool['normalization']['kimi_included'])

    def test_work_pool_handles_missing_zero_and_stale_sources(self):
        # No sources at all: both components missing, total zero but incomplete.
        empty = economics.work_pool({}, {})
        self.assertEqual(empty['total'], 0.0)
        self.assertFalse(empty['complete'])
        self.assertEqual(empty['components']['balance']['status'], 'missing')
        self.assertEqual(empty['components']['plan']['status'], 'missing')

        # Zero balance is a real zero; a missing plan keeps the pool partial.
        zero_balance = {
            'deepseek': {'state': 'ok', 'stale': False, 'available': False,
                         'balances': [{'currency': 'CNY', 'remaining': 0}], 'windows': []}}
        pool = economics.work_pool({}, zero_balance)
        self.assertEqual(pool['components']['balance']['amount'], 0.0)
        self.assertEqual(pool['components']['balance']['status'], 'unavailable')
        self.assertEqual(pool['components']['plan']['status'], 'missing')
        self.assertEqual(pool['total'], 0.0)
        self.assertFalse(pool['complete'])

        stale = {'deepseek': {'state': 'ok', 'stale': True, 'available': True,
                              'balances': [{'currency': 'CNY', 'remaining': 1.0}], 'windows': []}}
        pool = economics.work_pool({}, stale)
        self.assertEqual(pool['components']['balance']['status'], 'stale')
        self.assertFalse(pool['complete'])

    def test_work_pool_never_uses_nominal_plan_price_as_allowance(self):
        quota = {'volcengine-agent-plan': {'state': 'ok', 'stale': False, 'windows': [
            {'name': 'AFPFiveHour', 'valid': True, 'limit': 100, 'remaining': 50}]}}
        pool = economics.work_pool({}, quota)
        # No valid monthly AFP window means no plan allowance (kimi_plan_afp_eq nominal
        # purchase price is never substituted for a live allowance).
        self.assertIsNone(pool['components']['plan']['amount'])
        self.assertEqual(pool['components']['plan']['status'], 'missing')

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
