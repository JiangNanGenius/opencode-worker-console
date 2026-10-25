import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import kimi_account as account


class KimiAccountTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(account, '_web_load', return_value=None)
        guard.start()
        self.addCleanup(guard.stop)

    def test_official_schema_all_four_windows_and_zero(self):
        value = account.normalize({'usages': {
            'limit5h': {'usedRatio': 0}, 'limit7d': {'usedRatio': .3},
            'monthTotal': {'usedRatio': 1, 'resetAt': '2026-11-19T04:00:00Z'},
            'monthCode': {'usedRatio': .6}}})
        self.assertEqual([x['kind'] for x in value['windows']],
                         ['short', 'weekly', 'month_total', 'month_code'])
        self.assertEqual(value['windows'][0]['remaining_percent'], 100)
        self.assertEqual(value['windows'][2]['remaining'], 0)
        self.assertFalse(value['available'])
        self.assertEqual(value['weekly_applicability'], 'applicable')
        self.assertIsNone(value['windows'][2]['duration_minutes'])

    def test_new_plan_missing_weekly_is_not_applicable_only_with_total(self):
        value = account.normalize({'usages': {'limit5h': {'usedRatio': .2},
                                             'monthTotal': {'usedRatio': .7}}})
        self.assertEqual(value['weekly_applicability'], 'not_applicable')
        self.assertEqual(len(value['windows']), 2)
        self.assertEqual(account.normalize({'usages': {}})['weekly_applicability'], 'unknown')
        self.assertIsNone(account.normalize({'usages': {}})['available'])

    def test_invalid_ratios_do_not_become_empty_or_applicable(self):
        for ratio in [True, '0.2', -1, 2, float('nan'), float('inf'), None]:
            result = account.normalize({'usages': {'limit7d': {'usedRatio': ratio},
                                                    'monthTotal': {'usedRatio': .1}}})
            self.assertEqual(result['weekly_applicability'], 'unknown')
            self.assertEqual(len(result['windows']), 1)

    def test_invalid_schema_fails_closed(self):
        for value in [None, [], {}, {'usages': []}]:
            with self.assertRaises(account.AccountError):
                account.normalize(value)

    def test_expired_and_malformed_timestamp(self):
        result = account.normalize({'usages': {'monthTotal': {
            'usedRatio': .2, 'resetAt': '2020-01-01T00:00:00Z'}}})
        self.assertEqual(result['windows'][0]['resets_at'], '2020-01-01T00:00:00Z')
        self.assertIsNone(account._timestamp('2026-10-24T04:00:00'))
        self.assertIsNone(account._timestamp('secret-token'))

    def test_login_allowlist_and_redaction(self):
        value = account._login_result({'status': 'pending', 'user_code': 'ABCD-1234',
            'verification_uri': 'https://www.kimi.com/code/authorize_device?access_token=secret',
            'verification_uri_complete': 'https://evil.test/secret', 'access_token': 'SECRET',
            'refresh_token': 'SECRET2', 'device_code': 'SECRET3', 'interval': 5})
        self.assertEqual(value['verification_url'], 'https://www.kimi.com/code/authorize_device')
        self.assertEqual(value['user_code'], 'ABCD-1234')
        self.assertNotIn('SECRET', json.dumps(value))
        self.assertNotIn('secret', json.dumps(value))
        for url in ['https://evil.test/', 'http://www.kimi.com/', 'https://user@www.kimi.com/']:
            self.assertNotIn('verification_url', account._login_result({'verification_uri': url}))

    def test_http_loopback_only(self):
        for origin in ['https://127.0.0.1:1', 'http://localhost:1', 'http://evil.test',
                       'http://127.0.0.1:2/x', 'http://user@127.0.0.1:2']:
            with self.assertRaises(account.AccountError):
                account._http(origin, '/api/v1/auth')
        with self.assertRaises(account.AccountError):
            account._http('http://127.0.0.1:12', '/api/v1/sessions')

    def test_private_token_rejects_world_readable_and_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'token'
            path.write_text('secret')
            path.chmod(0o644)
            with self.assertRaises(account.AccountError):
                account._private(path)
            path.chmod(0o600)
            self.assertEqual(account._private(path), 'secret')
            link = Path(td) / 'link'
            link.symlink_to(path)
            with self.assertRaises(account.AccountError):
                account._private(link)

    def test_in_band_error_never_reports_success_or_raw_message(self):
        with patch.object(account, '_service', return_value=('http://127.0.0.1:1', 123)), \
             patch.object(account, '_http', return_value={'code': 0, 'data': {
                 'kind': 'error', 'status': 401, 'message': 'SECRET'}}):
            result = account.fetch()
        self.assertEqual(result['state'], 'unavailable')
        self.assertEqual(result['error'], 'account_auth_required')
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertIsNone(result['available'])

    def test_account_and_inference_binding_requires_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(account.common, 'STATE', Path(td)), \
             patch.object(account, '_account_id', return_value=('account-hash', {})), \
             patch.object(account, '_inference_identity', return_value={}), \
             patch.object(account.common, 'credential_identity', return_value='inference-hash'):
            self.assertEqual(account._binding('account-hash')['state'], 'unverified')
            with self.assertRaises(ValueError):
                account.bind_current_account()
            self.assertEqual(account.bind_current_account(True)['state'], 'verified')
            self.assertEqual(account._binding('other-account')['state'], 'mismatch')
            self.assertEqual((Path(td) / 'kimi-account-binding.json').stat().st_mode & 0o777, 0o600)
            with patch.object(account.common, 'credential_identity', return_value='rotated'):
                self.assertEqual(account._binding('account-hash')['state'], 'mismatch')

    def test_fetch_filters_all_account_contact_and_secrets(self):
        responses = [{'kind': 'ok', 'quota': {'usages': {'monthTotal': {'usedRatio': .5}},
                                               'extraUsage': {'token': 'SECRET'}}},
                     {'kind': 'ok', 'userInfo': {'userId': 'private-id', 'nickname': 'Test',
                         'userLevelName': 'Max', 'email': 'private@email', 'phone': 'private-phone',
                         'access_token': 'SECRET'}}]
        with patch.object(account, '_call', side_effect=responses), \
             patch.object(account, '_binding', return_value={'state': 'unverified'}):
            result = account.fetch()
        self.assertEqual(result['account_binding']['state'], 'unverified')
        self.assertEqual(result['account'], {'nickname': 'Test', 'userLevelName': 'Max'})
        for text in ['SECRET', 'private-id', 'private@email', 'private-phone']:
            self.assertNotIn(text, json.dumps(result))

    def test_service_rejects_auth_disabled(self):
        with patch.object(account, '_instances', return_value=[('http://127.0.0.1:1', 123)]), \
             patch.object(account, '_http', return_value={'code': 0, 'data': {'dangerous_bypass_auth': True}}):
            self.assertFalse(account.ready())

    def test_status_is_only_auth_metadata(self):
        with patch.object(account, '_call', return_value={'managed_provider': {
            'status': 'authenticated', 'access_token': 'SECRET'}}), \
             patch.object(account, '_service', return_value=('http://127.0.0.1:58627', 123)), \
             patch.object(account, '_http', return_value={'code': 0, 'data': {'server_version': '2.3.1'}}):
            result = account.status()
        self.assertTrue(result['authenticated'])
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(result['service_info']['version'], '2.3.1')

    def test_token_embedded_newline_is_rejected_before_request(self):
        for token in ['abc\nsecret', 'abc\rsecret']:
            with patch.object(account, '_private', return_value=token):
                with self.assertRaisesRegex(account.AccountError, 'invalid_service_token'):
                    account._http('http://127.0.0.1:58627', '/api/v1/auth')

    def test_minimal_path_launch_uses_absolute_node(self):
        with patch.dict(os.environ, {'PATH': '/usr/bin:/bin'}), \
             patch.object(account.shutil, 'which', return_value=None), \
             patch.object(Path, 'is_file', return_value=True), \
             patch.object(account.os, 'access', return_value=True):
            command = account._launch_command(Path('/runtime/kimi'))
        self.assertTrue(Path(command[0]).is_absolute())
        self.assertEqual(command[1], '/runtime/kimi')

    def test_stop_does_not_signal_unowned_or_reused_pid(self):
        with patch.object(account.common, 'read_json', return_value={'pid': 123, 'port': 58627}), \
             patch.object(account, '_instances', return_value=[]), \
             patch.object(account.os, 'kill') as kill:
            self.assertEqual(account.stop()['state'], 'not_owned')
            kill.assert_not_called()

    def test_account_identity_is_opaque_and_key_rotation_invalidates_it(self):
        with patch.object(account, '_account_id', return_value=('account-hash', {})), \
             patch.object(account.common, 'credential_identity', return_value='inference-hash'):
            first = account.credential_identity()
        self.assertEqual(len(first), 64)
        self.assertNotIn('account-hash', first)
        with patch.object(account, '_account_id', return_value=('account-hash', {})), \
             patch.object(account.common, 'credential_identity', return_value='rotated'):
            self.assertNotEqual(first, account.credential_identity())

    def test_official_account_identity_match_and_mismatch(self):
        with patch.object(account.common, 'credential_identity', return_value='key-hash'), \
             patch.object(account, '_inference_identity', return_value={'userId': 'same'}):
            self.assertEqual(account._binding('hash', {'userId': 'same'}),
                             {'state': 'verified', 'method': 'official_identity_match'})
            self.assertEqual(account._binding('hash', {'userId': 'different'})['state'], 'mismatch')
        with patch.object(account.common, 'credential_identity', return_value='key-hash'), \
             patch.object(account, '_inference_identity', return_value={'globalId': 'same'}):
            self.assertEqual(account._binding('hash', {'globalId': 'same'})['state'], 'verified')

    def test_missing_identity_is_never_equal_to_missing(self):
        with patch.object(account.common, 'credential_identity', return_value='key-hash'), \
             patch.object(account.common, 'read_json', return_value={}):
            for inference, oauth in [({}, {}), ({'userId': ''}, {'userId': ''}),
                                     ({'userId': 'id'}, {}), ({}, {'userId': 'id'})]:
                with patch.object(account, '_inference_identity', return_value=inference):
                    self.assertEqual(account._binding('hash', oauth)['state'], 'unverified')

    def test_ownership_endpoint_401_and_invalid_schema_are_unverified(self):
        from urllib.error import HTTPError
        with patch.object(account.common, 'auth_key', return_value='SECRET'), \
             patch.object(account.urllib.request, 'build_opener') as opener:
            opener.return_value.open.side_effect = HTTPError('https://api.kimi.com/coding/v1/me',
                                                            401, 'SECRET', {}, None)
            self.assertEqual(account._inference_identity(), {})

    def test_cli_unavailable_still_returns_verified_positive_web_total(self):
        web = {'state': 'ok', 'source': account.WEB_SOURCE, 'sampled_at': 123,
               'account_binding': {'state': 'verified', 'method': 'official_identity_match'},
               'windows': [account.normalize_web_total({
                   'subscriptionBalance': {'amountUsedRatio': .3327}})]}
        with patch.object(account, '_call', side_effect=account.AccountError('account_service_unavailable')), \
             patch.object(account, 'fetch_web_total', return_value=web):
            result = account.fetch()
        self.assertEqual(result['state'], 'ok')
        self.assertFalse(result['stale'])
        self.assertEqual([w['kind'] for w in result['windows']], ['month_total'])
        self.assertEqual(result['windows'][0]['remaining_percent'], 66.73)
        self.assertEqual(result['account_binding']['state'], 'verified')
        self.assertEqual(result['weekly_applicability'], 'unknown')
        self.assertEqual(result['oauth_account']['state'], 'unavailable')
        self.assertNotIn('error', result)

    def test_cli_unavailable_and_web_total_zero_is_not_unknown(self):
        web = {'state': 'ok', 'source': account.WEB_SOURCE, 'sampled_at': 123,
               'account_binding': {'state': 'verified', 'method': 'official_identity_match'},
               'windows': [account.normalize_web_total({
                   'subscriptionBalance': {'amountUsedRatio': 1}})]}
        with patch.object(account, '_call', side_effect=account.AccountError('account_auth_required')), \
             patch.object(account, 'fetch_web_total', return_value=web):
            result = account.fetch()
        self.assertEqual(result['state'], 'ok')
        self.assertEqual(result['windows'][0]['remaining'], 0)
        self.assertFalse(result['available'])
        self.assertEqual(result['oauth_account']['error'], 'account_auth_required')
        self.assertEqual(result['weekly_applicability'], 'unknown')


class KimiWebAccountTests(unittest.TestCase):
    def setUp(self):
        self.session = {'access_token': 'ACCESS-SECRET', 'refresh_token': 'REFRESH-SECRET'}
        for target, value in [('_vault_get', None), ('_vault_put', None)]:
            guard = patch.object(account, target, return_value=value)
            guard.start()
            self.addCleanup(guard.stop)
        guard = patch.object(account.common, 'credential_identity', return_value='key-hash')
        guard.start()
        self.addCleanup(guard.stop)
        guard = patch.object(account, '_inference_identity', return_value={'userId': 'same-user'})
        guard.start()
        self.addCleanup(guard.stop)

    def test_web_total_and_code_consumption_are_not_separate_quotas(self):
        row = account.normalize_web_total({'subscription_balance': {
            'amount_used_ratio': .3327, 'kimi_code_used_ratio': .3,
            'expire_time': '2026-11-19T04:00:00Z'}})
        self.assertEqual(row['remaining_percent'], 66.73)
        self.assertEqual(row['kind'], 'month_total')
        self.assertEqual(row['source'], account.WEB_SOURCE)
        self.assertNotIn('month_code', json.dumps(row))
        row = account.normalize_web_total({'subscription_balance': {'amount_used_ratio': 1}})
        self.assertEqual(row['remaining'], 0)
        self.assertEqual(account.normalize_web_total({
            'subscription_balance': {'amount_used_ratio': 0}})['remaining'], 1)

    def test_live_website_camelcase_contract_and_reset(self):
        row = account.normalize_web_total({'subscriptionBalance': {
            'amountUsedRatio': .3327, 'kimiCodeUsedRatio': .3242,
            'expireTime': '2026-11-19T00:00:00Z'},
            'ratelimitCode7d': {'ratio': .8462},
            'ratelimitCode5h': {'ratio': .0001}})
        self.assertEqual(row['remaining_percent'], 66.73)
        self.assertIsNone(row['resets_at'])
        self.assertEqual(row['expires_at'], '2026-11-19T00:00:00Z')
        self.assertEqual(row['kind'], 'month_total')
        self.assertNotIn('month_code', json.dumps(row))

    def test_unknown_or_invalid_total_is_not_zero(self):
        for payload in [None, {}, {'subscription_balance': {}},
                        {'subscription_balance': {'amount_used_ratio': True}},
                        {'subscription_balance': {'amount_used_ratio': float('nan')}}]:
            with self.assertRaises(account.AccountError):
                account.normalize_web_total(payload)

    def test_import_requires_explicit_save_permission(self):
        with self.assertRaises(ValueError):
            account.import_web_session(self.session)
        account._vault_put.assert_not_called()

    def test_import_verifies_ownership_and_only_returns_safe_metadata(self):
        with patch.object(account, '_web_request', return_value={'user': {'id': 'same-user'}}):
            result = account.import_web_session(self.session, confirmed=True)
        self.assertEqual(result['state'], 'connected')
        self.assertNotIn('SECRET', json.dumps(result))
        args = account._vault_put.call_args.args
        stored = json.loads(args[0])
        self.assertEqual(stored['access_token'], self.session['access_token'])
        self.assertNotIn('same-user', json.dumps(stored))

    def test_wrong_account_import_does_not_replace_good_grant(self):
        with patch.object(account, '_web_request', return_value={'user': {'id': 'other-user'}}):
            self.assertEqual(account.import_web_session(self.session, True)['state'], 'account_mismatch')
        account._vault_put.assert_not_called()

    def test_refresh_exact_contract_and_rotated_tokens_stay_in_keychain(self):
        with patch.object(account, '_web_request', side_effect=[
            account.AccountError('web_http_401'),
            {'access_token': 'NEW-ACCESS-SECRET', 'refresh_token': 'NEW-REFRESH-SECRET'},
            {'subscription_balance': {'amount_used_ratio': .2}}]) as request:
            value = account._web_call(dict(self.session), account.WEB_STATS_URL, {'mute_notice': False})
        self.assertEqual(value['subscription_balance']['amount_used_ratio'], .2)
        self.assertEqual(request.call_args_list[1].args,
                         (account.WEB_REFRESH_URL, {'refresh_token': 'REFRESH-SECRET'}))
        self.assertEqual(request.call_args_list[2].args[2], 'NEW-ACCESS-SECRET')
        stored = json.loads(account._vault_put.call_args.args[0])
        self.assertEqual(stored['refresh_token'], 'NEW-REFRESH-SECRET')

    def test_live_camelcase_refresh_pair_is_saved_before_retry(self):
        with patch.object(account, '_web_request', side_effect=[
            account.AccountError('web_http_401'),
            {'accessToken': 'NEW-ACCESS-SECRET', 'refreshToken': 'NEW-REFRESH-SECRET'},
            {'subscriptionBalance': {'amountUsedRatio': .3327}}]):
            result = account._web_call(dict(self.session), account.WEB_STATS_URL, {'mute_notice': False})
        self.assertEqual(result['subscriptionBalance']['amountUsedRatio'], .3327)
        stored = json.loads(account._vault_put.call_args.args[0])
        self.assertEqual(stored['access_token'], 'NEW-ACCESS-SECRET')
        self.assertEqual(stored['refresh_token'], 'NEW-REFRESH-SECRET')

    def test_camelcase_global_identity_is_authoritative(self):
        with patch.object(account, '_inference_identity', return_value={'globalId': 'global'}), \
             patch.object(account, '_web_request', return_value={'user': {'globalId': 'global'}}):
            result = account._web_account_binding(dict(self.session))
        self.assertEqual(result['state'], 'verified')

    def test_revoked_refresh_removes_web_secrets_and_requests_reauth(self):
        with patch.object(account, '_web_request', side_effect=[
            account.AccountError('web_http_401'), account.AccountError('web_http_401')]):
            with self.assertRaisesRegex(account.AccountError, 'web_reauth_required'):
                account._web_call(dict(self.session), account.WEB_STATS_URL, {})
        self.assertEqual(json.loads(account._vault_put.call_args.args[0]), {})

    def test_unrelated_upstream_failure_does_not_refresh_or_leak(self):
        with patch.object(account, '_web_load', return_value=dict(self.session)), \
             patch.object(account, '_web_request', side_effect=account.AccountError('web_http_503')) as request:
            result = account.fetch_web_total()
        self.assertEqual(result['state'], 'unavailable')
        self.assertEqual(request.call_count, 1)
        self.assertNotIn('SECRET', json.dumps(result))

    def test_fetch_different_account_has_no_total(self):
        with patch.object(account, '_web_load', return_value=dict(self.session)), \
             patch.object(account, '_web_request', return_value={'user': {'id': 'other-user'}}):
            result = account.fetch_web_total()
        self.assertEqual(result['state'], 'account_mismatch')
        self.assertEqual(result['windows'], [])

    def test_fetch_total_zero_is_real_exhaustion(self):
        with patch.object(account, '_web_load', return_value=dict(self.session)), \
             patch.object(account, '_web_request', side_effect=[
                 {'user': {'id': 'same-user'}},
                 {'subscription_balance': {'amount_used_ratio': 1}}]):
            result = account.fetch_web_total()
        self.assertEqual(result['state'], 'ok')
        self.assertFalse(result['available'])
        self.assertEqual(result['windows'][0]['remaining'], 0)
        self.assertNotIn('SECRET', json.dumps(result))

    def test_grant_rotation_rechecks_identity_before_merging_total(self):
        with patch.object(account, '_web_load', return_value=dict(self.session)), \
             patch.object(account, '_web_request', side_effect=[
                 {'user': {'id': 'same-user'}}, account.AccountError('web_http_401'),
                 {'access_token': 'OTHER', 'refresh_token': 'OTHER-REFRESH'},
                 {'subscription_balance': {'amount_used_ratio': .2}},
                 {'user': {'id': 'other-user'}}]):
            result = account.fetch_web_total()
        self.assertEqual(result['state'], 'account_mismatch')
        self.assertEqual(result['windows'], [])

    def test_web_status_invalidates_after_key_rotation_and_forget(self):
        session = dict(self.session, inference_hash='old-key')
        with patch.object(account, '_web_load', return_value=session):
            self.assertEqual(account.web_status()['state'], 'account_mismatch')
        result = account.forget_web_session(True)
        self.assertEqual(result['state'], 'disconnected')
        self.assertEqual(json.loads(account._vault_put.call_args.args[0]), {})

    def test_web_endpoint_and_token_controls(self):
        with self.assertRaises(account.AccountError):
            account._web_request('https://evil.test', {}, 'SECRET')
        with self.assertRaises(account.AccountError):
            account._web_request(account.WEB_STATS_URL, {}, 'bad\nTOKEN')
        for payload in [{'access_token': 'only'}, {'access_token': 'x\nbad', 'refresh_token': 'r'}]:
            with self.assertRaises(account.AccountError):
                account._valid_web_tokens(payload)

    def test_vault_deadline_discards_private_partial_output(self):
        import subprocess
        with patch.object(account.subprocess, 'run', side_effect=subprocess.TimeoutExpired(
                ['helper'], 5, output=b'ACCESS-SECRET')) as run:
            with self.assertRaisesRegex(account.AccountError, '^web_keychain_timeout$'):
                account._vault('get')
        self.assertEqual(run.call_args.kwargs['timeout'], 5)
        self.assertEqual(run.call_args.kwargs['stderr'], subprocess.DEVNULL)
        self.assertEqual(run.call_args.args[0][0], account.VAULT_PYTHON)
        self.assertNotIn('ACCESS-SECRET', repr(run.call_args.args[0]))

    def test_vault_put_uses_only_private_stdin_and_captured_stdout(self):
        import subprocess
        response = subprocess.CompletedProcess([], 0, stdout=b'')
        with patch.object(account.subprocess, 'run', return_value=response) as run:
            account._vault('put', b'ACCESS-SECRET')
        self.assertNotIn('ACCESS-SECRET', repr(run.call_args.args[0]))
        self.assertNotIn('ACCESS-SECRET', account.VAULT_HELPER)
        self.assertEqual(run.call_args.kwargs['stdout'], subprocess.PIPE)
        wire = json.loads(run.call_args.kwargs['input'])
        self.assertEqual(wire['service'], account.WEB_KEYCHAIN_SERVICE)
        import base64
        self.assertEqual(base64.b64decode(wire['payload']), b'ACCESS-SECRET')

    def test_vault_failure_is_safe_metadata_not_a_missing_balance(self):
        import subprocess
        with patch.object(account.subprocess, 'run', return_value=subprocess.CompletedProcess([], 3, stdout=b'PRIVATE')):
            with self.assertRaisesRegex(account.AccountError, '^web_keychain_unavailable$'):
                account._vault('get')
        with patch.object(account, '_web_load', side_effect=account.AccountError('web_keychain_timeout')):
            result = account.fetch_web_total()
            state = account.web_status()
        self.assertEqual(result['state'], 'unavailable')
        self.assertEqual(result['windows'], [])
        self.assertEqual(state['error'], 'web_keychain_timeout')
        self.assertNotIn('PRIVATE', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
