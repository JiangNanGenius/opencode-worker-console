import base64
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import transcript
import diagnostics


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(common, 'STATE', self.root/'state'),
                        patch.object(common, 'CONFIG', self.root/'config.json'),
                        patch.dict(os.environ, {'XDG_DATA_HOME':str(self.root/'data')})]
        for p in self.patches: p.start()
        common.init()
        common.write_json(common.CONFIG, {'profiles':{}})
        common.write_json(common.task_path('job-test'), {'id':'job-test','session_id':'ses_test',
            'title':'Fixture','directory':'/old/workspace','session_directory':'/moved/workspace','status':'completed'})
        self.messages = [{'info':{'id':'msg_'+str(i), 'role':'user' if i%2 else 'assistant'},
                          'parts':[{'type':'text','text':'message '+str(i)}]} for i in range(1,8)]
        self.calls=[]

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def api(self, path, directory=None, method='GET', data=None, **kw):
        from urllib.parse import parse_qs, urlsplit
        self.calls.append((path,directory,method))
        self.assertEqual(method,'GET')
        if path=='/session/ses_test': return {'id':'ses_test','directory':'/moved/workspace','title':'Fixture'}
        self.assertEqual(directory,'/moved/workspace')
        query=parse_qs(urlsplit(path).query)
        messages=self.messages
        if 'before' in query:
            cursor=json.loads(base64.urlsafe_b64decode(query['before'][0]+'='*(-len(query['before'][0])%4)))
            i=next(i for i,m in enumerate(messages) if m['info']['id']==cursor['id'])
            messages=messages[:i]
        next_before=None
        if 'limit' in query:
            limit=int(query['limit'][0]);has_more=len(messages)>limit;messages=messages[-limit:]
            if has_more:next_before=base64.urlsafe_b64encode(json.dumps({'id':messages[0]['info']['id'],'time':1}).encode()).decode().rstrip('=')
        return (messages,next_before) if kw.get('include_cursor') else messages

    def test_live_pages_cover_every_message_without_duplicates(self):
        ids=[];before=None
        with patch.object(common,'api',side_effect=self.api):
            while True:
                page=transcript.read('job-test',limit=2,before=before)
                ids=[m['info']['id'] for m in page['messages']]+ids
                if not page['has_more']:break
                before=page['next_before']
        self.assertEqual(ids,[m['info']['id'] for m in self.messages])
        self.assertEqual(page['session']['directory'],'/moved/workspace')
        self.assertEqual(page['source'],'live_opencode')

    def test_full_keeps_tool_inputs_outputs_and_all_messages(self):
        self.messages[1]['parts']=[{'type':'tool','tool':'bash','state':{'input':{'command':'example-test'},
                                    'output':'x'*9000,'metadata':{'exit':7}}}]
        with patch.object(common,'api',side_effect=self.api):
            page=transcript.read('ses_test',limit=6)
            full=transcript.read('ses_test',full=True)
        self.assertEqual(page['truncated_fields'],1)
        self.assertIn('[truncated;',page['messages'][0]['parts'][0]['state']['output'])
        self.assertEqual(full['messages'],self.messages)
        self.assertEqual(full['truncated_fields'],0)
        self.assertFalse(full['has_more'])
        self.assertEqual(self.calls[-1][0],'/session/ses_test/message')

    def test_redacts_api_oauth_password_and_sensitive_fields(self):
        api_key='fixture-api-credential-value'
        access='fixture-oauth-access-value'
        refresh='fixture-oauth-refresh-value'
        server='fixture-server-password-value'
        common.write_json(self.root/'data/opencode/auth.json',{'api':{'type':'api','key':api_key},
                          'oauth':{'type':'oauth','access':access,'refresh':refresh}})
        (common.STATE/'server-password').write_text(server)
        self.messages[0]['parts']=[{'type':'text','text':' '.join((api_key,access,refresh,server))},
                                  {'type':'tool','state':{'input':{'Authorization':'Bearer fixture'}}}]
        with patch.object(common,'api',side_effect=self.api):full=transcript.read('ses_test',full=True)
        encoded=json.dumps(full)
        for secret in (api_key,access,refresh,server,'Bearer fixture'):self.assertNotIn(secret,encoded)
        self.assertIn('<redacted>',encoded)

    def test_saved_snapshot_is_explicit_offline_and_paginated(self):
        common.write_json(common.artifact_dir('job-test')/'messages.json',self.messages)
        common.update('job-test',session_deleted=True)
        with patch.object(common,'api') as api:
            with self.assertRaisesRegex(ValueError,'deleted'):transcript.read('job-test')
            first=transcript.read('job-test',limit=2,saved=True)
            second=transcript.read('job-test',limit=2,before=first['next_before'],saved=True)
            full=transcript.read('job-test',full=True,saved=True)
            api.assert_not_called()
        self.assertEqual(full['messages'],self.messages)
        self.assertEqual(full['source'],'saved_artifact')
        self.assertEqual(full['session']['directory'],'/old/workspace')
        self.assertEqual(second['messages'][-1]['info']['id'],'msg_5')

    def test_export_is_private_and_never_overwrites(self):
        common.write_json(common.artifact_dir('job-test')/'messages.json',self.messages)
        full=transcript.read('job-test',full=True,saved=True)
        path=self.root/'session.transcript.json'
        result=transcript.export(full,path)
        self.assertNotIn('messages',result)
        self.assertEqual(json.loads(path.read_text())['messages'],self.messages)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
        with self.assertRaises(FileExistsError):transcript.export(full,path)
        symlink=self.root/'link.json';symlink.symlink_to(path)
        with self.assertRaises(FileExistsError):transcript.export(full,symlink)

    def test_invalid_input_and_unavailable_snapshot(self):
        with patch.object(common,'api') as api:
            for kw in ({'limit':0},{'limit':101},{'limit':True},{'before':'../bad'}, {'before':'msg_x','full':True}):
                with self.assertRaises(ValueError):transcript.read('ses_test',**kw)
            with self.assertRaises(ValueError):transcript.read('bad/path')
            with self.assertRaises(ValueError):transcript.read('ses_test',saved=True)
            with self.assertRaises(ValueError):transcript.read('job-test',saved=True)
            api.assert_not_called()

    def test_default_collect_does_not_load_transcript(self):
        common.write_json(common.artifact_dir('job-test')/'messages.json',self.messages)
        result=diagnostics.collect('job-test')
        self.assertNotIn('messages',json.dumps(result))

    def test_cli_saved_export_does_not_start_opencode(self):
        common.write_json(common.artifact_dir('job-test')/'messages.json',self.messages)
        path=self.root/'cli.transcript.json'
        env=dict(os.environ,DELEGATE_STATE=str(common.STATE),DELEGATE_CONFIG=str(common.CONFIG))
        cli=Path(__file__).resolve().parents[1]/'scripts/delegate.py'
        run=subprocess.run([sys.executable,str(cli),'transcript','job-test','--saved','--full','--output',str(path)],
                           env=env,capture_output=True,text=True)
        self.assertEqual(run.returncode,0,run.stderr)
        result=json.loads(run.stdout)
        self.assertNotIn('messages',result)
        self.assertEqual(result['returned_messages'],7)
        self.assertFalse((common.STATE/'services.json').exists())
