"""Dependency-free browser logic checks; no real accounts or model requests."""
from pathlib import Path
import shutil
import subprocess
import unittest


class WebTaskViewTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for JavaScript behavior checks')
    def test_task_view_behavior(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(['node', '--test', str(root / 'tests/task_view.test.js')],
                                cwd=str(root), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
