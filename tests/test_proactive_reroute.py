import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import quota
import worker


class ProactiveRerouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / 'state'
        self.config_path = Path(self.tmp.name) / 'config.json'
        self.cfg = {
            'proactive_long_task_reroute': True, 'proactive_reroute_after_seconds': 900,
            'profiles': {
                'senior-code': {'model': 'ark/evolving'},
                'ark-auto': {'model': 'ark/auto'},
                'fallback': {'model': 'deepseek/flash'},
            },
            'routing_policy': {
                'fast': [[{'profile': 'ark-auto', 'weight': 1}],
                         [{'profile': 'fallback', 'weight': 1}]],
                'background': [[{'profile': 'senior-code', 'weight': 1}],
                               [{'profile': 'ark-auto', 'weight': 1}],
                               [{'profile': 'fallback', 'weight': 1}]],
            },
            'quota_spillover': {'enabled': True, 'profile': 'fallback',
                                'tiers': ['fast', 'background'], 'max_share_percent': 30,
                                'level2_runway_percent': 25},
        }
        self.config_path.write_text(json.dumps(self.cfg))
        self.patchers = [patch.object(common, 'STATE', self.state),
                         patch.object(worker, 'STATE', self.state),
                         patch.object(common, 'CONFIG', self.config_path),
                         patch.object(worker, 'config', return_value=self.cfg),
                         patch.object(worker, 'credential_identity', return_value='identity')]
        for item in self.patchers: item.start()
        common.init()
        common.write_json(common.task_path('job-long'), {
            'id': 'job-long', 'title': 'Long task', 'status': 'running',
            'requested_profile': 'auto', 'tier': 'normal', 'profile': 'senior-code',
            'started_at': time.time() - 1000, 'directory': str(Path(self.tmp.name)),
            'session_id': 'ses_long', 'message_id': 'msg_old',
            'dispatch_attempted_at': time.time() - 1000,
        })
        common.write_json(self.state / 'quota.json', {})

    def tearDown(self):
        for item in reversed(self.patchers): item.stop()
        self.tmp.cleanup()

    def test_level2_queues_same_session_fast_route_without_aborting(self):
        with patch.object(quota, 'tier_guidance', return_value={'conservation_level': 2}), \
             patch.object(quota, 'route', return_value=('ark-auto', 'quota_conservation_level2')), \
             patch.object(worker, 'call', return_value={}) as call:
            changed = worker.proactive_reroute_if_needed(common.task('job-long'), {'type': 'busy'})
        self.assertTrue(changed)
        updated = common.task('job-long')
        self.assertEqual(updated['session_id'], 'ses_long')
        self.assertEqual(updated['profile'], 'ark-auto')
        self.assertEqual(updated['proactive_reroute_levels'], [2])
        self.assertTrue(updated['route_history'][-1]['queued_boundary_switch'])
        self.assertEqual(call.call_args.args[1], '/prompt_async')
        self.assertIn('Do not repeat completed', call.call_args.args[3]['parts'][0]['text'])

    def test_deep_and_explicit_tasks_never_move(self):
        for fields in ({'tier': 'deep'}, {'requested_profile': 'senior-code'}):
            common.update('job-long', **fields)
            with patch.object(worker, 'call') as call:
                self.assertFalse(worker.proactive_reroute_if_needed(
                    common.task('job-long'), {'type': 'busy'}))
            call.assert_not_called()

