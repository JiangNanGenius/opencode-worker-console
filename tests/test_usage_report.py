"""Timestamped usage events: exact rolling windows that survive cleanup."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import analytics
import common
import task_activity
import usage_ledger


def usage(**overrides):
    value = {'input': 0, 'output': 0, 'reasoning': 0, 'cache_read': 0, 'cache_write': 0,
             'total': 0, 'cost': 0.0, 'source': 'saved', 'complete': True}
    value.update(overrides)
    return value


def event(message_id, at, model, **overrides):
    value = {'id': message_id, 'at': at, 'model': model, 'input': 0, 'output': 0,
             'reasoning': 0, 'cache_read': 0, 'cache_write': 0, 'total': 0, 'cost': None}
    value.update(overrides)
    return value


class UsageEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / 'state'
        self.config = Path(self.tmp.name) / 'config.json'
        common.write_json(self.config, {
            'profiles': {'ali-max': {'model': 'bailian-token-plan/qwen3.8-max'},
                         'fallback': {'model': 'deepseek/deepseek-flash'}},
        })
        self.patchers = [patch.object(common, 'STATE', self.state),
                         patch.object(common, 'CONFIG', self.config)]
        for item in self.patchers:
            item.start()
        common.init()
        self.now = time.time()

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.tmp.cleanup()

    def add_task(self, task_id, created, finished, status='completed', profile='fallback',
                 value=None, events=None):
        task = {'id': task_id, 'status': status, 'profile': profile, 'created_at': created,
                'finished_at': finished, 'usage': value or usage()}
        common.write_json(common.task_path(task_id), task)
        if events:
            usage_ledger.record_events(task_id, events, now=self.now)
        return task

    def window(self, report, hours):
        return next(item for item in report['windows'] if item['hours'] == hours)

    # -- event extraction -------------------------------------------------
    def test_events_deduplicate_by_message_id_and_normalize_timestamps(self):
        messages = [
            {'info': {'role': 'assistant', 'id': 'm1', 'providerID': 'bailian-token-plan',
                      'modelID': 'qwen3.8-max', 'time': {'created': self.now - 120,
                                                         'completed': (self.now - 60) * 1000},
                      'tokens': {'input': 10, 'output': 5, 'reasoning': 3,
                                 'cache': {'read': 7, 'write': 1}}, 'cost': 0.5,
                      'text': 'must never be copied'}},
            {'info': {'role': 'assistant', 'id': 'm1', 'providerID': 'bailian-token-plan',
                      'modelID': 'qwen3.8-max', 'time': {'completed': self.now * 1000},
                      'tokens': {'input': 10, 'output': 5, 'reasoning': 3}}},
            {'info': {'role': 'assistant', 'id': 'm2', 'providerID': 'deepseek',
                      'modelID': 'deepseek-flash', 'time': {'created': self.now - 30},
                      'tokens': {'input': 2, 'output': 1}}},
            {'info': {'role': 'user', 'id': 'u1', 'tokens': {'input': 999}}},
        ]
        events = task_activity.usage_events_from_messages(messages)
        self.assertEqual([item['id'] for item in events], ['m1', 'm2'])
        first = events[0]
        self.assertAlmostEqual(first['at'], self.now - 60, places=3)
        self.assertEqual(first['model'], 'bailian-token-plan/qwen3.8-max')
        self.assertEqual(first['input'], 10)
        self.assertEqual(first['output'], 5)
        self.assertEqual(first['reasoning'], 3)
        self.assertEqual(first['cache_read'], 7)
        self.assertEqual(first['cache_write'], 1)
        self.assertEqual(first['total'], 26)
        self.assertNotIn('text', json.dumps(events))
        second = events[1]
        self.assertAlmostEqual(second['at'], self.now - 30, places=3)

    # -- rolling windows --------------------------------------------------
    def test_long_running_task_counts_only_recent_event_deltas(self):
        self.add_task('job-long', self.now - 2 * 86400, self.now, status='running',
                      value=usage(input=10_000_000, total=10_000_000),
                      events=[event('m1', self.now - 3 * 3600, 'deepseek/deepseek-flash',
                                    input=5000, total=5000),
                              event('m2', self.now - 600, 'bailian-token-plan/qwen3.8-max',
                                    input=20000, output=1000, total=21000),
                              event('m3', self.now - 120, 'bailian-token-plan/qwen3.8-max',
                                    input=9000, total=9000)])
        report = analytics.usage_report(now=self.now, window_hours=(1, 24, 48))
        hour = self.window(report, 1)
        self.assertEqual(hour['event_tokens'], 30000)
        self.assertEqual(hour['coarse_tokens'], 0)
        self.assertEqual(hour['events'], 2)
        self.assertEqual(hour['event_tasks'], 1)
        self.assertEqual(hour['precision'], 'timestamped')
        self.assertNotIn(10_000_000, [hour['observed_tokens'], hour['event_tokens']])
        day = self.window(report, 24)
        self.assertEqual(day['event_tokens'], 35000)
        self.assertEqual(day['events'], 3)
        ali = next(item for item in hour['models']
                   if item['model'] == 'bailian-token-plan/qwen3.8-max')
        self.assertEqual(ali['event_tokens'], 30000)
        self.assertAlmostEqual(ali['event_credits'],
                               (29000 * 1200 + 1000 * 3600) / 1_000_000, places=6)
        self.assertIsNone(next(item for item in day['models']
                               if item['model'] == 'deepseek/deepseek-flash')['estimated_credits'])

    def test_completion_does_not_dump_lifetime_into_short_window(self):
        self.add_task('job-done', self.now - 2 * 86400, self.now,
                      value=usage(input=8_000_000, total=8_000_000),
                      events=[event('m1', self.now - 300, 'deepseek/deepseek-flash',
                                    input=1000, total=1000)])
        hour = self.window(analytics.usage_report(now=self.now, window_hours=(1,)), 1)
        self.assertEqual(hour['event_tokens'], 1000)
        self.assertEqual(hour['coarse_tokens'], 0)
        self.assertEqual(hour['observed_tokens'], 1000)

    def test_repeated_observation_does_not_double_count(self):
        events = [event('m1', self.now - 600, 'deepseek/deepseek-flash', input=500, total=500),
                  event('m2', self.now - 300, 'deepseek/deepseek-flash', input=700, total=700)]
        self.add_task('job-repeat', self.now - 3600, None, status='running',
                      value=usage(input=1200, total=1200), events=events)
        self.assertFalse(usage_ledger.record_events('job-repeat', events, now=self.now))
        self.assertFalse(usage_ledger.record_events('job-repeat', [events[0]], now=self.now))
        hour = self.window(analytics.usage_report(now=self.now, window_hours=(1,)), 1)
        self.assertEqual(hour['event_tokens'], 1200)
        self.assertEqual(hour['events'], 2)

    def test_cleanup_ledger_preserves_timestamped_events(self):
        task = self.add_task('job-cleaned', self.now - 2 * 86400, self.now,
                             value=usage(input=9_000_000, total=9_000_000),
                             events=[event('m1', self.now - 600, 'bailian-token-plan/qwen3.8-max',
                                           input=4000, output=100, total=4100)])
        entry = usage_ledger.record(task, 30, now=self.now)
        self.assertEqual(len(entry['usage_events']), 1)
        common.task_path('job-cleaned').unlink()
        usage_ledger.drop_events('job-cleaned')
        hour = self.window(analytics.usage_report(now=self.now, window_hours=(1,)), 1)
        self.assertEqual(hour['event_tokens'], 4100)
        self.assertEqual(hour['coarse_tokens'], 0)
        self.assertEqual(hour['event_tasks'], 1)

    def test_legacy_record_without_events_is_coarse_and_separate(self):
        self.add_task('job-legacy', self.now - 1800, self.now - 600,
                      value=usage(input=50_000, total=50_000))
        hour = self.window(analytics.usage_report(now=self.now, window_hours=(1,)), 1)
        self.assertEqual(hour['event_tokens'], 0)
        self.assertEqual(hour['coarse_tokens'], 50000)
        self.assertEqual(hour['coarse_tasks'], 1)
        self.assertEqual(hour['precision'], 'coarse')
        self.assertEqual(hour['event_credits'], 0)

    def test_mixed_records_report_precision_mixed(self):
        self.add_task('job-events', self.now - 1200, self.now - 600,
                      value=usage(input=100, total=100),
                      events=[event('m1', self.now - 900, 'deepseek/deepseek-flash',
                                    input=100, total=100)])
        self.add_task('job-coarse', self.now - 1200, self.now - 600,
                      value=usage(input=200, total=200))
        hour = self.window(analytics.usage_report(now=self.now, window_hours=(1,)), 1)
        self.assertEqual(hour['event_tokens'], 100)
        self.assertEqual(hour['coarse_tokens'], 200)
        self.assertEqual(hour['precision'], 'mixed')
        self.assertEqual(hour['event_tasks'], 1)
        self.assertEqual(hour['coarse_tasks'], 1)


if __name__ == '__main__':
    unittest.main()
