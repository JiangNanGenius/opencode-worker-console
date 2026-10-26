from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import unittest
import urllib.error
import urllib.parse
from contextlib import ExitStack
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import bailian_personal_ak as ak


def envelope(value):
    return {'code': 200, 'successResponse': True, 'data': {'DataV2': {'data': {
        'success': True, 'code': 'SUCCESS', 'data': value}}}}


class PersonalAccessKeyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.credentials = self.stack.enter_context(patch.object(
            ak.aliyun_openapi, 'resolve_credentials',
            return_value=('ak-fixture', 'sk-fixture', 'credential_reference')))
        self.opener = self.stack.enter_context(patch.object(ak.urllib.request, 'build_opener'))
        self.opener.side_effect = AssertionError('Live network is forbidden in tests')
        self.write = self.stack.enter_context(patch.object(ak.common, 'write_json'))

    def response(self, body, status=200):
        response = Mock()
        response.status = status
        response.read.return_value = json.dumps(body).encode() if isinstance(body, dict) else body
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def queue(self, *responses):
        self.opener.side_effect = None
        self.opener.return_value.open.side_effect = responses

    def assertGeneric(self, error):
        self.assertEqual(str(error), 'Personal Token Plan quota is unavailable.')

    def test_acs3_empty_post_fixture_includes_content_type_in_signature(self):
        headers = ak._sign('ak-fixture', 'sk-fixture',
                           datetime(2026, 10, 26, tzinfo=timezone.utc), 'nonce-fixture')
        self.assertEqual(headers['Authorization'], 'ACS3-HMAC-SHA256 Credential=ak-fixture,'
            'SignedHeaders=content-type;host;x-acs-action;x-acs-content-sha256;x-acs-date;'
            'x-acs-signature-nonce;x-acs-version,'
            'Signature=fbc1d5028d98fc0d8bc5b3d516f301e8f1a236fd6057bab48e8ae8dad2646fbe')
        self.assertEqual(headers['content-type'], 'application/json')
        self.assertEqual(headers['x-acs-action'], 'GenerateCLIAccessToken')
        self.assertEqual(headers['x-acs-version'], '2026-02-10')
        self.assertNotIn('sk-fixture', json.dumps(headers))

    def test_full_transport_uses_account_ak_then_bearer_without_cookie(self):
        responses = [self.response({'cliAccessToken': 'cli-token-fixture'})] + [
            self.response(envelope({'read': index})) for index in range(3)]
        self.queue(*responses)
        self.assertEqual(ak.fetch_payloads({}), ({'read': 0}, {'read': 1}, {'read': 2}))
        calls = self.opener.return_value.open.call_args_list
        self.assertEqual(len(calls), 4)
        token_request = calls[0].args[0]
        self.assertEqual(token_request.full_url,
            'https://modelstudio.cn-beijing.aliyuncs.com/modelstudio/cli/generateAccessToken')
        self.assertEqual(token_request.method, 'POST')
        self.assertEqual(token_request.data, b'')
        self.assertEqual(token_request.get_header('Content-type'), 'application/json')
        self.assertIn('Credential=ak-fixture', token_request.get_header('Authorization'))
        for call, api in zip(calls[1:], (ak.SUBSCRIPTION, ak.USAGE, ak.ADDON)):
            request = call.args[0]
            url = urllib.parse.urlsplit(request.full_url)
            self.assertEqual(url.scheme, 'https')
            self.assertEqual(url.netloc, 'bailian-cs.console.aliyun.com')
            self.assertEqual(url.path, '/cli/api.json')
            self.assertEqual(urllib.parse.parse_qs(url.query)['api'], [api])
            self.assertEqual(request.method, 'POST')
            self.assertEqual(request.get_header('Authorization'), 'Bearer cli-token-fixture')
            self.assertIsNone(request.get_header('Cookie'))
            form = urllib.parse.parse_qs(request.data.decode())
            self.assertEqual(set(form), {'params', 'region'})
            self.assertEqual(form['region'], ['cn-beijing'])
            self.assertEqual(json.loads(form['params'][0]), {'Api': api, 'V': '1.0',
                'Data': {'cornerstoneParam': {'protocol': 'V2', 'console': 'ONE_CONSOLE',
                    'productCode': 'p_efm', 'switchUserType': 3, 'consoleSite': 'BAILIAN_ALIYUN'}}})
        for call, response in zip(calls, responses):
            self.assertEqual(call.kwargs, {'timeout': 15})
            response.read.assert_called_once_with(1024 * 1024 + 1)
        for call in self.opener.call_args_list:
            self.assertEqual(call.args, (ak.common.NoRedirect,))
        self.write.assert_not_called()

    def test_missing_control_credentials_does_not_call_network(self):
        self.credentials.return_value = (None, None, None)
        with self.assertRaises(ak.QuotaError) as raised:
            ak.fetch_payloads()
        self.assertGeneric(raised.exception)
        self.opener.assert_not_called()

    def test_fresh_token_is_minted_each_tick_and_never_returned_or_persisted(self):
        with patch.object(ak, '_mint_token', side_effect=['first-token', 'second-token']) as mint, \
                patch.object(ak, '_gateway', return_value={'quota': 1}) as gateway:
            first = ak.fetch_payloads()
            second = ak.fetch_payloads()
        self.assertEqual(mint.call_count, 2)
        self.assertEqual([call.args[1] for call in gateway.call_args_list],
                         ['first-token'] * 3 + ['second-token'] * 3)
        self.assertNotIn('token', json.dumps((first, second)))
        self.assertFalse(any('token' in key.lower() and not callable(value)
                             for key, value in vars(ak).items() if not key.startswith('__')))
        self.write.assert_not_called()

    def test_not_logined_refreshes_once_and_retries_only_failed_read(self):
        expired = self.response({'code': 200, 'successResponse': True,
            'data': {'success': False, 'errorCode': 'Console.NotLogined'}})
        self.queue(self.response({'cliAccessToken': 'old-token'}),
                   self.response(envelope({'subscription': 1})), expired,
                   self.response({'cliAccessToken': 'new-token'}),
                   self.response(envelope({'usage': 1})), self.response(envelope({'addon': 1})))
        result = ak.fetch_payloads()
        self.assertEqual(result, ({'subscription': 1}, {'usage': 1}, {'addon': 1}))
        calls = self.opener.return_value.open.call_args_list
        self.assertEqual(len(calls), 6)
        self.assertEqual(calls[4].args[0].get_header('Authorization'), 'Bearer new-token')
        self.assertEqual(calls[5].args[0].get_header('Authorization'), 'Bearer new-token')

    def test_repeated_expiry_has_one_refresh_maximum_and_generic_failure(self):
        with patch.object(ak, '_mint_token', side_effect=['first', 'second']) as mint, \
                patch.object(ak, '_gateway', side_effect=ak.QuotaError(True)) as gateway:
            with self.assertRaises(ak.QuotaError) as raised:
                ak.fetch_payloads()
        self.assertEqual(mint.call_count, 2)
        self.assertEqual(gateway.call_count, 2)
        self.assertTrue(raised.exception.auth)
        self.assertGeneric(raised.exception)

    def test_addon_failure_remains_optional_after_successful_monthly_reads(self):
        with patch.object(ak, '_mint_token', return_value='token'), patch.object(ak, '_gateway',
                side_effect=[{'subscription': 1}, {'usage': 1}, ak.QuotaError()]):
            self.assertEqual(ak.fetch_payloads(), ({'subscription': 1}, {'usage': 1}, None))

    def test_only_three_read_apis_are_allowed(self):
        for api in ('https://foreign.example', ak.SUBSCRIPTION + '/delete',
                    'zeldaEasy.cornerstone-portal.cs-console.loginInfo'):
            with self.assertRaises(ak.QuotaError) as raised:
                ak._gateway(api, 'token')
            self.assertGeneric(raised.exception)
        self.opener.assert_not_called()

    def test_network_redirect_http_and_body_failures_do_not_echo_secrets(self):
        errors = [TimeoutError('secret-account ak-fixture'),
                  urllib.error.HTTPError('https://private', 302, 'sk-fixture', {},
                                         io.BytesIO(b'private-account')),
                  urllib.error.HTTPError('https://private', 401, 'sk-fixture', {},
                                         io.BytesIO(b'private-account'))]
        for error in errors:
            self.queue(error)
            with self.assertRaises(ak.QuotaError) as raised:
                ak._mint_token('ak-fixture', 'sk-fixture')
            self.assertGeneric(raised.exception)
        for body in (b'<html>Login private-account</html>', b'x' * (1024 * 1024 + 1),
                     {}, {'cliAccessToken': ''}, {'cliAccessToken': 'token\nsecret'}):
            self.queue(self.response(body))
            with self.assertRaises(ak.QuotaError) as raised:
                ak._mint_token('ak-fixture', 'sk-fixture')
            self.assertGeneric(raised.exception)
        for body in ({}, {'code': 200, 'successResponse': True},
                     envelope([]), {'code': 500, 'successResponse': False}):
            self.queue(self.response(body))
            with self.assertRaises(ak.QuotaError) as raised:
                ak._gateway(ak.USAGE, 'cli-token-fixture')
            self.assertGeneric(raised.exception)


if __name__ == '__main__':
    unittest.main()
