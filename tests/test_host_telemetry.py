"""Collector tests: acquisition, retention, unplug, staleness, errors, board v2.

Everything is mocked at module boundaries; no real sensors, daemons or
provider CLIs are touched.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import host_telemetry as ht


class TelemetryAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.collector = ht.Collector(state_dir=self.root, codex_root=self.root / 'no-codex')

    def tearDown(self):
        self.tmp.cleanup()

    def _acquire(self, now=1000.0, **overrides):
        defaults = dict(cpu_percent=42.5, gpu_percent=17.0,
                        memory={'total_bytes': 24 * 2 ** 30, 'used_bytes': 12 * 2 ** 30,
                                'used_percent': 50.0},
                        memory_pressure_percent=41.0,
                        network_counters=(1000, 2000), disk_counters=None,
                        disks=[{'name': 'Mac SSD', 'mountpoint': '/System/Volumes/Data',
                                'internal': True, 'total_gb': 460.0, 'free_gb': 100.0,
                                'used_percent': 78.0}],
                        uptime_seconds=3600.0, fan_status=None, gpu=17.0)
        defaults.update(overrides)
        with patch.object(ht, '_cpu_percent', return_value=defaults['cpu_percent']), \
                patch.object(ht, '_gpu_percent', return_value=defaults['gpu']), \
                patch.object(ht, '_memory', return_value=defaults['memory']), \
                patch.object(ht, '_memory_pressure_percent', return_value=defaults['memory_pressure_percent']), \
                patch.object(ht, '_network_counters', return_value=defaults['network_counters']), \
                patch.object(ht, '_disk_counters', return_value=defaults['disk_counters']), \
                patch.object(ht, '_disks', return_value=defaults['disks']), \
                patch.object(ht, '_uptime_seconds', return_value=defaults['uptime_seconds']), \
                patch.object(ht, '_fan_status', return_value=defaults['fan_status']):
            return self.collector.collect_host_once(now=now)

    def test_sample_collects_metrics_and_first_rates_are_null(self):
        sample = self._acquire()
        self.assertEqual(sample['cpu_percent'], 42.5)
        self.assertEqual(sample['gpu_percent'], 17.0)
        self.assertEqual(sample['memory_pressure_percent'], 41.0)
        self.assertIsNone(sample['net_in_bps'])  # first sample has no delta
        self.assertIsNone(sample['disk_total_bps'])
        self.assertEqual(sample['disks'][0]['name'], 'Mac SSD')
        self.assertEqual(sample['uptime_hours'], 1.0)

    def test_rates_computed_from_cumulative_deltas(self):
        self._acquire(now=1000.0, network_counters=(1000, 2000),
                      disk_counters={'disk0': {'read_mb': 100.0, 'write_mb': 50.0, 'total_mb': 150.0}})
        second = self._acquire(now=1002.0, network_counters=(3000, 6000),
                               disk_counters={'disk0': {'read_mb': 110.0, 'write_mb': 60.0, 'total_mb': 170.0}})
        self.assertEqual(second['net_in_bps'], 1000.0)
        self.assertEqual(second['net_out_bps'], 2000.0)
        self.assertEqual(second['disk_read_bps'], 5e6)
        self.assertEqual(second['disk_write_bps'], 5e6)
        self.assertEqual(second['disk_total_bps'], 10e6)

    def test_macos_disk_split_is_explicitly_unavailable(self):
        sample = self._acquire(disk_counters={'disk0': {'read_mb': None, 'write_mb': None, 'total_mb': 99.0}})
        self.assertIsNone(sample['disk_read_bps'])
        self.assertIsNone(sample['disk_write_bps'])
        self.assertIsNotNone(sample['disk_total_bps'] is None)  # still no delta on first sample

    def test_unplugged_disk_disappears_from_sample(self):
        two = [{'name': 'Mac SSD', 'mountpoint': '/System/Volumes/Data', 'internal': True,
                'total_gb': 460.0, 'free_gb': 100.0, 'used_percent': 78.0},
               {'name': 'USB', 'mountpoint': '/Volumes/USB', 'internal': False,
                'total_gb': 100.0, 'free_gb': 50.0, 'used_percent': 50.0}]
        self._acquire(disks=two)
        sample = self._acquire(disks=two[:1])
        self.assertEqual([d['name'] for d in sample['disks']], ['Mac SSD'])

    def test_unplug_between_discovery_and_usage_is_skipped(self):
        import shutil
        with patch.object(ht.shutil, 'disk_usage', side_effect=[shutil.disk_usage('/'), OSError('ejected')]):
            with patch.object(ht, '_disk_candidates', return_value=[('/System/Volumes/Data', 'Mac SSD'),
                                                                    ('/Volumes/USB', 'USB')]):
                sample = self.collector.collect_host_once(now=1000.0)
        self.assertEqual(len(sample['disks']), 1)

    def test_sensor_failure_yields_null_not_zero(self):
        sample = self._acquire(cpu_percent=None, gpu=None, memory_pressure_percent=None,
                               network_counters=(None, None),
                               fan_status={'fans': None, 'temp_cpu_c': None, 'temp_gpu_c': None,
                                           'thermal_pressure': None, 'control_mode': None,
                                           'active_profile': None, 'safety_engaged': None})
        self.assertIsNone(sample['cpu_percent'])
        self.assertIsNone(sample['gpu_percent'])
        self.assertIsNone(sample['memory_pressure_percent'])
        self.assertIsNone(sample['net_in_bps'])
        self.assertIsNone(sample['temp_cpu_c'])
        self.assertIsNone(sample['fans'])

    def test_memory_pressure_uses_kernel_level(self):
        class FakePath:
            def __init__(self, value):
                self.value = str(value)

            def is_file(self):
                return self.value == '/usr/sbin/sysctl'

        with patch.object(ht, 'Path', FakePath), patch.object(ht, '_run', return_value='59'):
            self.assertIsNone(ht._memory_pressure_percent())
        with patch.object(ht, '_run', return_value='1'):
            self.assertEqual(ht._memory_pressure_state(), 'normal')

    def test_memory_pressure_linux_psi(self):
        psi = 'some avg10=7.50 avg60=5.00 avg300=1.00 total=123\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n'

        class FakePath:
            def __init__(self, value):
                self.value = str(value)

            def is_file(self):
                return self.value == '/proc/pressure/memory'

            def read_text(self):
                return psi

        with patch.object(ht, 'Path', FakePath):
            self.assertEqual(ht._memory_pressure_percent(), 7.5)

    def test_collector_loop_survives_acquisition_errors(self):
        with patch.object(ht, '_cpu_percent', side_effect=RuntimeError('boom')):
            sample = self.collector.collect_host_once(now=1000.0)
        self.assertIsNone(sample['cpu_percent'])
        snapshot = self.collector.snapshot(now=1001.0)
        self.assertFalse(snapshot['stale'])

    def test_stale_flag_when_sample_ages(self):
        self._acquire(now=1000.0)
        self.assertTrue(self.collector.snapshot(now=1000.0 + ht.STALE_AFTER_SECONDS + 1)['stale'])
        self.assertFalse(self.collector.snapshot(now=1001.0)['stale'])
        self.assertTrue(ht.Collector(state_dir=self.root).snapshot()['stale'])


class TelemetryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.collector = ht.Collector(state_dir=self.root, codex_root=self.root / 'no-codex')
        self.collector.store = ht.TelemetryStore(self.root / 'host' / 'telemetry.sqlite')

    def tearDown(self):
        self.tmp.cleanup()

    def _sample(self, ts, cpu):
        return {'sampled_at': ts, 'cpu_percent': cpu, 'gpu_percent': None,
                'memory_percent': 50.0, 'memory_pressure_percent': None, 'net_in_bps': None,
                'net_out_bps': None, 'disk_read_bps': None, 'disk_write_bps': None,
                'disk_total_bps': None, 'temp_cpu_c': None, 'temp_gpu_c': None}

    def test_retention_prunes_old_raw_and_minutes_and_caps_rows(self):
        store = self.collector.store
        now = 10_000_000.0
        store.add_raw(self._sample(now - ht.RAW_TTL_SECONDS - 10, 1))
        store.add_raw(self._sample(now - 10, 2))
        store.add_minute(int(now - ht.MINUTES_TTL_SECONDS) // 60 - 1, self._sample(now, 3))
        store.add_minute(int(now) // 60, self._sample(now, 4))
        store.prune(now)
        raw_ts = [ts for ts, _ in store.raw_rows(0)]
        minute_ts = [ts for ts, _ in store.minute_rows(0)]
        self.assertEqual(raw_ts, [now - 10])
        self.assertEqual(len(minute_ts), 1)
        for index in range(ht.RAW_CAP + 50):
            store.add_raw(self._sample(now + index, 5))
        store.prune(now + ht.RAW_CAP + 50)
        with store._connect() as con:
            count = con.execute('SELECT COUNT(*) FROM raw').fetchone()[0]
        self.assertLessEqual(count, ht.RAW_CAP)

    def test_minute_aggregation_averages_and_flushes_on_rollover(self):
        buffer = ht._MinuteBuffer()
        first = buffer.add(self._sample(60 * 1000 + 1, 10))
        self.assertIsNone(first)
        buffer.add(self._sample(60 * 1000 + 30, 30))
        flushed = buffer.add(self._sample(60 * 1001 + 5, 50))
        self.assertIsNotNone(flushed)
        minute, aggregate = flushed
        self.assertEqual(minute, 1000)
        self.assertEqual(aggregate['cpu_percent'], 20.0)  # average of 10 and 30
        self.assertEqual(aggregate['samples'], 2)

    def test_history_ranges_and_null_gaps_not_zero_filled(self):
        store = self.collector.store
        now = 20_000_000.0
        store.add_raw(self._sample(now - 800, 10))
        # gap: no samples between
        store.add_raw(self._sample(now - 10, 30))
        history = self.collector.history('15m', now=now)
        points = history['series']['cpu_percent']
        self.assertEqual([p['v'] for p in points], [10, 30])
        self.assertGreater(points[1]['t'] - points[0]['t'], 700)
        empty = self.collector.history('24h', now=now)
        self.assertEqual(empty['series']['cpu_percent'], [])
        with self.assertRaises(ValueError):
            self.collector.history('1w', now=now)


class BoardAndTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        # Shared quota refreshes must read fixture state, never this host's cache.
        state_patch = patch.object(ht, 'STATE', self.root)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        (self.root / 'tasks').mkdir()
        common.write_json(self.root / 'tasks' / 'job-1.json',
                          {'id': 'job-1', 'title': 'Build', 'status': 'running', 'created_at': 1,
                           'started_at': 900.0, 'updated_at': 990.0, 'actual_models': ['acme/m1'],
                           'source_dir': '/tmp/proj', 'group_title': 'proj'})
        common.write_json(self.root / 'tasks' / 'job-2.json',
                          {'id': 'job-2', 'title': 'Done', 'status': 'completed', 'created_at': 2,
                           'started_at': 800.0, 'finished_at': 890.0, 'updated_at': 895.0})
        self.collector = ht.Collector(state_dir=self.root, codex_root=self.root / 'no-codex')

    def tearDown(self):
        self.tmp.cleanup()

    def test_board_file_v2_payload_aliases_and_owner_only_mode(self):
        sample = {'sampled_at': 1234.0, 'cpu_percent': 33.0, 'temp_cpu_c': 44.5, 'temp_gpu_c': 40.1,
                  'memory_percent': 50.0, 'memory_used_gb': 12.0, 'memory_total_gb': 24.0,
                  'disks': [{'name': 'Mac SSD', 'mountpoint': '/System/Volumes/Data', 'internal': True,
                             'total_gb': 460.0, 'free_gb': 100.0, 'used_percent': 78.0}],
                  'uptime_hours': 2.0, 'fans': [{'name': 'Fan 0', 'rpm': 1200}],
                  'thermal_pressure': 'nominal', 'net_in_bps': 5.0, 'net_out_bps': 6.0,
                  'disk_total_bps': 7.0, 'gpu_percent': 9.0, 'memory_pressure_percent': 10.0}
        self.collector.latest_system = sample
        self.collector.latest_quotas = [{'id': 'codex', 'state': 'unavailable'}]
        self.collector.publish_board()
        board = json.loads(self.collector.board_path.read_text())
        self.assertEqual(board['schema_version'], 2)
        self.assertIsInstance(board['unix_time'], int)
        self.assertIsInstance(board['utc_offset_seconds'], int)
        system = board['system']
        # Backward-compatible aliases expected by the board firmware.
        self.assertEqual(system['cpu'], 33.0)
        self.assertEqual(system['cpu_temp_c'], 44.5)
        self.assertEqual(system['gpu_temp_c'], 40.1)
        self.assertIn('host', system)
        self.assertEqual(system['memory_percent'], 50.0)
        self.assertEqual(system['disks'][0]['free_gb'], 100.0)
        # New names are published alongside for richer consumers.
        self.assertEqual(system['cpu_percent'], 33.0)
        self.assertEqual(board['quotas'][0]['id'], 'codex')
        self.assertEqual(self.collector.board_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)


    def test_board_quota_ids_match_firmware_without_mutating_web_snapshot(self):
        windows = [{'label': '每月', 'remaining_percent': 2.48, 'resets_at': 1792511999.0}]
        self.collector.latest_quotas = [
            {'id': 'codex', 'sampled_at': 1234, 'state': 'ok', 'windows': [], 'balances': []},
            {'id': 'kimi-for-coding', 'sampled_at': 1234, 'state': 'ok', 'windows': windows, 'balances': []},
            {'id': 'volcengine-agent-plan', 'sampled_at': 1234, 'state': 'ok', 'windows': windows, 'balances': []},
            {'id': 'deepseek', 'sampled_at': 1234, 'state': 'ok', 'windows': [],
             'balances': [{'currency': 'CNY', 'remaining': 40}]}]
        # The existing firmware looks up codex/kimi/ark/deepseek by exact ID.
        board = self.collector.board_payload()
        ids = [q['id'] for q in board['quotas']]
        self.assertEqual(ids, ['codex', 'kimi', 'ark', 'deepseek'])
        ark = next(q for q in board['quotas'] if q['id'] == 'ark')
        self.assertEqual(ark['sampled_at'], 1234)
        self.assertEqual(ark['windows'][0]['remaining_percent'], 2.48)
        web = self.collector.snapshot()
        self.assertEqual([q['id'] for q in web['quotas']],
                         ['codex', 'kimi-for-coding', 'volcengine-agent-plan', 'deepseek'])
        ark['windows'][0]['remaining_percent'] = 0
        self.assertEqual(self.collector.latest_quotas[2]['windows'][0]['remaining_percent'], 2.48)

    def test_alibaba_quota_row_uses_compact_alias_with_snapshot_freshness(self):
        quota = {'id': 'bailian-token-plan', 'state': 'ok', 'sampled_at': 1234,
                 'windows': [{'label': '每月', 'remaining_percent': 99.9, 'resets_at': 1795553999.0}],
                 'balances': [], 'snapshot': {'captured_at': 1234, 'fresh_for_minutes': 1440,
                                              'freshness': 'fresh'}}
        self.collector.latest_quotas = [quota]
        board = self.collector.board_payload()
        self.assertEqual([q['id'] for q in board['quotas']], ['aliyun'])
        self.assertEqual(board['quotas'][0]['windows'][0]['remaining_percent'], 99.9)
        # Additive snapshot metadata survives the compact alias unchanged.
        self.assertEqual(board['quotas'][0]['snapshot']['freshness'], 'fresh')
        # The web snapshot keeps the canonical provider ID.
        self.assertEqual([q['id'] for q in self.collector.snapshot()['quotas']],
                         ['bailian-token-plan'])

    def test_normalize_worker_marks_stale_snapshot_and_keeps_window_amount(self):
        record = {'state': 'ok', 'sampled_at': 1000.0,
                  'windows': [{'name': 'CreditsPool', 'valid': True, 'duration_minutes': 43200,
                               'remaining_percent': 99.9, 'remaining': 99926.94, 'limit': 100000.0,
                               'resets_at': '2026-11-16T10:00:00+00:00'}],
                  'balances': [],
                  'available': None,
                  'snapshot': {'captured_at': '2026-10-16T18:21:54+00:00',
                               'fresh_for_minutes': 60, 'freshness': 'stale', 'expired': False}}
        row = ht.normalize_worker('bailian-token-plan', record)
        self.assertEqual(row['id'], 'bailian-token-plan')
        self.assertEqual(row['name'], '阿里百炼')
        self.assertEqual(row['windows'][0]['label'], '每月')
        self.assertEqual(row['windows'][0]['remaining'], 99926.94)
        self.assertEqual(row['windows'][0]['unit'], 'credits')
        self.assertTrue(row['stale'])
        self.assertEqual(row['snapshot']['freshness'], 'stale')
        fresh = dict(record, snapshot=dict(record['snapshot'], freshness='fresh'))
        fresh_row = ht.normalize_worker('bailian-token-plan', fresh)
        self.assertNotIn('stale', fresh_row)
        self.assertEqual(fresh_row['available'], None)
        # Absolute freshness: the snapshot horizon, capped by the plan reset.
        captured = ht.epoch(record['snapshot']['captured_at'])
        self.assertEqual(fresh_row['valid_until'], captured + 60 * 60)
        capped = dict(record, snapshot=dict(record['snapshot'], freshness='fresh',
                                            resets_at=record['snapshot']['captured_at']))
        self.assertEqual(ht.normalize_worker('bailian-token-plan', capped)['valid_until'], captured)
        # A reset-expired snapshot propagates expiration instead of age-only freshness.
        expired = dict(record, windows=[dict(record['windows'][0], valid=False, expired=True)],
                       snapshot=dict(record['snapshot'], freshness='stale', expired=True))
        expired_row = ht.normalize_worker('bailian-token-plan', expired)
        self.assertTrue(expired_row['snapshot']['expired'])
        self.assertTrue(expired_row['stale'])
        self.assertEqual(expired_row['windows'], [])

    def test_normalize_worker_reports_billing_exhaustion_and_availability(self):
        record = {'state': 'billing_blocked', 'sampled_at': 1000.0, 'available': False,
                  'billing': {'reason': 'monthly_usage_limit', 'monthly_exhausted': True,
                              'message': 'private text must not leak'},
                  'windows': [{'valid': True, 'remaining_percent': 10, 'duration_minutes': 10080,
                               'resets_at': '2026-10-20T00:00:00Z'}], 'balances': []}
        row = ht.normalize_worker('kimi-for-coding', record)
        self.assertEqual(row['valid_until'], 1000.0 + ht.LIVE_QUOTA_VALID_SECONDS)
        self.assertFalse(row['available'])
        self.assertTrue(row['billing']['monthly_exhausted'])
        self.assertEqual(row['billing']['reason'], 'monthly_usage_limit')
        self.assertNotIn('message', row['billing'])
        self.assertEqual(row['windows'][0]['remaining_percent'], 10.0)


class TaskCollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / 'tasks').mkdir()
        (self.root / 'progress').mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _task(self, name, **fields):
        base = {'id': name, 'title': 'Task ' + name, 'status': 'running', 'created_at': 1,
                'started_at': 900.0, 'updated_at': 990.0}
        base.update(fields)
        common.write_json(self.root / 'tasks' / (name + '.json'), base)

    def test_opencode_running_only_with_model_and_real_progress(self):
        self._task('job-live', actual_models=['acme/solo'], source_dir='/tmp/proj',
                   group_title='proj')
        self._task('job-done', status='completed', finished_at=950.0)
        common.write_json(self.root / 'progress' / 'job-live.json',
                          {'percent': 42.0, 'phase': 'build', 'github_run': {'url': 'x'}})
        with patch.object(ht, 'STATE', self.root):
            result = ht.collect_opencode_tasks(now=1000.0)
        self.assertEqual(result['state'], 'ok')
        self.assertEqual([i['id'] for i in result['items']], ['job-live'])
        item = result['items'][0]
        self.assertEqual(item['model'], 'solo')
        self.assertEqual(item['progress']['percent'], 42.0)
        # An object-shaped github_run is not a URL and must be dropped.
        self.assertNotIn('github_run', item['progress'])

    def test_progress_requires_real_values(self):
        self._task('job-empty-progress')
        common.write_json(self.root / 'progress' / 'job-empty-progress.json', {})
        with patch.object(ht, 'STATE', self.root):
            result = ht.collect_opencode_tasks(now=1000.0)
        self.assertNotIn('progress', result['items'][0])

    def test_codex_missing_store_is_unavailable_not_error(self):
        collector = ht.CodexTasks(root=self.root / 'no-codex')
        result = collector.collect(now=1000.0)
        self.assertEqual(result['state'], 'unavailable')
        self.assertEqual(result['items'], [])

    def test_secret_like_titles_are_redacted(self):
        self.assertEqual(ht._display_title('use password=hunter2 now'), '未命名任务')
        self.assertEqual(ht._display_title('call https://example.test/x'), '未命名任务')
        self.assertEqual(ht._display_title('Real work'), 'Real work')


class QuotaCollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_quotas_read_daemon_cache_and_codex_cli(self):
        common.write_json(self.root / 'quota.json', {
            'kimi-for-coding': {'state': 'ok', 'sampled_at': 1000.0, 'available': True,
                                'windows': [{'valid': True, 'remaining_percent': 83.0,
                                             'duration_minutes': 300, 'resets_at': '2026-10-13T17:47:51Z'}],
                                'balances': []},
            'deepseek': {'state': 'ok', 'sampled_at': 1000.0,
                         'balances': [{'currency': 'CNY', 'remaining': 12.5}], 'windows': []},
            'volcengine-agent-plan': {'state': 'unavailable'}})
        codex = json.dumps({'usage': {'primary': {'usedPercent': 30, 'windowMinutes': 300,
                                                   'resetsAt': '2026-10-13T18:00:00Z'}}})
        completed = subprocess_completed(codex)
        import quota
        with patch.object(ht, 'STATE', self.root), \
                patch.object(quota, 'STATE', self.root), \
                patch.object(ht.subprocess, 'run', return_value=completed):
            quotas = ht.collect_quotas(now=1000.0)
        by_id = {q['id']: q for q in quotas}
        self.assertEqual(by_id['codex']['state'], 'ok')
        self.assertEqual(by_id['codex']['windows'][0]['remaining_percent'], 70.0)
        self.assertEqual(by_id['kimi-for-coding']['windows'][0]['label'], '5小时')
        self.assertEqual(by_id['deepseek']['balances'][0]['remaining'], 12.5)
        self.assertEqual(by_id['volcengine-agent-plan']['state'], 'unavailable')

    def test_failed_codex_sample_preserves_previous_windows(self):
        common.write_json(self.root / 'quota.json', {})
        collector = ht.Collector(state_dir=self.root, codex_root=self.root / 'no')
        collector.latest_quotas = [{'id': 'codex', 'state': 'ok', 'sampled_at': 500.0,
                                    'windows': [{'label': '5小时', 'remaining_percent': 55.0,
                                                 'resets_at': 900.0}], 'balances': []}]
        with patch.object(ht.subprocess, 'run', side_effect=OSError('no CLI')):
            merged = collector.collect_quotas_once(now=1000.0)
        codex = merged[0]
        self.assertEqual(codex['state'], 'unavailable')
        self.assertEqual(codex['windows'][0]['remaining_percent'], 55.0)
        self.assertEqual(codex['sampled_at'], 500.0)


class SequentialPlanQuotaTests(unittest.TestCase):
    """Sequential Alibaba plans: independent Team/Personal cards from the
    identity-bound namespace; an old Team-edition writer can never win."""

    BINDING = 'b' * 64

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.identity_patch = patch("bailian_quota.display_identity", return_value=self.BINDING)
        self.identity_patch.start()

    def tearDown(self):
        self.identity_patch.stop()
        self.tmp.cleanup()

    def _team_shared(self, sampled_at=9000.0):
        """Shape the long-lived Team-edition writer puts into quota.json."""
        return {'state': 'ok', 'sampled_at': sampled_at, 'checked_at': sampled_at,
                'source': 'openapi_live', 'available': False, '_credential': 'a' * 64,
                'windows': [{'name': 'Credits(max)', 'limit': 424194.0, 'remaining': 0.0,
                             'remaining_percent': 0.0, 'duration_minutes': 44640,
                             'resets_at': '2026-11-16T10:00:00+00:00', 'valid': True}],
                'balances': []}

    def _namespace(self, team_sampled=1000.0, personal_sampled=1001.0, active='personal',
                   binding=None, personal_percent=97.48):
        binding = binding or self.BINDING
        team = {'state': 'ok', 'source': 'openapi_live', 'plan_edition': 'team',
                'available': False, 'sampled_at': team_sampled,
                'windows': [{'name': 'Credits(max)', 'limit': 424194.0, 'remaining': 0.0,
                             'remaining_percent': 0.0, 'duration_minutes': 44640,
                             'resets_at': '2026-11-16T10:00:00+00:00', 'valid': True}],
                'balances': []}
        personal = {'state': 'ok', 'source': 'personal_console', 'plan_edition': 'personal',
                    'available': True, 'sampled_at': personal_sampled,
                    'windows': [{'name': 'Monthly', 'unit': 'percent',
                                 'remaining_percent': personal_percent,
                                 'duration_minutes': 45111.2333333333,
                                 'resets_at': '2099-11-26T16:00:00+00:00', 'valid': True}],
                    'balances': []}
        return {'state': 'ok', 'source': 'plan_sequence', 'plan_edition': 'sequential',
                '_plan_identity': binding, '_credential': binding, '_display_identity': binding, 'available': True,
                'attempted_at': personal_sampled, 'windows': [], 'balances': [],
                'plan_quotas': {'team': team, 'personal': personal},
                'active_plan': active, 'sampled_at': personal_sampled,
                'checked_at': personal_sampled}

    @staticmethod
    def _edition(edition):
        return patch.object(ht, '_PLAN_EDITION_CACHE',
                            {'edition': edition, 'checked_at': time.time()})

    def test_sequential_expands_independent_team_and_personal_cards(self):
        common.write_json(self.root / 'bailian-plan-quota.json', self._namespace())
        # The old writer stamped the shared file later than the namespace did.
        shared = self._team_shared(sampled_at=9000.0)
        with self._edition('sequential'), patch.object(ht, 'STATE', self.root):
            rows = ht.worker_rows('bailian-token-plan', shared)
        self.assertEqual([row['id'] for row in rows], ['aliyun-team', 'aliyun-personal'])
        self.assertEqual([row['name'] for row in rows], ['阿里团队', '阿里个人'])
        team, personal = rows
        self.assertTrue(all(row['plan_edition'] == 'sequential' for row in rows))
        self.assertEqual(team['windows'][0]['remaining'], 0.0)
        self.assertEqual(team['windows'][0]['limit'], 424194.0)
        self.assertEqual(team['windows'][0]['unit'], 'credits')
        self.assertEqual(team['windows'][0]['remaining_percent'], 0.0)
        self.assertFalse(team['available'])
        self.assertEqual(team['sampled_at'], 1000.0)
        self.assertEqual(team['source'], 'openapi_live')
        self.assertEqual(team['plan_slot'], 'standby')
        personal_window = personal['windows'][0]
        self.assertEqual(personal_window['remaining_percent'], 97.48)
        self.assertNotIn('remaining', personal_window)
        self.assertNotIn('limit', personal_window)
        self.assertEqual(personal_window['unit'], 'percent')
        self.assertEqual(personal_window['label'], '每月')
        self.assertTrue(personal['available'])
        self.assertEqual(personal['sampled_at'], 1001.0)
        self.assertEqual(personal['source'], 'personal_console')
        self.assertEqual(personal['plan_slot'], 'active')
        self.assertNotIn('billing', personal)
        # The newer legacy Team sample never leaks into either plan card.
        self.assertTrue(all(row['sampled_at'] < 9000.0 for row in rows))

    def test_cached_quotas_replace_the_legacy_row_and_mark_true_age(self):
        common.write_json(self.root / 'quota.json', {
            'bailian-token-plan': self._team_shared(sampled_at=9000.0),
            'deepseek': {'state': 'ok', 'sampled_at': 9000.0, 'windows': [], 'balances': []}})
        common.write_json(self.root / 'bailian-plan-quota.json', self._namespace())
        previous = [
            {'id': 'codex', 'state': 'ok', 'sampled_at': 9000.0, 'windows': [], 'balances': []},
            {'id': 'bailian-token-plan', 'state': 'ok', 'sampled_at': 9000.0,
             'windows': [{'label': '每月', 'remaining_percent': 0.0}], 'balances': []}]
        with self._edition('sequential'), patch.object(ht, 'STATE', self.root), \
                patch.object(ht, 'billing_overlay', return_value={}):
            rows = ht.cached_worker_quotas(previous)
        ids = [row['id'] for row in rows]
        self.assertEqual(ids, ['codex', 'deepseek', 'aliyun-team', 'aliyun-personal'])
        self.assertNotIn('bailian-token-plan', ids)
        self.assertNotIn('aliyun', ids)
        # Samples from 1000/1001 are genuinely old, so each card is stale.
        self.assertTrue(all(row['stale'] for row in rows if row['id'].startswith('aliyun-')))

    def test_team_edition_removes_split_rows_and_restores_single_alias(self):
        common.write_json(self.root / 'bailian-plan-quota.json', self._namespace())
        team_record = {'state': 'ok', 'sampled_at': 9000.0, 'checked_at': 9000.0,
                       'source': 'openapi_live', 'available': False,
                       'windows': [{'name': 'Credits(max)', 'limit': 424194.0, 'remaining': 0.0,
                                    'remaining_percent': 0.0, 'duration_minutes': 44640,
                                    'resets_at': '2099-11-16T10:00:00+00:00', 'valid': True}],
                       'balances': []}
        common.write_json(self.root / 'quota.json', {'bailian-token-plan': team_record})
        previous = [
            {'id': 'codex', 'state': 'ok', 'sampled_at': 9000.0, 'windows': [], 'balances': []},
            {'id': 'aliyun-team', 'state': 'ok', 'sampled_at': 9000.0,
             'windows': [{'label': '每月', 'remaining_percent': 0.0}], 'balances': []},
            {'id': 'aliyun-personal', 'state': 'ok', 'sampled_at': 9000.0,
             'windows': [{'label': '每月', 'remaining_percent': 97.48}], 'balances': []}]
        collector = ht.Collector(state_dir=self.root, codex_root=self.root / 'no-codex')
        collector.latest_quotas = previous
        with self._edition('team'), patch.object(ht, 'STATE', self.root), \
                patch.object(ht, 'billing_overlay', return_value={}):
            rows = ht.cached_worker_quotas(previous)
            board = collector.board_payload()
        self.assertEqual([row['id'] for row in rows], ['codex', 'bailian-token-plan'])
        self.assertEqual([q['id'] for q in board['quotas']], ['codex', 'aliyun'])
        self.assertEqual(board['schema_version'], 2)

    def test_board_v2_carries_both_plan_cards_with_schema_two(self):
        common.write_json(self.root / 'quota.json', {
            'bailian-token-plan': self._team_shared(sampled_at=9000.0)})
        common.write_json(self.root / 'bailian-plan-quota.json', self._namespace())
        collector = ht.Collector(state_dir=self.root, codex_root=self.root / 'no-codex')
        collector.latest_quotas = [
            {'id': 'codex', 'state': 'ok', 'sampled_at': 9000.0, 'windows': [], 'balances': []}]
        with self._edition('sequential'), patch.object(ht, 'STATE', self.root), \
                patch.object(ht, 'billing_overlay', return_value={}):
            board = collector.board_payload()
        self.assertEqual(board['schema_version'], 2)
        ids = [q['id'] for q in board['quotas']]
        self.assertEqual(ids, ['codex', 'aliyun-team', 'aliyun-personal'])
        self.assertLessEqual(len(ids), 8)
        personal = next(q for q in board['quotas'] if q['id'] == 'aliyun-personal')
        self.assertEqual(personal['name'], '阿里个人')
        self.assertEqual(personal['windows'][0]['remaining_percent'], 97.48)
        self.assertNotIn('remaining', personal['windows'][0])

    def test_sequential_without_bound_namespace_is_unknown_not_team_zero(self):
        common.write_json(self.root / 'quota.json', {
            'bailian-token-plan': self._team_shared(sampled_at=9000.0)})
        with self._edition('sequential'), patch.object(ht, 'STATE', self.root):
            rows = ht.worker_rows('bailian-token-plan', self._team_shared())
        self.assertEqual([row['id'] for row in rows], ['aliyun-team', 'aliyun-personal'])
        self.assertEqual([row['state'] for row in rows], ['unknown', 'unknown'])
        self.assertEqual([row['available'] for row in rows], [None, None])
        self.assertEqual([row['windows'] for row in rows], [[], []])
        # A conflicting identity binding is not a bound namespace record either.
        broken = self._namespace()
        broken['_credential'] = 'c' * 64
        common.write_json(self.root / 'bailian-plan-quota.json', broken)
        with self._edition('sequential'), patch.object(ht, 'STATE', self.root):
            rows = ht.worker_rows('bailian-token-plan', self._team_shared())
        self.assertEqual([row['state'] for row in rows], ['unknown', 'unknown'])

    def test_partial_plan_failure_stays_on_its_own_card(self):
        namespace = self._namespace()
        namespace['plan_quotas']['team'] = {
            'state': 'unknown', 'source': 'plan_sequence', 'plan_edition': 'team',
            'available': None, 'windows': [], 'balances': [], 'stale': True}
        common.write_json(self.root / 'bailian-plan-quota.json', namespace)
        with self._edition('sequential'), patch.object(ht, 'STATE', self.root):
            team, personal = ht.worker_rows('bailian-token-plan', self._team_shared())
        self.assertEqual(team['state'], 'unknown')
        self.assertTrue(team['stale'])
        self.assertEqual(team['windows'], [])
        self.assertEqual(personal['state'], 'ok')
        self.assertTrue(personal['available'])
        self.assertNotIn('stale', personal)  # Team failure is never inherited
        self.assertEqual(personal['windows'][0]['remaining_percent'], 97.48)

    def test_plan_edition_cache_is_bounded_and_fails_closed(self):
        import bailian_quota
        with patch.object(ht, '_PLAN_EDITION_CACHE', {'edition': 'team', 'checked_at': 0.0}), \
                patch.object(bailian_quota, 'plan_edition',
                             return_value='sequential') as plan_edition:
            self.assertEqual(ht._cached_plan_edition(), 'sequential')
            self.assertEqual(ht._cached_plan_edition(), 'sequential')
        self.assertEqual(plan_edition.call_count, 1)
        with patch.object(ht, '_PLAN_EDITION_CACHE', {'edition': 'sequential', 'checked_at': 0.0}), \
                patch.object(bailian_quota, 'plan_edition', side_effect=RuntimeError('no config')):
            self.assertEqual(ht._cached_plan_edition(), 'team')
        with patch.object(ht, '_PLAN_EDITION_CACHE',
                          {'edition': 'sequential', 'checked_at': time.time()}), \
                patch.object(bailian_quota, 'plan_edition',
                             side_effect=AssertionError('must not read')):
            self.assertEqual(ht._cached_plan_edition(), 'sequential')


    def test_old_self_consistent_identity_is_not_current_display_binding(self):
        old = self._namespace(binding='c' * 64)
        common.write_json(self.root / ht.PLAN_CACHE_FILE, old)
        with self._edition('sequential'), patch.object(ht, 'STATE', self.root):
            rows = ht.worker_rows(ht.BAILIAN_PROVIDER, old)
        self.assertEqual([row['state'] for row in rows], ['unknown', 'unknown'])
        del old['_display_identity']
        common.write_json(self.root / ht.PLAN_CACHE_FILE, old)
        with self._edition('sequential'), patch.object(ht, 'STATE', self.root):
            rows = ht.worker_rows(ht.BAILIAN_PROVIDER, old)
        self.assertEqual([row['windows'] for row in rows], [[], []])

    def test_dedicated_namespace_works_without_shared_file(self):
        common.write_json(self.root / ht.PLAN_CACHE_FILE, self._namespace())
        with self._edition('sequential'), patch.object(ht, 'STATE', self.root), \
                patch.object(ht, 'billing_overlay', return_value={}):
            rows = ht.cached_worker_quotas([{'id': 'codex', 'windows': [], 'balances': []}])
        self.assertEqual([r['id'] for r in rows], ['codex', 'aliyun-team', 'aliyun-personal'])
        self.assertTrue(rows[-1]['available'])

    def test_config_stat_invalidates_edition_cache_immediately(self):
        config_file = self.root / 'config.json'
        common.write_json(config_file, {'bailian_plan_edition': 'sequential'})
        with patch.object(common, 'CONFIG', config_file), \
                patch.object(ht, '_PLAN_EDITION_CACHE', {'edition': 'team', 'checked_at': 0.0}):
            self.assertEqual(ht._cached_plan_edition(), 'sequential')
            common.write_json(config_file, {'bailian_plan_edition': 'team'})
            self.assertEqual(ht._cached_plan_edition(), 'team')


class KimiWindowOrderTests(unittest.TestCase):
    """The four real account windows must never lose to legacy ordering."""

    @staticmethod
    def _base(minutes=300, name=None, remaining=10.0):
        row = {'valid': True, 'remaining_percent': remaining, 'duration_minutes': minutes}
        if name:
            row['name'] = name
        return row

    def test_four_verified_account_windows_survive_legacy_five_window_source(self):
        now = time.time()
        record = {
            'state': 'ok', 'sampled_at': now, 'available': True, 'balances': [],
            'windows': [self._base(300, 'window_0', 10.0),
                        self._base(10080, 'overall', 20.0),
                        self._base(1440, 'legacy-daily', 30.0),
                        self._base(1440, 'legacy-extra-a', 40.0),
                        self._base(None, 'legacy-extra-b', 50.0)],
            'account': {'state': 'ok', 'sampled_at': now, 'stale': False,
                        'account_binding': {'state': 'verified'},
                        'windows': [
                            {'name': 'limit5h', 'kind': 'short', 'remaining_percent': 14.92,
                             'duration_minutes': 300, 'valid': True},
                            {'name': 'limit7d', 'kind': 'weekly', 'remaining_percent': 44.64,
                             'duration_minutes': 10080, 'valid': True},
                            {'name': 'monthTotal', 'kind': 'month_total',
                             'remaining_percent': 79.05, 'valid': True,
                             'expires_at': '2099-11-19T00:00:00Z'},
                            {'name': 'monthCode', 'kind': 'month_code',
                             'remaining_percent': 88.5, 'valid': True,
                             'expires_at': '2099-11-19T00:00:00Z'}]}}
        row = ht.normalize_worker('kimi-for-coding', record)
        self.assertEqual([w['kind'] for w in row['windows']],
                         ['short', 'weekly', 'month_total', 'month_code'])
        self.assertEqual([w['remaining_percent'] for w in row['windows']],
                         [14.92, 44.64, 79.05, 88.5])
        self.assertEqual(row['sampled_at'], now)

    def test_expired_account_rows_keep_the_endpoint_source_and_legacy_counts(self):
        now = time.time()
        record = {
            'state': 'ok', 'sampled_at': now, 'available': True, 'balances': [],
            'windows': [self._base(300, 'window_0', 10.0), self._base(10080, 'overall', 20.0)],
            'account': {'state': 'ok', 'sampled_at': now, 'stale': False,
                        'account_binding': {'state': 'verified'},
                        'windows': [
                            {'name': 'monthTotal', 'kind': 'month_total',
                             'remaining_percent': 5.0, 'valid': True,
                             'expires_at': '2020-01-01T00:00:00Z'},
                            {'name': 'limit5h', 'kind': 'short', 'remaining_percent': 12.0,
                             'duration_minutes': 300, 'resets_at': '2020-01-01T00:00:00Z',
                             'valid': True}]}}
        row = ht.normalize_worker('kimi-for-coding', record)
        self.assertEqual([w['kind'] for w in row['windows']], ['short', 'weekly'])
        self.assertEqual(row['windows'][0]['remaining_percent'], 10.0)
        self.assertEqual(row['windows'][1]['remaining_percent'], 20.0)
        # Legacy three-window and five-window sources stay renderable.
        legacy = {'state': 'ok', 'sampled_at': now, 'available': True, 'balances': [],
                  'windows': [self._base(300, 'window_0', 11.0),
                              self._base(10080, 'overall', 21.0),
                              self._base(43200, 'monthly', 31.0)]}
        legacy_row = ht.normalize_worker('kimi-for-coding', legacy)
        self.assertEqual(len(legacy_row['windows']), 3)
        self.assertEqual(legacy_row['windows'][2]['label'], '每月')
        five = dict(legacy, windows=[self._base(300, 'window_0', 11.0),
                                     self._base(10080, 'overall', 21.0),
                                     self._base(1440, 'day-a', 31.0),
                                     self._base(1440, 'day-b', 41.0),
                                     self._base(None, 'extra', 51.0)])
        five_row = ht.normalize_worker('kimi-for-coding', five)
        self.assertEqual(len(five_row['windows']), 4)
        self.assertEqual([w['label'] for w in five_row['windows']][:2], ['5小时', '每周'])


def subprocess_completed(stdout):
    import subprocess
    return subprocess.CompletedProcess(['x'], 0, stdout, '')


if __name__ == '__main__':
    unittest.main()
