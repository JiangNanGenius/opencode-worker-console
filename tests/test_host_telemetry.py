"""Collector tests: acquisition, retention, unplug, staleness, errors, board v2.

Everything is mocked at module boundaries; no real sensors, daemons or
provider CLIs are touched.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
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
        with patch.object(ht, 'STATE', self.root), \
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


def subprocess_completed(stdout):
    import subprocess
    return subprocess.CompletedProcess(['x'], 0, stdout, '')


if __name__ == '__main__':
    unittest.main()
