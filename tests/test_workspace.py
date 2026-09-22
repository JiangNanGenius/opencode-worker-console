import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import delegate
import workspace


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.config = self.root / 'config.json'
        self.c = {'server_url': 'http://127.0.0.1:1', 'profiles': {
            'fallback': {'model': 'deepseek/deepseek-flash'}}}
        self.config.write_text(json.dumps(self.c))
        self.work = self.root / 'plain-dir'
        self.work.mkdir()
        self.work = self.work.resolve()
        self.patchers = [patch.object(m, 'STATE', self.state) for m in (common, workspace, delegate)]
        self.patchers += [patch.object(common, 'CONFIG', self.config),
                          patch.object(delegate, 'CONFIG', self.config)]
        for p in self.patchers:
            p.start()
        common.init()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.tmp.cleanup()

    def remote_write(self, **kw):
        spec = {'directory': str(self.work), 'objective': 'restart the remote service',
                'mode': 'write', 'scopes': [], 'targets': ['ssh:example.com:nginx'],
                'profile': 'fallback'}
        spec.update(kw)
        return common.task(delegate.submit(spec)['id'])

    def test_remote_only_write_prepares_in_plain_non_git_directory(self):
        self.assertIsNone(workspace.git_root(self.work))
        t = self.remote_write()
        self.assertEqual(t['workspace'], 'shared')
        directory = workspace.prepare(t)
        self.assertEqual(directory, str(self.work))
        baseline = common.read_json(common.artifact_dir(t['id']) / 'baseline.json', {})
        self.assertIsNone(baseline['source_status'])
        self.assertEqual(baseline['files'], {})
        t['directory'] = directory
        changes = workspace.collect_changes(t)
        self.assertEqual(changes['changed_files'], [])
        self.assertTrue(Path(changes['patch']).exists())

    def test_remote_write_with_large_stays_shared_without_git(self):
        # 'large' must not force an isolated worktree when there is no local scope.
        t = self.remote_write(large=True)
        self.assertEqual(t['workspace'], 'shared')

    def test_large_repository_write_uses_main_workspace_unless_isolation_is_explicit(self):
        t = common.task(delegate.submit({
            'directory': str(self.work), 'objective': 'update the repository',
            'mode': 'write', 'scopes': ['.'], 'large': True,
            'profile': 'fallback'})['id'])
        self.assertEqual(t['workspace'], 'shared')
        self.assertEqual(workspace.prepare(t), str(self.work))

    def test_scope_validation_and_isolated_git_requirement_unchanged(self):
        with self.assertRaises(ValueError):
            delegate.submit({'directory': str(self.work), 'objective': 'x', 'mode': 'write',
                             'scopes': ['../escape'], 'targets': ['ssh:h:s']})
        with self.assertRaises(ValueError):
            delegate.submit({'directory': str(self.work), 'objective': 'x', 'mode': 'write',
                             'scopes': ['a.txt'], 'workspace': 'isolated'})
        # Read tasks stay valid with no scopes and no targets.
        t = common.task(delegate.submit({'directory': str(self.work), 'objective': 'look',
                                         'mode': 'read'})['id'])
        self.assertEqual(t['scopes'], [])
        self.assertEqual(t['targets'], [])

    def test_local_scope_snapshot_unchanged_in_git_repository(self):
        import subprocess
        subprocess.run(['git', 'init', '-q'], cwd=str(self.work), check=True)
        (self.work / 'a.txt').write_text('original\n')
        t = common.task(delegate.submit({'directory': str(self.work), 'objective': 'edit',
                                         'mode': 'write', 'scopes': ['a.txt'],
                                         'profile': 'fallback'})['id'])
        directory = workspace.prepare(t)
        self.assertEqual(directory, str(self.work))
        baseline = common.read_json(common.artifact_dir(t['id']) / 'baseline.json', {})
        self.assertIn('a.txt', baseline['files'])
        self.assertIsNotNone(baseline['source_status'])

    def isolated_task(self, task_id='job-isolated'):
        subprocess.run(['git', 'init', '-q'], cwd=str(self.work), check=True)
        (self.work / 'a.txt').write_text('original\n')
        subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                        'add', 'a.txt'], cwd=str(self.work), check=True)
        subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                        'commit', '-qm', 'initial'], cwd=str(self.work), check=True)
        return {'id': task_id, 'title': 'isolated', 'source_dir': str(self.work),
                'directory': str(self.state / 'worktrees' / task_id), 'workspace': 'isolated',
                'status': 'completed', 'mode': 'write', 'scopes': ['a.txt']}

    def test_release_unchanged_isolated_worktree(self):
        task = self.isolated_task()
        path = Path(workspace.prepare(task))
        status = workspace.isolated_release_status(task)
        self.assertTrue(status['eligible'])
        self.assertEqual(status['reason'], 'no_changes')
        released = workspace.release_isolated(task)
        self.assertTrue(released['released'])
        self.assertFalse(path.exists())

    def test_release_retains_unintegrated_changes(self):
        task = self.isolated_task('job-isolated-changed')
        path = Path(workspace.prepare(task))
        (path / 'a.txt').write_text('worker change\n')
        status = workspace.release_isolated(task)
        self.assertFalse(status['released'])
        self.assertEqual(status['reason'], 'unintegrated_changes')
        self.assertTrue(path.exists())

    def test_explicit_discard_removes_cancelled_unintegrated_worktree_only(self):
        task = self.isolated_task('job-isolated-cancelled')
        task['status'] = 'cancelled'
        path = Path(workspace.prepare(task))
        (path / 'a.txt').write_text('discarded worker change\n')
        result = workspace.discard_cancelled_isolated(task)
        self.assertTrue(result['released'])
        self.assertEqual(result['reason'], 'cancelled_discarded')
        self.assertFalse(path.exists())

        completed = dict(task, id='job-isolated-completed', title='completed', status='completed',
                         directory=str(self.state / 'worktrees' / 'job-isolated-completed'))
        completed_path = Path(workspace.prepare(completed))
        (completed_path / 'a.txt').write_text('must stay\n')
        refused = workspace.discard_cancelled_isolated(completed)
        self.assertFalse(refused['released'])
        self.assertEqual(refused['reason'], 'not_cancelled_isolated')
        self.assertTrue(completed_path.exists())

    def test_user_confirmed_discard_removes_terminal_unintegrated_worktree(self):
        task = self.isolated_task('job-isolated-confirmed')
        path = Path(workspace.prepare(task))
        (path / 'a.txt').write_text('confirmed obsolete change\n')
        result = workspace.discard_terminal_isolated(task)
        self.assertTrue(result['released'])
        self.assertEqual(result['reason'], 'user_confirmed_discard')
        self.assertGreater(result['bytes'], 0)
        self.assertFalse(path.exists())

    def test_user_confirmed_discard_handles_missing_source_and_refuses_symlink(self):
        task = self.isolated_task('job-isolated-missing-source')
        path = Path(workspace.prepare(task))
        task['source_dir'] = str(self.root / 'missing-source')
        result = workspace.discard_terminal_isolated(task)
        self.assertTrue(result['released'])
        self.assertFalse(path.exists())

        outside = self.root / 'outside'
        outside.mkdir()
        link_task = dict(task, id='job-isolated-link',
                         directory=str(self.state / 'worktrees' / 'job-isolated-link'))
        link = self.state / 'worktrees' / link_task['id']
        link.symlink_to(outside, target_is_directory=True)
        refused = workspace.discard_terminal_isolated(link_task)
        self.assertFalse(refused['released'])
        self.assertEqual(refused['reason'], 'unsafe_worktree_path')
        self.assertTrue(outside.exists())

    def test_release_integrated_worktree(self):
        task = self.isolated_task('job-isolated-integrated')
        path = Path(workspace.prepare(task))
        (path / 'a.txt').write_text('integrated result\n')
        task['integrated_at'] = time.time()
        released = workspace.release_isolated(task)
        self.assertTrue(released['released'])
        self.assertEqual(released['reason'], 'integrated')
        self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
