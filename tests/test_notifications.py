import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import notifications


class _Response:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def read(self, _limit): return b'{"code":200}'


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / 'state'
        self.patcher = patch.object(common, 'STATE', self.state)
        self.patcher.start()
        common.init()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_bark_send_never_returns_endpoint_or_response(self):
        cfg = {'enabled': True, 'credential': 'bark-endpoint', 'group': 'Desk'}
        with patch.object(notifications.credentials, '_resolve_reference',
                          return_value='https://api.day.app/private-key/'), \
             patch.object(notifications.urllib.request.OpenerDirector, 'open', return_value=_Response()):
            result = notifications.send('Title', 'Body', config=cfg)
        self.assertTrue(result['sent'])
        self.assertNotIn('private-key', json.dumps(result))

    def test_quota_events_seed_then_notify_only_on_transition(self):
        cfg = {'notifications': {'enabled': True, 'quota_transitions': True}}
        snapshot = {'ark': {'available': True}}
        with patch.object(notifications.quota, 'tier_guidance', side_effect=[
                 {'conservation_level': 0},
                 {'conservation_level': 2, 'combined_remaining_percent': 20},
             ]), patch.object(notifications, 'send', return_value={'sent': True}) as send:
            self.assertEqual(notifications.observe_quota(cfg, snapshot), [])
            notifications.observe_quota(cfg, snapshot)
        send.assert_called_once()
        self.assertIn('二级降载', send.call_args.args[0])

    def test_task_completion_is_explicit_opt_in(self):
        cfg = {'notifications': {'enabled': True}}
        with patch.object(notifications.common, 'config', return_value=cfg), \
             patch.object(notifications, 'send', return_value={'sent': True}) as send:
            self.assertFalse(notifications.task_completed({'id': 'ordinary'})['sent'])
            self.assertTrue(notifications.task_completed({
                'id': 'important', 'title': 'TestFlight processed',
                'notify_on_complete': True})['sent'])
        send.assert_called_once()
        self.assertIn('TestFlight processed', send.call_args.args[1])
