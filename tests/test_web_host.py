"""Host overview frontend contract checks: default home, preserved routes,
no fabricated telemetry or progress, localized keys, authenticated asset."""
from pathlib import Path
import shutil
import subprocess
import unittest


class WebHostTests(unittest.TestCase):
    def test_host_overview_is_the_default_home(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / 'web' / 'index.html').read_text(encoding='utf-8')
        manage = (root / 'web' / 'manage.js').read_text(encoding='utf-8')
        nav = html[html.index('<nav class="primary-nav"'):html.index('</nav>')]
        self.assertLess(nav.index('data-view="host"'), nav.index('data-view="tasks"'))
        host_button = nav[nav.index('data-view="host"'):nav.index('>', nav.index('data-view="host"'))]
        task_button = nav[nav.index('data-view="tasks"'):nav.index('>', nav.index('data-view="tasks"'))]
        self.assertIn('selected', host_button)
        self.assertIn('aria-current="page"', host_button)
        self.assertNotIn('selected', task_button)
        self.assertIn('id="view-host"', html)
        self.assertIn('/console-assets/host.js', html)
        # The tasks view remains intact and hidden until selected.
        self.assertIn('id="view-tasks" class="view-panel" hidden', html)
        # Default view wiring and all legacy views remain addressable.
        self.assertIn("let activeView='host'", manage)
        for view in ('tasks', 'stats', 'sessions', 'memory', 'models', 'setup'):
            self.assertIn('data-view="' + view + '"', html)

    def test_host_view_has_no_decorative_load_animation_or_fake_ci(self):
        root = Path(__file__).resolve().parents[1]
        host_js = (root / 'web' / 'host.js').read_text(encoding='utf-8')
        self.assertNotIn('Math.random', host_js)
        self.assertNotIn('spinner', host_js.lower())
        # Progress markup is emitted only from real progress payloads.
        self.assertIn('progress.percent != null', host_js)
        self.assertNotIn('percent: 100', host_js)

    def test_console_serves_host_js_privately_and_exposes_host_apis(self):
        root = Path(__file__).resolve().parents[1]
        server = (root / 'scripts' / 'console.py').read_text(encoding='utf-8')
        self.assertIn("'host.js': 'text/javascript'", server)
        for route in ('/console-api/host/snapshot', '/console-api/host/history',
                      '/console-api/host/capabilities', '/console-api/host/operations',
                      '/console-api/host/action'):
            self.assertIn(route, server)

    def test_host_i18n_keys_exist_in_all_locales(self):
        root = Path(__file__).resolve().parents[1]
        i18n = (root / 'web' / 'i18n.js').read_text(encoding='utf-8')
        for locale in ('en', 'zh-CN', 'zh-TW', 'ja', 'ko'):
            marker = (("'" + locale + "': {") if '-' in locale else (locale + ': {'))
            start = i18n.index(marker)
            end = i18n.index('\n    }', start)
            block = i18n[start:end]
            for key in ('nav.host', 'hostov.heading', 'hostov.cpu', 'hostov.fans',
                        'hostov.powerOps', 'hostov.powerOp.sleep'):
                self.assertIn("'" + key + "'", block, key + ' missing in ' + locale)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for JavaScript behavior checks')
    def test_host_view_behavior(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(['node', '--test', str(root / 'tests' / 'web_host.test.js')],
                                cwd=str(root), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
