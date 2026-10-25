"""Selected statistics period excludes older lifetime usage and model splits."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import analytics
import console


class StatsPeriodTests(unittest.TestCase):
    def setUp(self):
        self.now = 2_000_000_000

    def record(self, task_id, age, total=100):
        return {'task_id': task_id, 'created_at': self.now - age * 86400,
                'finished_at': self.now - age * 86400 + 1,
                'usage': {'total': total}, 'status': 'completed', 'source': 'ledger'}

    def test_last_month_excludes_old_retained_tasks(self):
        records = [self.record('old', 31), self.record('recent', 29), self.record('future', -1)]
        with patch.object(analytics.usage_ledger, 'events_for_task', return_value=[]):
            result = analytics._period_records(records, self.now, 30)
        self.assertEqual([row['task_id'] for row in result], ['recent'])
        self.assertEqual(result[0]['period_precision'], 'coarse')
        self.assertIs(analytics._period_records(records, self.now, None), records)

    def test_cross_month_task_uses_only_recent_response_tokens(self):
        record = self.record('long', 40, total=900)
        record.update(status='running', finished_at=None)
        events = [{'at': self.now - 35 * 86400, 'model': 'old/model', 'total': 700},
                  {'at': self.now - 86400, 'model': 'current/model', 'total': 200}]
        with patch.object(analytics.usage_ledger, 'events_for_task', return_value=events):
            result = analytics._period_records([record], self.now, 30)
        self.assertEqual(result[0]['usage']['total'], 200)
        self.assertEqual(result[0]['usage']['by_model'], [{'model': 'current/model', 'total': 200}])
        self.assertEqual(result[0]['period_precision'], 'timestamped')

    def test_timestamped_old_responses_never_fall_back_to_lifetime(self):
        record = self.record('recent', 1, total=900)
        with patch.object(analytics.usage_ledger, 'events_for_task', return_value=[
                {'at': self.now - 35 * 86400, 'total': 900, 'model': 'old/model'}]):
            result = analytics._period_records([record], self.now, 30)
        self.assertIsNone(result[0]['usage']['total'])
        self.assertEqual(result[0]['usage']['by_model'], [])

    def test_invalid_period_is_rejected_before_state_access(self):
        for value in ('365', 1, True, [], {}):
            with self.assertRaises(ValueError):
                analytics.summary(now=self.now, days=value)

    def test_http_statistics_route_parses_its_own_period_query(self):
        for url, expected in (('/console-api/stats', '30'),
                              ('/console-api/stats?days=all', 'all')):
            handler = object.__new__(console.Handler)
            handler.path = url
            handler.reply = lambda *args, **kwargs: None
            with patch.object(analytics, 'summary', return_value={}) as summary:
                handler.console_get('/console-api/stats')
            summary.assert_called_once_with(days=expected)


if __name__ == '__main__':
    unittest.main()
