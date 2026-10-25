from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import delegate
import worker


class MaintenanceBarrierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        for module in (common, delegate, worker):
            p = patch.object(module, 'STATE', self.state)
            p.start()
            self.addCleanup(p.stop)

    def test_direct_daemon_start_cannot_observe_paused_checkpoints(self):
        common.write_json(self.state / 'maintenance.json', {'paused_services': ['pool']})
        with patch.object(delegate, 'run_task') as run, patch.object(delegate.quota, 'view') as view:
            self.assertIsNone(delegate.daemon())
        run.assert_not_called()
        view.assert_not_called()
        self.assertFalse((self.state / 'daemon.lock').exists())
        self.assertFalse((self.state / 'heartbeat.json').exists())

    def test_capacity_resume_never_dispatches_through_pool_maintenance(self):
        common.write_json(self.state / 'maintenance.json', {'paused_services': ['pool']})
        with patch.object(worker, 'call') as call, patch.object(worker, 'task') as task:
            self.assertTrue(worker.resume_capacity_wait({'id': 'job-checkpoint'}, [], {'type': 'idle'}))
        call.assert_not_called()
        task.assert_not_called()

    def test_running_daemon_observer_stops_when_barrier_appears(self):
        calls = {'maintenance': 0}
        def read(path, default=None):
            if Path(path).name == 'maintenance.json':
                calls['maintenance'] += 1
                return {} if calls['maintenance'] == 1 else {'paused_services': ['pool']}
            return {} if default is None else default
        with patch.object(delegate, 'read_json', side_effect=read), \
                patch.object(delegate.quota, 'view', return_value={}), \
                patch.object(delegate.signal, 'signal'), \
                patch.object(delegate, 'run_task') as run, \
                patch.object(delegate, 'config') as config:
            delegate.daemon()
        self.assertEqual(calls['maintenance'], 2)
        run.assert_not_called()
        config.assert_not_called()


if __name__ == '__main__':
    unittest.main()
