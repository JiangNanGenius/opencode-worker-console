"""Structured control tests: validation, replay protection, PID reuse, audit.

All platform effects (daemon IPC, sudo helper, process signals, ps/pmset
output) are mocked; no real process is ever signalled.
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
import host_controls as hc


def completed(stdout='', returncode=0, stderr=''):
    import subprocess
    return subprocess.CompletedProcess(['x'], returncode, stdout, stderr)


class ControlsTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / 'tasks').mkdir(parents=True)
        self.patches = [patch.object(common, 'STATE', self.root),
                        patch.object(ht, 'STATE', self.root)]
        for item in self.patches:
            item.start()
        common.write_json(self.root / 'tasks' / 'job-active.json',
                          {'id': 'job-active', 'status': 'running', 'created_at': 1,
                           'started_at': time.time() - 10})

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()


FAN_STATUS = {'available': True, 'mode': 'curve', 'profile': 'Balanced',
              'fans': [{'name': 'Fan 0', 'rpm': 1384, 'target': 1382.5,
                        'min_rpm': 1000, 'max_rpm': 4900, 'safety_engaged': False}],
              'safety_engaged': False, 'rpm_min': 1000, 'rpm_max': 4900,
              'profiles': ['Silent', 'Balanced', 'Cool', 'Max'], 'thermal_pressure': 'nominal'}


def daemon_router(status=None, profiles=None):
    """Mock daemon_request: record payloads, simulate profile/mode application."""
    calls = []
    state = {'mode': status['mode'], 'profile': status['profile']}

    def fake(payload, timeout=1.5):
        calls.append(payload)
        command = payload.get('cmd')
        if command == 'applyProfile':
            state['profile'] = payload.get('profileName')
            return {'ok': True}
        if command == 'setControlMode':
            state['mode'] = payload.get('controlMode')
            return {'ok': True}
        if command == 'setTarget':
            return {'ok': True}
        if command == 'status':
            fan = (status.get('fans') or [{}])[0]
            return {'ok': True, 'status': {
                'controlMode': state['mode'], 'activeProfileName': state['profile'],
                'thermalPressure': 'nominal', 'safetyEngaged': False,
                'fans': [{'info': {'index': 0, 'name': fan.get('name', 'Fan 0'),
                                   'actualRPM': fan.get('rpm', 1384),
                                   'minRPM': fan.get('min_rpm', 1000),
                                   'maxRPM': fan.get('max_rpm', 4900)},
                          'commandedRPM': fan.get('target'), 'safetyEngaged': False}],
                'groups': [{'group': 'cpu', 'max': 50.0, 'mean': 45.0, 'count': 23},
                           {'group': 'gpu', 'max': 45.0, 'mean': 42.0, 'count': 42}]}}
        if command == 'listProfiles':
            return {'ok': True, 'profiles': [{'name': n, 'points': []} for n in
                                             (profiles or ['Silent', 'Balanced', 'Cool', 'Max'])]}
        return {'ok': True}

    return fake, calls


class FanControlTests(ControlsTestBase):
    def test_profile_allowlist_and_safety_never_touched(self):
        fake, calls = daemon_router(status=FAN_STATUS)
        hc.sent_commands.clear()
        with patch.object(ht, 'daemon_request', side_effect=fake):
            state = hc.set_fan_profile('Cool')
            self.assertEqual(state['profile'], 'Cool')
            with self.assertRaises(ValueError):
                hc.set_fan_profile('Turbo')
            hc.set_fan_auto()
        self.assertIn('applyProfile', hc.sent_commands)
        self.assertIn('setControlMode', hc.sent_commands)
        self.assertNotIn('setSafety', hc.sent_commands)
        self.assertNotIn('saveProfile', hc.sent_commands)
        self.assertNotIn('deleteProfile', hc.sent_commands)
        for payload in calls:
            self.assertIn(payload['cmd'], {'status', 'listProfiles'} | hc.FAN_WRITE_COMMANDS)

    def test_rpm_bounds_validation_and_idempotent_replay(self):
        fixed = dict(FAN_STATUS, mode='fixed', fans=[dict(FAN_STATUS['fans'][0], target=2000)])
        fake, calls = daemon_router(status=fixed)
        hc.sent_commands.clear()
        with patch.object(ht, 'daemon_request', side_effect=fake):
            with self.assertRaises(ValueError):
                hc.set_fan_rpm(6000)
            with self.assertRaises(ValueError):
                hc.set_fan_rpm(500)
            hc.set_fan_rpm(2000)  # already pinned at 2000 → no write
        writes = [c for c in calls if c['cmd'] in hc.FAN_WRITE_COMMANDS]
        self.assertEqual(writes, [])
        self.assertNotIn('setTarget', hc.sent_commands)

    def test_rpm_write_sends_fixed_mode_then_target(self):
        fake, calls = daemon_router(status=FAN_STATUS)
        with patch.object(ht, 'daemon_request', side_effect=fake):
            hc.set_fan_rpm(2400)
        commands = [c['cmd'] for c in calls if c['cmd'] in hc.FAN_WRITE_COMMANDS]
        self.assertEqual(commands, ['setControlMode', 'setTarget'])
        target = [c for c in calls if c['cmd'] == 'setTarget'][0]
        self.assertEqual(target['rpm'], 2400)

    def test_fan_unavailable_raises_runtime(self):
        with patch.object(ht, 'daemon_request', side_effect=OSError('no socket')):
            self.assertFalse(hc.fan_status()['available'])
            with self.assertRaises(RuntimeError):
                hc.set_fan_rpm(2000)


class PowerModeTests(ControlsTestBase):
    CAP = 'Capabilities for AC Power:\n displaysleep\n lowpowermode\n highpowermode\n'
    CUSTOM = 'AC Power:\n lowpowermode 1\n highpowermode 0\n'

    def _run_router(self):
        def fake(args, timeout=5, env=None):
            if args[:3] == ['pmset', '-g', 'cap']:
                return completed(self.CAP)
            if args[:3] == ['pmset', '-g', 'custom']:
                return completed(self.CUSTOM)
            return completed('')
        return fake

    def test_capabilities_parsed_from_pmset(self):
        with patch.object(hc.sys, 'platform', 'darwin'), \
                patch.object(hc.shutil, 'which', side_effect=lambda name: '/usr/bin/pmset' if name == 'pmset' else None), \
                patch.object(hc, '_run', side_effect=self._run_router()):
            caps = hc.power_capabilities()
        self.assertEqual(caps['supported'], ['lowpowermode', 'highpowermode'])
        self.assertEqual(caps['current'], {'lowpowermode': 1, 'highpowermode': 0})

    def test_unsupported_platform_reports_empty(self):
        with patch.object(hc.sys, 'platform', 'linux'):
            self.assertEqual(hc.power_capabilities(), {'supported': [], 'current': {}})

    def test_set_mode_requires_supported_and_verified_helper(self):
        with patch.object(hc.sys, 'platform', 'darwin'), \
                patch.object(hc.shutil, 'which', side_effect=lambda name: '/usr/bin/pmset' if name == 'pmset' else None), \
                patch.object(hc, '_run', side_effect=self._run_router()), \
                patch.object(hc, 'helper_status', return_value={'installed': True, 'verified': False}):
            with self.assertRaises(ValueError):
                hc.set_power_mode('diskssleep', True)
            with self.assertRaises(RuntimeError):
                hc.set_power_mode('lowpowermode', False)
            with patch.object(hc, 'helper_status', return_value={'installed': True, 'verified': True}), \
                    patch.object(hc, '_helper_run', return_value={'ok': True}) as helper:
                after = hc.set_power_mode('lowpowermode', False)
        helper.assert_called_once_with(['power-mode', 'lowpowermode', '0'])
        self.assertEqual(after['current']['lowpowermode'], 1)  # readback router is static


class CaffeinateTests(ControlsTestBase):
    def setUp(self):
        super().setUp()
        identity_patch = patch('service.identity',return_value='test identity')
        identity_patch.start()
        self.addCleanup(identity_patch.stop)

    def _which(self, name):
        return '/usr/bin/caffeinate' if name == 'caffeinate' else None

    def test_timed_lifecycle_and_expiry(self):
        class FakeProc:
            pid = 43210

        with patch.object(hc.shutil, 'which', side_effect=self._which), \
                patch.object(hc.subprocess, 'Popen', return_value=FakeProc()), \
                patch.object(hc, '_caffeinate_matches', return_value=True):
            status = hc.set_caffeinate('timed', minutes=30)
            self.assertTrue(status['active'])
            self.assertEqual(status['mode'], 'timed')
            with patch.object(hc, '_caffeinate_matches', return_value=True), \
                    patch.object(hc.os, 'kill') as kill:
                hc._caffeinate_step(now=time.time() + 31 * 60)
                kill.assert_called_once()
            self.assertEqual(hc.caffeinate_status()['mode'], 'off')

    def test_while_tasks_idle_grace(self):
        class FakeProc:
            pid = 43211

        now = time.time()
        with patch.object(hc.shutil, 'which', side_effect=self._which), \
                patch.object(hc.subprocess, 'Popen', return_value=FakeProc()):
            hc.set_caffeinate('while_tasks')
        with patch.object(hc, '_caffeinate_matches', return_value=True), \
                patch.object(hc.os, 'kill') as kill, \
                patch.object(hc, '_active_task_count', return_value=0):
            hc._caffeinate_step(now=now + 3)
            self.assertFalse(kill.called)  # inside grace period
            hc._caffeinate_step(now=now + hc.CAFFEINATE_IDLE_GRACE_SECONDS + 6)
            self.assertTrue(kill.called)
        self.assertEqual(hc.caffeinate_status()['mode'], 'off')

    def test_rejects_unknown_mode_and_bad_minutes(self):
        with patch.object(hc.shutil, 'which', side_effect=self._which):
            with self.assertRaises(ValueError):
                hc.set_caffeinate('forever')
            with self.assertRaises(ValueError):
                hc.set_caffeinate('timed', minutes=0)
            with self.assertRaises(ValueError):
                hc.set_caffeinate('timed', minutes=10 ** 6)


PS_OUTPUT = """  101 Mon Oct 13 01:00:00 2026 {uid} 12.5  3.0  100000 {name}
  102 Mon Oct 13 01:00:00 2026 {other} 99.0 50.0 2000000 otherproc
  103 Mon Oct 13 01:00:00 2026 {uid}  5.0  1.0   50000 second
""".format(uid=os.getuid(), other=(0 if os.getuid() != 0 else 1), name='workerproc')


class ProcessTests(ControlsTestBase):
    def test_top_processes_filters_other_users_and_protected(self):
        with patch.object(hc, '_run', return_value=completed(PS_OUTPUT)), \
                patch.object(hc, '_protected_pids', return_value={1, 103}):
            rows = hc.top_processes(10, 'cpu')
        self.assertEqual([r['pid'] for r in rows], [101])
        self.assertEqual(rows[0]['name'], 'workerproc')
        self.assertNotIn('argv', rows[0])

    def test_terminate_validates_identity_and_reuse(self):
        started = 1700000000
        info = {'pid': 101, 'started_at': started, 'cpu_percent': 1.0, 'mem_percent': 1.0,
                'rss_bytes': 100, 'name': 'workerproc', 'uid': os.getuid()}
        with patch.object(hc, '_process_info', return_value=info), \
                patch.object(hc, '_protected_pids', return_value={1, 103}), \
                patch.object(hc.os, 'kill') as kill:
            result = hc.terminate_process(101, started, 'term')
            self.assertEqual(result['signal'], 'term')
            kill.assert_called_once()
            with self.assertRaises(ValueError):  # stale start token → PID reused
                hc.terminate_process(101, started + 9999, 'kill')
            kill.assert_called_once()
            with self.assertRaises(ValueError):
                hc.terminate_process(103, started, 'term')  # protected
            with self.assertRaises(ValueError):
                hc.terminate_process(101, started, 'pause')  # not term/kill

    def test_terminate_rejects_other_user_and_missing(self):
        info = {'pid': 104, 'started_at': 1700000000, 'cpu_percent': 1.0, 'mem_percent': 1.0,
                'rss_bytes': 100, 'name': 'rootproc', 'uid': (0 if os.getuid() != 0 else 1)}
        with patch.object(hc, '_process_info', return_value=info), \
                patch.object(hc, '_protected_pids', return_value={1}):
            with self.assertRaises(ValueError):
                hc.terminate_process(104, 1700000000, 'term')
        with patch.object(hc, '_process_info', return_value=None):
            with self.assertRaises(ValueError):
                hc.terminate_process(999999, 1700000000, 'term')


class ServiceTests(ControlsTestBase):
    def test_allowlist_only_and_idle_guard(self):
        with self.assertRaises(ValueError):
            hc.restart_service('nginx')
        with self.assertRaises(ValueError):
            hc.restart_service('server')  # job-active is running
        common.update('job-active', status='completed')
        with patch.object(hc, '_pid_alive', return_value=False), \
                patch.object(hc.subprocess, 'Popen') as popen, \
                patch.object(hc, '_run', return_value=completed('Mon Oct 13 01:00:00 2026')):
            popen.return_value.pid = 777
            result = hc.restart_service('pool')
        self.assertEqual(result['status'], 'accepted')
        records = common.read_json(self.root / 'services.json', {})
        self.assertNotIn('pool', records)  # detached worker owns the service registry update


class PowerOperationTests(ControlsTestBase):
    def setUp(self):
        super().setUp()
        import console_auth
        console_auth.set_user('admin', 'test-password', self.root)
        self.password = 'test-password'

    def _request(self, op='sleep', jobs=None, confirm=True, password=None, peer='10.0.0.1'):
        return hc.request_power_operation(op, confirm, jobs if jobs is not None else hc.affected_jobs(),
                                          password if password is not None else self.password,
                                          'admin', peer=peer)

    def test_happy_path_records_accepted_then_dispatched(self):
        with patch.object(hc, 'affected_jobs', return_value=['job-active']), \
                patch.object(hc, '_helper_run', return_value={'ok': True}) as helper:
            record = self._request()
        self.assertEqual(record['status'], 'dispatched')
        helper.assert_called_once_with(['power', 'sleep'])
        stored = hc.read_operations()[0]
        self.assertEqual(stored['id'], record['id'])
        self.assertEqual(stored['status'], 'dispatched')

    def test_confirm_and_password_and_jobs_are_validated(self):
        with patch.object(hc, 'affected_jobs', return_value=['job-active']), \
                patch.object(hc, '_helper_run', return_value={'ok': True}):
            with self.assertRaises(ValueError):
                self._request(confirm=False)
            with self.assertRaises(ValueError):
                self._request(password='wrong-password')
            with self.assertRaises(ValueError):
                self._request(jobs=['job-other'])

    def test_replay_within_ttl_rejected_and_throttle_applies(self):
        with patch.object(hc, 'affected_jobs', return_value=['job-active']), \
                patch.object(hc, '_helper_run', return_value={'ok': True}):
            self._request()
            with self.assertRaises(ValueError):
                self._request()  # duplicate within dedupe window
            for _ in range(5):  # exhaust the login throttle for this peer
                with self.assertRaises(ValueError):
                    self._request(password='bad')
            with self.assertRaises(ValueError):
                self._request(password='bad')

    def test_helper_failure_marked_failed(self):
        with patch.object(hc, 'affected_jobs', return_value=['job-active']), \
                patch.object(hc, '_helper_run', side_effect=RuntimeError('helper missing')):
            record = self._request()
        self.assertEqual(record['status'], 'failed')
        self.assertIn('helper', record['error'])

    def test_reconcile_marks_restart_completed_after_reboot(self):
        with patch.object(hc, 'affected_jobs', return_value=['job-active']), \
                patch.object(hc, '_helper_run', return_value={'ok': True}), \
                patch.object(hc, '_current_uptime_seconds', return_value=5000.0):
            record = self._request(op='restart')
        with patch.object(hc, '_current_uptime_seconds', return_value=20.0):
            operations = hc.reconcile_operations(now=record['dispatched_at'] + 60)
        self.assertEqual(operations[0]['status'], 'completed')

    def test_affected_jobs_include_active_codex(self):
        collector = ht.Collector(state_dir=self.root, codex_root=self.root / 'no')
        collector.latest_tasks = {'codex': {'items': [{'id': 'thread-1', 'status': 'running'}]},
                                  'opencode': {'items': []}}
        collector.latest_system = {'sampled_at': time.time()}
        with patch.object(ht, '_COLLECTOR', collector):
            jobs = hc.affected_jobs()
        self.assertEqual(jobs, ['job-active', 'thread-1'])


class AuditAndDispatchTests(ControlsTestBase):
    def test_audit_is_metadata_only_and_bounded(self):
        hc.audit('test.event', password='secret-value', pid=123)
        log = (self.root / 'host' / 'audit.log').read_text()
        self.assertIn('test.event', log)
        self.assertNotIn('secret-value', log)  # credential-like keys never reach the log
        self.assertNotIn('password', log)
        for index in range(30):
            hc.audit('flood', index=index)
        self.assertEqual(len((self.root / 'host' / 'audit.log').read_text().splitlines()), 31)
        with patch.object(hc, 'AUDIT_MAX_BYTES', 200), patch.object(hc, 'AUDIT_LINE_LIMIT', 10):
            hc.audit('big', payload='x' * 500)
        rotated = (self.root / 'host' / 'audit.log').read_text().splitlines()
        self.assertLessEqual(len(rotated), 5)
        self.assertLess((self.root / 'host' / 'audit.log').stat().st_size, 1823)

    def test_dispatch_routes_fixed_actions(self):
        fake, _ = daemon_router(status=FAN_STATUS)
        with patch.object(ht, 'daemon_request', side_effect=fake):
            result = hc.dispatch('fan.profile', {'name': 'Silent'}, 'admin')
        self.assertTrue(result['available'])
        with self.assertRaises(ValueError):
            hc.dispatch('terminal.exec', {}, 'admin')
        with self.assertRaises(ValueError):
            hc.dispatch('fan.profile', {'name': 'Nope'}, 'admin')

    def test_capabilities_reflect_helper_state(self):
        with patch.object(hc.shutil, 'which', side_effect=lambda name: '/usr/bin/pmset' if name == 'pmset' else None), \
                patch.object(hc, 'helper_status', return_value={'installed': True, 'verified': True}), \
                patch.object(hc, 'fan_status', return_value=FAN_STATUS), \
                patch.object(hc.sys, 'platform', 'darwin'), \
                patch.object(hc, '_run', return_value=completed('')):
            caps = hc.capabilities()
        self.assertTrue(caps['fans']['available'])
        self.assertTrue(caps['power']['available'])
        self.assertTrue(caps['power_modes']['writable'])
        self.assertEqual(caps['services'], ['server', 'pool', 'console'])


if __name__ == '__main__':
    unittest.main()

class ProcessIdentityRegression(unittest.TestCase):
    def test_lookup_uses_same_columns_as_parser(self):
        row='123 Mon Oct 12 10:00:00 2026 501 1.0 2.0 2048 sample'
        with patch.object(hc,'_run',return_value=completed(row)) as run:
            self.assertEqual(hc._process_info(123)['pid'],123)
            self.assertIn('pcpu=,pmem=,rss=',run.call_args.args[0][-1])

class CaffeinateIdentityRegression(ControlsTestBase):
    def test_stale_pid_is_never_signalled(self):
        with patch('service.identity',return_value='new process'), patch.object(hc.os,'kill') as kill:
            hc._caffeinate_stop({'pid':123,'identity':'old process'},'test')
        kill.assert_not_called()
