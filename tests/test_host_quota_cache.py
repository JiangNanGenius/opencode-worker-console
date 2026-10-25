import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import host_telemetry as ht


class HostQuotaCacheTests(unittest.TestCase):
    def test_latest_worker_reading_is_reused_without_a_provider_or_codex_query(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            record = {'state': 'ok', 'source': 'openapi_live', 'sampled_at': time.time(),
                      'available': True, 'windows': [{'valid': True, 'remaining': 80000,
                          'limit': 100000, 'remaining_percent': 80}],
                      'live_error': {'state': 'unavailable', 'detail': 'PRIVATE',
                                     'attempted_at': time.time()}}
            (state / 'quota.json').write_text(json.dumps({'bailian-token-plan': record}))
            old = [{'id': 'codex', 'state': 'ok', 'windows': []},
                   {'id': 'bailian-token-plan', 'state': 'ok', 'sampled_at': 1}]
            with patch.object(ht, 'STATE', state), patch.object(ht, 'billing_overlay', return_value={}), \
                    patch.object(ht.subprocess, 'run', side_effect=AssertionError('No CLI/network calls')):
                rows = ht.cached_worker_quotas(old)
            self.assertEqual(rows[0], old[0])
            row = rows[1]
            self.assertEqual(row['windows'][0]['remaining'], 80000)
            self.assertEqual(row['source'], 'openapi_live')
            self.assertTrue(row['stale'])
            self.assertNotIn('PRIVATE', json.dumps(row))
            self.assertEqual(old[1]['sampled_at'], 1)

    def test_missing_cache_keeps_the_collector_sample(self):
        with tempfile.TemporaryDirectory() as td, patch.object(ht, 'STATE', Path(td)):
            previous = [{'id': 'codex'}]
            self.assertEqual(ht.cached_worker_quotas(previous), previous)


if __name__ == '__main__':
    unittest.main()
