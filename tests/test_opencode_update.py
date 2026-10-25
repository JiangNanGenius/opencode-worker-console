import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import opencode_update


class OpenCodeUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'opencode_binary': '/old/opencode',
                                           'opencode_updates': {'enabled': True,
                                                                'check_interval_hours': 24}}))
        self.patchers = [patch.object(common, 'STATE', self.state),
                         patch.object(common, 'CONFIG', self.config),
                         patch.object(opencode_update, 'STATE', self.state),
                         patch.object(opencode_update, 'STATUS', self.state / 'opencode-update.json'),
                         patch.object(opencode_update, 'CONFIG', self.config),
                         patch.object(opencode_update, 'sessions_busy', return_value=False)]
        for item in self.patchers:
            item.start()
        common.init()

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.tmp.cleanup()

    def status_value(self):
        return json.loads((self.state / 'opencode-update.json').read_text())

    def write_status(self, **fields):
        value = {'time': time.time(), **fields}
        (self.state / 'opencode-update.json').write_text(json.dumps(value))
        return value

    def add_task(self, task_id, status):
        common.write_json(common.task_path(task_id), {'id': task_id, 'created_at': 1,
                                                      'status': status})

    def maintenance(self):
        path = self.state / 'maintenance.json'
        return json.loads(path.read_text()) if path.exists() else None

    # -- settings and checks ---------------------------------------------
    def test_settings_default_daily_and_validated(self):
        self.assertEqual(opencode_update.settings(), {'enabled': True, 'check_interval_hours': 24})
        self.assertEqual(opencode_update.validate({'enabled': False, 'check_interval_hours': 12}),
                         {'enabled': False, 'check_interval_hours': 12})
        with self.assertRaises(ValueError):
            opencode_update.validate({'enabled': True, 'check_interval_hours': 0})

    def test_check_reports_available_without_installing(self):
        with patch.object(opencode_update, 'current_version', return_value='1.18.30'), \
             patch.object(opencode_update, 'latest_version', return_value='1.18.32'), \
             patch.object(opencode_update, 'install_managed') as install:
            result = opencode_update.run(check_only=True)
        self.assertEqual(result['state'], 'update_available')
        install.assert_not_called()

    def test_failed_check_backs_off_and_daemon_retries_when_due(self):
        with patch.object(opencode_update, 'current_version', return_value='1.18.30'), \
             patch.object(opencode_update, 'latest_version', side_effect=RuntimeError('offline')):
            result = opencode_update.run(check_only=True)
        self.assertEqual(result['state'], 'check_failed')
        self.assertGreater(result['next_check_at'], time.time() + 3000)
        # A daemon retry before the backoff expires must not touch the network.
        with patch.object(opencode_update, 'current_version', return_value='1.18.30'), \
             patch.object(opencode_update, 'latest_version', side_effect=AssertionError('network used')), \
             patch.object(opencode_update, 'install_managed') as install:
            waiting = opencode_update.run()
        self.assertEqual(waiting['state'], 'check_failed')
        install.assert_not_called()
        # After the backoff it queries again (a running task defers only the cutover).
        saved = self.status_value()
        saved['next_check_at'] = time.time() - 1
        (self.state / 'opencode-update.json').write_text(json.dumps(saved))
        self.add_task('job-running', 'running')
        with patch.object(opencode_update, 'current_version', return_value='1.18.30'), \
             patch.object(opencode_update, 'latest_version', return_value='1.18.32'), \
             patch.object(opencode_update, 'install_managed', return_value='/managed/1.18.32/opencode'):
            result = opencode_update.run()
        self.assertEqual(result['state'], 'waiting_for_idle')
        self.assertEqual(result['latest'], '1.18.32')

    def test_daily_interval_queries_registry_even_when_staged_target_is_current(self):
        self.write_status(state='updated', latest='1.19.3', staged=True,
                          checked_at=time.time() - 25 * 3600)
        self.add_task('job-running', 'running')
        with patch.object(opencode_update, 'current_version', return_value='1.19.3'), \
             patch.object(opencode_update, '_staged_binary', return_value='/managed/1.19.3/opencode'), \
             patch.object(opencode_update, 'latest_version', return_value='1.19.4') as latest, \
             patch.object(opencode_update, 'install_managed', return_value='/managed/1.19.4/opencode'):
            result = opencode_update.run()
        latest.assert_called_once()
        self.assertEqual(result['latest'], '1.19.4')
        self.assertEqual(result['state'], 'waiting_for_idle')

    def test_pending_newer_staged_target_reuses_local_without_registry(self):
        self.write_status(state='waiting_for_idle', latest='1.19.3', checked_at=time.time() - 3600,
                          local_retry_at=time.time() - 1)
        self.add_task('job-running', 'running')
        with patch.object(opencode_update, 'current_version', return_value='1.19.2'), \
             patch.object(opencode_update, '_staged_binary', return_value='/managed/1.19.3/opencode'), \
             patch.object(opencode_update, 'latest_version', side_effect=AssertionError('network used')), \
             patch.object(opencode_update, 'install_managed', return_value='/managed/1.19.3/opencode'):
            result = opencode_update.run()
        self.assertEqual(result['state'], 'waiting_for_idle')
        self.assertEqual(result['reason'], 'worker_tasks_active')
        self.assertEqual(result['latest'], '1.19.3')

    def test_daily_boundary_rechecks_even_with_newer_staged_target(self):
        # Current 1.19.2, staged 1.19.3, but the daily interval elapsed: one
        # registry query must find and stage the newest stable 1.19.4.
        self.write_status(state='waiting_for_idle', latest='1.19.3', checked_at=time.time() - 25 * 3600,
                          local_retry_at=time.time() - 1)
        self.add_task('job-running', 'running')
        with patch.object(opencode_update, 'current_version', return_value='1.19.2'), \
             patch.object(opencode_update, '_staged_binary', return_value='/managed/1.19.3/opencode'), \
             patch.object(opencode_update, 'latest_version', return_value='1.19.4') as latest, \
             patch.object(opencode_update, 'install_managed', return_value='/managed/1.19.4/opencode') as install:
            result = opencode_update.run()
        latest.assert_called_once()
        install.assert_called_once_with('1.19.4')
        self.assertEqual(result['state'], 'waiting_for_idle')
        self.assertEqual(result['latest'], '1.19.4')
        self.assertEqual(common.task('job-running')['status'], 'running')

    # -- staged cutover ---------------------------------------------------
    def test_apply_stages_while_pool_is_busy_and_defers_cutover(self):
        self.add_task('job-running', 'running')
        with patch.object(opencode_update, 'current_version', return_value='1.18.30'), \
             patch.object(opencode_update, 'latest_version', return_value='1.18.32'), \
             patch.object(opencode_update, 'install_managed', return_value='/managed/1.18.32/opencode') as install, \
             patch('service.reload_runtime') as reload_runtime:
            result = opencode_update.run()
        self.assertEqual(result['state'], 'waiting_for_idle')
        self.assertEqual(result['reason'], 'worker_tasks_active')
        install.assert_called_once()
        reload_runtime.assert_not_called()
        self.assertEqual(json.loads(self.config.read_text())['opencode_binary'], '/old/opencode')
        self.assertIsNone(self.maintenance())
        self.assertTrue(self.status_value()['staged'])

    def test_apply_switches_binary_when_idle_and_verifies_live_version(self):
        with patch.object(opencode_update, 'current_version', return_value='1.18.30'), \
             patch.object(opencode_update, 'latest_version', return_value='1.18.32'), \
             patch.object(opencode_update, 'live_version', return_value='1.18.32'), \
             patch.object(opencode_update, 'install_managed', return_value='/managed/1.18.32/opencode'), \
             patch('service.reload_runtime', return_value={'ok': True}):
            result = opencode_update.run()
        self.assertEqual(result['state'], 'updated')
        self.assertEqual(json.loads(self.config.read_text())['opencode_binary'],
                         '/managed/1.18.32/opencode')
        self.assertIsNone(self.maintenance())

    def test_queued_handoff_reserves_barrier_and_preserves_queue(self):
        self.add_task('job-queued', 'queued')
        with patch.object(opencode_update, 'current_version', return_value='1.18.30'), \
             patch.object(opencode_update, 'latest_version', return_value='1.18.32'), \
             patch.object(opencode_update, 'live_version', return_value='1.18.32'), \
             patch.object(opencode_update, 'install_managed', return_value='/managed/1.18.32/opencode'), \
             patch('service.reload_runtime', return_value={'ok': True}) as reload_runtime:
            result = opencode_update.run()
        self.assertEqual(result['state'], 'updated')
        reload_runtime.assert_called_once_with(allow_queued=True, maintenance_held=True)
        self.assertEqual(common.task('job-queued')['status'], 'queued')
        self.assertIsNone(self.maintenance())

    def test_rollback_restores_old_binary_when_live_version_mismatches(self):
        with patch.object(opencode_update, 'current_version', return_value='1.18.30'), \
             patch.object(opencode_update, 'latest_version', return_value='1.18.32'), \
             patch.object(opencode_update, 'live_version', return_value='1.18.30'), \
             patch.object(opencode_update, 'install_managed', return_value='/managed/1.18.32/opencode'), \
             patch('service.reload_runtime', return_value={'ok': True}) as reload_runtime:
            result = opencode_update.run()
        self.assertEqual(result['state'], 'rollback_restored')
        self.assertEqual(json.loads(self.config.read_text())['opencode_binary'], '/old/opencode')
        self.assertEqual(reload_runtime.call_count, 2)
        self.assertIsNone(self.maintenance())

    def test_rollback_restores_old_binary_when_restart_fails(self):
        with patch.object(opencode_update, 'current_version', return_value='1.18.30'), \
             patch.object(opencode_update, 'latest_version', return_value='1.18.32'), \
             patch.object(opencode_update, 'install_managed', return_value='/managed/1.18.32/opencode'), \
             patch('service.reload_runtime', side_effect=[RuntimeError('health timeout'),
                                                          {'ok': True}]):
            result = opencode_update.run()
        self.assertEqual(result['state'], 'rollback_restored')
        self.assertEqual(json.loads(self.config.read_text())['opencode_binary'], '/old/opencode')
        self.assertIsNone(self.maintenance())

    # -- maintenance leases ----------------------------------------------
    def test_stale_bridge_lease_is_recovered(self):
        (self.state / 'maintenance.json').write_text(json.dumps(
            {'time': time.time() - 2 * 3600, 'reason': 'opencode_update',
             'pid': 2 ** 30, 'from': '1.18.30', 'to': '1.18.32'}))
        with patch.object(opencode_update, 'current_version', return_value='1.18.30'), \
             patch.object(opencode_update, 'latest_version', return_value='1.18.32'), \
             patch.object(opencode_update, 'live_version', return_value='1.18.32'), \
             patch.object(opencode_update, 'install_managed', return_value='/managed/1.18.32/opencode'), \
             patch('service.reload_runtime', return_value={'ok': True}):
            result = opencode_update.run()
        self.assertEqual(result['state'], 'updated')
        self.assertIsNone(self.maintenance())

    def test_foreign_lease_is_preserved(self):
        (self.state / 'maintenance.json').write_text(json.dumps(
            {'time': time.time(), 'reason': None, 'paused_services': ['pool']}))
        with patch.object(opencode_update, 'current_version', return_value='1.18.30'), \
             patch.object(opencode_update, 'latest_version', return_value='1.18.32'), \
             patch.object(opencode_update, 'install_managed', return_value='/managed/1.18.32/opencode'), \
             patch('service.reload_runtime') as reload_runtime:
            result = opencode_update.run()
        self.assertEqual(result['state'], 'waiting_for_idle')
        self.assertEqual(result['reason'], 'maintenance_active')
        reload_runtime.assert_not_called()
        self.assertTrue((self.state / 'maintenance.json').exists())

    # -- due() cadence ----------------------------------------------------
    def test_due_uses_local_retry_cadence_and_backoff(self):
        future = time.time() + 300
        self.write_status(state='waiting_for_idle', local_retry_at=future)
        self.assertFalse(opencode_update.due())
        self.write_status(state='waiting_for_idle', local_retry_at=time.time() - 1)
        self.assertTrue(opencode_update.due())
        self.write_status(state='check_failed', next_check_at=future, checked_at=time.time() - 10 * 3600)
        self.assertFalse(opencode_update.due())
        self.write_status(state='check_failed', next_check_at=time.time() - 1, checked_at=time.time() - 10 * 3600)
        self.assertTrue(opencode_update.due())
        self.write_status(state='up_to_date', checked_at=time.time() - 23 * 3600)
        self.assertFalse(opencode_update.due())
        self.write_status(state='up_to_date', checked_at=time.time() - 25 * 3600)
        self.assertTrue(opencode_update.due())


if __name__ == '__main__': unittest.main()
