import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import quota
import workspace
import worker
import delegate
import console as console_server


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.config = self.root / 'config.json'
        self.c = {'auto_approve': False, 'max_parallel': 3, 'max_kimi_parallel': 1, 'kimi_reserve_percent': 20,
                  'server_url': 'http://127.0.0.1:1', 'profiles': {
                      'fast-code': {'model': 'deepseek/deepseek-flash'},
                      'senior-code': {'model': 'kimi-for-coding/kimi-for-coding'},
                      'deep-research': {'model': 'kimi-for-coding/k3'}}}
        self.config.write_text(json.dumps(self.c))
        self.patchers = [patch.object(m, 'STATE', self.state) for m in (common, quota, workspace, worker, delegate)]
        self.patchers += [patch.object(common, 'CONFIG', self.config), patch.object(delegate, 'CONFIG', self.config)]
        for p in self.patchers:
            p.start()
        common.init()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.q = {p: {'state': 'ok', 'stale': False, 'sampled_at': time.time(), 'available': True,
                      'windows': [{'remaining_percent': 80}]} for p in ('deepseek', 'kimi-for-coding')}

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.tmp.cleanup()

    def git(self, *args):
        return workspace.run(['git', *args], self.repo).stdout

    def setup_git(self):
        self.git('init', '-q')
        self.git('config', 'user.name', 'Pool Test')
        self.git('config', 'user.email', 'pool-test@example.invalid')
        (self.repo / 'a.txt').write_text('original\n')
        self.git('add', 'a.txt')
        self.git('commit', '-qm', 'fixture')

    def spec(self, **kw):
        s = {'directory': str(self.repo), 'objective': 'bounded task', 'mode': 'write', 'scopes': ['a.txt'],
             'profile': 'fast-code'}
        s.update(kw)
        return s

    def new(self, **kw):
        return common.task(delegate.submit(self.spec(**kw))['id'])

    def test_console_health_requires_live_pool_and_server(self):
        with patch.object(console_server, 'read_json', side_effect=lambda path, default: {'time': time.time()} if path.name == 'heartbeat.json' else {'pool': {}}), patch.object(console_server.service, 'alive', return_value=True), patch.object(console_server, 'api', return_value={'healthy': True}):
            self.assertEqual(console_server.service_health(), {'pool': True, 'server': True})
            with patch.object(console_server, 'api', side_effect=OSError('offline')):
                self.assertFalse(console_server.service_health()['server'])
            with patch.object(console_server.service, 'alive', return_value=False):
                self.assertFalse(console_server.service_health()['pool'])

    def test_quota_currency_and_unknown(self):
        n = quota.normalize('deepseek', {'balance_infos': [{'currency': 'CNY', 'total_balance': '36.66'},
                                                        {'currency': 'USD', 'total_balance': '2'}]})
        self.assertIsNone(n['available'])
        self.assertEqual(len(n['balances']), 2)
        self.assertIsNone(quota.number('NaN'))
        self.assertIsNone(quota.number(True))

    def test_kimi_dynamic_window_and_missing(self):
        n = quota.normalize('kimi-for-coding', {'limits': [
            {'window': {'duration': 2, 'timeUnit': 'TIME_UNIT_HOUR'},
             'detail': {'limit': '100', 'remaining': '15', 'resetTime': 'later'}},
            {'detail': {}}], 'usage': {'limit': 100, 'remaining': 80}})
        self.assertEqual(n['windows'][0]['duration_minutes'], 120)
        self.assertEqual(n['windows'][0]['remaining_percent'], 15)
        self.assertIsNone(n['windows'][1]['remaining_percent'])
        self.assertIsNone(n['available'])

    def test_normal_background_prefers_kimi_fast_prefers_deepseek(self):
        t = self.new(profile='auto')
        self.assertEqual(quota.route(t, self.c, self.q)[0], 'senior-code')
        t['urgency'] = 'fast'
        self.assertEqual(quota.route(t, self.c, self.q)[0], 'fast-code')
        t['complexity'] = 'deep'
        self.assertEqual(quota.route(t, self.c, self.q)[0], 'deep-research')

    def test_low_quota_fallback_and_explicit_wait(self):
        self.q['kimi-for-coding']['windows'][0]['remaining_percent'] = 10
        t = self.new(profile='auto')
        self.assertEqual(quota.route(t, self.c, self.q)[0], 'fast-code')
        t['requested_profile'] = 'senior-code'
        self.assertIsNone(quota.route(t, self.c, self.q)[0])
        t.update(requested_profile='deep-research', complexity='deep')
        self.assertEqual(quota.route(t, self.c, self.q)[0], 'deep-research')

    def test_expired_cache_not_zero_auth_blocks(self):
        q = {'deepseek': {'state': 'unavailable', 'sampled_at': time.time() - 1000, 'available': False}}
        self.assertTrue(quota.allowed('deepseek', q, 'normal')[0])
        q['deepseek']['state'] = 'auth_error'
        self.assertFalse(quota.allowed('deepseek', q, 'normal')[0])

    def test_overlap_queues_non_overlap_parallel(self):
        a = self.new(scopes=['src'])
        b = self.new(scopes=['src/x.py'])
        c = self.new(scopes=['docs'])
        a.update(status='running', profile='fast-code')
        choices = delegate.choose_ready([a, b, c], self.c, self.q)
        self.assertEqual(choices[0][2], 'scope_or_resource_in_use')
        self.assertEqual(choices[1][1], 'fast-code')

    def test_kimi_profiles_share_concurrency(self):
        a = self.new(profile='senior-code', scopes=['a'])
        b = self.new(profile='deep-research', scopes=['b'], complexity='deep')
        a.update(status='running', profile='senior-code')
        self.assertEqual(delegate.choose_ready([a, b], self.c, self.q)[0][2], 'provider_at_capacity')

    def test_resource_locks_cross_isolation(self):
        a = self.new(resources=['build-output'])
        b = dict(a, workspace='isolated')
        self.assertTrue(workspace.conflicts(a, b))
        b['resources'] = []
        self.assertFalse(workspace.conflicts(a, b))

    def test_reject_scope_escape_and_globs(self):
        for scope in ['../other', '/tmp/other', '*.py', '.git/config']:
            with self.assertRaises(ValueError):
                self.new(scopes=[scope])
        (self.repo / 'link').symlink_to(self.root)
        with self.assertRaises(ValueError):
            self.new(scopes=['link'])

    def test_shared_delta_preserves_preexisting_change(self):
        self.setup_git()
        (self.repo / 'a.txt').write_text('user change\n')
        t = self.new()
        t['directory'] = workspace.prepare(t)
        (self.repo / 'a.txt').write_text('user change\nworker addition\n')
        r = workspace.collect_changes(t)
        patch_text = Path(r['patch']).read_text()
        self.assertIn('+worker addition', patch_text)
        self.assertNotIn('-original', patch_text)
        self.assertEqual(r['changed_files'], ['a.txt'])

    def test_worktree_current_state_and_integration_conflict(self):
        self.setup_git()
        (self.repo / 'a.txt').write_text('user change\n')
        (self.repo / 'note.txt').write_text('untracked dependency\n')
        t = self.new(workspace='isolated')
        t['directory'] = workspace.prepare(t)
        wd = Path(t['directory'])
        self.assertEqual((wd / 'note.txt').read_text(), 'untracked dependency\n')
        (wd / 'a.txt').write_text('user change\nworker addition\n')
        workspace.collect_changes(t)
        t['status'] = 'completed'
        self.assertEqual((self.repo / 'a.txt').read_text(), 'user change\n')
        self.assertTrue(workspace.integrate(t)['check'] == 'passed')
        (self.repo / 'a.txt').write_text('new user change\n')
        with self.assertRaises(ValueError):
            workspace.integrate(t, True)
        (self.repo / 'a.txt').write_text('user change\n')
        workspace.integrate(t, True)
        self.assertEqual((self.repo / 'a.txt').read_text(), 'user change\nworker addition\n')

    def test_auto_approve_default_and_session_policy_snapshot(self):
        import server
        self.c.pop('auto_approve')
        self.config.write_text(json.dumps(self.c))
        t = self.new()
        self.assertEqual(worker.permissions(t), [{'permission': '*', 'pattern': '*', 'action': 'allow'}])
        self.assertIn('Auto Approve is enabled', worker.prompt(t))
        overlay = server.runtime_overlay(self.c)
        self.assertEqual(overlay['permission'], 'allow')
        self.assertTrue(all(a['permission'] == 'allow' for a in overlay['agent'].values()))
        t['directory'] = str(self.repo)
        t['auto_approve'] = False
        self.assertEqual(worker.permissions(t)[0]['action'], 'deny')
        self.assertIn('Restricted mode', worker.prompt(t))
        self.c['auto_approve'] = False
        overlay = server.runtime_overlay(self.c)
        self.assertEqual(overlay['permission'], 'ask')
        self.assertTrue(all(a['permission']['*'] == 'deny' for a in overlay['agent'].values()))

    def test_native_permissions_bound_read_write_shell(self):
        t = self.new(commands=['python3 -m unittest -v'])
        t['directory'] = str(self.repo)
        rules = worker.permissions(t)
        self.assertTrue(any(r['permission'] == 'edit' and r['pattern'] == str(self.repo / 'a.txt') and r['action'] == 'allow' for r in rules))
        self.assertFalse(any(r['permission'] == 'bash' and r['pattern'] == '*' and r['action'] == 'allow' for r in rules))
        t['mode'] = 'read'
        self.assertFalse(any(r['permission'] == 'edit' and r['action'] == 'allow' for r in worker.permissions(t)))

    def test_report_parses_final_json_without_opencode_format(self):
        report = {'outcome': 'done', 'summary': 'Observed', 'evidence': ['a.py:1'], 'tests': [], 'unresolved': []}
        messages = [{'info': {'role': 'assistant', 'modelID': 'deepseek-flash', 'providerID': 'deepseek'},
                     'parts': [{'type': 'text', 'text': json.dumps(report)}]}]
        self.assertEqual(worker.summarize_messages(messages)['structured'], report)
        messages[0]['parts'][0]['text'] = '{"outcome":"done"}'
        self.assertIsNone(worker.summarize_messages(messages)['structured'])

    def test_parent_relationship_inherits_caller_group(self):
        a = self.new(group_id='group-a', group_title='Parent project')
        b = self.new(group_id='group-a', parent_task_id=a['id'])
        self.assertEqual(b['parent_task_id'], a['id'])
        with self.assertRaises(ValueError):
            self.new(group_id='group-b', parent_task_id=a['id'])

    def test_reset_seconds_and_milliseconds(self):
        a = quota.window('a', {'limit': 100, 'remaining': 20, 'resetTime': 1800000000})
        b = quota.window('a', {'limit': 100, 'remaining': 20, 'resetTime': 1800000000000})
        self.assertEqual(a['resets_at'], b['resets_at'])

    def test_recovery_never_replays_prompt(self):
        t = self.new(mode='read', scopes=[])
        t = common.update(t['id'], directory=str(self.repo), session_id='ses_test', message_id='msg_test',
                          profile='fast-code', status='uncertain', started_at=time.time() - 100,
                          dispatch_attempted_at=time.time() - 60)
        stop = threading.Event()
        with patch.object(worker, 'call', return_value=[]) as call, patch.object(worker, 'api', return_value={}), \
             patch.object(worker, 'finish', return_value={}) as finish:
            worker.run_task(t['id'], stop)
            self.assertFalse(any(x.args[1] == '/prompt_async' for x in call.call_args_list))
            self.assertEqual(finish.call_args.args[2], 'needs_attention')

    def test_cancel_waits_for_confirmed_abort(self):
        t = self.new(mode='read', scopes=[])
        t['session_id'], t['directory'] = 'ses_test', str(self.repo)
        with patch.object(worker, 'call'), patch.object(worker, 'idle', return_value=False), patch.object(worker.time, 'sleep'):
            self.assertFalse(worker.stop(t))
        with patch.object(worker, 'call'), patch.object(worker, 'idle', return_value=True):
            self.assertTrue(worker.stop(t))

    def test_wait_result_requires_continuation_until_terminal(self):
        running = delegate.wait_result({'id': 'job-test', 'status': 'running'}, 30)
        self.assertFalse(running['terminal'])
        self.assertTrue(running['continue_waiting'])
        self.assertEqual(running['next_action'], 'call_wait_again')
        completed = delegate.wait_result({'id': 'job-test', 'status': 'completed'}, 2)
        self.assertTrue(completed['terminal'])
        self.assertFalse(completed['continue_waiting'])
        self.assertEqual(completed['next_action'], 'collect_and_review')
        attention = delegate.wait_result({'id': 'job-test', 'status': 'needs_attention'}, 2)
        self.assertEqual(attention['next_action'], 'collect_and_inspect_errors')

    def test_console_local_auth_and_cross_origin_guards(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), console_server.Handler)
        server.daemon_threads = True
        server.cookie = 'test-local-capability'
        url = 'http://127.0.0.1:' + str(server.server_port)
        self.c['console_url'] = url
        self.config.write_text(json.dumps(self.c))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(url + '/console') as r:
                cookie = r.headers.get('Set-Cookie').split(';')[0]
                self.assertIn(b'Worker Desk', r.read())
                self.assertIn('HttpOnly', r.headers.get('Set-Cookie'))
            for path, headers, code in [
                ('/console-api/state', {}, 401),
                ('/console', {'Host': 'attacker.example'}, 403),
                ('/console', {'Origin': 'https://attacker.example'}, 403),
                ('/console', {'Sec-Fetch-Site': 'cross-site'}, 403),
            ]:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(urllib.request.Request(url + path, headers=headers))
                self.assertEqual(error.exception.code, code)
            with patch.object(console_server, 'state', return_value={'tasks': [], 'quota': {}}):
                req = urllib.request.Request(url + '/console-api/state', headers={'Cookie': cookie})
                with urllib.request.urlopen(req) as r:
                    self.assertEqual(json.load(r)['tasks'], [])
        finally:
            server.shutdown()
            server.server_close()

    def test_console_asset_allowlist_serves_i18n_and_rejects_unknown(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), console_server.Handler)
        server.daemon_threads = True
        server.cookie = 'test-local-capability'
        url = 'http://127.0.0.1:' + str(server.server_port)
        self.c['console_url'] = url
        self.config.write_text(json.dumps(self.c))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(url + '/console') as r:
                cookie = r.headers.get('Set-Cookie').split(';')[0]
            # The i18n bundle is an explicit allowlisted asset and is served as JavaScript.
            req = urllib.request.Request(url + '/console-assets/i18n.js', headers={'Cookie': cookie})
            with urllib.request.urlopen(req) as r:
                body = r.read()
                self.assertEqual(r.status, 200)
                self.assertTrue(r.headers.get('Content-Type').startswith('text/javascript'))
                self.assertIn(b'worker-desk-locale', body)
                self.assertIn(b'global.I18n', body)
            # Asset requests still require the bootstrapped capability cookie.
            with self.assertRaises(urllib.error.HTTPError) as unauthenticated:
                urllib.request.urlopen(url + '/console-assets/i18n.js')
            self.assertEqual(unauthenticated.exception.code, 401)
            # Unknown or traversal asset paths are rejected without reaching the desktop allowlist.
            for path in ('/console-assets/unknown.js', '/console-assets/../i18n.js', '/console-assets/'):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(urllib.request.Request(url + path, headers={'Cookie': cookie}))
                self.assertEqual(error.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()

    def test_session_link_handles_non_ascii_directory(self):
        import base64
        url = console_server.session_url({'directory': '/repo/中文 空格', 'session_id': 'ses_test'})
        encoded = url.split('/')[1]
        decoded = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)).decode()
        self.assertEqual(decoded, '/repo/中文 空格')
        self.assertTrue(url.endswith('/session/ses_test'))


if __name__ == '__main__':
    unittest.main()
