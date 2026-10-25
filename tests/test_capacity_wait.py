"""Real durable task records, fake native transport: no model or credential I/O."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import delegate
import quota
import routing
import worker
import bailian_pool


class CapacityWaitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / 'state'
        self.now = 2000000000.0
        self.c = {'auto_approve': False, 'max_parallel_per_owner': 1,
                  'routing': {'ultra': 'ultra', 'deep': 'deep'},
                  'profiles': {'ultra': {'model': 'kimi-for-coding/k3.1-pro', 'variant': 'max'},
                               'deep': {'model': 'deepseek/strong'}}}
        self.q = {'kimi-for-coding': {'state': 'ok', 'available': True, 'stale': False,
                  'sampled_at': self.now - 1, 'windows': [
                      {'name': 'overall', 'duration_minutes': 10080, 'remaining_percent': 80,
                       'remaining': 80, 'valid': True}]}}
        self.patches = [patch.object(m, 'STATE', self.state)
                        for m in (common, worker, quota, routing, delegate)]
        self.patches += [patch.object(m, 'config', return_value=self.c)
                         for m in (common, worker, quota, delegate)]
        self.patches += [patch.object(worker.time, 'time', side_effect=lambda: self.now),
                         patch.object(worker, 'credential_identity', return_value='current-key-id'),
                         patch.object(quota, 'credential_identity', return_value='current-key-id'),
                         patch.object(quota, 'view', side_effect=lambda raw: self.q)]
        for p in self.patches:
            p.start()
        common.init()
        self.t = {'id': 'job-wait', 'title': 'Finish bounded work', 'status': 'running',
                  'created_at': self.now - 100,
                  'objective': 'Verify remaining deployment; deployment already performed.',
                  'mode': 'write', 'workspace': 'shared', 'commands': [], 'scopes': ['a.txt'], 'resources': ['fixture'],
                  'directory': self.tmp.name, 'source_dir': self.tmp.name, 'owner_thread_id': 'owner',
                  'session_id': 'original-session', 'message_id': 'original-message',
                  'dispatch_attempted_at': self.now - 100, 'profile': 'ultra',
                  'tier': 'ultra', 'requested_profile': 'auto', 'auto_approve': False,
                  '_billing_dispatch': {'provider': 'kimi-for-coding',
                                        'identity': 'current-key-id', 'at': self.now - 100}}
        common.write_json(common.task_path(self.t['id']), self.t)
        self.messages = [{'info': {'id': 'old-assistant', 'role': 'assistant'}, 'parts': [
            {'type': 'tool', 'tool': 'bash', 'callID': 'deploy-done',
             'state': {'status': 'completed', 'output': 'private output must not be copied'}}]}]

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def park(self, trigger='provider_usage_or_rate_limit'):
        with patch.object(quota, 'route', return_value=(None, 'unavailable')):
            return worker.reroute_after_capacity_stop(
                common.task('job-wait'), 'kimi-for-coding', trigger, self.messages)

    def restore(self):
        self.now += 60
        self.q['kimi-for-coding']['sampled_at'] = self.now

    def resume(self, messages=None, status=None):
        return worker.resume_capacity_wait(common.task('job-wait'),
                                           self.messages if messages is None else messages,
                                           {'type': 'idle'} if status is None else status)

    def test_wait_then_recovery_dispatches_once_with_checkpoint_and_same_identity(self):
        self.assertTrue(self.park())
        waiting = common.task('job-wait')
        self.assertEqual(waiting['status'], 'running')
        self.assertEqual(waiting['reason'], 'capacity_wait')
        self.assertEqual(waiting['session_id'], self.t['session_id'])
        self.assertEqual(waiting['dispatch_attempted_at'], self.t['dispatch_attempted_at'])
        with patch.object(worker, 'call', return_value={}) as call:
            self.assertTrue(self.resume())  # Old positive telemetry cannot recover.
            call.assert_not_called()
            self.restore()
            self.assertTrue(self.resume())
            sent = common.task('job-wait')
            self.assertEqual(sent['tier'], 'ultra')
            self.assertEqual(sent['profile'], 'ultra')
            self.assertEqual(sent['session_id'], self.t['session_id'])
            body = call.call_args.args[3]
            self.assertEqual(body['model']['modelID'], 'k3.1-pro')
            text = body['parts'][0]['text']
            self.assertIn('deploy-done', text)
            self.assertIn('Never repeat completed', text)
            self.assertNotIn('private output', text)
            self.assertNotEqual(sent['message_id'], self.t['message_id'])
            # Simulate a daemon restart from the on-disk intent, not a live object.
            for _ in range(3):
                self.assertTrue(self.resume())
            self.assertEqual(call.call_count, 1)
            ack = [{'info': {'id': sent['message_id'], 'role': 'user'}}]
            self.assertTrue(self.resume(ack))
            self.assertEqual(common.task('job-wait')['_capacity_wait']['phase'], 'sent')
            ack.append({'info': {'id': 'new-assistant', 'role': 'assistant',
                                 'parentID': sent['message_id']}})
            self.assertFalse(self.resume(ack))
            self.assertIsNone(common.task('job-wait')['_capacity_wait'])
            self.assertEqual(call.call_count, 1)

    def test_lost_ack_observes_durable_id_and_never_replays(self):
        self.park()
        self.restore()
        with patch.object(worker, 'call', side_effect=common.HttpFailure(None, 'lost')) as call:
            self.assertTrue(self.resume())
            intent = common.task('job-wait')
            self.assertEqual(intent['status'], 'uncertain')
            for _ in range(3):
                self.resume()
            self.assertEqual(call.call_count, 1)
            self.assertFalse(self.resume([{'info': {'id': 'response', 'role': 'assistant',
                                                  'parentID': intent['message_id']}}]))
            self.assertEqual(call.call_count, 1)

    def test_crash_after_persist_before_io_never_replays(self):
        self.park()
        w = common.task('job-wait')['_capacity_wait']
        common.update('job-wait', _capacity_wait=dict(w, phase='intent', message_id='durable-intent'),
                      message_id='durable-intent', status='uncertain')
        self.restore()
        with patch.object(worker, 'call') as call:
            self.resume()
            call.assert_not_called()

    def test_cancel_and_pending_retier_own_the_boundary(self):
        self.park()
        self.restore()
        with patch.object(worker, 'call') as call:
            common.update('job-wait', cancel_requested=True)
            self.resume()
            common.update('job-wait', cancel_requested=False, tier_change={'status': 'pending'})
            self.resume()
            call.assert_not_called()
            common.update('job-wait', tier_change={'status': 'applied'}, message_id='retier-owned')
            self.assertFalse(self.resume())
            call.assert_not_called()

    def test_busy_or_auth_or_unknown_trigger_cannot_dispatch(self):
        self.assertFalse(self.park('provider_billing_credential_rejected'))
        self.assertFalse(self.park('unknown_transport_error'))
        self.park()
        self.restore()
        with patch.object(worker, 'call') as call:
            self.resume(status={'type': 'busy'})
            self.q['kimi-for-coding'].update(state='auth_error', available=False)
            self.resume()
            call.assert_not_called()

    def test_auth_wrapped_as_gate_429_never_parks_or_falls_back(self):
        self.messages[-1]['info']['error'] = {'name': 'APIError', 'data': {
            'statusCode': 429, 'message': 'Kimi quota admission limit: credential_rejected'}}
        with patch.object(worker, 'call') as call:
            self.assertFalse(self.park())
            self.assertIsNone(common.task('job-wait').get('_capacity_wait'))
            call.assert_not_called()

    def test_explicit_monthly_override_cannot_authorize_automatic_resume(self):
        common.update('job-wait', requested_profile='ultra')
        self.assertTrue(self.park('provider_billing_monthly_usage_limit'))
        self.restore()
        with patch.object(quota, 'route', return_value=('ultra', 'explicit_profile_monthly_retry')), \
             patch.object(quota, 'allowed', return_value=(False, 'provider_billing_blocked')), \
             patch.object(worker, 'call') as call:
            self.resume()
            call.assert_not_called()

    def test_rejected_continuation_never_automatically_retries(self):
        self.park()
        self.restore()
        with patch.object(worker, 'call', side_effect=common.HttpFailure(401, 'refused')) as call:
            self.resume()
            self.resume()
            self.assertEqual(call.call_count, 1)
            self.assertEqual(common.task('job-wait')['_capacity_wait']['phase'], 'blocked')

    def test_wait_preserves_owner_resource_admission_and_low_ultra_limit(self):
        self.park()
        queued = dict(self.t, id='job-next', status='queued')
        selected = delegate.choose_ready([common.task('job-wait'), queued], self.c, self.q)
        self.assertEqual(selected[0][2], 'scope_or_resource_in_use')
        self.c['routing']['background'] = 'deep'
        disjoint = dict(queued, tier='normal', profile='deep', scopes=['b.txt'], resources=['different'])
        self.assertEqual(delegate.choose_ready([common.task('job-wait'), disjoint], self.c, self.q)[0][1],
                         'deep')
        waiting = common.task('job-wait')['_capacity_wait']
        common.update('job-wait', _capacity_wait=dict(waiting, phase='intent'))
        self.assertEqual(delegate.choose_ready([common.task('job-wait'), disjoint], self.c, self.q)[0][2],
                         'owner_at_capacity')
        common.update('job-wait', _capacity_wait=waiting)
        self.restore()
        other = dict(self.t, id='job-other', owner_thread_id='another', _capacity_wait=None)
        common.write_json(common.task_path(other['id']), other)
        with patch.object(quota, 'kimi_ultra_low_weekly', return_value=(True, 1)), \
             patch.object(worker, 'call') as call:
            self.resume()
            call.assert_not_called()

    def test_zero_and_stale_windows_or_rotated_credentials_keep_exclusion(self):
        self.park()
        self.restore()
        for change in ({'stale': True}, {'available': False}):
            with self.subTest(change=change):
                q = json.loads(json.dumps(self.q))
                q['kimi-for-coding'].update(change)
                self.assertEqual(worker.recovered_capacity_exclusions(
                    common.task('job-wait'), self.c, q)['excluded_providers'], ['kimi-for-coding'])
        with patch.object(worker, 'credential_identity', return_value='different-key'):
            self.assertEqual(worker.recovered_capacity_exclusions(
                common.task('job-wait'), self.c, self.q)['excluded_providers'], ['kimi-for-coding'])

    def test_monthly_exclusion_needs_guarded_release_as_well_as_fresh_windows(self):
        self.park('provider_billing_monthly_usage_limit')
        self.restore()
        t = common.task('job-wait')
        self.assertEqual(worker.recovered_capacity_exclusions(t, self.c, self.q)
                         ['excluded_providers'], ['kimi-for-coding'])
        common.write_json(self.state / quota.BILLING, {'kimi-for-coding': {
            'credential': 'current-key-id', 'cleared_at': self.now,
            'released': 'scheduled_reset', 'reason': 'monthly_usage_limit'}})
        self.assertEqual(worker.recovered_capacity_exclusions(t, self.c, self.q)
                         ['excluded_providers'], [])

    def test_cancel_arriving_during_route_wins_before_durable_intent(self):
        self.park()
        self.restore()
        route = quota.route

        def cancel_during_route(*args, **kwargs):
            result = route(*args, **kwargs)
            common.request_cancel('job-wait', 'user_requested')
            return result

        with patch.object(quota, 'route', side_effect=cancel_during_route), \
             patch.object(worker, 'call') as call:
            self.resume()
            call.assert_not_called()
            self.assertEqual(common.task('job-wait')['_capacity_wait']['phase'], 'waiting')

    def test_retained_quota_exclusion_recovers_only_from_real_current_key_success(self):
        self.park()
        self.restore()
        t = common.task('job-wait')
        t.pop('_capacity_exclusions')
        t['route_history'] = [{'from_provider': 'kimi-for-coding',
                               'trigger': 'provider_usage_or_rate_limit', 'at': self.now - 10}]
        self.assertEqual(worker.recovered_capacity_exclusions(t, self.c, self.q)
                         ['excluded_providers'], ['kimi-for-coding'])
        common.write_json(self.state / quota.BILLING, {'kimi-for-coding': {
            'credential': 'current-key-id', 'cleared_at': self.now,
            'released': 'model_success', 'reason': 'monthly_usage_limit'}})
        self.assertEqual(worker.recovered_capacity_exclusions(t, self.c, self.q)
                         ['excluded_providers'], [])
        t['route_history'][0]['trigger'] = 'credential_rejected'
        self.assertEqual(worker.recovered_capacity_exclusions(t, self.c, self.q)
                         ['excluded_providers'], ['kimi-for-coding'])

    def test_child_does_not_inherit_already_verified_recovered_exclusion(self):
        self.park()
        self.restore()
        common.update('job-wait', status='failed', group_id='test-group',
                      recovery={'blocked_provider': 'kimi-for-coding'})
        spec = {'directory': self.tmp.name, 'objective': 'Continue remaining verification',
                'mode': 'read', 'profile': 'auto', 'tier': 'ultra',
                'parent_task_id': 'job-wait', 'group_id': 'test-group'}
        child = common.task(delegate.submit(spec)['id'])
        self.assertEqual(child['excluded_providers'], [])
        # Mere aggregate or stale display cannot turn the old block healthy.
        self.q['kimi-for-coding']['stale'] = True
        blocked = common.task(delegate.submit(spec)['id'])
        self.assertEqual(blocked['excluded_providers'], ['kimi-for-coding'])

    def test_real_run_loop_idle_quota_error_waits_and_restart_continues_only_once(self):
        # Exercise both callers and real durable records, not just the helper.
        class OnePause:
            stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, seconds):
                self.stopped = True

        messages = [{'info': {'id': 'original-message', 'role': 'user'}},
                    {'info': {'id': 'quota-stop', 'role': 'assistant',
                              'parentID': 'original-message', 'providerID': 'kimi-for-coding',
                              'modelID': 'k3.1-pro', 'error': {
                                  'name': 'APIError', 'data': {'statusCode': 429,
                                                             'message': 'quota exhausted'}}}}]
        posts = []
        directories = []

        def api(path, directory=None, *args, **kwargs):
            if path == '/session/status':
                directories.append(directory)
                return {'original-session': {'type': 'idle'}}
            return []

        def call(t, path, method='GET', body=None):
            if path == '/message':
                return messages
            self.assertEqual(path, '/prompt_async')
            posts.append(body)

        with patch.object(worker, 'api', side_effect=api), \
             patch.object(worker, 'call', side_effect=call):
            worker.run_task('job-wait', OnePause())
            self.assertEqual(common.task('job-wait')['reason'], 'capacity_wait')
            self.assertEqual(posts, [])
            self.restore()
            worker.run_task('job-wait', OnePause())
            self.assertEqual(len(posts), 1)
            worker.run_task('job-wait', OnePause())
            self.assertEqual(len(posts), 1)
            # A user receipt with the old quota error still last is not enough
            # to finish the task or generate another continuation.
            messages.append({'info': {'id': posts[0]['messageID'], 'role': 'user'}})
            worker.run_task('job-wait', OnePause())
            self.assertEqual(len(posts), 1)
            self.assertEqual(common.task('job-wait')['_capacity_wait']['phase'], 'sent')
            self.assertTrue(directories)
            self.assertEqual(set(directories), {self.tmp.name})

    def test_live_fallback_returns_to_recovered_preferred_at_owned_idle_checkpoint(self):
        self.c['routing_policy'] = {'background': [[{'profile': 'ultra', 'weight': 1}],
                                                   [{'profile': 'deep', 'weight': 1}]]}
        common.update('job-wait', tier='normal', profile='deep',
                      excluded_providers=['kimi-for-coding'],
                      _capacity_exclusions={'kimi-for-coding': {
                          'identity': 'current-key-id', 'at': self.now - 180,
                          'reason': 'provider_usage_or_rate_limit'}},
                      route_history=[{'from_provider': 'kimi-for-coding', 'from_profile': 'ultra',
                                      'to_profile': 'deep', 'at': self.now - 180,
                                      'trigger': 'provider_usage_or_rate_limit'}])
        checkpoint = [{'info': {'id': 'fallback-turn', 'role': 'assistant',
                                'parentID': 'original-message',
                                'time': {'completed': self.now - 1}},
                       'parts': self.messages[0]['parts']}]
        with patch.object(worker, 'call', return_value={}) as call:
            self.assertFalse(worker.proactive_reroute_if_needed(
                common.task('job-wait'), {'type': 'busy'}, checkpoint))
            call.assert_not_called()
            self.assertTrue(worker.proactive_reroute_if_needed(
                common.task('job-wait'), {'type': 'idle'}, checkpoint))
            call.assert_not_called()  # first persist the waiting checkpoint
            self.resume(checkpoint)
            self.assertEqual(call.call_count, 1)
            t = common.task('job-wait')
            self.assertEqual(t['profile'], 'ultra')
            self.assertEqual(t['tier'], 'normal')
            self.assertEqual(t['session_id'], 'original-session')
            self.assertEqual(t['route_history'][-1]['trigger'], 'preferred_route_restored')
            self.assertEqual(t['excluded_providers'], [])
            self.resume(checkpoint)
            self.assertEqual(call.call_count, 1)

    def test_preferred_return_never_restarts_terminal_or_pinned_or_final_report(self):
        checkpoint = [{'info': {'id': 'a', 'role': 'assistant', 'parentID': 'original-message',
                                'time': {'completed': self.now - 1}}, 'parts': []}]
        with patch.object(worker, 'call') as call:
            for over in ({'status': 'completed'}, {'requested_profile': 'deep'},
                         {'_capacity_return_at': self.now - 1}):
                t = dict(self.t, **over)
                common.write_json(common.task_path('job-wait'), t)
                self.assertFalse(worker.proactive_reroute_if_needed(t, {'type': 'idle'}, checkpoint))
            report = {'outcome': 'done', 'summary': 'done', 'evidence': [], 'tests': [], 'unresolved': []}
            checkpoint[0]['parts'] = [{'type': 'text', 'text': json.dumps(report)}]
            common.write_json(common.task_path('job-wait'), self.t)
            self.assertFalse(worker.proactive_reroute_if_needed(self.t, {'type': 'idle'}, checkpoint))
            call.assert_not_called()

    def test_deepseek_actual_balance_shape_authorizes_bounded_retry_with_backoff(self):
        self.c['routing']['background'] = 'deep'
        common.update('job-wait', tier='normal', profile='deep', _billing_dispatch={
            'provider': 'deepseek', 'identity': 'current-key-id', 'at': self.now - 100})
        normalized = quota.normalize('deepseek', {'is_available': True, 'balance_infos': [
            {'currency': 'USD', 'total_balance': '0'},
            {'currency': 'CNY', 'total_balance': '1.25'}]})
        self.q['deepseek'] = dict(normalized, state='ok', stale=False, sampled_at=self.now)
        self.assertEqual(self.q['deepseek']['windows'], [])

        def park_deepseek():
            with patch.object(quota, 'route', return_value=(None, 'no_capacity')):
                return worker.reroute_after_capacity_stop(common.task('job-wait'), 'deepseek',
                                                        'provider_usage_or_rate_limit', self.messages)

        self.assertTrue(park_deepseek())
        before = common.task('job-wait')['_capacity_exclusions']['deepseek']
        self.assertTrue(park_deepseek())
        self.assertEqual(common.task('job-wait')['_capacity_exclusions']['deepseek'], before)
        with patch.object(worker, 'call', return_value={}) as call:
            self.now += 59
            self.q['deepseek']['sampled_at'] = self.now
            self.resume()
            call.assert_not_called()
            self.now += 1
            self.q['deepseek']['sampled_at'] = self.now
            self.resume()
            self.assertEqual(call.call_count, 1)
            self.assertEqual(common.task('job-wait')['_capacity_exclusions']['deepseek']['retry_count'], 1)
            # A second real rejection must retain the first attempt's count.
            self.assertTrue(park_deepseek())
            t = common.task('job-wait')
            self.assertEqual(t['_capacity_exclusions']['deepseek']['retry_count'], 2)
            self.now += 119
            self.q['deepseek']['sampled_at'] = self.now
            self.resume()
            self.assertEqual(call.call_count, 1)
            self.now += 1
            self.q['deepseek']['sampled_at'] = self.now
            self.resume()
            self.assertEqual(call.call_count, 2)
        # A real completed current-key response decays the next failure to 60s.
        t = common.task('job-wait')
        self.now += 1
        success = [{'info': {'role': 'assistant', 'providerID': 'deepseek', 'finish': 'stop',
                             'time': {'created': self.now, 'completed': self.now + 1},
                             'tokens': {'output': 1}}, 'parts': []}]
        worker.record_billing_success(t, success)
        self.now += 2
        self.assertTrue(park_deepseek())
        entry = common.task('job-wait')['_capacity_exclusions']['deepseek']
        self.assertEqual(entry['retry_count'], 1)
        self.assertEqual(entry['retry_after'], self.now + 60)
        common.update('job-wait', _capacity_wait=None, _capacity_exclusions={
            'deepseek': dict(entry, retry_count=15)})
        self.assertTrue(park_deepseek())
        self.assertEqual(common.task('job-wait')['_capacity_exclusions']['deepseek']['retry_after'],
                         self.now + 3600)

    def test_deepseek_balance_retry_rejects_zero_stale_unknown_and_auth(self):
        self.c['routing']['background'] = 'deep'
        common.update('job-wait', tier='normal', profile='deep', _billing_dispatch={
            'provider': 'deepseek', 'identity': 'current-key-id', 'at': self.now - 100})
        with patch.object(quota, 'route', return_value=(None, 'unavailable')):
            worker.reroute_after_capacity_stop(common.task('job-wait'), 'deepseek',
                                              'provider_usage_or_rate_limit', self.messages)
        self.now += 3601
        base = {'state': 'ok', 'available': True, 'stale': False, 'sampled_at': self.now,
                'windows': [], 'balances': [{'currency': 'USD', 'remaining': 2.0}]}
        with patch.object(worker, 'call') as call:
            for changes in ({'balances': [{'currency': 'USD', 'remaining': 0}]}, {'stale': True},
                            {'state': 'auth_error'}, {'state': 'unknown'}, {'available': False},
                            {'live_error': {'state': 'unavailable'}}, {'sampled_at': self.now - 4000}):
                self.q['deepseek'] = dict(base, **changes)
                self.resume()
            call.assert_not_called()

    def test_bailian_recovery_hook_uses_explicit_dict_and_exact_stop_time(self):
        provider = worker.BAILIAN_PROVIDER
        self.c['profiles']['pool'] = {'model': provider + '/qwen'}
        self.c['routing']['background'] = 'pool'
        self.c['bailian_pool'] = {'enabled': True}
        t = dict(self.t, tier='normal', profile='pool', excluded_providers=[provider],
                 _capacity_exclusions={provider: {'at': self.now - 100,
                 'reason': 'provider_usage_or_rate_limit', 'identity': 'old-pool-identity'}})
        self.q[provider] = {'state': 'ok', 'available': True, 'sampled_at': self.now,
                            'stale': False, 'windows': [{'remaining_percent': 99, 'valid': True}]}
        for result in ({}, {'available': False, 'healthy': True},
                       {'available': True, 'healthy': False, 'authorized_probe': False},
                       {'available': 'true', 'authorized_probe': True},
                       {'available': True, 'healthy': True, 'last_success_at': self.now},
                       {'available': True, 'healthy': False, 'authorized_probe': True}):
            with self.subTest(result=result), patch.object(bailian_pool, 'recovery_available',
                                                           create=True, return_value=result) as hook:
                eligible = result.get('available') is True and (
                    result.get('healthy') is True or result.get('authorized_probe') is True)
                self.assertEqual(worker.recovered_capacity_exclusions(t, self.c, self.q)
                                 ['excluded_providers'], [] if eligible else [provider])
                hook.assert_called_once_with(self.c, after=self.now - 100)
        # No historical identity is guessed: a trustworthy quota hop delegates
        # only current retry eligibility to the generation-aware pool hook.
        t.pop('_capacity_exclusions')
        t['route_history'] = [{'from_provider': provider, 'trigger': 'provider_usage_or_rate_limit',
                               'at': self.now - 200}]
        with patch.object(bailian_pool, 'recovery_available', create=True,
                          return_value={'available': True, 'authorized_probe': True}) as hook:
            self.assertEqual(worker.recovered_capacity_exclusions(t, self.c, self.q)
                             ['excluded_providers'], [])
            hook.assert_called_once_with(self.c, after=self.now - 200)
        t['route_history'] = []  # An operator exclusion has no automatic authority.
        with patch.object(bailian_pool, 'recovery_available', create=True) as hook:
            self.assertEqual(worker.recovered_capacity_exclusions(t, self.c, self.q)
                             ['excluded_providers'], [provider])
            hook.assert_not_called()

    def test_bailian_hook_cannot_clear_auth_or_billing_or_quota_floor(self):
        provider = worker.BAILIAN_PROVIDER
        self.c['profiles']['pool'] = {'model': provider + '/qwen'}
        self.c['routing']['background'] = 'pool'
        self.c['bailian_pool'] = {'enabled': True}
        t = dict(self.t, tier='normal', profile='pool', excluded_providers=[provider],
                 _capacity_exclusions={provider: {'at': self.now - 100,
                 'reason': 'provider_usage_or_rate_limit', 'identity': 'current-key-id'}})
        self.q[provider] = {'state': 'ok', 'available': True, 'windows': []}
        with patch.object(bailian_pool, 'recovery_available', create=True,
                          return_value={'available': True, 'healthy': True}) as hook:
            with patch.object(quota, 'billing_block', return_value={'reason': 'credential_rejected'}):
                self.assertEqual(worker.recovered_capacity_exclusions(t, self.c, self.q)
                                 ['excluded_providers'], [provider])
                self.assertTrue(hook.called)
            self.q[provider]['available'] = False
            self.assertEqual(worker.recovered_capacity_exclusions(t, self.c, self.q)
                             ['excluded_providers'], [provider])
            self.q[provider]['available'] = True
            t.update(tier='ultra', capability_floor='ultra')
            self.assertEqual(worker.recovered_capacity_exclusions(t, self.c, self.q)
                             ['excluded_providers'], [provider])

    def test_ready_original_wait_reserves_slot_before_new_work_but_blocked_wait_does_not(self):
        self.park()
        self.c['routing']['background'] = 'deep'
        new = dict(self.t, id='job-new', tier='normal', status='queued', profile='deep',
                   scopes=['different.txt'], resources=['other'])
        original = common.task('job-wait')
        self.assertEqual(delegate.choose_ready([new, original], self.c, self.q)[0][1], 'deep')
        self.restore()
        choices = delegate.choose_ready([new, original], self.c, self.q)
        self.assertEqual(choices[0][1:], (None, 'owner_at_capacity'))
        # Once the original has really resumed, its intent still owns the slot.
        with patch.object(worker, 'call', return_value={}):
            self.resume()
        self.assertEqual(delegate.choose_ready([new, common.task('job-wait')], self.c, self.q)
                         [0][2], 'owner_at_capacity')

    def test_recovery_snapshot_does_no_callback_io_under_scheduler_lock_and_fences_stale_views(self):
        self.park()
        self.restore()
        original = common.task('job-wait')
        self.c['routing']['background'] = 'deep'
        new = dict(self.t, id='job-new', tier='normal', status='queued', profile='deep',
                   scopes=['different.txt'], resources=['other'])
        views = delegate.preview_capacity_admissions([new, original], self.c, self.q)
        with common.locked(), patch.object(worker, 'recovered_capacity_exclusions',
                                           side_effect=AssertionError('I/O under global lock')):
            self.assertEqual(delegate.choose_ready([new, original], self.c, self.q,
                                                   recovery_views=views)[0][2], 'owner_at_capacity')
            # A changed/cancelled original cannot keep an old reservation alive.
            cancelled = dict(original, cancel_requested=True)
            self.assertEqual(delegate.choose_ready([new, cancelled], self.c, self.q,
                                                   recovery_views=views)[0][1], 'deep')

    def test_queued_maintenance_checkpoint_precedes_new_task_even_if_listed_last(self):
        checkpoint = dict(self.t, id='job-resume', status='queued',
                          _retier={'queued_checkpoint': True})
        new = dict(self.t, id='job-new', status='queued', session_id=None,
                   scopes=['different.txt'], resources=['other'])
        choices = delegate.choose_ready([new, checkpoint], self.c, self.q)
        self.assertEqual(choices[0][0]['id'], 'job-resume')
        self.assertEqual(choices[0][1], 'ultra')
        self.assertEqual(choices[1][2], 'owner_at_capacity')

    def test_actual_bailian_generation_hook_and_quota_due_probe_join_without_clearing_block(self):
        provider = worker.BAILIAN_PROVIDER
        self.c['profiles']['pool'] = {'model': provider + '/qwen'}
        self.c['routing']['background'] = 'pool'
        self.c['bailian_pool'] = {'enabled': True}
        t = dict(self.t, tier='normal', profile='pool', excluded_providers=[provider],
                 _capacity_exclusions={provider: {'at': self.now - 100,
                 'reason': 'provider_usage_or_rate_limit', 'identity': 'current-key-id'}})
        block = {'credential': 'current-key-id', 'opened_at': self.now - 200,
                 'reason': quota.MONTHLY_REASON}
        common.write_json(self.state / quota.BILLING, {provider: block})
        record = {'state': 'exhausted', 'generation': 'current-generation',
                  'retry_at': self.now - 1, 'blocked_at': self.now - 200}
        with patch.object(bailian_pool, 'pool_settings', return_value={'slots': [{'id': 'primary'}]}), \
             patch.object(bailian_pool, '_load_key', return_value=(b'public-fixture', 'current-generation')), \
             patch.object(bailian_pool, 'read_state', side_effect=lambda: {'slots': {'primary': record}}):
            self.q[provider] = {'state': 'ok', 'available': True, 'windows': [],
                                'inference_pool': dict(bailian_pool.recovery_available(self.c),
                                                       sampled_at=self.now)}
            self.assertTrue(self.q[provider]['inference_pool']['authorized_probe'])
            self.assertEqual(worker.recovered_capacity_exclusions(t, self.c, self.q)
                             ['excluded_providers'], [])
            self.assertEqual(common.read_json(self.state / quota.BILLING)[provider], block)
            # Same generation auth/unknown default slots do not recover an old exclusion.
            for replacement in ({'state': 'auth', 'generation': 'current-generation'}, {}):
                record.clear()
                record.update(replacement)
                self.q[provider]['inference_pool'] = dict(bailian_pool.recovery_available(self.c),
                                                         sampled_at=self.now)
                self.assertEqual(worker.recovered_capacity_exclusions(t, self.c, self.q)
                                 ['excluded_providers'], [provider])

    def test_precomputed_candidate_merges_only_exclusions_not_newer_authority(self):
        queued = dict(self.t, id='job-new', status='queued')
        views = delegate.preview_capacity_admissions([queued], self.c, self.q)
        changed = dict(queued, objective='New authoritative remaining goal', scopes=['new-scope.txt'],
                       errors=[{'source': 'bridge', 'code': 'new-observation'}])
        choices = delegate.choose_ready([changed], self.c, self.q, recovery_views=views)
        self.assertEqual(choices[0][0]['objective'], changed['objective'])
        self.assertEqual(choices[0][0]['scopes'], changed['scopes'])
        self.assertEqual(choices[0][0]['errors'], changed['errors'])

    def test_ready_wait_reserves_target_ultra_model_but_not_other_owners_normal_capacity(self):
        self.park()
        self.restore()
        original = common.task('job-wait')
        new = dict(self.t, id='job-new', status='queued', owner_thread_id='other-owner',
                   scopes=['different.txt'], resources=['other'])
        with patch.object(quota, 'kimi_ultra_low_weekly', return_value=(True, 1)):
            self.assertTrue(delegate.choose_ready([new, original], self.c, self.q)
                            [0][2].startswith('ultra_weekly_concurrency_limited'))
            self.c['routing']['background'] = 'deep'
            normal = dict(new, tier='normal', profile='deep')
            self.assertEqual(delegate.choose_ready([normal, original], self.c, self.q)[0][1], 'deep')

    def test_bailian_auth_exclusion_and_circuit_need_explicit_rotation_permission(self):
        provider = worker.BAILIAN_PROVIDER
        self.c['profiles']['pool'] = {'model': provider + '/qwen'}
        self.c['routing']['background'] = 'pool'
        self.c['bailian_pool'] = {'enabled': True}
        t = dict(self.t, tier='normal', profile='pool', excluded_providers=[provider],
                 _capacity_exclusions={provider: {'at': self.now - 100,
                 'reason': 'credential_rejected', 'identity': 'old-pool-identity'}})
        block = {'credential': 'current-key-id', 'opened_at': self.now - 200,
                 'reason': quota.AUTH_REASON}
        common.write_json(self.state / quota.BILLING, {provider: block})
        for rotation in (None, False, 'true', True):
            result = {'available': True, 'authorized_probe': True, 'credential_rotated': rotation}
            self.q[provider] = {'state': 'ok', 'available': True, 'windows': [],
                                'inference_pool': dict(result, configured=True, admission_available=True,
                                                       sampled_at=self.now)}
            with self.subTest(rotation=rotation), patch.object(bailian_pool, 'recovery_available',
                                                               return_value=result):
                self.assertEqual(worker.recovered_capacity_exclusions(t, self.c, self.q)
                                 ['excluded_providers'], [] if rotation is True else [provider])
                self.assertEqual(common.read_json(self.state / quota.BILLING)[provider], block)


if __name__ == '__main__':
    unittest.main()
