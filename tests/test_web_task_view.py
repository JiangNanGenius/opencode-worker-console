"""Dependency-free browser logic checks; no real accounts or model requests."""
from pathlib import Path
import shutil
import subprocess
import unittest


class WebTaskViewTests(unittest.TestCase):
    def test_operational_panels_stay_in_their_intended_views(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / 'web' / 'index.html').read_text(encoding='utf-8')
        css = (root / 'web' / 'style.css').read_text(encoding='utf-8')

        self.assertIn('id="host-status"', html)
        self.assertIn('id="stats-hour"', html)
        self.assertIn('id="stats-day"', html)
        self.assertNotIn('data-view="records"', html)

        stats_start = html.index('id="view-stats"')
        stats_end = html.index('id="view-sessions"')
        ledger = html.index('id="history-ledger"')
        self.assertLess(stats_start, ledger)
        self.assertLess(ledger, stats_end)

        # The desktop sidebar must stay out of document flow. A later shared
        # position rule once overrode its fixed positioning and pushed the
        # entire main view below the full sidebar height while loading.
        self.assertNotIn('main,.sidebar{position:relative', css)
        self.assertIn('.sidebar{position:fixed;', css)
        self.assertIn('main{position:relative;z-index:1}', css)
        self.assertIn('.sidebar{z-index:2}', css)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for JavaScript behavior checks')
    def test_task_view_behavior(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(['node', '--test', str(root / 'tests/task_view.test.js')],
                                cwd=str(root), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
