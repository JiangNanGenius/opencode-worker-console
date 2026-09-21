"""Segment-level DeepSeek accounting and cleaned-ledger retention.

Covers the mixed-provider bugs directly: a task that used Kimi and DeepSeek must
never have its whole token total charged to DeepSeek, an ambiguous legacy record
stays unknown, the cleanup ledger retains safe per-model rows, non-DeepSeek
evidence is excluded and no prompt/secret field can ride into the ledger.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import economics
import task_activity
import usage_ledger


def usage(**overrides):
    value = {'input': None, 'output': None, 'reasoning': None, 'cache_read': None,
             'cache_write': None, 'total': None, 'cost': None, 'source': 'saved',
             'complete': True}
    value.update(overrides)
    return value


def with_model(model, **overrides):
    row = usage(**overrides)
    row['model'] = model
    return row


class SegmentAccountingCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.config = self.root / 'config.json'
        self.patchers = [patch.object(common, 'STATE', self.state),
                         patch.object(common, 'CONFIG', self.config)]
        for item in self.patchers:
            item.start()
        common.init()
        common.write_json(self.config, {
            'profiles': {'fallback': {'model': 'deepseek/deepseek-flash'},
                         'senior-code': {'model': 'kimi-for-coding/kimi-for-coding'}},
            'economics': {},
        })
        # Monday 02:00 UTC is 10:00 Beijing, inside DeepSeek's peak band.
        self.now = datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc).timestamp()

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.tmp.cleanup()

    def config_value(self):
        return common.read_json(self.config)

    def add_task(self, task_id, **fields):
        task = {'id': task_id, 'status': 'completed', 'created_at': self.now - 600,
                'finished_at': self.now - 60, 'profile': 'fallback', 'actual_models': []}
        task.update(fields)
        common.write_json(common.task_path(task_id), task)
        return task

    def add_ledger_entry(self, task_id, usage_value, **fields):
        entry = {'task_id': task_id, 'status': 'completed', 'tier': 'normal', 'profile': 'fallback',
                 'actual_models': ['deepseek/deepseek-flash'], 'fallback_used': False,
                 'created_at': self.now - 600, 'finished_at': self.now - 120,
                 'usage': usage_value, 'deleted_at': self.now - 100, 'expires_at': self.now + 1000}
        entry.update(fields)
        common.write_json(common.STATE / 'usage-ledger' / (task_id + '.json'), entry)
        return entry

    def spend(self):
        return economics.recent_deepseek_spend(self.config_value(), self.now)

    # -- mixed provider segments -----------------------------------------
    def test_mixed_task_prices_only_the_deepseek_segment(self):
        deepseek = usage(input=500_000, cache_read=100_000, output=100_000,
                         reasoning=50_000, total=750_000)
        kimi = usage(input=1_000_000, cache_read=500_000, output=500_000, total=2_000_000)
        task = self.add_task(
            'job-mixed', actual_models=['kimi-for-coding/k3', 'deepseek/deepseek-flash'],
            usage=usage(input=1_500_000, cache_read=600_000, output=600_000,
                        reasoning=50_000, total=2_750_000,
                        by_model=[with_model('kimi-for-coding/k3', **kimi),
                                  with_model('deepseek/deepseek-flash', **deepseek)]))
        with patch.object(task_activity, 'usage_for_task', return_value=task['usage']):
            result = self.spend()
        segment = economics.deepseek_usage_cost(deepseek, 'deepseek/deepseek-flash', self.now)
        whole = economics.deepseek_usage_cost(task['usage'], 'deepseek/deepseek-flash', self.now)
        self.assertEqual(result['tokens'], 750_000)
        self.assertEqual(result['segment_count'], 1)
        self.assertEqual(result['task_count'], 1)
        self.assertAlmostEqual(result['estimated_spend_cny'], segment['cost_cny'], places=9)
        # Regression: charging the whole mixed task would have cost much more.
        self.assertLess(result['estimated_spend_cny'], whole['cost_cny'])
        # The exact per-segment peak timestamp is not retained; a band is reported.
        self.assertFalse(result['peak_confident'])
        self.assertLess(result['estimated_spend_cny_low'], result['estimated_spend_cny_high'])
        self.assertAlmostEqual(result['estimated_spend_cny_low'],
                               result['estimated_spend_cny_high'] / 2, delta=0.001)
        self.assertEqual(result['scope'], 'deepseek_segments_only')

    def test_legacy_mixed_record_is_unknown_instead_of_charged_to_last_model(self):
        self.add_task('job-legacy-mixed',
                      actual_models=['kimi-for-coding/k3', 'deepseek/deepseek-flash'],
                      usage=usage(input=1_000_000, total=1_000_000))
        only_legacy = self.spend()
        self.assertIsNotNone(only_legacy)
        self.assertIsNone(only_legacy['estimated_spend_cny'])
        self.assertIsNone(only_legacy['rate_balance_per_hour'])
        self.assertEqual(only_legacy['unknown_task_count'], 1)
        self.assertEqual(only_legacy['unknown_tokens'], 1_000_000)
        self.assertEqual(only_legacy['unknown_tasks'][0]['task_id'], 'job-legacy-mixed')
        self.assertIn('mixed models', only_legacy['unknown_tasks'][0]['reason'])

        clean = self.add_task('job-clean-ds', actual_models=['deepseek/deepseek-flash'],
                              usage=usage(input=1_000_000, total=1_000_000))
        result = self.spend()
        clean_cost = economics.deepseek_usage_cost(clean['usage'], 'deepseek/deepseek-flash',
                                                   self.now)['cost_cny']
        self.assertAlmostEqual(result['estimated_spend_cny'], clean_cost, places=9)
        self.assertEqual(result['tokens'], 1_000_000)
        self.assertEqual(result['task_count'], 1)
        self.assertEqual(result['unknown_task_count'], 1)
        self.assertEqual(result['unknown_tokens'], 1_000_000)

    def test_retained_breakdown_that_covers_only_part_of_the_total_is_not_extrapolated(self):
        row = usage(input=100_000, total=100_000)
        self.add_ledger_entry('job-partial', usage(
            input=1_000_000, total=1_000_000,
            by_model=[with_model('deepseek/deepseek-flash', **row)]))
        result = self.spend()
        expected = economics.deepseek_usage_cost(row, 'deepseek/deepseek-flash', self.now)['cost_cny']
        self.assertAlmostEqual(result['estimated_spend_cny'], expected, places=9)
        self.assertEqual(result['tokens'], 100_000)
        self.assertEqual(result['unattributed_tokens'], 900_000)

    def test_ambiguous_ledger_entry_without_breakdown_is_excluded(self):
        self.add_ledger_entry('job-legacy-ledger', usage(input=1_000_000, total=1_000_000),
                              actual_models=['kimi-for-coding/k3', 'deepseek/deepseek-flash'])
        result = self.spend()
        self.assertIsNotNone(result)
        self.assertIsNone(result['estimated_spend_cny'])
        self.assertEqual(result['unknown_task_count'], 1)
        self.assertEqual(result['unknown_tokens'], 1_000_000)

    # -- cleaned ledger retention ----------------------------------------
    def test_cleaned_ledger_prices_retained_per_model_rows(self):
        deepseek = usage(input=2_000_000, output=500_000, total=2_500_000)
        kimi = usage(input=4_000_000, output=1_000_000, total=5_000_000)
        self.add_ledger_entry('job-cleaned', usage(
            input=6_000_000, output=1_500_000, total=7_500_000,
            by_model=[with_model('kimi-for-coding/k3', **kimi),
                      with_model('deepseek/deepseek-flash', **deepseek)]))
        result = self.spend()
        expected = economics.deepseek_usage_cost(deepseek, 'deepseek/deepseek-flash', self.now)['cost_cny']
        self.assertAlmostEqual(result['estimated_spend_cny'], expected, places=9)
        self.assertEqual(result['tokens'], 2_500_000)
        self.assertEqual(result['segment_count'], 1)
        self.assertEqual(result['task_count'], 1)

    def test_cleanup_ledger_retains_compact_per_model_rows(self):
        deepseek = usage(input=100, cache_read=20, output=10, total=130)
        kimi = usage(input=50, total=50)
        task = self.add_task(
            'job-retained', profile='senior-code',
            actual_models=['deepseek/deepseek-flash', 'kimi-for-coding/k3'],
            usage=usage(input=150, cache_read=20, output=10, total=180,
                        by_model=[with_model('deepseek/deepseek-flash', **deepseek),
                                  with_model('kimi-for-coding/k3', **kimi)]))
        entry = usage_ledger.record(task, 30, now=self.now)
        self.assertEqual(entry['usage']['total'], 180)
        rows = {row['model']: row for row in entry['usage']['by_model']}
        self.assertEqual(set(rows), {'deepseek/deepseek-flash', 'kimi-for-coding/k3'})
        self.assertEqual(rows['deepseek/deepseek-flash']['input'], 100)
        self.assertEqual(rows['deepseek/deepseek-flash']['total'], 130)
        self.assertEqual(sorted(rows['deepseek/deepseek-flash']),
                         ['cache_read', 'cache_write', 'complete', 'cost', 'input', 'model',
                          'output', 'reasoning', 'source', 'total'])
        read = usage_ledger.entries(now=self.now)[0]
        self.assertEqual(read['usage']['by_model'], entry['usage']['by_model'])

    def test_cleanup_ledger_drops_unvalidated_and_secret_fields(self):
        secret = 'sk-live-super-secret-value-1234567890'
        raw = usage(input=100, total=100)
        raw['prompt'] = 'private prompt body'
        raw['api_key'] = secret
        raw['by_model'] = [
            {'model': 'deepseek/deepseek-flash', 'input': 100, 'total': 100,
             'prompt': 'segment prompt', 'text': 'segment text', 'api_key': secret,
             'payload': {'authorization': secret}},
            {'model': '', 'input': 5, 'total': 5, 'prompt': 'nameless prompt'},
            {'model': 'x' * 500, 'input': 5, 'total': 5},
            {'input': 5, 'total': 5},
            'not-a-dict',
        ]
        task = self.add_task('job-secrets', usage=usage(total=100))
        with patch.object(task_activity, 'snapshot', return_value={'usage': raw}):
            usage_ledger.record(task, 30, now=self.now)
        read = usage_ledger.entries(now=self.now)[0]
        serialized = json.dumps(read)
        stored = (self.state / 'usage-ledger' / 'job-secrets.json').read_text()
        for leaked in ('private prompt body', 'segment prompt', 'segment text', secret):
            self.assertNotIn(leaked, serialized)
            self.assertNotIn(leaked, stored)
        self.assertNotIn('api_key', serialized)
        self.assertNotIn('payload', serialized)
        self.assertEqual([row['model'] for row in read['usage']['by_model']],
                         ['deepseek/deepseek-flash'])
        self.assertEqual(sorted(read['usage']['by_model'][0]),
                         ['cache_read', 'cache_write', 'complete', 'cost', 'input', 'model',
                          'output', 'reasoning', 'source', 'total'])

    # -- exclusions and uncertainty --------------------------------------
    def test_non_deepseek_evidence_is_never_charged_to_a_deepseek_profile(self):
        # Kimi-only actual models on a DeepSeek-configured profile must not be
        # priced as DeepSeek just because the profile names a DeepSeek model.
        self.add_task('job-kimi-task', profile='fallback',
                      actual_models=['kimi-for-coding/k3'],
                      usage=usage(input=1_000_000, total=1_000_000))
        self.assertIsNone(self.spend())
        # A retained breakdown with no DeepSeek row is skipped even with a DS profile.
        self.add_ledger_entry('job-kimi-ledger', usage(
            input=1_000_000, total=1_000_000,
            by_model=[with_model('kimi-for-coding/k3', input=1_000_000, total=1_000_000)]),
            profile='fallback', actual_models=['kimi-for-coding/k3'])
        self.assertIsNone(self.spend())
        # A profile-only non-DeepSeek model is excluded as well.
        self.add_task('job-profile-kimi', profile='senior-code', actual_models=[],
                      usage=usage(input=1_000_000, total=1_000_000))
        self.assertIsNone(self.spend())

    def test_legacy_profile_attribution_is_priced_but_flagged_uncertain(self):
        task = self.add_task('job-profile-ds', profile='fallback', actual_models=[],
                             usage=usage(input=1_000_000, total=1_000_000))
        result = self.spend()
        expected = economics.deepseek_usage_cost(task['usage'], 'deepseek/deepseek-flash',
                                                 self.now)['cost_cny']
        self.assertAlmostEqual(result['estimated_spend_cny'], expected, places=9)
        self.assertEqual(result['inferred_segment_count'], 1)
        self.assertFalse(result['peak_confident'])
        self.assertIn('peak', result['price_band_uncertainty'])

        # A legacy fallback with no actual model evidence stays unknown.
        common.update('job-profile-ds', fallback_used=True)
        unknown = self.spend()
        self.assertIsNone(unknown['estimated_spend_cny'])
        self.assertEqual(unknown['unknown_task_count'], 1)
        self.assertIn('fallback', unknown['unknown_tasks'][0]['reason'])

    def test_lookback_window_excludes_older_records(self):
        self.add_task('job-old', created_at=self.now - 26 * 3600,
                      finished_at=self.now - 25 * 3600,
                      actual_models=['deepseek/deepseek-flash'],
                      usage=usage(input=1_000_000, total=1_000_000))
        self.add_task('job-recent', actual_models=['deepseek/deepseek-flash'],
                      usage=usage(input=500_000, total=500_000))
        result = self.spend()
        self.assertEqual(result['task_count'], 1)
        self.assertEqual(result['tokens'], 500_000)


if __name__ == '__main__':
    unittest.main()
