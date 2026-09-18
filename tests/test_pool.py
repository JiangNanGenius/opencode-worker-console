import http.client
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
import diagnostics
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
        self.identities = {'deepseek': 'cred-a', 'kimi-for-coding': 'cred-k'}
        self.patchers = [patch.object(m, 'STATE', self.state) for m in (common, quota, workspace, worker, delegate)]
        self.patchers += [patch.object(common, 'CONFIG', self.config), patch.object(delegate, 'CONFIG', self.config),
                          patch.object(quota, 'credential_identity', lambda p: self.identities.get(p))]
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

    def test_new_tasks_have_no_deadline_even_from_legacy_callers(self):
        for spec in ({}, {'complexity': 'deep'}, {'timeout_seconds': 1},
                     {'timeout_seconds': 30000}):
            with self.subTest(spec=spec):
                t = self.new(**spec)
                self.assertNotIn('timeout_seconds', t)

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

    def test_low_quota_blocks_without_fallback_and_keeps_pins(self):
        self.q['kimi-for-coding']['windows'][0]['remaining_percent'] = 10
        t = self.new(profile='auto')
        profile, why = quota.route(t, self.c, self.q)
        self.assertIsNone(profile)
        self.assertEqual(why, 'reserve_kimi_for_complex_work')
        alts = quota.alternatives(t, self.c, self.q)
        self.assertEqual([a['profile'] for a in alts], ['fast-code'])
        rec = quota.recovery(t, self.c, self.q)
        self.assertEqual(rec['suggested_action'], 'reselect_profile')
        self.assertFalse(rec['automatic_fallback'])
        t['requested_profile'] = 'senior-code'
        self.assertIsNone(quota.route(t, self.c, self.q)[0])
        t.update(requested_profile='deep-research', complexity='deep')
        self.assertEqual(quota.route(t, self.c, self.q)[0], 'deep-research')

    def test_reserve_defaults_to_zero_unless_configured(self):
        c = dict(self.c)
        c.pop('kimi_reserve_percent')
        self.q['kimi-for-coding']['windows'][0]['remaining_percent'] = 10
        t = self.new(profile='auto')
        self.assertEqual(quota.route(t, c, self.q)[0], 'senior-code')
        self.assertTrue(quota.allowed('kimi-for-coding', self.q, 'normal')[0])
        # An explicitly configured nonzero reserve is preserved.
        self.assertFalse(quota.allowed('kimi-for-coding', self.q, 'normal', 20)[0])

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

    def test_provider_caps_removed_kimi_profiles_dispatch_freely(self):
        a = self.new(profile='senior-code', scopes=['a'])
        b = self.new(profile='deep-research', scopes=['b'], complexity='deep')
        a.update(status='running', profile='senior-code')
        choices = delegate.choose_ready([a, b], self.c, self.q)
        self.assertEqual(choices[0][1], 'deep-research')

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

    def test_single_fenced_report_amid_prose_multiple_rejected(self):
        report = {'outcome': 'done', 'summary': 'Observed', 'evidence': [], 'tests': [], 'unresolved': []}
        text = 'All checks pass.\n```json\n' + json.dumps(report) + '\n```'
        messages = [{'info': {'role': 'assistant'}, 'parts': [{'type': 'text', 'text': text}]}]
        self.assertEqual(worker.summarize_messages(messages)['structured'], report)
        messages[0]['parts'][0]['text'] = text + '\nDone again.\n```json\n' + json.dumps(report) + '\n```'
        self.assertIsNone(worker.summarize_messages(messages)['structured'])
        messages[0]['parts'][0]['text'] = 'Note.\n```json\n{"outcome":"done"}\n```'
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

    def test_billing_circuit_blocks_stale_positive_and_pins_explicit(self):
        t = self.new()  # explicit fast-code on deepseek
        self.assertEqual(quota.route(t, self.c, self.q)[0], 'fast-code')
        quota.trip('deepseek', 'Insufficient Balance', 'm1')
        # The circuit overrides the fresh cached positive sample.
        profile, why = quota.route(t, self.c, self.q)
        self.assertIsNone(profile)
        self.assertEqual(why, 'provider_billing_blocked')
        rec = quota.recovery(t, self.c, self.q)
        self.assertEqual(rec['blocked_reason'], 'provider_billing_blocked')
        self.assertEqual(rec['suggested_action'], 'reselect_profile')
        self.assertEqual([a['profile'] for a in rec['alternatives']], ['senior-code', 'deep-research'])
        self.assertFalse(rec['automatic_fallback'])
        # The explicitly pinned task stays queued; nothing is switched automatically.
        choices = delegate.choose_ready([common.task(t['id'])], self.c, self.q)
        self.assertEqual(choices[0][1], None)
        self.assertEqual(choices[0][2], 'provider_billing_blocked')
        self.assertEqual(common.task(t['id'])['status'], 'queued')

    def test_billing_block_pins_deep_without_downgrade(self):
        quota.trip('kimi-for-coding', 'Insufficient Balance', 'm1')
        t = self.new(profile='auto', complexity='deep')
        profile, why = quota.route(t, self.c, self.q)
        self.assertIsNone(profile)
        self.assertEqual(why, 'provider_billing_blocked')
        rec = quota.recovery(t, self.c, self.q)
        self.assertEqual([a['profile'] for a in rec['alternatives']], ['fast-code'])

    def test_topup_recovers_and_old_errors_never_relatch(self):
        quota.trip('deepseek', 'Insufficient Balance', 'm1', occurred_at=time.time() - 100)
        self.assertFalse(quota.allowed('deepseek', self.q, 'normal')[0])
        fresh = {'state': 'ok', 'available': True, 'windows': [], 'balances': [], 'sampled_at': time.time()}
        with patch.object(quota, 'fetch_one', return_value=dict(fresh)):
            quota.refresh(force=True)
        self.assertIsNone(quota.billing_block('deepseek'))
        self.assertTrue(quota.allowed('deepseek', self.q, 'normal')[0])
        # Historical errors predating the recovery watermark never reopen the circuit.
        quota.trip('deepseek', 'Insufficient Balance', 'm1', occurred_at=time.time() - 100)
        quota.trip('deepseek', 'Insufficient Balance', 'm-unseen-old', occurred_at=time.time() - 90)
        self.assertIsNone(quota.billing_block('deepseek'))
        # A genuinely new billing failure re-arms it.
        quota.trip('deepseek', 'Insufficient Balance', 'm2')
        self.assertIsNotNone(quota.billing_block('deepseek'))

    def test_inflight_refresh_never_clears_newer_circuit(self):
        def fake_fetch(p):
            if p == 'deepseek':
                quota.trip('deepseek', 'Insufficient Balance', 'm-race')
            return {'state': 'ok', 'available': True, 'windows': [], 'balances': [],
                    'sampled_at': time.time()}
        with patch.object(quota, 'fetch_one', side_effect=fake_fetch):
            quota.refresh(force=True)
        self.assertIsNotNone(quota.billing_block('deepseek'))
        fresh = {'state': 'ok', 'available': True, 'windows': [], 'balances': [], 'sampled_at': time.time()}
        with patch.object(quota, 'fetch_one', return_value=dict(fresh)):
            quota.refresh(force=True)
        self.assertIsNone(quota.billing_block('deepseek'))

    def test_credential_rotation_releases_billing_circuit(self):
        quota.trip('deepseek', 'Insufficient Balance', 'm1')
        self.assertIsNotNone(quota.billing_block('deepseek'))
        self.identities['deepseek'] = 'cred-b'
        self.assertIsNone(quota.billing_block('deepseek'))
        self.assertTrue(quota.allowed('deepseek', self.q, 'normal')[0])
        # A recovery sample bound to the old credential must not clear the new circuit.
        quota.trip('deepseek', 'Insufficient Balance', 'm2')  # binds cred-b
        self.assertFalse(quota.clear('deepseek', sampled_since=time.time(), identity='cred-a'))
        self.assertIsNotNone(quota.billing_block('deepseek'))

    def test_view_overlays_billing_block_without_credential(self):
        quota.trip('deepseek', 'Insufficient Balance', 'm1')
        v = quota.view({'deepseek': {'state': 'ok', 'available': True, 'sampled_at': time.time()}})
        self.assertFalse(v['deepseek']['available'])
        self.assertEqual(v['deepseek']['state'], 'billing_blocked')
        self.assertIn('opened_at', v['deepseek']['billing'])
        self.assertNotIn('cred-a', json.dumps(v))

    def test_guidance_reads_historical_402_without_rearming(self):
        t = self.new()
        t = common.update(t['id'], status='failed', reason='APIError', errors=[
            {'source': 'model', 'http_status': 402, 'message': 'Insufficient Balance'}])
        g = quota.guidance(t, self.c, self.q)
        self.assertEqual(g['blocked_reason'], 'provider_billing_error')
        self.assertEqual(g['suggested_action'], 'inspect_partial_work_and_reselect_profile')
        self.assertIsNone(quota.billing_block('deepseek'))  # read-only, never re-arms
        # Completed or running jobs are never marked blocked by unrelated quota state.
        self.assertIsNone(quota.guidance(dict(t, status='completed', errors=[]), self.c, self.q))
        self.assertIsNone(quota.guidance(dict(t, status='running', errors=[]), self.c, self.q))

    def test_wait_on_blocked_queue_requests_reselection(self):
        t = self.new()
        quota.trip('deepseek', 'Insufficient Balance', 'm1')
        common.update(t['id'], recovery=quota.recovery(t, self.c, self.q))
        result = delegate.wait_result(common.task(t['id']), 1)
        self.assertFalse(result['terminal'])
        self.assertFalse(result['continue_waiting'])
        self.assertEqual(result['next_action'], 'reselect_profile_and_resubmit')
        self.assertTrue(result['attention'])
        self.assertEqual(result['recovery']['blocked_reason'], 'provider_billing_blocked')
        self.assertEqual([a['profile'] for a in result['recovery']['alternatives']],
                         ['senior-code', 'deep-research'])
        # Plain quota exhaustion with no viable alternative asks for top-up, not waiting.
        quota.clear('deepseek')
        q = {p: dict(v) for p, v in self.q.items()}
        q['deepseek']['available'] = False
        q['kimi-for-coding']['available'] = False
        self.assertEqual(quota.recovery(t, self.c, q)['suggested_action'],
                         'top_up_provider_account_or_wait_for_quota')

    MONTHLY = ("You've reached your monthly usage limit for this billing cycle. "
               "Your quota will be refreshed in the next cycle. To continue now, purchase extra "
               "usage or upgrade your plan: "
               "https://www.kimi.com/membership/subscription?tab=quota")

    def monthly_message(self, message_id='msg_monthly', completed=None):
        completed = completed or time.time()
        return {'info': {'id': message_id, 'role': 'assistant', 'providerID': 'kimi-for-coding',
                         'modelID': 'kimi-for-coding',
                         'time': {'created': int(completed * 1000) - 1000,
                                  'completed': int(completed * 1000)},
                         'error': {'name': 'APIError', 'data': {
                             'message': self.MONTHLY, 'statusCode': 403, 'isRetryable': False}}},
                'parts': []}

    def fail_monthly(self):
        t = self.new(profile='senior-code', mode='read', scopes=[])
        t = common.update(t['id'], status='running', profile='senior-code', started_at=time.time(),
                          timeout_seconds=60, directory=str(self.repo))
        result = worker.finish(common.task(t['id']), [self.monthly_message()])
        return common.task(t['id']), result

    def fresh(self, provider):
        if provider == 'kimi-for-coding':
            return {'state': 'ok', 'available': True, 'balances': [],
                    'windows': [{'name': 'window_0', 'remaining_percent': 100.0},
                                {'name': 'overall', 'remaining_percent': 60.0}],
                    'sampled_at': time.time()}
        return {'state': 'ok', 'available': True, 'balances': [{'currency': 'USD', 'remaining': 5.0}],
                'windows': [], 'sampled_at': time.time()}

    def test_hidden_monthly_limit_blocks_kimi_and_offers_deepseek_max_variant(self):
        self.c['profiles']['fast-code']['variant'] = 'max'
        self.config.write_text(json.dumps(self.c))
        t, result = self.fail_monthly()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'provider_billing_monthly_usage_limit')
        self.assertEqual(quota.billing_block('kimi-for-coding')['reason'], 'monthly_usage_limit')

        # Fresh positive usage telemetry for both providers never releases the hidden block.
        with patch.object(quota, 'fetch_one', side_effect=self.fresh):
            v = quota.refresh(force=True)
        self.assertIsNotNone(quota.billing_block('kimi-for-coding'))
        kimi = v['kimi-for-coding']
        self.assertFalse(kimi['available'])
        self.assertEqual(kimi['state'], 'billing_blocked')
        self.assertTrue(kimi['monthly_plan_exhausted'])
        self.assertEqual(kimi['billing']['reason'], 'monthly_usage_limit')
        self.assertTrue(kimi['billing']['telemetry_available'])
        self.assertIn('do not mean', kimi['billing']['warning'])
        self.assertTrue(v['deepseek']['available'])

        # K2.8 and K3 share the provider block and cannot be each other's alternative.
        auto = self.new(profile='auto')
        self.assertEqual(quota.route(auto, self.c, self.q)[1], 'provider_billing_blocked')
        rec = quota.recovery(auto, self.c, self.q)
        self.assertEqual(rec['blocked_reason'], 'provider_billing_blocked')
        self.assertEqual(rec['billing_reason'], 'monthly_usage_limit')
        self.assertEqual([a['profile'] for a in rec['alternatives']], ['fast-code'])
        self.assertEqual(rec['alternatives'][0]['variant'], 'max')
        self.assertFalse(rec['automatic_fallback'])
        self.assertTrue(rec['autonomous_reselection'])
        action = rec['autonomous_next_action']
        self.assertEqual(action['action'], 'reselect_profile_and_resubmit')
        self.assertEqual(action['preferred_variant'], 'max')
        self.assertFalse(action['requires_user_approval'])
        self.assertFalse(action['wait_for_quota'])
        self.assertIn('do not ask the user', action['instruction'])
        self.assertIn('do not wait for quota', action['instruction'])
        deep = self.new(profile='deep-research', complexity='deep')
        self.assertIsNone(quota.route(deep, self.c, self.q)[0])
        self.assertEqual([a['profile'] for a in quota.alternatives(deep, self.c, self.q)], ['fast-code'])

    def test_monthly_recovery_reaches_status_wait_collect_read_only(self):
        t, result = self.fail_monthly()
        self.assertEqual(result['reason'], 'provider_billing_monthly_usage_limit')
        self.assertEqual(result['recovery']['billing_reason'], 'monthly_usage_limit')
        billing_path = self.state / 'billing.json'
        before = billing_path.read_bytes()
        status = delegate.task_status(common.task(t['id']))
        self.assertEqual(status['recovery']['billing_reason'], 'monthly_usage_limit')
        self.assertEqual([a['profile'] for a in status['recovery']['alternatives']], ['fast-code'])
        waited = delegate.wait_result(common.task(t['id']), 1)
        self.assertTrue(waited['terminal'])
        self.assertFalse(waited['continue_waiting'])
        self.assertEqual(waited['next_action'], 'reselect_profile_and_resubmit')
        self.assertTrue(waited['attention'])
        self.assertTrue(waited['recovery']['autonomous_reselection'])
        collected = diagnostics.collect(t['id'])
        self.assertEqual(collected['recovery']['billing_reason'], 'monthly_usage_limit')
        self.assertEqual(collected['task']['recovery']['billing_reason'], 'monthly_usage_limit')
        self.assertEqual(collected['result']['reason'], 'provider_billing_monthly_usage_limit')
        # Guidance views are read-only: no circuit mutation and no historical replay.
        self.assertEqual(billing_path.read_bytes(), before)
        self.assertEqual(common.task(t['id'])['status'], 'failed')
        self.assertIsNotNone(quota.billing_block('kimi-for-coding'))

    def test_positive_refresh_cannot_clear_monthly_but_clears_balance(self):
        quota.trip('kimi-for-coding', 'monthly', 'm-k', occurred_at=time.time() - 60,
                   reason='monthly_usage_limit')
        quota.trip('deepseek', 'Insufficient Balance', 'm-d', occurred_at=time.time() - 60,
                   reason='insufficient_balance')
        with patch.object(quota, 'fetch_one', side_effect=self.fresh):
            quota.refresh(force=True)
        self.assertEqual(quota.billing_block('kimi-for-coding')['reason'], 'monthly_usage_limit')
        self.assertIsNone(quota.billing_block('deepseek'))
        # The Kimi view still refuses to advertise the positive window snapshot.
        v = quota.view(common.read_json(self.state / 'quota.json', {}))
        self.assertFalse(v['kimi-for-coding']['available'])
        self.assertNotIn('monthly_plan_exhausted', v['deepseek'])

    def test_manual_retry_release_is_authorization_not_proof(self):
        opened = time.time() - 120
        quota.trip('kimi-for-coding', self.MONTHLY, 'm-seen', occurred_at=opened,
                   reason='monthly_usage_limit')
        with self.assertRaises(ValueError):
            quota.retry_provider('unknown-provider')
        released = quota.retry_provider('kimi-for-coding')
        self.assertTrue(released['released'])
        self.assertEqual(released['action'], 'manual_retry_authorized')
        self.assertFalse(released['proven_recovery'])
        self.assertEqual(released['reason'], 'monthly_usage_limit')
        self.assertIn('not proof', released['note'])
        self.assertIn('new attempts', released['note'])
        self.assertIn('re-opens', released['note'])
        self.assertNotIn('one new attempt', released['note'])
        self.assertIsNone(quota.billing_block('kimi-for-coding'))
        # Seen IDs and the cleared_at watermark keep historical errors from relatching.
        quota.trip('kimi-for-coding', self.MONTHLY, 'm-seen', occurred_at=time.time(),
                   reason='monthly_usage_limit')
        quota.trip('kimi-for-coding', self.MONTHLY, 'm-old', occurred_at=opened,
                   reason='monthly_usage_limit')
        self.assertIsNone(quota.billing_block('kimi-for-coding'))
        # A genuinely new failure after the release re-opens the block; retry is permitted.
        quota.trip('kimi-for-coding', self.MONTHLY, 'm-new', occurred_at=time.time() + 1,
                   reason='monthly_usage_limit')
        self.assertEqual(quota.billing_block('kimi-for-coding')['reason'], 'monthly_usage_limit')

    def test_successful_reply_never_auto_clears_monthly_block(self):
        # Request credential provenance for a session reply is not verifiable here, so
        # automatic success-based clearing is intentionally absent. A completed,
        # attributed, error-free reply must leave the monthly block in place; only the
        # explicit local retry release, credential rotation or a balance replenishment
        # can clear a circuit.
        t = self.new(profile='senior-code', mode='read', scopes=[])
        t = common.update(t['id'], status='running', profile='senior-code', started_at=time.time(),
                          timeout_seconds=60, directory=str(self.repo))
        now = time.time()
        messages = [self.monthly_message('msg_err', completed=now - 50),
                    {'info': {'id': 'msg_ok', 'role': 'assistant', 'providerID': 'kimi-for-coding',
                              'modelID': 'kimi-for-coding',
                              'time': {'completed': int(now * 1000)}}, 'parts': []}]
        worker.finish(common.task(t['id']), messages)
        block = quota.billing_block('kimi-for-coding')
        self.assertIsNotNone(block)
        self.assertEqual(block['reason'], 'monthly_usage_limit')
        # A historical error ID already seen cannot relatch or clear anything.
        quota.trip('kimi-for-coding', self.MONTHLY, 'msg_err', occurred_at=now - 50,
                   reason='monthly_usage_limit')
        self.assertIsNotNone(quota.billing_block('kimi-for-coding'))
        # Explicit local retry release remains the only monthly path.
        self.assertTrue(quota.retry_provider('kimi-for-coding')['released'])
        self.assertIsNone(quota.billing_block('kimi-for-coding'))

    def test_billing_ingestion_requires_model_source_and_actual_provider(self):
        t = {'profile': 'senior-code'}
        now = time.time()
        # Tool-sourced text never trips a circuit even with billing-looking fields.
        worker.record_billing_errors(t, [{'source': 'tool', 'billing': True,
                                          'billing_reason': 'monthly_usage_limit'}])
        self.assertIsNone(quota.billing_block('kimi-for-coding'))
        self.assertIsNone(quota.billing_block('deepseek'))
        # Actual provider attribution wins over the pinned profile provider.
        worker.record_billing_errors(t, [{'source': 'model', 'provider': 'deepseek',
                                          'billing': True, 'billing_reason': 'monthly_usage_limit',
                                          'message': self.MONTHLY, 'message_id': 'm-actual',
                                          'occurred_at': now}])
        self.assertIsNotNone(quota.billing_block('deepseek'))
        self.assertIsNone(quota.billing_block('kimi-for-coding'))
        # An explicitly different, unconfigured provider is never charged to the pinned one.
        worker.record_billing_errors(t, [{'source': 'model', 'provider': 'unconfigured-x',
                                          'billing': True, 'billing_reason': 'monthly_usage_limit',
                                          'message': self.MONTHLY, 'message_id': 'm-other',
                                          'occurred_at': now}])
        self.assertIsNone(quota.billing_block('kimi-for-coding'))
        # Missing attribution falls back to the pinned provider.
        worker.record_billing_errors(t, [{'source': 'model', 'billing': True,
                                          'billing_reason': 'monthly_usage_limit',
                                          'message': self.MONTHLY, 'message_id': 'm-fallback',
                                          'occurred_at': now}])
        self.assertIsNotNone(quota.billing_block('kimi-for-coding'))

    def test_historical_balance_failure_excludes_provider_until_release(self):
        t = self.new(profile='fast-code', mode='read', scopes=[])
        t = common.update(t['id'], status='failed', reason='APIError', errors=[
            {'source': 'model', 'http_status': 402, 'message': 'Insufficient Balance'}])
        rec = quota.guidance(common.task(t['id']), self.c, self.q)
        self.assertEqual(rec['billing_reason'], 'insufficient_balance')
        self.assertEqual([a['profile'] for a in rec['alternatives']], ['senior-code', 'deep-research'])
        # A recorded quota replenishment is proven recovery for a balance block.
        quota.trip('deepseek', 'Insufficient Balance', 'm-402', reason='insufficient_balance')
        quota.clear('deepseek', evidence='quota')
        rec = quota.guidance(common.task(t['id']), self.c, self.q)
        self.assertEqual([a['profile'] for a in rec['alternatives']],
                         ['fast-code', 'senior-code', 'deep-research'])

    def test_historical_monthly_error_guides_away_from_same_provider_read_only(self):
        t = self.new(profile='senior-code', mode='read', scopes=[])
        # Legacy compact evidence: classification comes from the exact stored message.
        t = common.update(t['id'], status='failed', reason='APIError', errors=[
            {'source': 'model', 'code': 'APIError', 'http_status': 403, 'retryable': False,
             'message': self.MONTHLY}])
        before = (self.state / 'billing.json').read_bytes() if (self.state / 'billing.json').exists() else b''
        rec = quota.guidance(common.task(t['id']), self.c, self.q)
        self.assertEqual(rec['billing_reason'], 'monthly_usage_limit')
        self.assertEqual(rec['blocked_provider'], 'kimi-for-coding')
        self.assertEqual([a['profile'] for a in rec['alternatives']], ['fast-code'])
        self.assertIsNone(quota.billing_block('kimi-for-coding'))
        self.assertEqual((self.state / 'billing.json').read_bytes()
                         if (self.state / 'billing.json').exists() else b'', before)
        # An unrelated balance recovery is not proof that this monthly limit cleared.
        quota.trip('kimi-for-coding', 'Insufficient Balance', 'm-balance',
                   reason='insufficient_balance')
        quota.clear('kimi-for-coding', evidence='quota')
        rec = quota.guidance(common.task(t['id']), self.c, self.q)
        self.assertEqual([a['profile'] for a in rec['alternatives']], ['fast-code'])
        # After explicit manual retry authorization, the provider is a candidate again.
        quota.trip('kimi-for-coding', self.MONTHLY, 'm-manual', reason='monthly_usage_limit')
        quota.retry_provider('kimi-for-coding')
        rec = quota.guidance(common.task(t['id']), self.c, self.q)
        self.assertEqual([a['profile'] for a in rec['alternatives']],
                         ['fast-code', 'senior-code', 'deep-research'])

    def test_monthly_without_alternative_reports_specific_blockage(self):
        self.c['profiles']['fast-code']['enabled'] = False
        self.config.write_text(json.dumps(self.c))
        t, result = self.fail_monthly()
        self.assertEqual(result['reason'], 'provider_billing_monthly_usage_limit')
        rec = quota.recovery(t, self.c, self.q)
        self.assertEqual(rec['billing_reason'], 'monthly_usage_limit')
        self.assertEqual(rec['alternatives'], [])
        self.assertFalse(rec['autonomous_reselection'])
        self.assertEqual(rec['suggested_action'], 'top_up_or_authorize_manual_retry')
        self.assertFalse(rec['autonomous_next_action']['wait_for_quota'])
        self.assertIn('quota --retry-provider kimi-for-coding',
                      rec['autonomous_next_action']['retry_command'])
        waited = delegate.wait_result(common.task(t['id']), 0)
        self.assertTrue(waited['terminal'])
        self.assertTrue(waited['attention'])
        self.assertEqual(waited['next_action'], 'top_up_or_authorize_manual_retry')

    def running(self, owner, scope, profile='fast-code', group=None):
        t = self.new(scopes=[scope], profile=profile)
        t.update(status='running', profile=profile, owner_thread_id=owner,
                 group_id=group if group is not None else t['group_id'])
        return t

    def test_plain_exhaustion_and_recovery_reach_all_coordinator_views(self):
        import diagnostics
        t = self.new(profile='senior-code', mode='read', scopes=[])
        q = {p: dict(v) for p, v in self.q.items()}
        q['kimi-for-coding']['available'] = False
        common.write_json(self.state / 'quota.json', q)
        blocked = delegate.wait_result(t, 0)
        self.assertFalse(blocked['continue_waiting'])
        self.assertEqual(blocked['next_action'], 'reselect_profile_and_resubmit')
        self.assertEqual([p['profile'] for p in blocked['recovery']['alternatives']], ['fast-code'])
        t = common.update(t['id'], recovery=blocked['recovery'])
        common.write_json(self.state / 'quota.json', self.q)
        self.assertNotIn('recovery', delegate.task_status(t))
        healthy = delegate.wait_result(t, 0)
        self.assertTrue(healthy['continue_waiting'])
        self.assertNotIn('recovery', healthy)
        collected = diagnostics.collect(t['id'])
        self.assertNotIn('recovery', collected)
        self.assertNotIn('recovery', collected['task'])

    def test_owner_cap_blocks_fifth_same_conversation(self):
        acts = [self.running('thread-a', 'f%d.txt' % i) for i in range(4)]
        fifth = self.new(scopes=['f5.txt'])
        fifth['owner_thread_id'] = 'thread-a'
        self.assertEqual(delegate.choose_ready(acts + [fifth], self.c, self.q)[0][2],
                         'owner_at_capacity')

    def test_independent_conversations_have_no_shared_cap(self):
        acts = [self.running('thread-a', 'a%d.txt' % i) for i in range(4)]
        acts += [self.running('thread-b', 'b%d.txt' % i) for i in range(4)]
        # Eight actives far exceed the legacy max_parallel=3 fixture; no global cap applies.
        queued = self.new(scopes=['c.txt'])
        queued['owner_thread_id'] = 'thread-c'
        self.assertEqual(delegate.choose_ready(acts + [queued], self.c, self.q)[0][1], 'fast-code')
        blocked = self.new(scopes=['a5.txt'])
        blocked['owner_thread_id'] = 'thread-a'
        self.assertEqual(delegate.choose_ready(acts + [blocked], self.c, self.q)[0][2],
                         'owner_at_capacity')

    def test_legacy_provider_limits_do_not_constrain_owners(self):
        acts = [self.running('t%d' % i, 'k%d.txt' % i, profile='senior-code') for i in range(3)]
        queued = self.new(profile='senior-code', scopes=['k9.txt'])
        queued['owner_thread_id'] = 't9'
        # Legacy max_kimi_parallel=1 must not constrain separate owners.
        self.assertEqual(delegate.choose_ready(acts + [queued], self.c, self.q)[0][1],
                         'senior-code')

    def test_group_id_never_overrides_owner_thread(self):
        acts = [self.running('thread-a', 'g%d.txt' % i, group='shared') for i in range(4)]
        other = self.new(scopes=['g9.txt'])
        other.update(owner_thread_id='thread-b', group_id='shared')
        self.assertEqual(delegate.choose_ready(acts + [other], self.c, self.q)[0][1], 'fast-code')

    def test_legacy_owner_fallback_group_then_source(self):
        acts = []
        for i in range(4):
            t = self.new(scopes=['h%d.txt' % i])
            t.update(status='running', profile='fast-code', owner_thread_id=None, group_id='legacy-group')
            acts.append(t)
        fifth = self.new(scopes=['h5.txt'])
        fifth.update(owner_thread_id=None, group_id='legacy-group')
        self.assertEqual(delegate.choose_ready(acts + [fifth], self.c, self.q)[0][2],
                         'owner_at_capacity')
        bare = []
        for i in range(4):
            t = self.new(scopes=['s%d.txt' % i])
            t.update(status='running', profile='fast-code', owner_thread_id=None, group_id=None)
            bare.append(t)
        sixth = self.new(scopes=['s5.txt'])
        sixth.update(owner_thread_id=None, group_id=None)
        self.assertEqual(delegate.choose_ready(bare + [sixth], self.c, self.q)[0][2],
                         'owner_at_capacity')

    def test_per_owner_cap_validation_defaults_four(self):
        self.assertEqual(delegate.per_owner_cap({}), 4)
        for bad in (True, 'junk', 0, 17, 2.5):
            self.assertEqual(delegate.per_owner_cap(dict(self.c, max_parallel_per_owner=bad)), 4)
        c = dict(self.c, max_parallel_per_owner=1)
        a = self.new(scopes=['x1.txt'])
        a.update(status='running', profile='fast-code')
        b = self.new(scopes=['x2.txt'])
        self.assertEqual(delegate.choose_ready([a, b], c, self.q)[0][2], 'owner_at_capacity')

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

    def console_server(self):
        import console_auth
        server = ThreadingHTTPServer(('127.0.0.1', 0), console_server.Handler)
        server.daemon_threads = True
        self.c['console_url'] = 'http://127.0.0.1:' + str(server.server_port)
        self.config.write_text(json.dumps(self.c))
        server.cookie_name = 'delegate_console_session_' + str(server.server_port)
        server.allowed_origins = console_auth.origin_tuples(self.c)
        server.auth_root = self.state
        console_auth.set_user('admin', 'synthetic-pass', self.state)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def console_call(self, server, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        request_headers = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            request_headers.setdefault('Content-Type', 'application/json')
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        result = (response.status, {k.lower(): v for k, v in response.getheaders()}, data)
        connection.close()
        return result

    def test_console_local_auth_and_cross_origin_guards(self):
        server = self.console_server()
        try:
            status, headers, _ = self.console_call(server, 'GET', '/console', headers={'Accept': 'text/html'})
            self.assertEqual(status, 302)
            self.assertEqual(headers['location'], '/console-login')
            login_body = {'username': 'admin', 'password': 'synthetic-pass'}
            for method, path, extra, code in [
                ('GET', '/console-api/state', {}, 401),
                ('GET', '/console', {'Host': 'attacker.example'}, 403),
                ('GET', '/console', {'Origin': 'https://attacker.example'}, 403),
                ('GET', '/console', {'Sec-Fetch-Site': 'cross-site'}, 403),
                ('POST', '/console-api/auth/login', {'Sec-Fetch-Site': 'cross-site'}, 403),
            ]:
                body = login_body if method == 'POST' else None
                self.assertEqual(self.console_call(server, method, path, body=body, headers=extra)[0], code)
            status, headers, _ = self.console_call(server, 'POST', '/console-api/auth/login', body=login_body)
            self.assertEqual(status, 200)
            self.assertIn('HttpOnly', headers['set-cookie'])
            cookie = headers['set-cookie'].split(';')[0]
            with patch.object(console_server, 'state', return_value={'tasks': [], 'quota': {}}):
                status, _, data = self.console_call(server, 'GET', '/console-api/state',
                                                    headers={'Cookie': cookie})
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(data)['tasks'], [])
            status, _, data = self.console_call(server, 'GET', '/console', headers={'Cookie': cookie})
            self.assertEqual(status, 200)
            self.assertIn(b'Worker Desk', data)
        finally:
            server.shutdown()
            server.server_close()

    def test_console_asset_allowlist_serves_i18n_and_rejects_unknown(self):
        server = self.console_server()
        try:
            # The i18n bundle is public (the login page reuses it) and served as JavaScript.
            status, headers, body = self.console_call(server, 'GET', '/console-assets/i18n.js')
            self.assertEqual(status, 200)
            self.assertTrue(headers['content-type'].startswith('text/javascript'))
            self.assertIn(b'worker-desk-locale', body)
            self.assertIn(b'global.I18n', body)
            # Application assets stay behind the explicit login session.
            self.assertEqual(self.console_call(server, 'GET', '/console-assets/app.js')[0], 401)
            login = self.console_call(server, 'POST', '/console-api/auth/login',
                                      body={'username': 'admin', 'password': 'synthetic-pass'})
            cookie = login[1]['set-cookie'].split(';')[0]
            self.assertEqual(self.console_call(server, 'GET', '/console-assets/app.js',
                                               headers={'Cookie': cookie})[0], 200)
            # Unknown or traversal asset paths are rejected without reaching the allowlist.
            for path in ('/console-assets/unknown.js', '/console-assets/../i18n.js', '/console-assets/'):
                self.assertEqual(self.console_call(server, 'GET', path, headers={'Cookie': cookie})[0], 404)
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
