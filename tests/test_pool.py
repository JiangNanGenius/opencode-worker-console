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
