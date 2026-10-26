import copy
import hashlib
import io
import json
import sys
import unittest
import urllib.error
import urllib.parse
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import bailian_personal as personal
import bailian_personal_ak as personal_ak


NOW = 1793050000
KEY = 'test-inference-secret'
CONFIG = {'bailian_plan_edition': 'personal', 'bailian_personal_quota': {
    'enabled': True, 'keychain': {'service': 'workerdesk.bailian-personal-quota',
                                'account': 'default'}},
    'bailian_pool': {'enabled': True, 'slots': [
        {'id': 'personal', 'keychain': {'service': 'mock-inference', 'account': 'default'}}]}}
PAYLOAD = {'cookie': 'session=test-cookie-secret', 'sec_token': 'test-token-secret',
           'cornerstone': {'mock-account-id': 'private-account'},
           'instance_code': 'private-instance',
           'inference_key_sha256': hashlib.sha256(KEY.encode()).hexdigest(),
           'inference_identity': 'mock-provider-identity'}
SUBSCRIPTION = {'instanceCode': 'private-instance', 'specCode': 'pro', 'status': 'VALID',
                'startTime': 1793002126000, 'endTime': 1795708800000}
USAGE = {'per1MonthPercentage': 0.00448, 'per1MonthResetTime': 1795708800000}
ADDON = {'remainingCredits': 0, 'totalCredits': 0, 'activeCount': 0}


class PersonalQuotaTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(personal.time, 'time', return_value=NOW))
        self.config = self.stack.enter_context(patch.object(personal.common, 'config',
                                                           return_value=copy.deepcopy(CONFIG)))
        self.identity = self.stack.enter_context(patch.object(
            personal.common, 'credential_identity', return_value='mock-provider-identity'))
        self.keychain = self.stack.enter_context(patch.object(
            personal.keychain_secrets, 'get_bounded', return_value=json.dumps(PAYLOAD).encode()))
        self.load_key = self.stack.enter_context(patch.object(
            personal.bailian_pool, '_load_key', return_value=(KEY.encode(), 'g1_mock')))
        self.opener = self.stack.enter_context(patch.object(
            personal.urllib.request, 'build_opener'))
        self.opener.side_effect = AssertionError('Live network is forbidden in tests')

    def fetch(self, subscription=None, usage=None, addon=None):
        responses = [copy.deepcopy(SUBSCRIPTION if subscription is None else subscription),
                     copy.deepcopy(USAGE if usage is None else usage),
                     copy.deepcopy(ADDON if addon is None else addon)]
        with patch.object(personal, '_request', side_effect=responses) as request:
            result = personal.fetch()
        self.assertEqual([call.args[0] for call in request.call_args_list],
                         [personal.SUBSCRIPTION, personal.USAGE, personal.ADDON])
        return result

    def assertUnknown(self, value):
        self.assertEqual(value['state'], 'unknown')
        self.assertIsNone(value['available'])
        self.assertNotIn('sampled_at', value)
        self.assertEqual(value['windows'], [])
        self.assertNotEqual(value['state'], 'auth_error')

    def assertNoSecrets(self, value):
        raw = json.dumps(value)
        for secret in [KEY, PAYLOAD['cookie'], PAYLOAD['sec_token'], PAYLOAD['instance_code'],
                       PAYLOAD['inference_key_sha256'], 'mock-provider-identity',
                       'private-account', 'mock-account-id']:
            self.assertNotIn(secret, raw)

    def test_opt_in_is_required_before_credentials_or_network(self):
        for config in ({}, {'bailian_plan_edition': 'team'},
                       {'bailian_plan_edition': 'personal'},
                       dict(CONFIG, bailian_personal_quota={'enabled': False}),
                       dict(CONFIG, bailian_personal_quota={
                           'enabled': True, 'cookie': 'never-supported'})):
            with self.subTest(config=config):
                self.assertUnknown(personal.fetch(config))
        self.keychain.assert_not_called()
        self.load_key.assert_not_called()
        self.opener.assert_not_called()

    def test_keychain_unavailable_is_generic_unknown(self):
        self.keychain.side_effect = RuntimeError('secret-cookie private-account')
        result = personal.fetch()
        self.assertUnknown(result)
        self.assertNoSecrets(result)
        self.keychain.assert_called_once_with('workerdesk.bailian-personal-quota',
                                             'default', timeout=5)
        self.opener.assert_not_called()

    def test_monthly_fraction_is_not_percentage_or_fake_credits(self):
        result = self.fetch()
        self.assertEqual(result['source'], 'personal_console')
        self.assertEqual(result['plan_edition'], 'personal')
        self.assertEqual(result['sampled_at'], NOW)
        self.assertTrue(result['available'])
        self.assertEqual(len(result['windows']), 1)
        monthly = result['windows'][0]
        self.assertEqual(monthly['name'], 'Monthly')
        self.assertEqual(monthly['unit'], 'percent')
        self.assertAlmostEqual(monthly['remaining_percent'], 99.552)
        self.assertTrue(monthly['valid'])
        self.assertNotIn('remaining', monthly)
        self.assertNotIn('limit', monthly)
        self.assertEqual(monthly['duration_minutes'], (1795708800 - 1793002126) / 60)
        self.assertTrue(monthly['subscribed_at'].startswith('2026-'))
        self.assertNoSecrets(result)

    def test_only_real_zero_monthly_plus_known_empty_addons_is_exhausted(self):
        usage = dict(USAGE, per1MonthPercentage=1)
        for count in (0, 1, 3):
            result = self.fetch(usage=usage, addon=dict(ADDON, activeCount=count))
            self.assertFalse(result['available'])
            self.assertEqual(result['windows'][0]['remaining_percent'], 0)
            self.assertTrue(result['windows'][0]['valid'])
            self.assertEqual(result['addon']['active_count'], count)

    def test_positive_addon_is_metadata_until_monthly_is_exhausted(self):
        result = self.fetch(addon={'remainingCredits': 1, 'totalCredits': 100,
                                  'activeCount': 1})
        self.assertTrue(result['available'])
        self.assertEqual(len(result['windows']), 1)
        self.assertEqual(result['windows'][0]['name'], 'Monthly')
        self.assertTrue(result['windows'][0]['valid'])
        self.assertEqual(result['addon']['remaining'], 1)
        self.assertEqual(result['addon']['limit'], 100)

    def test_addon_positive_supersedes_monthly_exhaustion(self):
        result = self.fetch(usage=dict(USAGE, per1MonthPercentage=1), addon={
            'remainingCredits': 25, 'totalCredits': 100, 'activeCount': 1})
        self.assertTrue(result['available'])
        monthly, addon = result['windows']
        self.assertFalse(monthly['valid'])
        self.assertTrue(monthly['superseded_by_addon'])
        self.assertEqual(addon['remaining'], 25)
        self.assertEqual(addon['limit'], 100)
        self.assertEqual(addon['remaining_percent'], 25)

    def test_addon_unknown_never_turns_zero_monthly_into_inference_block(self):
        for addon in ({}, {'remainingCredits': 2, 'totalCredits': 1, 'activeCount': 1},
                      {'remainingCredits': True, 'totalCredits': 1, 'activeCount': 0}):
            result = self.fetch(usage=dict(USAGE, per1MonthPercentage=1), addon=addon)
            self.assertIsNone(result['available'])
            self.assertFalse(result['windows'][0]['valid'])
        with patch.object(personal, '_request', side_effect=[SUBSCRIPTION,
                dict(USAGE, per1MonthPercentage=1), personal._QuotaError(True)]):
            result = personal.fetch()
        self.assertEqual(result['state'], 'ok')
        self.assertIsNone(result['available'])
        self.assertFalse(result['windows'][0]['valid'])

    def test_addon_failure_does_not_discard_positive_monthly(self):
        with patch.object(personal, '_request', side_effect=[SUBSCRIPTION, USAGE,
                personal._QuotaError(True)]):
            result = personal.fetch()
        self.assertTrue(result['available'])
        self.assertTrue(result['windows'][0]['valid'])

    def test_malformed_ratios_and_timestamps_never_receive_sample_time(self):
        for ratio in (True, float('nan'), float('inf'), -0.1, 1.01, '0.1', None):
            with self.subTest(ratio=ratio):
                self.assertUnknown(self.fetch(usage=dict(USAGE, per1MonthPercentage=ratio)))
        for field, value in [('per1MonthResetTime', NOW * 1000),
                             ('per1MonthResetTime', True),
                             ('per1MonthResetTime', 1795808800000)]:
            self.assertUnknown(self.fetch(usage=dict(USAGE, **{field: value})))
        for subscription in (dict(SUBSCRIPTION, status='EXPIRED'),
                             dict(SUBSCRIPTION, endTime=NOW * 1000),
                             dict(SUBSCRIPTION, startTime=(NOW + 1) * 1000),
                             dict(SUBSCRIPTION, instanceCode='foreign-instance')):
            self.assertUnknown(self.fetch(subscription=subscription))

    def test_key_rotation_and_foreign_provider_binding_fail_before_network(self):
        for key, identity in [('rotated-key', 'mock-provider-identity'),
                              (KEY, 'foreign-provider')]:
            self.load_key.return_value = (key.encode(), 'g1_mock')
            self.identity.return_value = identity
            with patch.object(personal, '_request') as request:
                self.assertUnknown(personal.fetch())
                request.assert_not_called()
        self.opener.assert_not_called()

    def test_loader_contract_uses_bytes_not_slot_generation_for_key_digest(self):
        self.load_key.return_value = (KEY.encode(), 'generation-is-not-the-key')
        self.assertEqual(self.fetch()['state'], 'ok')
        self.load_key.assert_called_once()
        self.load_key.return_value = (b'foreign-key', 'generation-is-not-the-key')
        with patch.object(personal, '_request') as request:
            self.assertUnknown(personal.fetch())
            request.assert_not_called()

    def test_zero_or_multiple_configured_keys_are_rejected(self):
        for settings in (None, {'slots': []}, {'slots': [{}, {}]}):
            with patch.object(personal.bailian_pool, 'pool_settings', return_value=settings), \
                    patch.object(personal, '_request') as request:
                self.assertUnknown(personal.fetch())
                request.assert_not_called()
        self.load_key.assert_not_called()

    def test_identity_uses_only_reference_metadata(self):
        result = personal.credential_identity(CONFIG)
        expected = hashlib.sha256(('personal\0' +
            personal.bailian_pool.pool_identity(CONFIG)).encode()).hexdigest()
        self.assertEqual(result, expected)
        self.config.assert_not_called()
        self.identity.assert_not_called()
        self.keychain.assert_not_called()
        self.load_key.assert_not_called()
        self.opener.assert_not_called()

    def test_console_auth_failure_does_not_become_inference_auth_failure(self):
        with patch.object(personal, '_request', side_effect=personal._QuotaError(True)):
            result = personal.fetch()
        self.assertUnknown(result)
        self.assertEqual(result['live_error']['kind'], 'quota_auth')
        self.assertNoSecrets(result)

    def ak_config(self):
        return dict(CONFIG, bailian_personal_quota={'enabled': True, 'auth': 'access_key',
            'subscription_instance_hash': hashlib.sha256(
                PAYLOAD['instance_code'].encode()).hexdigest(),
            'inference_key_sha256': PAYLOAD['inference_key_sha256']})

    def test_account_access_key_path_uses_only_local_inference_binding(self):
        config = self.ak_config()
        with patch.object(personal_ak, 'fetch_payloads',
                          return_value=(SUBSCRIPTION, USAGE, ADDON)) as query:
            result = personal.fetch(config)
        self.assertEqual(result['state'], 'ok')
        self.assertEqual(result['auth_method'], 'access_key_cli_token')
        self.assertEqual(result['source'], 'personal_console')
        self.assertTrue(result['available'])
        query.assert_called_once_with(config)
        self.keychain.assert_not_called()
        self.assertNoSecrets(result)
        self.assertNotIn(config['bailian_personal_quota']['subscription_instance_hash'],
                         json.dumps(result))

    def test_access_key_is_default_for_new_block_but_browser_can_be_explicit(self):
        config = self.ak_config()
        del config['bailian_personal_quota']['auth']
        with patch.object(personal_ak, 'fetch_payloads',
                          return_value=(SUBSCRIPTION, USAGE, ADDON)):
            self.assertTrue(personal.fetch(config)['available'])
        config = copy.deepcopy(CONFIG)
        config['bailian_personal_quota']['auth'] = 'browser'
        with patch.object(personal, '_request', side_effect=[SUBSCRIPTION, USAGE, ADDON]):
            self.assertTrue(personal.fetch(config)['available'])

    def test_access_key_binding_rejects_wrong_key_and_foreign_subscription(self):
        config = self.ak_config()
        self.load_key.return_value = (b'foreign-inference-key', 'g1_mock')
        with patch.object(personal_ak, 'fetch_payloads') as query:
            self.assertUnknown(personal.fetch(config))
            query.assert_not_called()
        self.load_key.return_value = (KEY.encode(), 'g1_mock')
        with patch.object(personal_ak, 'fetch_payloads', return_value=(
                dict(SUBSCRIPTION, instanceCode='foreign-subscription'), USAGE, ADDON)):
            self.assertUnknown(personal.fetch(config))
        config['bailian_personal_quota']['subscription_instance_hash'] = 'bad-binding'
        with patch.object(personal_ak, 'fetch_payloads') as query:
            self.assertUnknown(personal.fetch(config))
            query.assert_not_called()
        self.keychain.assert_not_called()

    def test_access_key_session_expiry_stays_unknown_without_inference_auth_block(self):
        with patch.object(personal_ak, 'fetch_payloads', side_effect=personal_ak.QuotaError(True)):
            result = personal.fetch(self.ak_config())
        self.assertUnknown(result)
        self.assertEqual(result['live_error']['kind'], 'quota_auth')
        self.assertNoSecrets(result)

    def sequential_config(self):
        config = copy.deepcopy(self.ak_config())
        config['bailian_plan_edition'] = 'sequential'
        config['bailian_pool']['slots'] = [
            {'id': 'primary', 'keychain': {'service': 'mock-team-key', 'account': 'default'}},
            {'id': 'secondary', 'keychain': {'service': 'mock-personal-key', 'account': 'default'}}]
        config['bailian_personal_quota']['slot_id'] = 'secondary'
        return config

    def test_sequential_selects_personal_slot_without_reading_team_key(self):
        config = self.sequential_config()
        self.load_key.side_effect = lambda slot: (KEY.encode(), 'g1_mock') if \
            slot['id'] == 'secondary' else (b'team-inference-key', 'g1_team')
        with patch.object(personal_ak, 'fetch_payloads',
                          return_value=(SUBSCRIPTION, USAGE, ADDON)):
            result = personal.fetch(config)
        self.assertEqual(result['state'], 'ok')
        self.assertEqual(result['plan_edition'], 'personal')
        self.assertTrue(result['available'])
        self.assertEqual(self.load_key.call_count, 1)
        self.assertEqual(self.load_key.call_args.args[0]['id'], 'secondary')
        self.keychain.assert_not_called()

    def test_multiple_slots_require_explicit_existing_slot_id(self):
        for slot_id in (None, '', 'missing', 1, True):
            config = self.sequential_config()
            if slot_id is None:
                del config['bailian_personal_quota']['slot_id']
            else:
                config['bailian_personal_quota']['slot_id'] = slot_id
            with patch.object(personal_ak, 'fetch_payloads') as query:
                self.assertUnknown(personal.fetch(config))
                self.assertIsNone(personal.credential_identity(config))
                query.assert_not_called()
        self.load_key.assert_not_called()
        self.keychain.assert_not_called()

    def test_selecting_team_slot_cannot_reuse_personal_key_binding(self):
        config = self.sequential_config()
        config['bailian_personal_quota']['slot_id'] = 'primary'
        self.load_key.return_value = (b'team-inference-key', 'g1_team')
        with patch.object(personal_ak, 'fetch_payloads') as query:
            self.assertUnknown(personal.fetch(config))
            query.assert_not_called()
        self.assertEqual(self.load_key.call_args.args[0]['id'], 'primary')

    def test_personal_identity_ignores_team_slot_changes_without_reading_secrets(self):
        config = self.sequential_config()
        before = copy.deepcopy(config)
        identity = personal.credential_identity(config)
        self.assertIsNotNone(identity)
        self.assertEqual(config, before)
        config['bailian_pool']['slots'][0]['generation'] = 'rotated-team-generation'
        config['bailian_pool']['slots'][0]['keychain']['service'] = 'changed-team-service'
        self.assertEqual(personal.credential_identity(config), identity)
        config['bailian_plan_edition'] = 'personal'
        self.assertEqual(personal.credential_identity(config), identity)
        self.load_key.assert_not_called()
        self.keychain.assert_not_called()
        self.identity.assert_not_called()
        self.config.assert_not_called()
        self.opener.assert_not_called()

    def test_personal_identity_changes_for_selected_slot_or_account_binding(self):
        original = self.sequential_config()
        identity = personal.credential_identity(original)
        for field in ('subscription_instance_hash', 'inference_key_sha256'):
            config = copy.deepcopy(original)
            config['bailian_personal_quota'][field] = 'a' * 64
            self.assertNotEqual(personal.credential_identity(config), identity)
            config['bailian_personal_quota'][field] = 'invalid-hash'
            self.assertIsNone(personal.credential_identity(config))
        config = copy.deepcopy(original)
        config['bailian_pool']['slots'][1]['generation'] = 'new-personal-key'
        self.assertNotEqual(personal.credential_identity(config), identity)
        self.load_key.assert_not_called()
        self.keychain.assert_not_called()

    def test_browser_auth_still_supports_explicit_personal_slot(self):
        config = self.sequential_config()
        config['bailian_personal_quota'] = dict(CONFIG['bailian_personal_quota'],
                                               auth='browser', slot_id='secondary')
        with patch.object(personal, '_request', side_effect=[SUBSCRIPTION, USAGE, ADDON]):
            self.assertTrue(personal.fetch(config)['available'])
        self.assertEqual(self.load_key.call_args.args[0]['id'], 'secondary')

    def response(self, body, status=200):
        response = Mock()
        response.status = status
        response.read.return_value = body
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        self.opener.side_effect = None
        self.opener.return_value.open.return_value = response
        return response

    def test_transport_is_fixed_read_only_bounded_and_envelope_checked(self):
        envelope = {'code': 200, 'successResponse': True,
                    'data': {'DataV2': {'data': {'success': True, 'code': 'SUCCESS',
                                               'data': USAGE}}}}
        response = self.response(json.dumps(envelope).encode())
        self.assertEqual(personal._request(personal.USAGE, PAYLOAD), USAGE)
        self.opener.assert_called_once_with(personal.common.NoRedirect)
        call = self.opener.return_value.open.call_args
        self.assertEqual(call.kwargs, {'timeout': 15})
        request = call.args[0]
        self.assertEqual(request.method, 'POST')
        self.assertEqual(request.get_header('Referer'), 'https://bailian.console.aliyun.com/')
        self.assertEqual(request.get_header('Cookie'), PAYLOAD['cookie'])
        parsed = urllib.parse.urlsplit(request.full_url)
        self.assertEqual(parsed.scheme, 'https')
        self.assertEqual(parsed.netloc, 'bailian-cs.console.aliyun.com')
        self.assertEqual(urllib.parse.parse_qs(parsed.query)['api'], [personal.USAGE])
        form = urllib.parse.parse_qs(request.data.decode())
        self.assertEqual(form['region'], ['cn-beijing'])
        self.assertEqual(form['sec_token'], [PAYLOAD['sec_token']])
        self.assertEqual(json.loads(form['params'][0]), {'Api': personal.USAGE, 'V': '1.0',
                         'Data': {'cornerstoneParam': PAYLOAD['cornerstone']}})
        response.read.assert_called_once_with(1024 * 1024 + 1)
        with self.assertRaises(personal._QuotaError):
            personal._request('https://foreign.example/read', PAYLOAD)

    def test_transport_redirect_timeout_bad_schema_and_login_are_generic(self):
        for error in (TimeoutError(PAYLOAD['cookie']),
                      urllib.error.HTTPError('https://secret', 302, PAYLOAD['sec_token'], {},
                                             io.BytesIO(b'private-account')),
                      urllib.error.HTTPError('https://secret', 401, PAYLOAD['sec_token'], {},
                                             io.BytesIO(b'private-account'))):
            self.opener.side_effect = None
            self.opener.return_value.open.side_effect = error
            with self.assertRaises(personal._QuotaError) as raised:
                personal._request(personal.USAGE, PAYLOAD)
            self.assertEqual(str(raised.exception), personal._ERROR)
        self.opener.return_value.open.side_effect = None
        for body, status in [(b'<html>Login secret-cookie</html>', 200),
                             (b'{}', 200), (b'x' * (1024 * 1024 + 1), 200),
                             (b'{}', 403), (b'{"code":200,"successResponse":true}', 200),
                             (json.dumps({'code': 200, 'successResponse': True,
                                'data': {'DataV2': {'data': {'success': False,
                                  'code': 'SUCCESS', 'data': USAGE}}}}).encode(), 200)]:
            self.response(body, status)
            with self.assertRaises(personal._QuotaError) as raised:
                personal._request(personal.USAGE, PAYLOAD)
            self.assertEqual(str(raised.exception), personal._ERROR)


if __name__ == '__main__':
    unittest.main()
