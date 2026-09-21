import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import analytics
import common
import task_activity
import usage_ledger


class AnalyticsTests(unittest.TestCase):
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
            'profiles': {'normal': {'model': 'acme/model-a'}},
            'cleanup': {'usage_retention_days': 120},
        })

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.tmp.cleanup()

    def test_summary_combines_current_and_cleaned_usage_without_prompt_content(self):
        now = 2_000_000_000
        current = {'id': 'job-current', 'title': 'private title', 'objective': 'private prompt',
                   'status': 'running', 'tier': 'normal', 'profile': 'normal',
                   'created_at': now - 100, 'actual_models': ['acme/model-a'],
                   'fallback_used': False}
        cleaned = {'task_id': 'job-cleaned', 'title': 'old private title', 'status': 'completed',
                   'tier': 'deep', 'profile': 'normal', 'actual_models': ['acme/model-b'],
                   'fallback_used': True, 'created_at': now - 86400, 'finished_at': now - 86000,
                   'usage': {'total': 300, 'cost': 1.25}, 'expires_at': now + 1000,
                   'deleted_at': now - 100}
        common.write_json(self.state / 'tasks' / 'job-current.json', current)
        common.write_json(self.state / 'usage-ledger' / 'job-cleaned.json', cleaned)
        with patch.object(task_activity, 'usage_for_task', return_value={'total': 200, 'cost': .5}):
            result = analytics.summary(now=now)
        self.assertEqual(result['totals']['tasks'], 2)
        self.assertEqual(result['totals']['tokens'], 500)
        self.assertEqual(result['totals']['fallbacks'], 1)
        self.assertEqual(result['retention_days'], 120)
        encoded = json.dumps(result)
        self.assertNotIn('private prompt', encoded)
        self.assertNotIn('private title', encoded)
        self.assertEqual({row['name'] for row in result['by_model']}, {'acme/model-a', 'acme/model-b'})
        self.assertEqual(sum(row['tasks'] for row in result['recent']['hour']), 1)
        self.assertEqual(len(result['recent']['hour']), 12)
        self.assertEqual(len(result['recent']['day']), 24)

    def test_quota_history_strips_credential_identity_and_unknown_payload(self):
        common.write_json(self.state / 'quota-history.json', {
            'deepseek': {'_credential': 'secret-hash', 'samples': [{
                'time': 100, 'windows': {'five|300': {'remaining_percent': 50}},
                'balances': {'CNY': 8.5}, 'provider_payload': {'secret': 'never-return'},
            }]}
        })
        result = analytics.summary(now=200)
        encoded = json.dumps(result)
        self.assertNotIn('secret-hash', encoded)
        self.assertNotIn('never-return', encoded)
        self.assertEqual(result['quota_history']['deepseek'][0]['balances'], {'CNY': 8.5})


if __name__ == '__main__':
    unittest.main()
