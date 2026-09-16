import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cleanup
import common


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.config = self.root / 'config.json'
        self.write_config(None)
        self.patchers = [patch.object(common, 'STATE', self.state), patch.object(common, 'CONFIG', self.config)]
        for p in self.patchers:
            p.start()
        common.init()
        self.disk = {'free': 100 * cleanup.GiB}
        self.disk_patcher = patch.object(cleanup.shutil, 'disk_usage',
                                         side_effect=lambda path: SimpleNamespace(free=self.disk['free']))
        self.disk_patcher.start()

    def tearDown(self):
        self.disk_patcher.stop()
        for p in reversed(self.patchers):
            p.stop()
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def write_config(self, cleanup_policy):
        cfg = {'server_url': 'http://127.0.0.1:1234', 'max_parallel': 2, 'profiles': {}}
        if cleanup_policy is not None:
            cfg['cleanup'] = cleanup_policy
        self.config.write_text(json.dumps(cfg))

    def enabled(self, **overrides):
        pol = {'enabled': True, 'min_free_gb': 5, 'target_free_gb': 10,
               'min_age_days': 30, 'keep_recent': 0, 'interval_seconds': 3600}
        pol.update(overrides)
        self.write_config(pol)
        return pol

    def add_task(self, task_id, status='completed', age_days=60, session_id=None, title='Old task'):
        t = {'id': task_id, 'title': title, 'status': status,
             'created_at': time.time() - age_days * 86400, 'updated_at': time.time(),
             'source_dir': str(self.root / 'repo')}
        if session_id:
            t['session_id'] = session_id
        common.write_json(common.task_path(task_id), t)
        return t

    def make_artifacts(self, task_id, age_seconds=90 * 86400):
        art = self.state / 'artifacts' / task_id
        (art / 'before').mkdir(parents=True, exist_ok=True)
        (art / 'after').mkdir(parents=True, exist_ok=True)
        (art / 'before' / 'file.txt').write_text('before-body')
        (art / 'after' / 'file.txt').write_text('after-body')
        (art / 'messages.json').write_text('[{"parts": [{"text": "secret prompt body"}]}]')
        (art / 'result.json').write_text('{"outcome": "done"}')
        (art / 'summary.md').write_text('summary')
        (art / 'changes.patch').write_text('patch')
        (art / 'baseline.json').write_text('{}')
        stale = time.time() - age_seconds
        for name in ('before', 'after', 'messages.json'):
            os.utime(art / name, (stale, stale))
        return art

    def candidate_task_ids(self, result):
        return {c.get('task_id') for c in result['candidates'] if c.get('task_id')}

    # -- policy ----------------------------------------------------------
    def test_policy_defaults_disabled_and_overrides(self):
        self.write_config(None)
        self.assertEqual(cleanup.policy(), cleanup.DEFAULTS)
        self.assertFalse(cleanup.policy()['enabled'])
        self.write_config({'enabled': True, 'min_free_gb': 2, 'min_age_days': 7, 'keep_recent': 3,
                           'bogus': 'ignored', 'target_free_gb': -1})
        pol = cleanup.policy()
        self.assertTrue(pol['enabled'])
        self.assertEqual(pol['min_free_gb'], 2.0)
        self.assertEqual(pol['min_age_days'], 7)
        self.assertEqual(pol['keep_recent'], 3)
        self.assertEqual(pol['target_free_gb'], cleanup.DEFAULTS['target_free_gb'])
        self.assertNotIn('bogus', pol)
        self.assertEqual(set(pol), set(cleanup.DEFAULTS))

    # -- gating ----------------------------------------------------------
    def test_disabled_policy_never_deletes(self):
        self.write_config({'enabled': False, 'min_free_gb': 5, 'target_free_gb': 10,
                           'min_age_days': 30, 'keep_recent': 0})
        self.disk['free'] = 1 * cleanup.GiB
        self.add_task('job-old', 'completed', age_days=90)
        art = self.make_artifacts('job-old')
        result = cleanup.run(apply=True)
        self.assertFalse(result['applied'])
        self.assertEqual(result['skipped'], 'disabled')
        self.assertEqual(result['removed'], [])
        self.assertTrue((art / 'messages.json').exists())
        self.assertTrue((art / 'before').exists())

    def test_apply_requires_low_disk_and_force_bypasses(self):
        self.enabled()
        self.add_task('job-a', 'completed', age_days=90)
        art = self.make_artifacts('job-a')
        self.disk['free'] = 50 * cleanup.GiB
        result = cleanup.run(apply=True)
        self.assertFalse(result['applied'])
        self.assertEqual(result['skipped'], 'disk_ok')
        self.assertTrue((art / 'messages.json').exists())
        self.disk['free'] = 1 * cleanup.GiB
        result = cleanup.run(apply=True)
        self.assertTrue(result['applied'])
        self.assertFalse((art / 'messages.json').exists())
        # force authorizes even a disabled policy on a healthy disk
        self.write_config({'enabled': False, 'min_free_gb': 5, 'target_free_gb': 10,
                           'min_age_days': 30, 'keep_recent': 0})
        self.disk['free'] = 50 * cleanup.GiB
        self.add_task('job-b', 'completed', age_days=90)
        art_b = self.make_artifacts('job-b')
        forced = cleanup.run(apply=True, force=True)
        self.assertTrue(forced['applied'])
        self.assertFalse((art_b / 'messages.json').exists())

    # -- retention -------------------------------------------------------
    def test_age_recent_and_active_retention(self):
        self.enabled(keep_recent=1)
        self.add_task('job-old', 'completed', age_days=90)
        self.add_task('job-recent', 'completed', age_days=1)
        self.add_task('job-run', 'running', age_days=90)
        self.add_task('job-queued', 'queued', age_days=90)
        self.add_task('job-young', 'completed', age_days=10)
        for tid in ('job-old', 'job-recent', 'job-run', 'job-queued', 'job-young'):
            self.make_artifacts(tid)
        result = cleanup.preview()
        tids = self.candidate_task_ids(result)
        self.assertIn('job-old', tids)
        for tid in ('job-recent', 'job-run', 'job-queued', 'job-young'):
            self.assertNotIn(tid, tids)

    def test_preview_lists_sizes(self):
        self.enabled()
        self.add_task('job-old', 'completed', age_days=90)
        self.make_artifacts('job-old')
        result = cleanup.preview()
        self.assertTrue(result['dry_run'])
        self.assertGreater(result['candidate_bytes'], 0)
        kinds = [c['kind'] for c in result['candidates']]
        self.assertIn('artifact_before', kinds)
        self.assertIn('artifact_after', kinds)
        self.assertIn('artifact_messages', kinds)
        self.assertTrue(all('path' in c or 'session_id' in c for c in result['candidates']))

    # -- caches ----------------------------------------------------------
    def test_disposable_caches_removed_with_release_retention(self):
        self.enabled()
        self.disk['free'] = 1 * cleanup.GiB  # stays low, process all candidates
        cache = self.state / '__pycache__'
        cache.mkdir()
        (cache / 'x.pyc').write_text('cache')
        logs = self.state / 'logs'
        logs.mkdir(exist_ok=True)
        old_log = logs / 'pool.previous.log'
        old_log.write_text('old log')
        fresh_log = logs / 'console.previous.log'
        fresh_log.write_text('fresh log')
        stale = time.time() - 90 * 86400
        os.utime(old_log, (stale, stale))
        os.utime(cache, (stale, stale))
        releases = self.state / 'releases'
        names = ['20240101-000000', '20240201-000000', '20240301-000000']
        for name in names:
            (releases / name).mkdir(parents=True)
            (releases / name / 'file').write_text(name)
            os.utime(releases / name, (stale, stale))
        result = cleanup.run(apply=True)
        self.assertTrue(result['applied'])
        self.assertFalse(cache.exists())
        self.assertFalse(old_log.exists())
        self.assertTrue(fresh_log.exists())
        self.assertFalse((releases / names[0]).exists())
        self.assertTrue((releases / names[1]).exists())
        self.assertTrue((releases / names[2]).exists())

    # -- evidence --------------------------------------------------------
    def test_preserves_evidence_report_and_worktrees(self):
        self.enabled()
        self.disk['free'] = 1 * cleanup.GiB
        self.add_task('job-keep', 'completed', age_days=90)
        art = self.make_artifacts('job-keep')
        worktree = self.state / 'worktrees' / 'job-keep'
        worktree.mkdir(parents=True)
        (worktree / 'unintegrated.txt').write_text('worker changes')
        task_path = common.task_path('job-keep')
        result = cleanup.run(apply=True)
        self.assertTrue(result['applied'])
        for name in ('result.json', 'summary.md', 'changes.patch', 'baseline.json'):
            self.assertTrue((art / name).exists(), name)
        for name in ('before', 'after', 'messages.json'):
            self.assertFalse((art / name).exists(), name)
        self.assertTrue(task_path.exists())
        self.assertTrue((worktree / 'unintegrated.txt').exists())
        report = common.read_json(self.state / 'cleanup-last.json')
        serialized = json.dumps(report)
        self.assertNotIn('secret prompt body', serialized)
        self.assertNotIn('before-body', serialized)
        self.assertEqual(report['applied'], True)

    # -- symlinks / safety ----------------------------------------------
    def test_symlink_targets_and_trees_are_refused(self):
        self.enabled()
        self.disk['free'] = 1 * cleanup.GiB
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'precious.txt').write_text('precious')
        self.add_task('job-link', 'completed', age_days=90)
        art = self.make_artifacts('job-link')
        shutil.rmtree(art / 'before')
        (art / 'before').symlink_to(outside, target_is_directory=True)
        (art / 'messages.json').unlink()
        (art / 'messages.json').symlink_to(outside / 'precious.txt')
        (art / 'after' / 'nested-link').symlink_to(outside / 'precious.txt')
        result = cleanup.preview()
        kinds = [c['kind'] for c in result['candidates']]
        self.assertNotIn('artifact_before', kinds)
        self.assertNotIn('artifact_messages', kinds)
        with self.assertRaises(ValueError):
            cleanup._assert_under_state(Path('/etc'))
        with self.assertRaises(ValueError):
            cleanup._assert_under_state(self.state / 'tasks')
        with self.assertRaises(ValueError):
            cleanup._remove_local({'kind': 'previous_log', 'path': str(art / 'messages.json')})
        applied = cleanup.run(apply=True)
        self.assertTrue((outside / 'precious.txt').exists())
        self.assertTrue((art / 'before').is_symlink())
        self.assertTrue((art / 'messages.json').is_symlink())
        # the after tree contains a symlink, so the whole tree is rejected
        self.assertTrue((art / 'after').exists())
        self.assertTrue(any(e['kind'] == 'artifact_after' for e in applied['errors']))
        self.assertFalse(any(r['kind'] == 'artifact_before' for r in applied['removed']))

    # -- sessions --------------------------------------------------------
    def test_archived_old_idle_session_deleted_via_management(self):
        self.enabled()
        self.disk['free'] = 1 * cleanup.GiB
        archived_ms = int((time.time() - 90 * 86400) * 1000)
        self.add_task('job-ses', 'completed', age_days=90, session_id='ses_old', title='S')
        session = {'id': 'ses_old', 'title': 'Disposable session', 'directory': str(self.root),
                   'time': {'archived': archived_ms}}
        calls = []

        def api(path, directory=None, method='GET', data=None, timeout=15):
            calls.append((method, path))
            if path == '/experimental/session?limit=500&archived=true':
                return [session]
            if path == '/session/status':
                return {}
            if path == '/session/ses_old' and method == 'GET':
                return session
            if path == '/session/ses_old/children':
                return []
            if path == '/session/ses_old' and method == 'DELETE':
                return True
            raise AssertionError('unexpected api call ' + str((method, path)))

        with patch.object(common, 'api', side_effect=api):
            result = cleanup.run(apply=True)
        self.assertTrue(any(method == 'DELETE' for method, _ in calls))
        self.assertIn('session', [r['kind'] for r in result['removed']])
        self.assertTrue(common.task('job-ses')['session_deleted'])
        self.assertEqual(result['errors'], [])

    def test_unarchived_or_recent_sessions_are_skipped(self):
        self.enabled()
        self.disk['free'] = 1 * cleanup.GiB
        now_ms = int(time.time() * 1000)
        self.add_task('job-1', 'completed', age_days=90, session_id='ses_live')
        self.add_task('job-2', 'completed', age_days=90, session_id='ses_new')
        sessions = [
            {'id': 'ses_live', 'title': 'Live', 'directory': str(self.root), 'time': {}},
            {'id': 'ses_new', 'title': 'New archive', 'directory': str(self.root),
             'time': {'archived': now_ms}},
        ]
        calls = []

        def api(path, directory=None, method='GET', data=None, timeout=15):
            calls.append((method, path))
            if path == '/experimental/session?limit=500&archived=true':
                return sessions
            if path == '/session/status':
                return {}
            raise AssertionError('unexpected api call ' + str((method, path)))

        with patch.object(common, 'api', side_effect=api):
            result = cleanup.run(apply=True)
        self.assertFalse(any(method == 'DELETE' for method, _ in calls))
        self.assertNotIn('session', [r['kind'] for r in result['removed']])

    def test_missing_session_is_skipped_not_failed(self):
        self.enabled()
        self.disk['free'] = 1 * cleanup.GiB
        archived_ms = int((time.time() - 90 * 86400) * 1000)
        self.add_task('job-gone', 'completed', age_days=90, session_id='ses_gone')
        session = {'id': 'ses_gone', 'title': 'Gone', 'directory': str(self.root),
                   'time': {'archived': archived_ms}}

        def api(path, directory=None, method='GET', data=None, timeout=15):
            if path == '/experimental/session?limit=500&archived=true':
                return [session]
            if path == '/session/status':
                return {}
            if path == '/session/ses_gone' and method == 'GET':
                raise common.HttpFailure(404)
            raise AssertionError('unexpected api call ' + str((method, path)))

        with patch.object(common, 'api', side_effect=api):
            result = cleanup.run(apply=True)
        self.assertTrue(any(s['reason'] == 'missing' for s in result['skipped_items']))
        self.assertEqual(result['errors'], [])

    # -- target stop -----------------------------------------------------
    def test_stops_at_target_free_unless_force(self):
        self.enabled()
        self.disk['free'] = 1 * cleanup.GiB
        self.add_task('job-1', 'completed', age_days=90)
        self.add_task('job-2', 'completed', age_days=90)
        self.make_artifacts('job-1')
        self.make_artifacts('job-2')
        seen = {'count': 0}

        def fake_remove(item):
            seen['count'] += 1
            target = Path(item['path'])
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            self.disk['free'] = 50 * cleanup.GiB

        with patch.object(cleanup, '_remove_local', side_effect=fake_remove):
            result = cleanup.run(apply=True, force=False)
        self.assertTrue(result['stopped_at_target'])
        self.assertEqual(len(result['removed']), 1)
        self.assertEqual(seen['count'], 1)

        with patch.object(cleanup, '_remove_local', side_effect=fake_remove):
            forced = cleanup.run(apply=True, force=True)
        self.assertFalse(forced['stopped_at_target'])
        self.assertGreater(len(forced['removed']), 1)


if __name__ == '__main__':
    unittest.main()
