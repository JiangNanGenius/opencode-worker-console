import json
from pathlib import Path
import sys
import tempfile
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
            'fast-code': {'model': 'deepseek/deepseek-flash'}}}
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
                'profile': 'fast-code'}
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
                                         'profile': 'fast-code'})['id'])
        directory = workspace.prepare(t)
        self.assertEqual(directory, str(self.work))
        baseline = common.read_json(common.artifact_dir(t['id']) / 'baseline.json', {})
        self.assertIn('a.txt', baseline['files'])
        self.assertIsNotNone(baseline['source_status'])


if __name__ == '__main__':
    unittest.main()
