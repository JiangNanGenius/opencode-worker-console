"""Run browser-independent setup flow checks without services or paid models."""
from pathlib import Path
import shutil
import subprocess
import unittest


class WebSetupTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for JavaScript behavior checks')
    def test_wizard_behavior(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ['node', str(root / 'tests' / 'web_setup.test.js')],
            cwd=str(root), capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('wizard behavior tests passed', result.stdout)
