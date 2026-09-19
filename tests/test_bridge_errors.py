import io
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import diagnostics
import quota
import worker


class ErrorBridgeTests(unittest.TestCase):
    def test_billing_occurrence_uses_failure_completion_time(self):
        now = time.time()
        message = {'info': {'id': 'late-error', 'role': 'assistant',
                           'time': {'created': (now - 600) * 1000, 'completed': now * 1000},
                           'error': {'name': 'APIError', 'data': {'statusCode': 402}}}}
        self.assertAlmostEqual(diagnostics.from_messages([message])[0]['occurred_at'], now)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(common, 'STATE', self.root),
                        patch.object(worker, 'STATE', self.root),
                        patch.object(quota, 'STATE', self.root),
                        patch.object(quota, 'credential_identity', lambda p: 'cred-' + str(p)),
                        patch.object(common, 'CONFIG', self.root / 'config.json')]
        for p in self.patches: p.start()
        common.init()
        common.write_json(common.CONFIG, {'profiles': {'fast': {'model': 'test/model'}}})
        common.write_json(common.task_path('job-error'), {
            'id': 'job-error', 'profile': 'fast', 'status': 'running', 'mode': 'read',
            'scopes': [], 'directory': str(self.root), 'session_id': 'ses_error',
            'message_id': 'msg_test', 'started_at': time.time(), 'timeout_seconds': 30,
            'dispatch_attempted_at': time.time()})

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def test_model_error_reaches_finish_status_and_compact_collect(self):
        messages = [{'info': {'id': 'msg_reply', 'role': 'assistant', 'error': {
            'name': 'APIError', 'data': {'message': 'Rate limit exceeded', 'statusCode': 429,
                                       'isRetryable': True}}}, 'parts': []}]
        result = worker.finish(common.task('job-error'), messages)
        self.assertEqual(result['status'], 'failed')
        compact = diagnostics.collect('job-error')
        self.assertEqual(compact['task']['errors'][0]['message'], 'Rate limit exceeded')
        self.assertEqual(compact['result']['errors'][0]['http_status'], 429)
        self.assertTrue(compact['result']['errors'][0]['retryable'])
        self.assertEqual(compact['task']['session_id'], 'ses_error')

    def test_tool_nonzero_and_recovered_report_keep_error(self):
        report = {'outcome':'done','summary':'Recovered','evidence':[],'tests':[],'unresolved':[]}
        messages = [{'info': {'id':'msg_reply','role':'assistant','providerID':'test','modelID':'model'}, 'parts': [
            {'type':'tool','tool':'bash','callID':'call_test','state': {
                'status':'completed','input':{'command':'example-test'},
                'metadata':{'exit':7},'output':'Assertion failed'}},
            {'type':'text','text':json.dumps(report)}]}]
        worker.finish(common.task('job-error'), messages)
        result = diagnostics.collect('job-error')
        self.assertEqual(result['task']['status'], 'completed')
        self.assertEqual(result['result']['errors'][0]['exit_code'], 7)
        self.assertEqual(result['result']['errors'][0]['command'], 'example-test')

    def test_idle_model_error_finishes_without_completion_timestamp(self):
        messages = [{'info': {'id': 'msg_test', 'role': 'user'}, 'parts': []},
                    {'info': {'id': 'msg_reply', 'role': 'assistant',
                              'error': {'name': 'APIError', 'data': {'message': 'Provider unavailable'}}},
                     'parts': []}]
        with patch.object(worker, 'call', return_value=messages), patch.object(worker, 'api', return_value={}):
            worker.run_task('job-error', threading.Event())
        result = diagnostics.collect('job-error')
        self.assertEqual(result['task']['status'], 'failed')
        self.assertEqual(result['task']['errors'][0]['message'], 'Provider unavailable')

    def test_long_running_worker_repairs_repeated_errors_without_forced_stop(self):
        # Recover an old persisted job far beyond its former deadline. Observe more
        # than 80 busy iterations, including repeated failures, then a real completion.
        common.update('job-error', started_at=time.time() - 86400, timeout_seconds=1)
        report = {'outcome': 'done', 'summary': 'Recovered after long execution',
                  'evidence': [], 'tests': [], 'unresolved': []}
        failures = [{'type': 'tool', 'tool': 'bash', 'callID': 'retry-' + str(i),
                     'state': {'status': 'error', 'input': {'command': 'example-test'},
                               'error': 'Temporary failure'}} for i in range(2)]
        rounds = [0]
        shutdown = threading.Event()

        def pause(_seconds):
            rounds[0] += 1
            if rounds[0] > 86:
                raise AssertionError('Worker failed to recognize completion')

        def messages(*args):
            info = {'id': 'msg_reply', 'role': 'assistant', 'providerID': 'test', 'modelID': 'model'}
            parts = list(failures)
            if rounds[0] >= 85:
                info['time'] = {'completed': time.time() * 1000}
                parts.append({'type': 'text', 'text': json.dumps(report)})
            return [{'info': {'id': 'msg_test', 'role': 'user'}, 'parts': []},
                    {'info': info, 'parts': parts}]

        def native(path, *_args, **_kwargs):
            if path == '/session/status':
                return {'ses_error': {'type': 'busy' if rounds[0] < 85 else 'idle'}}
            return []

        with patch.object(shutdown, 'wait', side_effect=pause), \
                patch.object(worker, 'call', side_effect=messages), \
                patch.object(worker, 'api', side_effect=native), \
                patch.object(worker, 'stop') as abort:
            worker.run_task('job-error', shutdown)
        abort.assert_not_called()
        self.assertEqual(rounds[0], 85)
        result = diagnostics.collect('job-error')
        self.assertEqual(result['task']['status'], 'completed')
        self.assertEqual(result['result']['worker_report']['summary'], report['summary'])
        self.assertEqual(len([e for e in result['result']['errors'] if e['source'] == 'tool']), 2)

    def test_explicit_cancellation_still_aborts_unlimited_worker(self):
        common.update('job-error', cancel_requested=True)
        with patch.object(worker, 'stop', return_value=True) as abort, \
                patch.object(worker, 'call', return_value=[]):
            worker.run_task('job-error', threading.Event())
        abort.assert_called_once()
        self.assertEqual(common.task('job-error')['status'], 'cancelled')

    def test_only_real_completed_attributed_model_reply_is_recovery_evidence(self):
        now = time.time()
        t = common.task('job-error')
        t['_billing_dispatch'] = {'provider': 'test', 'identity': 'cred-test', 'at': now - 20}
        message = {'info': {'role': 'assistant', 'providerID': 'test', 'finish': 'stop',
                            'time': {'created': (now - 10) * 1000, 'completed': now * 1000}},
                   'parts': [{'type': 'text', 'text': 'Verified reply'}]}
        with patch.object(quota, 'observe_model_success', return_value=True, create=True) as observe:
            self.assertTrue(worker.record_billing_success(t, [message]))
            observe.assert_called_once_with('test', 'cred-test', (now - 10), now)
        variants = []
        pending = copy.deepcopy(message); del pending['info']['time']['completed']; variants.append(pending)
        failed = copy.deepcopy(message); failed['info']['error'] = {'name': 'APIError'}; variants.append(failed)
        wrong = copy.deepcopy(message); wrong['info']['providerID'] = 'other'; variants.append(wrong)
        empty = copy.deepcopy(message); empty['parts'] = []; variants.append(empty)
        aborted = copy.deepcopy(message); aborted['info']['finish'] = 'error'; variants.append(aborted)
        old = copy.deepcopy(message); old['info']['time']['created'] = (now - 30) * 1000; variants.append(old)
        with patch.object(quota, 'observe_model_success', create=True) as observe:
            for m in variants:
                self.assertFalse(worker.record_billing_success(t, [m]))
            self.assertFalse(worker.record_billing_success(common.task('job-error'), [message]))
            observe.assert_not_called()
        self.assertNotIn('_billing_dispatch', common.public_task(t))

    def test_http_error_keeps_safe_message_and_redacts_key(self):
        body = json.dumps({'error': {'message': 'Invalid sk-abcdefghijklmnop'},
                           'headers': {'Authorization': 'must-not-appear'}}).encode()
        upstream = urllib.error.HTTPError('http://localhost', 401, 'Unauthorized', {}, io.BytesIO(body))
        with patch('urllib.request.OpenerDirector.open', side_effect=upstream):
            with self.assertRaises(common.HttpFailure) as raised:
                common.request('http://localhost')
        payload = diagnostics.exception(raised.exception, 'dispatch')
        self.assertIn('Invalid <redacted>', payload['message'])
        self.assertNotIn('must-not-appear', json.dumps(payload))
        self.assertFalse(payload['retryable'])

    def test_ambiguous_transport_requires_observation(self):
        value = diagnostics.exception(common.HttpFailure(message='Connection reset'), 'dispatch', True)
        self.assertTrue(value['prompt_may_have_been_accepted'])
        self.assertEqual(value['suggested_action'], 'observe_existing_session')
        self.assertIn('Connection reset', value['message'])

    def test_preparation_exception_available_without_result_file(self):
        t = common.task('job-error')
        for key in ('directory', 'session_id', 'dispatch_attempted_at'): t.pop(key)
        common.write_json(common.task_path('job-error'), t)
        with patch.object(worker, 'prepare', side_effect=ValueError('Workspace missing')):
            worker.run_task('job-error', threading.Event())
        result = diagnostics.collect('job-error')
        self.assertIsNone(result['result'])
        self.assertEqual(result['task']['status'], 'failed')
        self.assertEqual(result['task']['errors'][0]['message'], 'Workspace missing')

    def test_pending_questions_in_compact_result(self):
        pending = [{'sessionID':'ses_error','questions':[{'question':'Which format?'}]}]
        common.write_json(common.artifact_dir('job-error') / 'pending.json', pending)
        worker.finish(common.task('job-error'), [], 'needs_attention', 'permission_or_question_pending')
        self.assertEqual(diagnostics.collect('job-error')['result']['pending'], pending)

    def test_billing_error_trips_circuit_and_guides_coordinator(self):
        messages = [{'info': {'id': 'msg_bill', 'role': 'assistant',
                              'time': {'created': 1700000000000},
                              'error': {'name': 'APIError', 'data': {
                                  'message': 'Insufficient Balance', 'statusCode': 402,
                                  'isRetryable': False}}}, 'parts': []}]
        result = worker.finish(common.task('job-error'), messages)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'provider_billing_insufficient_balance')
        self.assertIsNotNone(quota.billing_block('test'))
        compact = diagnostics.collect('job-error')
        err = compact['result']['errors'][0]
        self.assertTrue(err['billing'])
        self.assertFalse(err['retryable'])
        self.assertEqual(err['suggested_action'], 'inspect_partial_work_and_reselect_profile')
        rec = compact['recovery']
        self.assertEqual(rec['blocked_reason'], 'provider_billing_error')
        self.assertFalse(rec['automatic_fallback'])
        # Sole profile is circuit-blocked: top-up is the only offered path right now.
        self.assertEqual(rec['suggested_action'], 'top_up_provider_account_then_resubmit')
        self.assertEqual(rec['alternatives'], [])
        # After top-up recovery, historical errors inform guidance but never re-arm.
        quota.clear('test')
        self.assertIsNone(quota.billing_block('test'))
        compact = diagnostics.collect('job-error')
        self.assertEqual(compact['recovery']['suggested_action'],
                         'inspect_partial_work_and_reselect_profile')
        self.assertEqual([a['profile'] for a in compact['recovery']['alternatives']], ['fast'])
        self.assertIsNone(quota.billing_block('test'))

    def test_tool_text_and_transport_never_trip_billing(self):
        report = {'outcome':'blocked','summary':'x','evidence':[],'tests':[],'unresolved':[]}
        messages = [{'info': {'id':'msg_reply','role':'assistant','providerID':'test','modelID':'model'},
                     'parts': [
                         {'type':'tool','tool':'bash','callID':'c1','state': {
                             'status':'error','input':{'command':'curl api'},
                             'error':'log line: HTTP 402 Insufficient Balance'}},
                         {'type':'text','text':json.dumps(report)}]}]
        worker.finish(common.task('job-error'), messages)
        self.assertIsNone(quota.billing_block('test'))
        self.assertNotEqual(common.task('job-error').get('reason'),
                            'provider_billing_insufficient_balance')
