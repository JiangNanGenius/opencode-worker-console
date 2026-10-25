import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import console


class QuotaCredentialViewTests(unittest.TestCase):
    def test_alibaba_pair_is_available_as_metadata_without_values_or_paths(self):
        records = [{'name': name, 'source': 'file', 'available': True,
                    'file': '/private/value', 'secret': 'DO_NOT_EXPOSE'}
                   for name in ('aliyun-control-ak', 'aliyun-control-sk', 'unrelated-private')]
        with patch.object(console.credentials, 'listing', return_value={'credentials': records}):
            value = console.quota_credential_refs()
        self.assertEqual({row['name'] for row in value['credentials']},
                         {'aliyun-control-ak', 'aliyun-control-sk'})
        self.assertNotIn('DO_NOT_EXPOSE', str(value))
        self.assertNotIn('/private/value', str(value))


if __name__ == '__main__':
    unittest.main()
