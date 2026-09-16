import io
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
import worker


class ErrorBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(common, 'STATE', self.root),
                        patch.object(worker, 'STATE', self.root),
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
