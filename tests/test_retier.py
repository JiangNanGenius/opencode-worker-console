"""Same-task capability retiering: backend, worker boundaries and CLI contract.

Isolated state/config only (run via scripts/run_tests.py); never touches the
real default runtime and never runs a paid live job.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import delegate
import quota
import retier
import service
import steering
import worker

WINDOW_5H = ("You've reached your 5-hour usage limit for this usage window. "
             "Please try again later.")


def handoff(**over):
    value = {'done': 'Backend wiring done',
             'remaining': ['Add focused isolated tests', 'Verify HTTP contract'],
             'decisions': 'Routing stays profile=auto',
             'evidence': ['compile checks passed'],
             'do_not_repeat': 'Do not run paid live jobs'}
    value.update(over)
    return value


CFG = {
    'profiles': {
        'ark-fast': {'model': 'ark/fast', 'variant': 'thinky'},
        'worker-normal': {'model': 'acme/normal'},
        'senior-deep': {'model': 'kimi-alias/deep'},
    },
    'routing_policy': {
        'fast': [[{'profile': 'ark-fast', 'weight': 1}]],
        'background': [[{'profile': 'worker-normal', 'weight': 1}]],
        'deep': [[{'profile': 'senior-deep', 'weight': 1}]],
    },
    'auto_approve': True,
}


class RetierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / 'state'
        self.config_path = self.root / 'config.json'
        self.config_path.write_text(json.dumps(CFG))
        self.bodies = []
        self.patches = [
            patch.object(common, 'STATE', self.state),
            patch.object(common, 'CONFIG', self.config_path),
            patch.object(common, 'credential_identity', lambda p: 'identity-' + str(p)),
            patch.object(common, 'redact', lambda x: x),
            patch.object(quota, 'STATE', self.state),
            patch.object(worker, 'STATE', self.state),
            patch.object(common, 'api', side_effect=self._api),
        ]
        for item in self.patches:
            item.start()
        common.init()
        common.write_json(self.state / 'quota.json', {})
        self.write_task('job-run')

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def _api(self, path, directory=None, method='GET', data=None, **kwargs):
        if method == 'POST' and path.endswith('/prompt_async'):
            self.bodies.append(data)
            return None
        if path == '/session/status':
            return {'ses_run': {'type': 'busy'}}
        return {}

    def write_task(self, task_id, **over):
        t = {'id': task_id, 'title': 'Job', 'objective': 'Do the thing',
             'status': 'running', 'requested_profile': 'auto', 'mode': 'read',
             'scopes': [], 'targets': [], 'commands': [], 'resources': [],
             'tier': 'deep', 'urgency': 'background', 'complexity': 'deep',
             'profile': 'senior-deep', 'directory': str(self.root),
             'session_id': 'ses_run', 'message_id': 'msg_orig',
             'created_at': time.time(), 'started_at': time.time(),
             'dispatch_attempted_at': time.time() - 60,
             '_billing_dispatch': {'provider': 'kimi-alias', 'identity': 'i-old',
                                   'at': time.time() - 60}}
        t.update(over)
        common.write_json(common.task_path(task_id), t)
        return t

    def report_message(self, message_id, parent, model='kimi-alias/deep', tokens=5,
                       text=None):
        report = text or json.dumps({'outcome': 'done', 'summary': 'ok', 'evidence': [],
                                     'tests': [], 'unresolved': []})
        return {'info': {'id': message_id, 'role': 'assistant', 'parentID': parent,
                         'providerID': model.split('/')[0], 'modelID': model.split('/')[1],
                         'tokens': {'input': 10, 'output': tokens},
                         'time': {'created': (time.time() - 5) * 1000,
                                  'completed': time.time() * 1000},
                         'finish': 'stop'},
                'parts': [{'type': 'text', 'text': report}]}


class QueuedTests(RetierTests):
    def setUp(self):
        super().setUp()
        self.write_task('job-q', status='queued', tier='deep', profile=None,
                        session_id=None, dispatch_attempted_at=None, started_at=None,
                        _billing_dispatch=None)

    def test_queued_apply_before_dispatch_and_first_prompt_carries_checkpoint(self):
        result = retier.send('job-q', 'fast', 'Remaining work is mechanical', handoff(), 'q-1')
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['from_tier'], 'deep')
        t = common.task('job-q')
        self.assertEqual(t['tier'], 'fast')
        self.assertEqual(t['urgency'], 'fast')
        self.assertEqual(t['complexity'], 'normal')
        self.assertIsNone(t['requested_tier'])
        self.assertEqual([c['status'] for c in t['tier_history']], ['applied'])
        self.assertEqual(self.bodies, [])  # queued apply never sends a native prompt
        # The checkpoint is stored privately for the first dispatch only.
        self.assertTrue(t['_retier']['queued_checkpoint'])
        self.assertEqual(t['_retier']['handoff']['do_not_repeat'], ['Do not run paid live jobs'])
        # Emulate the pool assigning the new tier, then the worker's first dispatch.
        common.update('job-q', status='starting', profile='ark-fast', started_at=time.time())
        shutdown = threading.Event()
        shutdown.set()  # exit right after the dispatch block: observe no further
        captured = []

        def api(path, directory=None, method='GET', data=None, **kwargs):
            captured.append((path, method, data))
            if path == '/session':
                return {'id': 'ses_new'}
            return None
        with patch.object(worker, 'api', side_effect=api), \
             patch.object(worker, 'credential_identity', return_value='i-new'):
            worker.run_task('job-q', shutdown)
        prompt_post = next(d for p, m, d in captured
                           if m == 'POST' and p == '/session/ses_new/prompt_async')
        text = prompt_post['parts'][0]['text']
        self.assertIn('Pre-dispatch tier change for this task: deep -> fast', text)
        self.assertIn('Add focused isolated tests', text)          # Remaining honored
        self.assertIn('Do not run paid live jobs', text)           # Do-not-repeat honored
        self.assertNotIn('_retier', json.dumps(prompt_post))
        after = common.task('job-q')
        self.assertIsNone(after['_retier'])                        # consumed once
        self.assertEqual(after['tier'], 'fast')

    def test_queued_clears_stale_profile_selection(self):
        common.update('job-q', profile='senior-deep', route_reason='weighted:stage0')
        retier.send('job-q', 'fast', 'uncertainty resolved', handoff(), 'q-2')
        t = common.task('job-q')
        self.assertIsNone(t.get('profile'))
        self.assertIsNone(t.get('route_reason'))

    def test_queued_revalidation_beats_dispatch_race(self):
        def racing(t2, tier, c, q):
            # The pool assigns and starts the task between the outer read and
            # the durable write; the apply must then refuse, never clear it.
            common.update(t2['id'], status='starting', profile='ark-fast',
                          route_reason='weighted:stage0', started_at=time.time())
            return 'ark-fast', 'weighted', {}
        with patch.object(retier, '_route_for', side_effect=racing):
            with self.assertRaises(ValueError) as caught:
                retier.send('job-q', 'fast', 'why', handoff(), 'q-3')
        self.assertIn('began dispatching', str(caught.exception))
        t = common.task('job-q')
        self.assertEqual(t['status'], 'starting')
        self.assertEqual(t['profile'], 'ark-fast')
        self.assertEqual(t['tier'], 'deep')
        self.assertNotIn('tier_change', t)

    def test_queued_rejects_without_route_and_leaves_task_intact(self):
        with patch.object(quota, 'route', return_value=(None, 'provider_billing_blocked')):
            with self.assertRaises(ValueError) as caught:
                retier.send('job-q', 'fast', 'why', handoff(), 'q-4')
        self.assertIn('No route is available', str(caught.exception))
        t = common.task('job-q')
        self.assertEqual(t['tier'], 'deep')
        self.assertNotIn('tier_change', t)
        self.assertEqual(self.bodies, [])


class RunningTests(RetierTests):
    def test_deep_to_fast_applies_only_on_observed_assistant_ack(self):
        before = time.time()
        result = retier.send('job-run', 'fast', 'Remaining work is mechanical', handoff(), 'req-1')
        t = common.task('job-run')
        change = result
        self.assertEqual(change['status'], 'pending')
        self.assertEqual(set(change), {'request_id', 'from_tier', 'to_tier', 'status',
                                       'reason', 'created_at', 'message_id'})
        # Tier and dispatch segment are preserved while only queued.
        self.assertEqual(t['tier'], 'deep')
        self.assertEqual(t['profile'], 'senior-deep')
        self.assertEqual(t['message_id'], 'msg_orig')
        self.assertEqual(t['_billing_dispatch']['provider'], 'kimi-alias')
        self.assertEqual(t['requested_tier'], 'fast')
        self.assertEqual(len(self.bodies), 1)
        body = self.bodies[0]
        self.assertEqual(body['agent'], 'ark-fast')
        self.assertEqual(body['model'], {'providerID': 'ark', 'modelID': 'fast'})
        self.assertEqual(body['variant'], 'thinky')
        self.assertEqual(body['messageID'], change['message_id'])
        self.assertIn('Coordinator tier change for the current task: deep -> fast',
                      body['parts'][0]['text'])
        self.assertIn('Do not replay whole completed', body['parts'][0]['text'])
        hop = t['route_history'][-1]
        self.assertEqual(hop['switch_state'], 'queued')
        self.assertTrue(hop['queued_boundary_switch'])
        # HTTP acceptance alone never activates anything:
        self.assertFalse(retier.settled(t, [{'info': {'role': 'assistant', 'parentID': 'msg_orig'}}]))
        boundary = {'info': {'id': change['message_id'], 'role': 'user'}}
        ack = self.report_message('msg_ack', change['message_id'], model='ark/fast')
        observed = retier.observe(t, [boundary, ack])
        self.assertEqual(observed['tier'], 'fast')
        self.assertEqual(observed['profile'], 'ark-fast')
        self.assertEqual(observed['tier_change']['status'], 'applied')
        self.assertIsNotNone(observed['tier_change']['applied_at'])
        self.assertIsNone(observed['requested_tier'])
        self.assertIsNone(observed['_retier'])
        # The active segment is rebound only by this observed ack, to the
        # continuation that actually ran and the identity captured at its send.
        self.assertEqual(observed['message_id'], change['message_id'])
        self.assertAlmostEqual(observed['dispatch_attempted_at'], change['created_at'])
        self.assertEqual(observed['_billing_dispatch'],
                         {'provider': 'ark', 'identity': 'identity-ark',
                          'at': change['created_at']})
        self.assertEqual([c['status'] for c in observed['tier_history']], ['applied'])
        self.assertGreaterEqual(observed['tier_change']['created_at'], before)
        self.assertTrue(retier.settled(observed, [boundary, ack]))

    def test_normal_to_deep_same_routed_model_still_acks_by_parent(self):
        common.update('job-run', tier='normal', complexity='normal', profile='worker-normal',
                      urgency='background')
        with patch.object(quota, 'route', return_value=('worker-normal', 'weighted:stage0')):
            result = retier.send('job-run', 'deep', 'Remaining uncertainty is real',
                                 handoff(), 'req-n2d')
        self.assertEqual(result['status'], 'pending')
        t = common.task('job-run')
        self.assertEqual(t.get('route_history', []), [])  # same routed model: no switch hop
        self.assertEqual(self.bodies[0]['agent'], 'worker-normal')
        self.assertEqual(t['tier'], 'normal')
        boundary = {'info': {'id': result['message_id'], 'role': 'user'}}
        ack = self.report_message('msg_ack2', result['message_id'], model='acme/normal')
        observed = retier.observe(t, [boundary, ack])
        self.assertEqual(observed['tier'], 'deep')
        self.assertEqual(observed['profile'], 'worker-normal')
        # Even when the routed model did not change, the observed continuation
        # becomes the active segment: message/time/billing boundaries rebind to
        # the identity captured at its dispatch, never the stale original one.
        self.assertEqual(observed['message_id'], result['message_id'])
        self.assertAlmostEqual(observed['dispatch_attempted_at'], result['created_at'])
        self.assertEqual(observed['_billing_dispatch'],
                         {'provider': 'acme', 'identity': 'identity-acme',
                          'at': result['created_at']})
        self.assertEqual(observed['tier_change']['status'], 'applied')

    def test_duplicate_request_id_returns_original_without_replay(self):
        first = retier.send('job-run', 'fast', 'why', handoff(), 'dup-1')
        again = retier.send('job-run', 'fast', 'why', handoff(), 'dup-1')
        self.assertEqual(first, again)
        self.assertEqual(len(self.bodies), 1)
        with self.assertRaises(ValueError):
            retier.send('job-run', 'fast', 'different reason', handoff(), 'dup-1')
        self.assertEqual(len(self.bodies), 1)

    def test_at_most_one_unresolved_change(self):
        retier.send('job-run', 'fast', 'why', handoff(), 'one-1')
        with self.assertRaises(ValueError) as caught:
            retier.send('job-run', 'fast', 'why', handoff(), 'one-2')
        self.assertIn('already pending', str(caught.exception))
        self.assertEqual(len(self.bodies), 1)

    def test_explicit_rejection_keeps_original_and_stays_replayable(self):
        def reject(path, directory=None, method='GET', data=None, **kwargs):
            if method == 'POST':
                raise common.HttpFailure(422, 'agent unknown')
            return {}
        with patch.object(common, 'api', side_effect=reject):
            result = retier.send('job-run', 'fast', 'why', handoff(), 'rej-1')
        self.assertEqual(result['status'], 'rejected')
        t = common.task('job-run')
        self.assertEqual(t['tier'], 'deep')            # original tier intact
        self.assertEqual(t['profile'], 'senior-deep')
        self.assertEqual(t['message_id'], 'msg_orig')  # original dispatch intact
        self.assertEqual(t['_billing_dispatch']['provider'], 'kimi-alias')
        self.assertIsNone(t['requested_tier'])
        self.assertEqual(t['route_history'][-1]['switch_state'], 'rejected')
        self.assertTrue(any(e.get('http_status') == 422 for e in t['errors']))
        # Rejected is resolved: completion is not held by the gate.
        self.assertTrue(retier.settled(t, []))
        # A later different request must not make this rejection forgettable.
        retier.send('job-run', 'fast', 'why', handoff(), 'rej-2')
        replay = retier.send('job-run', 'fast', 'why', handoff(), 'rej-1')
        self.assertEqual(replay, result)
        self.assertEqual(len(self.bodies), 1)  # only rej-2 was ever sent

    def test_transport_uncertainty_is_retained_observed_never_retried(self):
        def flaky(path, directory=None, method='GET', data=None, **kwargs):
            if method == 'POST':
                raise common.HttpFailure(None, 'connection reset')
            return {}
        with patch.object(common, 'api', side_effect=flaky):
            result = retier.send('job-run', 'fast', 'why', handoff(), 'unc-1')
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(len(self.bodies), 0)
        again = retier.send('job-run', 'fast', 'why', handoff(), 'unc-1')
        self.assertEqual(again, result)
        self.assertEqual(len(self.bodies), 0)  # never blindly retried
        t = common.task('job-run')
        self.assertEqual(t['tier'], 'deep')
        boundary = {'info': {'id': result['message_id'], 'role': 'user'}}
        ack = self.report_message('msg_unc_ack', result['message_id'], model='ark/fast')
        observed = retier.observe(t, [boundary, ack])
        self.assertEqual(observed['tier'], 'fast')
        self.assertEqual(observed['tier_change']['status'], 'applied')

    def test_refusals_terminal_cancel_pin_floor_idle_status(self):
        cases = [
            ({'status': 'completed'}, 'only applies to a live task'),
            ({'cancel_requested': True}, 'cancellation is in flight'),
            ({'requested_profile': 'senior-deep'}, 'pins a named profile'),
            ({'capability_floor': 'deep', 'capability_reason': 'hard reasoning'}, 'capability floor'),
            ({'status': 'starting'}, 'starting'),
            ({'tier': 'fast'}, 'already runs at tier fast'),
        ]
        for over, needle in cases:
            self.write_task('job-refuse', **over)
            with self.subTest(str(over)):
                with self.assertRaises(ValueError) as caught:
                    retier.send('job-refuse', 'fast', 'why', handoff(), 'ref-' + needle.replace(' ', '_'))
                self.assertIn(needle, str(caught.exception))
        self.assertEqual(self.bodies, [])

    def test_conservation_switch_and_tier_change_serialize(self):
        # A queued conservation switch owns the next boundary.
        common.update('job-run', route_history=[{
            'from_profile': 'senior-deep', 'to_profile': 'ark-fast',
            'queued_boundary_switch': True, 'message_id': 'msg_switch',
            'switch_state': 'queued', 'trigger': 'quota_conservation_level_2'}])
        with self.assertRaises(ValueError) as caught:
            retier.send('job-run', 'fast', 'why', handoff(), 'ser-0')
        self.assertIn('routing switch is already queued', str(caught.exception))
        self.assertEqual(self.bodies, [])
        # Proactive conservation yields to an unresolved tier change without
        # touching native I/O.
        common.update('job-run', route_history=[])
        retier.send('job-run', 'fast', 'why', handoff(), 'ser-1')
        t = common.task('job-run')
        with patch.object(worker, 'config', return_value=CFG), \
             patch.object(worker, 'call') as call:
            self.assertFalse(worker.proactive_reroute_if_needed(t, {'type': 'busy'}))
        call.assert_not_called()
        self.assertEqual(len(self.bodies), 1)

    def test_capacity_reroute_yields_to_delivered_unacked_continuation(self):
        change = retier.send('job-run', 'fast', 'why', handoff(), 'cap-1')
        delivered = [{'info': {'id': 'msg_orig', 'role': 'user'}},
                     self.report_message('msg_old', 'msg_orig'),
                     {'info': {'id': change['message_id'], 'role': 'user'}}]
        with patch.object(worker, 'call', return_value=[]) as call:
            self.assertFalse(worker.reroute_after_capacity_stop(
                common.task('job-run'), 'kimi-alias', 'provider_usage_or_rate_limit',
                delivered))
        call.assert_not_called()  # no competing continuation, no message re-read
        t = common.task('job-run')
        self.assertEqual(t['tier_change']['status'], 'pending')  # retained, not forgotten
        self.assertEqual(t['tier'], 'deep')

    def test_capacity_reroute_settles_undelivered_continuation_and_recovers(self):
        change = retier.send('job-run', 'fast', 'why', handoff(), 'cap-2')
        not_delivered = [{'info': {'id': 'msg_orig', 'role': 'user'}},
                         self.report_message('msg_old', 'msg_orig')]
        with patch.object(worker, 'call', return_value=None) as call, \
             patch.object(quota, 'route', return_value=('worker-normal', 'weighted:stage1')):
            self.assertTrue(worker.reroute_after_capacity_stop(
                common.task('job-run'), 'kimi-alias', 'provider_usage_or_rate_limit',
                not_delivered))
        t = common.task('job-run')
        self.assertEqual(t['tier'], 'deep')  # original tier intact after the loss
        self.assertEqual(t['tier_change']['status'], 'rejected')
        self.assertEqual(t['tier_history'][-1]['status'], 'rejected')
        self.assertTrue(any('tier_continuation_unobserved_at_stop' in str(e)
                            for e in t['errors']))
        prompts = [c for c in call.call_args_list if c.args[1] == '/prompt_async']
        self.assertEqual(len(self.bodies), 1)  # tier send only; never a replay
        self.assertEqual(len(prompts), 1)      # one fallback continuation
        self.assertNotEqual(prompts[0].args[3]['messageID'], change['message_id'])

    def test_capacity_reroute_applies_acknowledged_continuation_then_recovers(self):
        change = retier.send('job-run', 'fast', 'why', handoff(), 'cap-3')
        acked = [{'info': {'id': 'msg_orig', 'role': 'user'}},
                 self.report_message('msg_old', 'msg_orig'),
                 {'info': {'id': change['message_id'], 'role': 'user'}},
                 self.report_message('msg_cap3', change['message_id'], model='ark/fast')]
        with patch.object(worker, 'call', return_value=None) as call, \
             patch.object(quota, 'route', return_value=('worker-normal', 'weighted:stage1')):
            self.assertTrue(worker.reroute_after_capacity_stop(
                common.task('job-run'), 'ark', 'provider_usage_or_rate_limit', acked))
        t = common.task('job-run')
        self.assertEqual(t['tier'], 'fast')  # acknowledged change was applied, not lost
        self.assertEqual(t['tier_change']['status'], 'applied')
        self.assertEqual(len(self.bodies), 1)  # tier send; no replay
        self.assertEqual(len([c for c in call.call_args_list
                              if c.args[1] == '/prompt_async']), 1)


class StopLifecycleTests(RetierTests):
    """Caller-level regressions for the usage-window stop/recover race."""

    def _run_window(self, route_result):
        change = retier.send('job-run', 'fast', 'why', handoff(), 'wl-1')
        status_seq = [{'ses_run': {'type': 'retry', 'message': WINDOW_5H,
                                   'statusCode': 429, 'attempt': 2}},
                      {'ses_run': {'type': 'idle'}}]
        delivered = [{'info': {'id': 'msg_orig', 'role': 'user'}},
                     {'info': {'id': 'msg_err', 'role': 'assistant', 'parentID': 'msg_orig',
                               'providerID': 'kimi-alias', 'modelID': 'deep',
                               'time': {'created': (time.time() - 9) * 1000,
                                        'completed': (time.time() - 8) * 1000}},
                      'parts': []},
                     {'info': {'id': change['message_id'], 'role': 'user'}}]
        seen_calls = []
        shutdown = threading.Event()

        def api(path, directory=None, method='GET', data=None, **kwargs):
            if path == '/session/status':
                return status_seq.pop(0)
            return {}

        def call_fn(t2, suffix, method='GET', data=None):
            seen_calls.append((suffix, method, data))
            if suffix == '/message':
                return delivered
            return None

        real_reroute = worker._reroute_after_capacity_stop

        def reroute_spy(t2, provider, trigger):
            result = real_reroute(t2, provider, trigger)
            shutdown.set()  # end the loop after exactly one recovery pass
            return result
        with patch.object(worker, 'api', side_effect=api), \
             patch.object(worker, 'call', side_effect=call_fn), \
             patch.object(quota, 'route', return_value=route_result), \
             patch.object(worker, '_reroute_after_capacity_stop', side_effect=reroute_spy):
            worker.run_task('job-run', shutdown)
        return change, seen_calls

    def test_window_stop_settles_accepted_tier_then_falls_back(self):
        change, seen_calls = self._run_window(('worker-normal', 'weighted:stage1'))
        posts = [d for s, m, d in seen_calls if s == '/prompt_async']
        aborts = [s for s, m, d in seen_calls if s == '/abort']
        self.assertEqual(len(aborts), 1)   # stop happened exactly once, inside the lock
        self.assertEqual(len(posts), 1)    # one fallback continuation; tier one was not replayed
        self.assertNotEqual(posts[0]['messageID'], change['message_id'])
        t = common.task('job-run')
        self.assertEqual(t['status'], 'running')
        self.assertEqual(t['tier'], 'deep')  # original tier intact; loss was definitive
        self.assertEqual(t['tier_change']['status'], 'rejected')
        self.assertEqual(t['tier_history'][-1]['status'], 'rejected')
        self.assertEqual(t['profile'], 'worker-normal')
        self.assertTrue(any('tier_continuation_aborted_before_ack' in str(e)
                            for e in t['errors']))

    def test_window_stop_without_route_finishes_never_stranded(self):
        change, seen_calls = self._run_window((None, 'all_stages_blocked'))
        t = common.task('job-run')
        self.assertEqual(t['status'], 'needs_attention')
        self.assertEqual(t['tier'], 'deep')
        # The forced terminal result is only reached because the tier change was
        # already settled under the same lock: nothing pending behind a forced
        # finish, and the accepted continuation is audit-retained, not forgotten.
        self.assertEqual(t['tier_change']['status'], 'rejected')
        self.assertTrue((common.artifact_dir('job-run') / 'result.json').exists())
        self.assertFalse(any(s == '/prompt_async' for s, m, d in seen_calls))


class SteeringInteractionTests(RetierTests):
    def test_new_steer_is_rejected_while_tier_change_unresolved(self):
        retier.send('job-run', 'fast', 'why', handoff(), 'st-1')
        with self.assertRaises(ValueError) as caught:
            steering.send('job-run', 'Focus here', 'steer-1')
        self.assertIn('tier change', str(caught.exception))

    def test_applied_tier_boundary_supersedes_older_guidance_for_completion(self):
        result = retier.send('job-run', 'fast', 'why', handoff(), 'st-2')
        # An earlier accepted guidance item must not deadlock completion.
        common.update('job-run', guidance=[{'request_id': 'g-1', 'message_id': 'msg_g',
                                            'text': 'older steer', 'created_at': time.time() - 30,
                                            'status': 'accepted'}])
        messages = [{'info': {'id': 'msg_g', 'role': 'user'}},
                    self.report_message('msg_gr', 'msg_g'),
                    {'info': {'id': result['message_id'], 'role': 'user'}},
                    self.report_message('msg_sr', result['message_id'], model='ark/fast')]
        t = common.task('job-run')
        self.assertFalse(steering.settled(t, messages))  # not yet observed-applied
        observed = retier.observe(t, messages)
        self.assertEqual(observed['tier_change']['status'], 'applied')
        self.assertTrue(steering.settled(observed, messages))
        self.assertTrue(retier.settled(observed, messages))
        finished = worker.finish(observed, messages)
        self.assertEqual(finished['status'], 'completed')
        self.assertEqual(finished['tier'], 'fast')

    def test_guidance_newer_than_boundary_still_owes_reply(self):
        result = retier.send('job-run', 'fast', 'why', handoff(), 'st-3')
        t = retier.observe(common.task('job-run'), [
            {'info': {'id': result['message_id'], 'role': 'user'}},
            self.report_message('msg_sr', result['message_id'], model='ark/fast')])
        self.assertEqual(t['tier'], 'fast')
        guidance = [{'request_id': 'g-2', 'message_id': 'msg_g2', 'text': 'later steer',
                     'created_at': time.time() + 5, 'status': 'accepted'}]
        common.update('job-run', guidance=guidance)
        messages = [{'info': {'id': result['message_id'], 'role': 'user'}},
                    {'info': {'id': 'msg_g2', 'role': 'user'}},
                    self.report_message('msg_sr', result['message_id'], model='ark/fast')]
        # Last reply is the older tier boundary, but the steer is newer: still owed.
        self.assertFalse(steering.settled(common.task('job-run'), messages))

    def test_processed_steer_then_applied_retier_then_confirmed_fallback(self):
        result = retier.send('job-run', 'fast', 'why', handoff(), 'fb-1')
        steer = {'request_id': 'g-3', 'message_id': 'msg_g3', 'text': 'steer',
                 'created_at': time.time() - 30, 'status': 'accepted'}
        common.update('job-run', guidance=[steer])
        tier_ack = retier.observe(common.task('job-run'), [
            {'info': {'id': 'msg_g3', 'role': 'user'}},
            self.report_message('msg_g3_reply', 'msg_g3'),
            {'info': {'id': result['message_id'], 'role': 'user'}},
            self.report_message('msg_tier_reply', result['message_id'], model='ark/fast')])
        self.assertEqual(tier_ack['tier'], 'fast')
        # A later confirmed capacity fallback: existing machinery rebinds the
        # segment message and dispatch time when its continuation is accepted.
        common.update('job-run', message_id='msg_fb', dispatch_attempted_at=time.time())
        messages = [
            {'info': {'id': 'msg_g3', 'role': 'user'}},
            self.report_message('msg_g3_reply', 'msg_g3'),
            {'info': {'id': result['message_id'], 'role': 'user'}},
            self.report_message('msg_tier_reply', result['message_id'], model='ark/fast'),
            {'info': {'id': 'msg_fb', 'role': 'user'}},
            self.report_message('msg_fb_reply', 'msg_fb', model='acme/normal'),
        ]
        self.assertTrue(steering.settled(common.task('job-run'), messages))
        # A stale pre-steer output is still never accepted: the newest reply is
        # the reply to the ORIGINAL pre-steer dispatch, not to any boundary.
        common.update('job-run', dispatch_attempted_at=time.time() - 120)
        stale = [{'info': {'id': 'msg_orig', 'role': 'user'}},
                 {'info': {'id': 'msg_g3', 'role': 'user'}},
                 self.report_message('msg_orig_reply', 'msg_orig')]
        self.assertFalse(steering.settled(common.task('job-run'), stale))
        self.assertTrue(retier.settled(common.task('job-run'), messages))


class ValidationTests(RetierTests):
    def setUp(self):
        super().setUp()
        self.write_task('job-v')
        self.write_task('job-q7', status='queued', tier='deep', profile=None,
                        session_id=None, dispatch_attempted_at=None, started_at=None,
                        _billing_dispatch=None)

    def test_handoff_shape_size_and_credential_boundaries(self):
        with self.assertRaises(ValueError):
            retier.send('job-v', 'fast', 'why', {'remaining': []}, 'v-1')
        with self.assertRaises(ValueError):
            retier.send('job-v', 'fast', 'why', {'remaining': '   '}, 'v-2')
        # Unknown keys are refused without echoing the caller's key names.
        with self.assertRaises(ValueError) as caught:
            retier.send('job-v', 'fast', 'why',
                        {'remaining': ['ok'], 'sk-live-token-abc': 'x'}, 'v-3')
        self.assertIn('unsupported fields', str(caught.exception))
        self.assertNotIn('sk-live-token-abc', str(caught.exception))
        with self.assertRaises(ValueError):
            retier.send('job-v', 'fast', 'why', {'remaining': [1]}, 'v-4')
        with self.assertRaises(ValueError):
            retier.send('job-v', 'fast', 'why', {'remaining': ['x' * 25000]}, 'v-5')
        with self.assertRaises(ValueError):
            retier.send('job-v', 'fast', 'why', 'not-a-dict', 'v-6')

    def test_text_and_array_handoff_fields_normalize_consistently(self):
        captured = {}
        original = retier._apply_queued

        def spy(t2, tier, reason, handoff_value, request_id, content):
            captured['handoff'] = handoff_value
            return original(t2, tier, reason, handoff_value, request_id, content)
        with patch.object(retier, '_apply_queued', side_effect=spy):
            retier.send('job-q7', 'fast', 'why',
                        {'done': 'one item', 'remaining': ' two item '}, 'v-7')
        self.assertEqual(captured['handoff']['done'], ['one item'])
        self.assertEqual(captured['handoff']['remaining'], ['two item'])
        self.assertEqual(captured['handoff']['decisions'], [])

    def test_credential_like_values_rejected_at_the_redaction_boundary(self):
        def fake_redact(value):
            return value.replace('sk-live-token', '<redacted>') if isinstance(value, str) else value
        with patch.object(common, 'redact', side_effect=fake_redact):
            with self.assertRaises(ValueError) as caught:
                retier.send('job-v', 'fast', 'why', {'remaining': ['use sk-live-token now']}, 'v-8')
            self.assertIn('credential-like', str(caught.exception))
            with self.assertRaises(ValueError):
                retier.send('job-v', 'fast', 'use sk-live-token', {'remaining': ['ok']}, 'v-9')
            # Credential-looking request IDs are rejected before persistence.
            with self.assertRaises(ValueError) as caught:
                retier.send('job-v', 'fast', 'why', {'remaining': ['ok']}, 'req-sk-live-token-x')
            self.assertIn('Request ID contains a credential-like value', str(caught.exception))
        t = common.task('job-v')
        self.assertNotIn('tier_change', t)
        self.assertNotIn('_retier_hashes', t)
        self.assertEqual(self.bodies, [])

    def test_reason_and_request_id_are_required_and_stable(self):
        with self.assertRaises(ValueError):
            retier.send('job-v', 'fast', '  ', {'remaining': ['work']}, 'ok-id')
        with self.assertRaises(ValueError):
            retier.send('job-v', 'fast', 'why', {'remaining': ['work']}, ' ')
        with self.assertRaises(ValueError):
            retier.send('job-v', 'fast', 'why', {'remaining': ['work']}, None)
        with self.assertRaises(ValueError):
            retier.send('job-v', 'fast', 'why', {'remaining': ['work']}, 'has space')
        self.assertEqual(self.bodies, [])

    def test_handoff_file_owner_only_boundary(self):
        good = self.root / 'handoff.json'
        good.write_text(json.dumps(handoff()))
        os.chmod(good, 0o600)
        # A real owner-only file passes the S_ISREG/ownership/mode boundary.
        self.assertEqual(retier.read_handoff_file(str(good))['remaining'][0],
                         'Add focused isolated tests')
        os.chmod(good, 0o644)
        with self.assertRaises(ValueError) as caught:
            retier.read_handoff_file(str(good))
        self.assertNotIn(str(good), str(caught.exception))
        self.assertNotIn('focused', str(caught.exception))
        os.chmod(good, 0o600)
        big = self.root / 'big.json'
        big.write_bytes(b'{"remaining": ["' + b'x' * 70000 + b'"]}')
        os.chmod(big, 0o600)
        with self.assertRaises(ValueError):
            retier.read_handoff_file(str(big))
        bad = self.root / 'bad.json'
        bad.write_text('nope ' + 'secret-content')
        os.chmod(bad, 0o600)
        with self.assertRaises(ValueError) as caught:
            retier.read_handoff_file(str(bad))
        self.assertNotIn('secret-content', str(caught.exception))
        link = self.root / 'link.json'
        link.symlink_to(good)
        with self.assertRaises(ValueError):
            retier.read_handoff_file(str(link))
        with self.assertRaises(ValueError):
            retier.read_handoff_file('relative.json')


class ExposureTests(RetierTests):
    def test_compact_fields_survive_all_shaping(self):
        result = retier.send('job-run', 'fast', 'why', handoff(), 'ex-1')
        t = common.task('job-run')
        for view in (common.public_task, common.coordinator_task):
            out = view(t)
            self.assertEqual(out['tier'], 'deep')
            self.assertEqual(out['requested_tier'], 'fast')
            self.assertEqual(set(out['tier_change']),
                             {'request_id', 'from_tier', 'to_tier', 'status', 'reason',
                              'created_at', 'message_id'})
            blob = json.dumps(out, ensure_ascii=False)
            self.assertNotIn('_retier', blob)
            self.assertNotIn('Add focused isolated tests', blob)  # handoff stays private
            self.assertNotIn('handoff.json', blob)
        # The compact coordinator view never discloses the routed model.
        coordinator = common.coordinator_task(t)
        self.assertNotIn('profile', coordinator)
        status = delegate.task_status(t)
        self.assertEqual(status['tier_change']['status'], 'pending')
        # After application the same fields carry applied_at through CLI status.
        boundary = {'info': {'id': result['message_id'], 'role': 'user'}}
        ack = self.report_message('msg_ex', result['message_id'], model='ark/fast')
        observed = retier.observe(t, [boundary, ack])
        out = delegate.task_status(observed)
        self.assertEqual(out['tier'], 'fast')
        self.assertIn('applied_at', out['tier_change'])
        self.assertIsNone(out['requested_tier'])
        # The default wait window follows the effective tier.
        self.assertEqual(delegate.wait_window_seconds(out), 300)


class CliTests(RetierTests):
    def setUp(self):
        super().setUp()
        self.write_task('job-cli', status='queued', tier='deep', profile=None,
                        session_id=None, dispatch_attempted_at=None,
                        started_at=None, _billing_dispatch=None)

    def run_cli(self, argv):
        stdout = io.StringIO()
        code = None
        with patch.object(service, 'start', lambda: None), \
             contextlib.redirect_stdout(stdout):
            sys.argv = ['delegate.py', *argv]
            try:
                delegate.main()
            except SystemExit as e:
                code = e.code
        return code, stdout.getvalue()

    def test_task_retier_cli_contract(self):
        handoff_path = self.root / 'cli-handoff.json'
        handoff_path.write_text(json.dumps(handoff()))
        os.chmod(handoff_path, 0o600)
        code, out = self.run_cli(['task', 'retier', 'job-cli', '--tier', 'fast',
                                  '--reason', 'uncertainty resolved',
                                  '--handoff-file', str(handoff_path),
                                  '--request-id', 'cli-req-1'])
        self.assertIn(code, (None, 0))
        payload = json.loads(out)
        self.assertEqual(payload['status'], 'applied')
        self.assertEqual(payload['request_id'], 'cli-req-1')
        self.assertNotIn('handoff', json.dumps(payload))
        self.assertEqual(common.task('job-cli')['tier'], 'fast')

    def test_task_retier_cli_unsafe_handoff_fails_generically(self):
        handoff_path = self.root / 'cli-handoff.json'
        handoff_path.write_text(json.dumps(handoff()))
        os.chmod(handoff_path, 0o644)
        with self.assertRaises(ValueError) as caught:
            self.run_cli(['task', 'retier', 'job-cli', '--tier', 'fast',
                          '--reason', 'why', '--handoff-file', str(handoff_path),
                          '--request-id', 'cli-req-2'])
        self.assertNotIn(str(handoff_path), str(caught.exception))
        self.assertNotIn('focused', str(caught.exception))
        self.assertEqual(common.task('job-cli')['tier'], 'deep')

    def test_task_retier_cli_requires_all_safety_flags(self):
        code, out = self.run_cli(['task', 'retier', 'job-cli', '--tier', 'fast'])
        self.assertEqual(code, 2)


if __name__ == '__main__':
    unittest.main()
