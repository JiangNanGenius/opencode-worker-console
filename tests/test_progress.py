import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import progress


class ProgressTests(unittest.TestCase):
    def test_report_calculates_percent_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 'state'
            (state / 'tasks').mkdir(parents=True)
            (state / 'tasks/task-1.json').write_text(json.dumps({'id': 'task-1'}))
            with patch.object(common, 'STATE', state), patch.object(progress.time, 'time', side_effect=[100, 200]):
                first = progress.report('task-1', 'Compile', current=2, total=10)
                second = progress.report('task-1', 'Compile', current=5, total=10)
            self.assertEqual(first['percent'], 20)
            self.assertEqual(second['percent'], 50)
            self.assertEqual(second['eta_seconds'], 100)
            self.assertEqual(len(second['history']), 2)

    def test_unknown_progress_stays_truthfully_indeterminate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 'state'
            (state / 'tasks').mkdir(parents=True)
            (state / 'tasks/task-1.json').write_text(json.dumps({'id': 'task-1'}))
            with patch.object(common, 'STATE', state):
                value = progress.report('task-1', 'Upload', message='Waiting for remote service')
                loaded = progress.snapshot('task-1', refresh_github=False)
            self.assertIsNone(value['percent'])
            self.assertIsNone(value['eta_seconds'])
            self.assertEqual(loaded['phase'], 'Upload')

    def test_failed_progress_does_not_claim_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 'state'
            (state / 'tasks').mkdir(parents=True)
            (state / 'tasks/task-1.json').write_text(json.dumps({'id': 'task-1'}))
            with patch.object(common, 'STATE', state):
                progress.report('task-1', 'Compile', current=3, total=10)
                value = progress.finish('task-1', 'failed', 'Compiler exited')
            self.assertEqual(value['status'], 'failed')
            self.assertEqual(value['current'], 3)
            self.assertEqual(value['percent'], 30)
            self.assertIsNone(value['eta_seconds'])

    def test_github_steps_produce_observed_percentage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 'state'
            (state / 'tasks').mkdir(parents=True)
            (state / 'tasks/task-1.json').write_text(json.dumps({'id': 'task-1'}))
            payload = {'name': 'CI', 'status': 'in_progress', 'conclusion': None,
                       'startedAt': '1970-01-01T00:01:40Z',
                       'jobs': [{'name': 'Build', 'status': 'in_progress', 'steps': [
                           {'name': 'Checkout', 'status': 'completed'},
                           {'name': 'Compile', 'status': 'in_progress'}]}]}
            completed = type('Run', (), {'stdout': json.dumps(payload)})()
            calls = []

            def run(args, **_kwargs):
                calls.append(args)
                return completed

            with patch.object(common, 'STATE', state), \
                 patch.object(progress.subprocess, 'run', side_effect=run), \
                 patch.object(progress.time, 'time', side_effect=[100, 130]):
                progress.report('task-1', 'CI', github_run='https://github.com/a/b/actions/runs/1')
                value = progress.snapshot('task-1')
            self.assertEqual(value['percent'], 50)
            self.assertEqual(value['status'], 'running')
            self.assertEqual(value['phase'], 'Compile')
            self.assertEqual(value['github_state'], 'live')
            self.assertEqual(calls[0][0:7], ['gh', 'run', 'view', '1', '--repo', 'a/b', '--json'])

    def test_github_sampling_failure_is_durable_and_keeps_unknown_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / 'state'
            (state / 'tasks').mkdir(parents=True)
            (state / 'tasks/task-1.json').write_text(json.dumps({'id': 'task-1'}))
            with patch.object(common, 'STATE', state), \
                 patch.object(progress.subprocess, 'run', side_effect=RuntimeError('secret detail')), \
                 patch.object(progress.time, 'time', side_effect=[100, 130]):
                progress.report('task-1', 'CI', github_run='https://github.com/a/b/actions/runs/1')
                value = progress.snapshot('task-1')
            self.assertIsNone(value['percent'])
            self.assertEqual(value['github_state'], 'unavailable')
            self.assertNotIn('secret detail', value['github_error'])
            saved = json.loads((state / 'progress/task-1.json').read_text())
            self.assertEqual(saved['github_state'], 'unavailable')

    def test_github_cancelled_run_is_terminal_without_fake_eta(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / 'state'
            (state / 'tasks').mkdir(parents=True)
            (state / 'tasks/task-1.json').write_text(json.dumps({'id': 'task-1'}))
            payload = {'name': 'CI', 'status': 'completed', 'conclusion': 'cancelled',
                       'jobs': [{'steps': []}]}
            completed = type('Run', (), {'stdout': json.dumps(payload)})()
            with patch.object(common, 'STATE', state), \
                 patch.object(progress.subprocess, 'run', return_value=completed), \
                 patch.object(progress.time, 'time', side_effect=[100, 130]):
                progress.report('task-1', 'CI', github_run='https://github.com/a/b/actions/runs/2')
                value = progress.snapshot('task-1')
            self.assertEqual(value['status'], 'cancelled')
            self.assertEqual(value['percent'], 100)
            self.assertEqual(value['eta_seconds'], 0)


if __name__ == '__main__':
    unittest.main()
