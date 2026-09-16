import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import common
import steering
import management
import bootstrap


class ControlsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.patches=[patch.object(common,'STATE',self.root/'state'),patch.object(common,'CONFIG',self.root/'config.json')]
        for p in self.patches:p.start()
        common.init();common.write_json(common.CONFIG,{'profiles':{'worker':{'model':'acme/test'}}})
        common.write_json(common.task_path('job-test'),{'id':'job-test','status':'running','created_at':1,'session_id':'ses_test','directory':str(self.root),'profile':'worker'})
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def test_guidance_idempotency_and_no_permission_expansion(self):
        bodies=[]
        def api(path,directory=None,method='GET',data=None,**kwargs):
            if method=='POST':bodies.append(data);return None
            return {'ses_test':{'type':'busy'}}
        with patch.object(common,'api',side_effect=api),patch.object(common,'redact',side_effect=lambda x:x):
            first=steering.send('job-test','Check the boundary case','request-1')
            self.assertEqual(first,steering.send('job-test','Check the boundary case','request-1'))
            with self.assertRaises(ValueError):steering.send('job-test','Different','request-1')
        self.assertEqual(len(bodies),1);self.assertNotIn('tools',bodies[0]);self.assertNotIn('permission',bodies[0])
        self.assertEqual(first['status'],'accepted')
    def test_transport_ambiguity_not_replayed(self):
        def api(path,directory=None,method='GET',data=None,**kwargs):
            if method=='POST':raise common.HttpFailure()
            return {'ses_test':{'type':'busy'}}
        with patch.object(common,'api',side_effect=api) as call,patch.object(common,'redact',side_effect=lambda x:x):
            self.assertEqual(steering.send('job-test','A','stable')['status'],'uncertain')
            n=call.call_count;steering.send('job-test','A','stable');self.assertEqual(call.call_count,n)
    def test_idle_task_does_not_start_new_turn(self):
        with patch.object(common,'api',return_value={}),patch.object(common,'redact',side_effect=lambda x:x),self.assertRaises(ValueError):
            steering.send('job-test','A')
    def test_completion_waits_for_guidance_reply(self):
        t={'guidance':[{'message_id':'msg_new','status':'accepted'}]}
        self.assertFalse(steering.settled(t,[{'info':{'role':'assistant','parentID':'msg_old'}}]))
        messages=[{'info':{'id':'msg_new','role':'user'}},{'info':{'role':'assistant','parentID':'msg_new'}}]
        self.assertTrue(steering.settled(t,messages))
    def test_native_id_timestamp_order(self):
        with patch.object(common.time,'time',return_value=1789600000):a=common.message_id()
        with patch.object(common.time,'time',return_value=1789600001):b=common.message_id()
        self.assertLess(a,b);self.assertRegex(a,r'^msg_[0-9a-f]{26}$')
    def test_workspace_move_preserves_source_and_no_file_transfer(self):
        common.update('job-test',status='completed')
        dest=self.root/'second';dest.mkdir();moves=[]
        def api(path,directory=None,method='GET',data=None,**kwargs):
            if path=='/session/status':return {}
            if path.startswith('/experimental/control-plane'):
                moves.append(data);return None
            return {'id':'ses_test','title':'Test','directory':str(dest if moves else self.root)}
        with patch.object(common,'api',side_effect=api):
            result=management.update_session('ses_test',{'action':'bind','directory':str(dest)})
        self.assertEqual(result['directory'],str(dest));self.assertFalse(moves[0]['moveChanges'])
        self.assertEqual(common.task('job-test')['directory'],str(self.root))
        self.assertEqual(common.task('job-test')['session_directory'],str(dest))
    def test_permission_upgrade_only_changes_idle_owned_sessions(self):
        common.update('job-test',status='completed')
        calls=[]
        def api(path,directory=None,method='GET',data=None,**kwargs):
            if method=='PATCH': calls.append((path,data)); return {}
            return {}
        with patch.object(common,'api',side_effect=api):
            r=management.sync_worker_permissions()
            self.assertEqual(r['updated'],['ses_test'])
            self.assertEqual(calls,[('/session/ses_test',{'permission':[{'permission':'*','pattern':'*','action':'allow'}]})])
            self.assertTrue(common.task('job-test')['auto_approve'])
            self.assertEqual(management.sync_worker_permissions()['updated'],[])
        common.update('job-test',auto_approve=False,status='running')
        with patch.object(common,'api') as api:
            self.assertEqual(management.sync_worker_permissions()['skipped'],['ses_test'])
            api.assert_not_called()
        common.update('job-test',status='completed')
        with patch.object(common,'api',return_value={'ses_test':{'type':'busy'}}) as api:
            self.assertEqual(management.sync_worker_permissions()['skipped'],['ses_test'])
            self.assertEqual(api.call_count,1)

    def test_workspace_registry_validation(self):
        with self.assertRaises(ValueError):management.save_workspace({'directory':'relative'})
        management.save_workspace({'directory':str(self.root),'name':'First'})
        management.save_workspace({'directory':str(self.root),'name':'Renamed'})
        self.assertEqual(len(management.workspaces()),1)
        self.assertEqual(management.workspaces()[0]['name'],'Renamed')
    def test_existing_opencode_never_installs(self):
        binary=self.root/'opencode';binary.write_text('test');binary.chmod(0o700)
        with patch.object(bootstrap.subprocess,'run') as run:
            self.assertEqual(bootstrap.ensure_opencode(str(binary)),str(binary));run.assert_not_called()
    def test_auto_install_uses_official_pinned_npm_package(self):
        def run(args,**kwargs):
            prefix=Path(args[args.index('--prefix')+1]);binary=prefix/'node_modules/.bin/opencode';binary.parent.mkdir(parents=True);binary.write_text('test');binary.chmod(0o700)
        with patch.object(bootstrap.shutil,'which',side_effect=lambda x:'/fake/npm' if x=='npm' else None),patch.object(bootstrap.subprocess,'run',side_effect=run) as called:
            binary=bootstrap.ensure_opencode()
        self.assertTrue(Path(binary).exists());args=called.call_args.args[0]
        self.assertIn('opencode-ai@1.18.30',args);self.assertIn('--registry=https://registry.npmjs.org',args)
