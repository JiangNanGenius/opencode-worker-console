"""Service lifecycle ordering: OpenViking stays up until OpenCode has disposed.

The OpenCode memory plugin commits sessions and captures turns while it shuts
down, so stopping the memory service first would lose those writes. These tests
pin the order and the recovery behavior when a managed stop does not complete.
"""
import sys
import types
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import service


class StopOrderingTests(unittest.TestCase):
    def test_auto_start_honors_maintenance_pause_without_restarting_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            (state / 'logs').mkdir()
            calls = []
            config = {'memory': {'enabled': False}, 'opencode_binary': '/tmp/opencode',
                      'console_url': 'http://127.0.0.1:51528'}
            def read(path, default=None):
                return {'paused_services': ['pool', 'server']} if path.name == 'maintenance.json' else {}
            def launch(argv, **kwargs):
                calls.append(argv)
                return types.SimpleNamespace(pid=123)
            bootstrap = types.SimpleNamespace(ensure_opencode=lambda path: path)
            with patch.object(service, 'STATE', state), patch.object(service, 'init'), \
                 patch.object(service, 'config', return_value=config), \
                 patch.object(service, 'read_json', side_effect=read), \
                 patch.object(service, 'identity', return_value='identity'), \
                 patch.object(service.subprocess, 'Popen', side_effect=launch), \
                 patch.dict(sys.modules, {'bootstrap': bootstrap}):
                result = service.start()
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0][1].endswith('console.py'))
            self.assertEqual(set(result['services']), {'console'})

    def _stop(self, alive_names, drained=None, ov_running=True):
        calls = []
        records = {name: {'name': name, 'pid': 100 + index}
                   for index, name in enumerate(('console', 'pool', 'server'))}
        live = {'console', 'pool', 'server'}

        def fake_alive(entry):
            return bool(entry) and entry.get('name') in live

        def kill(pid, sig):
            calls.append(('kill', pid))
            name = next(key for key, value in records.items() if value['pid'] == pid)
            live.discard(name)

        module = types.SimpleNamespace()

        def running_record():
            calls.append(('ov-record', None))
            return {'pid': 999} if ov_running else None

        def drain(timeout=120):
            calls.append(('ov-drain', timeout))
            return drained or {'settled': True}

        def stop():
            calls.append(('ov-stop', None))
            return {'running': False}

        module.running_record = running_record
        module.drain = drain
        module.stop = stop
        with patch.object(service, 'tasks', return_value=[]), \
             patch.object(service, 'read_json', return_value=records), \
             patch.object(service, 'alive', side_effect=fake_alive), \
             patch.object(service, 'config', return_value={'memory': {'backend': 'openviking'}}), \
             patch.object(service.os, 'kill', side_effect=kill), \
             patch.object(service.time, 'sleep', return_value=None), \
             patch.dict(sys.modules, {'openviking_service': module}):
            result = service.stop()
        return result, calls

    def test_openviking_stops_after_managed_services(self):
        result, calls = self._stop(alive_names={'console', 'pool', 'server'})
        self.assertEqual(result['services'], 'stopped')
        last_kill = max(index for index, call in enumerate(calls) if call[0] == 'kill')
        drain_index = calls.index(('ov-drain', 120))
        stop_index = calls.index(('ov-stop', None))
        self.assertLess(last_kill, drain_index)
        self.assertLess(drain_index, stop_index)

    def test_openviking_is_not_stopped_when_a_service_will_not_exit(self):
        calls = []
        records = {'server': {'name': 'server', 'pid': 101}}
        module = types.SimpleNamespace()

        def forbidden(*args, **kwargs):
            raise AssertionError('OpenViking must not be touched while OpenCode is stopping')

        module.running_record = forbidden
        module.drain = forbidden
        module.stop = forbidden
        with patch.object(service, 'tasks', return_value=[]), \
             patch.object(service, 'read_json', return_value=records), \
             patch.object(service, 'alive', side_effect=lambda entry: bool(entry) and entry.get('name') == 'server'), \
             patch.object(service, 'config', return_value={'memory': {'backend': 'openviking'}}), \
             patch.object(service.os, 'kill', return_value=None), \
             patch.object(service.time, 'sleep', return_value=None), \
             patch.dict(sys.modules, {'openviking_service': module}):
            with self.assertRaises(RuntimeError):
                service.stop()

    def test_unsettled_commits_keep_openviking_running(self):
        with self.assertRaises(RuntimeError):
            self._stop(alive_names={'console', 'pool', 'server'},
                       drained={'settled': False, 'pending': 2, 'running': 1})

    def test_legacy_backend_leaves_openviking_untouched(self):
        calls = []
        module = types.SimpleNamespace()

        def touched(*args, **kwargs):
            calls.append('touched')
            return {'pid': 999}

        module.running_record = touched
        module.drain = touched
        module.stop = touched
        with patch.object(service, 'tasks', return_value=[]), \
             patch.object(service, 'read_json', return_value={}), \
             patch.object(service, 'alive', return_value=False), \
             patch.object(service, 'config', return_value={'memory': {'backend': 'opencode-mem'}}), \
             patch.dict(sys.modules, {'openviking_service': module}):
            service.stop()
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
