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
        cfg = {'enabled': True, 'credential': 'bark-endpoint', 'group': 'Desk',
               'icon': 'https://cdn.example/worker-desk.png'}
        with patch.object(notifications.credentials, '_resolve_reference',
                          return_value='https://api.day.app/private-key/'), \
             patch.object(notifications.urllib.request.OpenerDirector, 'open', return_value=_Response()) as opened:
            result = notifications.send('Title', 'Body', config=cfg)
        self.assertTrue(result['sent'])
        self.assertNotIn('private-key', json.dumps(result))
        payload = json.loads(opened.call_args.args[0].data)
        self.assertEqual(payload['icon'], cfg['icon'])

    def test_notification_icon_is_https_and_can_be_disabled(self):
        with self.assertRaisesRegex(ValueError, 'HTTPS URL'):
            notifications.validate({'icon': 'http://example.test/icon.png'})
        self.assertEqual(notifications.validate({'icon': ''})['icon'], '')

    def test_multiple_clients_are_deduplicated_and_fail_independently(self):
        cfg = {'enabled': True,
               'credentials': ['bark-endpoint', 'bark-ipad-endpoint', 'bark-endpoint']}
        with patch.object(notifications.credentials, '_resolve_reference',
                          side_effect=['https://api.day.app/first/',
                                       notifications.credentials.CredentialError('unavailable')]), \
             patch.object(notifications.urllib.request.OpenerDirector, 'open',
                          return_value=_Response()) as opened:
            result = notifications.send('Title', 'Body', config=cfg)
        self.assertTrue(result['sent'])
        self.assertEqual(result['sent_count'], 1)
        self.assertEqual(result['failed_count'], 1)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual([item['credential'] for item in result['deliveries']],
                         ['bark-endpoint', 'bark-ipad-endpoint'])
        self.assertNotIn('api.day.app', json.dumps(result))

    def test_legacy_single_credential_normalizes_to_client_list(self):
        value = notifications.normalize({'credential': 'bark-old-endpoint'})
        self.assertEqual(value['credentials'], ['bark-old-endpoint'])
        self.assertEqual(value['credential'], 'bark-old-endpoint')
        with self.assertRaisesRegex(ValueError, '1-8'):
            notifications.validate({'credentials': []})
        with self.assertRaisesRegex(ValueError, 'non-empty strings'):
            notifications.validate({'credentials': ['bark-phone', None]})

    def test_transparent_exhaustion_and_recovery_are_silent(self):
        cfg = {'notifications': {'enabled': True}, 'profiles': {'test': {}}}
        with patch.object(notifications.quota, 'route', return_value=('fallback', 'ok')), \
             patch.object(notifications, 'send', return_value={'sent': True}) as send:
            for available in (True, False, False, True):
                notifications.observe_quota(cfg, {'kimi-for-coding': {
                    'state': 'ok', 'stale': False, 'available': available,
                    'windows': [{'valid': True, 'duration_minutes': 300,
                                 'remaining_percent': 0}]}})
        send.assert_not_called()

    def test_route_outage_dedup_and_rearm(self):
        cfg = {'notifications': {'enabled': True}, 'profiles': {'test': {}}}
        with patch.object(notifications.quota, 'route', return_value=(None, 'exhausted')) as route, \
             patch.object(notifications, 'send', return_value={'sent': True}) as send:
            notifications.observe_quota(cfg, {})
            notifications.observe_quota(cfg, {})
            self.assertEqual(send.call_count, 1)
            route.return_value = ('fallback', 'ok')
            notifications.observe_quota(cfg, {})
            self.assertEqual(send.call_count, 1)
            route.return_value = (None, 'exhausted')
            notifications.observe_quota(cfg, {})
            self.assertEqual(send.call_count, 2)

    def test_recharge_threshold_dedup_rearm_and_stale(self):
        cfg = {'notifications': {'enabled': True}}
        def snapshot(balance, stale=False):
            return {'deepseek': {'state': 'ok', 'stale': stale,
                'balances': [{'currency': 'CNY', 'remaining': balance}]}}
        with patch.object(notifications.quota, 'tier_guidance', return_value={}), \
             patch.object(notifications, 'send', return_value={'sent': True}) as send:
            notifications.observe_quota(cfg, snapshot(40))
            self.assertEqual(send.call_count, 0)
            notifications.observe_quota(cfg, snapshot(39))
            notifications.observe_quota(cfg, snapshot(30))
            self.assertEqual(send.call_count, 1)
            notifications.observe_quota(cfg, snapshot(80, True))
            notifications.observe_quota(cfg, snapshot(30))
            self.assertEqual(send.call_count, 1)
            notifications.observe_quota(cfg, snapshot(80))
            notifications.observe_quota(cfg, snapshot(39))
            self.assertEqual(send.call_count, 2)

    def test_balance_severity_and_kimi_window_exhaustion(self):
        cfg = {'notifications': {'enabled': True}}
        def sample(balance, percent):
            return {'deepseek': {'state': 'ok', 'stale': False,
                       'balances': [{'currency': 'CNY', 'remaining': balance}]},
                    'kimi-for-coding': {'state': 'ok', 'stale': False, 'available': True,
                       'windows': [{'valid': True, 'remaining_percent': percent, 'duration_minutes': 300}]}}
        with patch.object(notifications.quota, 'tier_guidance', return_value={}), \
             patch.object(notifications, 'send', return_value={'sent': True}) as send:
            for balance in [39, 29, 0, 0]:
                notifications.observe_quota(cfg, sample(balance, 0))
            self.assertEqual(send.call_count, 3)  # cash edges only; Kimi exhaustion is silent
            notifications.observe_quota(cfg, sample(50, 100))
            notifications.observe_quota(cfg, sample(0, 0))
            self.assertEqual(send.call_count, 4)

    def test_durable_conservation_confirmation_dedup_and_recovery(self):
        import economics
        snapshot = {'volcengine-agent-plan': {'state': 'ok', 'stale': False}}
        guidance = {'provider_conservation_level': {'volcengine-agent-plan': 2},
                    'runway_threshold_percent': 38, 'level2_runway_percent': 25}
        window = {'duration_minutes': 10080, 'resets_at': '2099-01-01T00:00:00Z'}
        with patch.object(notifications.quota, 'tier_guidance', return_value=guidance), \
             patch.object(economics, '_selected_window', return_value=window), \
             patch.object(economics, '_window_fit', return_value={'status': 'ok', 'pressure_runway': .1}) as fit, \
             patch.object(notifications.time, 'time', return_value=10000) as clock, \
             patch.object(notifications, 'send', return_value={'sent': True}) as send:
            observe = lambda: notifications._observe_long_conservation({}, {}, snapshot)
            observe()
            clock.return_value = 11799
            observe()
            send.assert_not_called()
            clock.return_value = 11800
            observe()
            observe()
            send.assert_called_once()
            self.assertIn('持续降载', send.call_args.args[0])
            fit.return_value = {'status': 'ok', 'pressure_runway': 1.0}
            observe()
            clock.return_value = 13600
            observe()  # sustained healthy state rearms without a recovery notification
            send.assert_called_once()
            fit.return_value = {'status': 'ok', 'pressure_runway': .1}
            observe()
            clock.return_value = 15400
            observe()
            self.assertEqual(send.call_count, 2)

    def test_short_stale_or_imminent_refill_never_alerts(self):
        import economics
        snapshot = {'kimi-for-coding': {'state': 'ok', 'stale': False}}
        with patch.object(notifications.quota, 'tier_guidance', return_value={
                'provider_conservation_level': {'kimi-for-coding': 2},
                'runway_threshold_percent': 38, 'level2_runway_percent': 25}), \
             patch.object(economics, '_selected_window', return_value={'duration_minutes': 300}) as window, \
             patch.object(economics, '_window_fit', return_value={'status': 'ok', 'pressure_runway': .1}), \
             patch.object(notifications.time, 'time', return_value=10000) as clock, \
             patch.object(notifications, 'send') as send:
            for instant in (10000, 20000):
                clock.return_value = instant
                notifications._observe_long_conservation({}, {}, snapshot)
            window.return_value = {'duration_minutes': 10080, 'resets_at': '1970-01-01T05:40:00Z'}
            notifications._observe_long_conservation({}, {}, snapshot)
            snapshot['kimi-for-coding']['stale'] = True
            window.return_value = {'duration_minutes': 10080}
            notifications._observe_long_conservation({}, {}, snapshot)
            send.assert_not_called()

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
