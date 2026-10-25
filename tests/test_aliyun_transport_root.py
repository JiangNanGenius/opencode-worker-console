import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import aliyun_openapi as adapter
import credentials
from common import HttpFailure, NoRedirect


class AliyunTransportTests(unittest.TestCase):
    def test_half_registered_pair_never_mixes_environment_identity(self):
        with patch.object(credentials, '_load_registry', return_value={
                'aliyun-control-ak': {'source': 'env', 'env': 'TEST_AK'}}), \
                patch.dict(os.environ, {'TEST_AK': 'test-ak',
                    'ALIBABA_CLOUD_ACCESS_KEY_ID': 'other-ak',
                    'ALIBABA_CLOUD_ACCESS_KEY_SECRET': 'other-sk'}, clear=True):
            self.assertEqual(adapter.resolve_credentials(), (None, None, None))

    def test_complete_standard_environment_pair(self):
        with patch.object(credentials, '_load_registry', return_value={}), \
                patch.dict(os.environ, {'ALIBABA_CLOUD_ACCESS_KEY_ID': 'test-ak',
                    'ALIBABA_CLOUD_ACCESS_KEY_SECRET': 'test-sk'}, clear=True):
            self.assertEqual(adapter.resolve_credentials(), ('test-ak', 'test-sk', 'environment'))

    def test_request_pins_official_host_no_redirect_and_bounded_io(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"Success":true,"Data":{}}'
        opener = MagicMock(); opener.open.return_value = response
        with patch.object(adapter.urllib.request, 'build_opener', return_value=opener) as factory:
            adapter.call('test-ak', 'test-sk')
        factory.assert_called_once_with(NoRedirect)
        self.assertEqual(opener.open.call_args.args[0].full_url,
                         'https://modelstudio.cn-beijing.aliyuncs.com/tokenplan/subscription/stats')
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 15)
        response.__enter__.return_value.read.assert_called_once_with(adapter.MAX_RESPONSE_BYTES + 1)

    def test_gateway_errors_never_return_auth_data_or_error_body(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError('https://example.invalid', 403,
                                                          'test-ak test-sk', {}, None)
        with patch.object(adapter.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaises(HttpFailure) as result:
                adapter.call('test-ak', 'test-sk')
        self.assertEqual(result.exception.status, 403)
        self.assertNotIn('test-ak', str(result.exception))
        self.assertNotIn('test-sk', str(result.exception))

    def test_malformed_success_flag_is_not_a_real_account_reading(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"Success":"true"}'
        opener = MagicMock(); opener.open.return_value = response
        with patch.object(adapter.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaises(HttpFailure):
                adapter.call('test-ak', 'test-sk')


if __name__ == '__main__':
    unittest.main()
