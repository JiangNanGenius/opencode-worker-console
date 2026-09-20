import sys
import unittest
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

    def test_validation_rejects_unknown_negative_or_missing_values(self):
        valid = dict(economics.DEFAULTS)
        self.assertEqual(economics.validate(valid), valid)
        for bad in ({}, dict(valid, extra=1), dict(valid, afp_cny_per_unit=-1),
                    dict(valid, kimi_plan_cny=True)):
            with self.assertRaises(ValueError):
                economics.validate(bad)


if __name__ == '__main__':
    unittest.main()
