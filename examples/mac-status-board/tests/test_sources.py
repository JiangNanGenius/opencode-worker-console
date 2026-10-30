import plistlib,os,copy,datetime,json,pathlib,sqlite3,sys,tempfile,threading,time,unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'host'))
from dashboard_sources import *
from run_agent import BoardSource,Dashboard,SharedDashboard
import status_agent
from types import SimpleNamespace

class LocalStub:
    instances=[]
    def __init__(self):
        self.stop=threading.Event();self.started=False;LocalStub.instances.append(self)
    def snapshot(self):return {'marker':'local'}
    def start(self):self.started=True

class Sources(unittest.TestCase):
    def test_board_source_prefers_bridge_and_stops_legacy_fetches(self):
        LocalStub.instances.clear()
        with tempfile.TemporaryDirectory() as root:
            path=pathlib.Path(root)/'host-board.json'
            with patch('run_agent.Dashboard',LocalStub):
                b=BoardSource(path)
                self.assertEqual(b.snapshot()['marker'],'local')  # no bridge file yet: collectors only source
                self.assertTrue(LocalStub.instances[0].started)
                self.assertFalse(LocalStub.instances[0].stop.is_set())
                path.write_text(json.dumps({'schema_version':2,'system':{'cpu':1,'marker':'shared'},'tasks':{},'quotas':[]}))
                snap=b.snapshot()
                self.assertEqual(snap['system']['marker'],'shared')
                self.assertTrue(LocalStub.instances[0].stop.is_set())  # legacy adapters halted, no duplicate account API calls
                path.unlink()
                self.assertEqual(b.snapshot()['marker'],'local')  # a withdrawn bridge falls back, still never both at once
                self.assertEqual(len(LocalStub.instances),2);self.assertTrue(LocalStub.instances[1].started)

    def test_snapshot_writer_uses_producer_data_when_bridge_exists(self):
        with tempfile.TemporaryDirectory() as root:
            shared=pathlib.Path(root)/'host-board.json';out=pathlib.Path(root)/'snap.json'
            shared.write_text(json.dumps({'schema_version':2,'system':{'cpu':7,'marker':'shared'},'tasks':{},'quotas':[]}))
            argv=['run_agent.py','--snapshot',str(out),'--shared',str(shared)]
            with patch.object(sys,'argv',argv):
                import run_agent;run_agent.main()
            self.assertEqual(json.loads(out.read_text())['system']['marker'],'shared')
    def test_shared_snapshot_preserves_sampling_age_and_last_record(self):
        with tempfile.TemporaryDirectory() as root:
            path=pathlib.Path(root)/'snapshot.json'
            path.write_text(json.dumps({'schema_version':2,'unix_time':100,'system':{'sampled_at':90,'cpu':12},'tasks':{},'quotas':[]}))
            source=SharedDashboard(path)
            self.assertEqual(source.snapshot()['system']['sampled_at'],90)
            path.write_text('broken')
            self.assertEqual(source.snapshot()['system']['cpu'],12)
            path.unlink()
            self.assertEqual(source.snapshot()['system']['sampled_at'],90)

    def test_mounted_external_disks_and_unplug(self):
        parts=[SimpleNamespace(mountpoint=p,device='/dev/disk'+str(i)) for i,p in enumerate(['/Volumes/Recovery','/Volumes/Image','/Volumes/External-Disk-2','/Volumes/External-Disk'])]
        def info(args,timeout):
            p=args[-1]
            return plistlib.dumps({'Internal':p.endswith('Recovery'),'BusProtocol':'Disk Image' if p.endswith('Image') else 'USB','VolumeName':pathlib.Path(p).name})
        usage=SimpleNamespace(free=10*1024**3,total=20*1024**3,percent=50)
        status_agent._disk_info_cache.clear()
        with patch('status_agent.psutil.disk_partitions',return_value=parts),patch('status_agent.subprocess.check_output',side_effect=info),patch('status_agent.psutil.disk_usage',return_value=usage):
            self.assertEqual([d['name'] for d in status_agent.collect_disks()],['Mac SSD','External-Disk','External-Disk-2'])
        with patch('status_agent.psutil.disk_partitions',return_value=[]),patch('status_agent.psutil.disk_usage',return_value=usage):
            self.assertEqual([d['name'] for d in status_agent.collect_disks()],['Mac SSD'])
        self.assertEqual(status_agent._disk_info_cache,{})
    def test_timezone_changes_while_agent_runs(self):
        previous=os.environ.get('TZ')
        try:
            for zone in ['Australia/Sydney','Asia/Shanghai','America/New_York']:
                os.environ['TZ']=zone
                with patch('run_agent.time.time',return_value=1791849600):snap=Dashboard().snapshot()
                self.assertEqual(snap['utc_offset_seconds'],time.localtime(snap['unix_time']).tm_gmtoff)
            self.assertEqual(snap['utc_offset_seconds'],-14400)
        finally:
            if previous is None:os.environ.pop('TZ',None)
            else:os.environ['TZ']=previous
            time.tzset()
    def test_model_uses_observed_records_only(self):
        self.assertEqual(task_model({'profile':'ark-auto'}),'模型未知')
        self.assertEqual(task_model({'actual_models':'p/a'}),'模型未知')
        self.assertEqual(task_model({'actual_models':['volcengine-agent-plan/ark-code-latest']}),'ark-code-latest')
        self.assertEqual(task_model({'actual_models':['p/a','p/a','q/b']}),'模型未知')
        self.assertEqual(task_model({'actual_models':[None,{},'https://bad.example?token=secret']}),'模型未知')
    def test_current_model_overrides_history_and_profile(self):
        self.assertEqual(task_model({'current_model':'deepseek/deepseek-flash','actual_models':['ark/ark-code-latest','deepseek/deepseek-flash'],'profile':'ark-auto'}),'deepseek-flash')
    def test_task_lifecycle_boundaries(self):
        self.assertEqual(classify('completed',1000,1300,1000),'completed')
        self.assertIsNone(classify('completed',1000,1301,1000))
        self.assertEqual(classify('needs_attention',1000,87400),'attention')
        self.assertIsNone(classify('needs_attention',1000,87401))
        self.assertEqual(classify('inProgress',1000,1100,active_age=121),'unknown')
        self.assertIsNone(classify('interrupted',1000,1100))
    def test_public_title_never_uses_sensitive_prompt(self):
        for s in ['WiFi password example-secret','配置 API key 测试','https://example.test/a']:
            self.assertEqual(display_title(s),'未命名任务')
        self.assertEqual(display_title('修复中文界面'),'修复中文界面')
    def test_quotas_missing_is_not_zero_and_100_is_zero_remaining(self):
        self.assertEqual(normalize_codex([{'provider':'codex','usage':{}}])['windows'],[])
        q=normalize_codex([{'provider':'codex','usage':{'secondary':{'usedPercent':100,'windowMinutes':10080,'resetsAt':'2026-10-10T02:05:20Z'}}}],1000)
        self.assertEqual(q['windows'][0]['remaining_percent'],0)
        self.assertEqual(q['windows'][0]['label'],'每周')
        q=normalize_codex([{'provider':'codex','usage':{'primary':{'usedPercent':False}}}])
        self.assertEqual(q['windows'],[])
        q=normalize_worker('deepseek',{'state':'ok','sampled_at':1000,'balances':[{'currency':'CNY','remaining':12.3}]})
        self.assertEqual(q['balances'][0]['remaining'],12.3);self.assertEqual(q['windows'],[])
    def test_aliyun_row_keeps_credits_valid_until_and_unknown_never_healthy(self):
        q=normalize_worker('bailian-token-plan',{'state':'ok','sampled_at':1000,'valid_until':1000+24*3600,'available':True,'windows':[
            {'valid':True,'duration_minutes':43200,'remaining_percent':99.9,'remaining':99926.94,'limit':100000,'resets_at':'2026-11-16T10:00:00Z'},
            {'valid':True,'duration_minutes':300,'remaining_percent':100,'remaining':49000,'limit':49152,'resets_at':1100}],'balances':[]})
        self.assertEqual(q['id'],'aliyun');self.assertEqual(q['name'],'阿里百炼')
        self.assertEqual(q['valid_until'],1000+24*3600)
        self.assertEqual(q['windows'][0]['unit'],'credits')
        self.assertEqual(q['windows'][0]['remaining'],99926.94);self.assertEqual(q['windows'][0]['limit'],100000.0)
        self.assertIs(q['available'],True)
        # A row without amounts never invents a unit or a zero balance.
        q=normalize_worker('bailian-token-plan',{'state':'ok','sampled_at':1000,'windows':[{'valid':True,'duration_minutes':1440,'remaining_percent':50}],'balances':[]})
        self.assertNotIn('unit',q['windows'][0]);self.assertNotIn('remaining',q['windows'][0])
        # Unknown availability stays an explicit null, never a healthy True and never a fake False.
        q=normalize_worker('bailian-token-plan',{'state':'ok','sampled_at':1000,'available':None,'windows':[],'balances':[]})
        self.assertIsNone(q['available'])
        # Stale valid_until (before sampled_at) is dropped, so the 15-minute rule applies.
        q=normalize_worker('bailian-token-plan',{'state':'ok','sampled_at':5000,'valid_until':4000,'windows':[],'balances':[]})
        self.assertNotIn('valid_until',q)
    def test_fallback_quota_snapshot_keeps_five_configured_rows_visible(self):
        def boom(*a,**k):raise RuntimeError('sampler not deployed yet')
        with patch('dashboard_sources.worker_quota',side_effect=boom),patch('dashboard_sources.codex_quota',side_effect=boom):
            rows=quota_snapshot()
        self.assertEqual([r['id'] for r in rows],['codex','kimi','ark','deepseek','aliyun'])
        self.assertEqual(rows[-1]['name'],'阿里百炼');self.assertEqual(rows[-1]['state'],'unavailable')
        self.assertEqual(rows[-1]['sampled_at'],0);self.assertEqual(rows[-1]['windows'],[])
        self.assertNotIn('available',rows[-1])  # never fabricates an exhaustion verdict
    def test_incremental_tail_partial_lines_and_truncation(self):
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d)/'events';c=CodexTasks(d)
            line=json.dumps({'timestamp':'2026-10-05T09:00:00Z','type':'event_msg','payload':{'type':'task_started'}}).encode()+b'\n'
            p.write_bytes(line[:50]);self.assertIsNone(c.activity(p,0)['event'])
            with p.open('ab') as f:f.write(line[50:])
            self.assertEqual(c.activity(p,0)['event'],'task_started')
            self.assertEqual(c.activity(p,0)['event'],'task_started')
            p.write_bytes(b'{}\n');self.assertIsNone(c.activity(p,0)['event'])
    def test_corrupt_worker_record_and_order(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d)/'tasks';root.mkdir()
            (root/'bad.json').write_text('{')
            for i,state in enumerate(['running','needs_attention','completed']):
                (root/('%d.json'%i)).write_text(json.dumps({'id':str(i),'title':'任务','status':state,'updated_at':1000,'started_at':900,'finished_at':1000 if state=='completed' else 0}))
            v=opencode_tasks(1100,d);self.assertEqual([t['status'] for t in v['items']],['running']);self.assertEqual(v['active'],1);self.assertEqual(v['total'],1)
            self.assertEqual(opencode_tasks(1201,d)['items'],[])
    def test_readonly_databases_and_display_name(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d);now=time.time()
            con=sqlite3.connect(root/'state_5.sqlite');con.execute('create table threads(id,title,name,cwd,rollout_path,updated_at,archived)')
            con.execute('insert into threads values(?,?,?,?,?,?,?)',('t','password secret','安全标题','/tmp/project',str(root/'rollout'),now,0));con.commit();con.close()
            con=sqlite3.connect(root/'thread_history_1.sqlite');con.execute('create table thread_turns(thread_id,status,started_at,completed_at,rollout_ordinal)');con.execute('insert into thread_turns values(?,?,?,?,?)',('t','inProgress',now-30,None,1));con.commit();con.close()
            (root/'rollout').write_text(json.dumps({'timestamp':datetime.datetime.fromtimestamp(now,datetime.timezone.utc).isoformat(),'type':'event_msg','payload':{'type':'task_started'}})+'\n')
            before={p.name:p.read_bytes() for p in root.glob('*.sqlite')}
            v=CodexTasks(root).collect(now);self.assertEqual(v['items'][0]['title'],'安全标题');self.assertEqual(v['items'][0]['status'],'running')
            self.assertEqual(before,{p.name:p.read_bytes() for p in root.glob('*.sqlite')})
    def test_quota_failure_keeps_original_sampling_age(self):
        d=Dashboard();old={'id':'codex','state':'ok','sampled_at':1000,'windows':[{'remaining_percent':25}],'balances':[]};d.value['quotas']=[old]
        class Result:stdout=json.dumps([{'id':'codex','state':'unavailable','sampled_at':0,'windows':[],'balances':[]}])
        with patch('run_agent.subprocess.run',return_value=Result()):d.quota_once()
        self.assertEqual(d.value['quotas'][0]['sampled_at'],1000)
        self.assertEqual(d.value['quotas'][0]['windows'],old['windows'])
        self.assertEqual(d.value['quotas'][0]['state'],'unavailable')
    def test_count_and_bound(self):
        v=[{'id':str(i),'status':'running','updated_at':i} for i in range(30)]
        self.assertEqual(len(sort_tasks(v)),12);self.assertEqual(sort_tasks(v)[0]['id'],'29')
class SequentialFallbackQuotas(unittest.TestCase):
    def sequence(self):
        return {'plan_edition':'sequential','state':'ok','sampled_at':9999,
                'available':False,'stale':True,'billing':{'reason':'old-team-zero'},
                'active_plan':'personal','plan_quotas':{
                    'team':{'state':'ok','sampled_at':1000,'available':False,
                            'windows':[{'name':'Credits(max)','valid':True,'remaining':0,
                                        'limit':100000,'remaining_percent':0,'duration_minutes':44640}]},
                    'personal':{'state':'ok','sampled_at':1001,'available':True,
                                'source':'personal_console','windows':[{'name':'Monthly','valid':True,
                                    'unit':'percent','duration_minutes':40320,'remaining_percent':97.1234}]}}}

    def terminal_sequence(self,personal_percent=0.0):
        now=time.time();team_end=datetime.datetime.fromtimestamp(now+17*86400,datetime.timezone.utc).isoformat()
        personal_end=datetime.datetime.fromtimestamp(now+27*86400,datetime.timezone.utc).isoformat()
        team={'state':'ok','source':'openapi_live','plan_edition':'team','sampled_at':now,
              'available':False,'windows':[{'name':'Credits(max)','limit':250000.0,'remaining':0.0,
              'remaining_percent':0.0,'resets_at':team_end,'valid':True}],'balances':[],
              'subscription':{'start_at':datetime.datetime.fromtimestamp(now-14*86400,datetime.timezone.utc).isoformat(),'end_at':team_end}}
        personal={'state':'ok','source':'personal_console','plan_edition':'personal','sampled_at':now,
                  'available':personal_percent>0,'windows':[{'name':'Monthly','unit':'percent',
                  'remaining_percent':personal_percent,'resets_at':personal_end,'valid':True}],'balances':[],
                  'subscription':{'start_at':datetime.datetime.fromtimestamp(now-4*86400,datetime.timezone.utc).isoformat(),'end_at':personal_end},
                  'addon':{'known':True,'remaining':0.0,'limit':40000.0,'used':40000.0,
                           'active_count':2,'remaining_percent':0.0,
                           'nearest_expires_at':datetime.datetime.fromtimestamp(now+29*86400,datetime.timezone.utc).isoformat()}}
        return {'plan_edition':'sequential','state':'ok','sampled_at':now,'checked_at':now,
                'attempted_at':now,'available':personal_percent>0,'windows':[],'balances':[],
                'plan_quotas':{'team':team,'personal':personal},'active_plan':'team'}

    def test_terminal_fallback_rows_are_filtered_until_replenished(self):
        self.assertEqual(worker_rows('bailian-token-plan',self.terminal_sequence()),[])
        rows=worker_rows('bailian-token-plan',self.terminal_sequence(personal_percent=97.48))
        self.assertEqual([row['id'] for row in rows],['aliyun-personal'])
        zero={'state':'ok','sampled_at':time.time(),'available':False,'windows':[],
              'balances':[{'currency':'CNY','remaining':0.0}]}
        self.assertEqual(worker_rows('deepseek',zero),[])
        self.assertEqual(len(worker_rows('deepseek',dict(zero,available=True,balances=[{'currency':'CNY','remaining':1.0}]))),1)

    def test_quota_snapshot_omits_terminal_alibaba_rows(self):
        import dashboard_sources as ds
        def collect(provider):
            if provider=='bailian-token-plan':return worker_rows('bailian-token-plan',self.terminal_sequence())
            if provider=='deepseek':return worker_rows('deepseek',{'state':'ok','sampled_at':time.time(),
                'available':False,'windows':[],'balances':[{'currency':'CNY','remaining':0.0}]})
            return normalize_worker(provider,{'state':'unknown'})
        with patch.object(ds,'worker_quota',side_effect=collect), \
                patch.object(ds,'codex_quota',return_value={'id':'codex','state':'unknown'}):
            rows=quota_snapshot()
        ids=[row['id'] for row in rows]
        self.assertNotIn('aliyun-team',ids);self.assertNotIn('aliyun-personal',ids);self.assertNotIn('deepseek',ids)
        self.assertIn('kimi',ids);self.assertIn('ark',ids)

    def test_independent_cards_do_not_inherit_parent_or_team_zero(self):
        team,personal=worker_rows('bailian-token-plan',self.sequence())
        self.assertEqual([team['id'],personal['id']],['aliyun-team','aliyun-personal'])
        self.assertEqual([team['name'],personal['name']],['阿里团队','阿里个人'])
        self.assertEqual([team['sampled_at'],personal['sampled_at']],[1000,1001])
        self.assertFalse(team['available']);self.assertTrue(personal['available'])
        self.assertEqual(team['windows'][0]['remaining'],0)
        self.assertEqual(team['windows'][0]['unit'],'credits')
        self.assertEqual(personal['windows'][0]['unit'],'percent')
        self.assertEqual(personal['windows'][0]['remaining_percent'],97.123)
        self.assertNotIn('remaining',personal['windows'][0])
        self.assertNotIn('billing',personal);self.assertNotIn('stale',personal)
        self.assertEqual(personal['plan_slot'],'active')

    def test_monthly_name_is_independent_of_28_or_31_day_cycle(self):
        for minutes in (28*1440,31*1440):
            value=self.sequence();value['plan_quotas']['personal']['windows'][0]['duration_minutes']=minutes
            self.assertEqual(worker_rows('bailian-token-plan',value)[1]['windows'][0]['label'],'每月')
        personal={'plan_edition':'personal','state':'ok','sampled_at':1000,'windows':[
            {'name':'Monthly','valid':True,'unit':'percent','remaining_percent':80,'duration_minutes':31*1440}]}
        row=worker_rows('bailian-token-plan',personal)[0]
        self.assertEqual(row['id'],'aliyun');self.assertEqual(row['windows'][0]['label'],'每月')
        self.assertEqual(row['windows'][0]['unit'],'percent')
        personal['windows']=[{'name':'Short','valid':True,'unit':'percent','remaining_percent':80,'duration_minutes':300}]
        self.assertEqual(worker_rows('bailian-token-plan',personal)[0]['windows'][0]['label'],'5小时')

    def test_partial_failure_and_unknown_are_independent(self):
        value=self.sequence();value['plan_quotas']['team']={'state':'unavailable','available':None,
            'sampled_at':900,'stale':True,'windows':[]}
        team,personal=worker_rows('bailian-token-plan',value)
        self.assertEqual(team['state'],'unavailable');self.assertTrue(team['stale'])
        self.assertEqual(team['sampled_at'],900);self.assertIsNone(team['available'])
        self.assertEqual(personal['state'],'ok');self.assertNotIn('stale',personal)
        value['plan_quotas'].pop('personal')
        unknown=worker_rows('bailian-token-plan',value)[1]
        self.assertEqual(unknown['state'],'unknown');self.assertIsNone(unknown['available'])
        self.assertEqual(unknown['sampled_at'],0);self.assertEqual(unknown['windows'],[])

    def test_worker_quota_fetches_once_and_snapshot_flattens_only_sequential(self):
        import dashboard_sources as ds
        from unittest.mock import Mock
        fetch=Mock(return_value=self.sequence())
        with patch.dict(sys.modules,{'quota':SimpleNamespace(fetch_one=fetch)}):
            split=worker_quota('bailian-token-plan')
        fetch.assert_called_once_with('bailian-token-plan')
        self.assertEqual(len(split),2)
        def collect(provider):
            if provider=='bailian-token-plan':return split
            return normalize_worker(provider,{'state':'unknown'})
        with patch.object(ds,'worker_quota',side_effect=collect), \
                patch.object(ds,'codex_quota',return_value={'id':'codex','state':'unknown'}):
            rows=quota_snapshot()
        self.assertEqual([r['id']for r in rows],['codex','kimi','ark','deepseek','aliyun-team','aliyun-personal'])

    def test_legacy_team_and_other_providers_remain_single_rows(self):
        team=self.sequence()['plan_quotas']['team']
        row=worker_rows('bailian-token-plan',team)
        self.assertEqual(len(row),1);self.assertEqual(row[0]['id'],'aliyun')
        self.assertEqual(row[0]['windows'][0]['unit'],'credits')
        self.assertEqual(len(worker_rows('deepseek',{'state':'unknown'})),1)

    def test_kimi_total_and_code_monthly_survive_legacy_window_cap(self):
        now=time.time()
        value={'state':'ok','sampled_at':now,'windows':[
            {'valid':True,'duration_minutes':300,'remaining_percent':10},
            {'valid':True,'duration_minutes':10080,'remaining_percent':20},
            {'valid':True,'duration_minutes':1440,'name':'old-daily','remaining_percent':30},
            {'valid':True,'duration_minutes':None,'name':'extra','remaining_percent':40}],
            'account':{'state':'ok','sampled_at':now,'account_binding':{'state':'verified'},
                       'windows':[{'kind':kind,'valid':True,'remaining_percent':amount}
                                  for kind,amount in [('month_total',81),('month_code',72)]]}}
        row=normalize_worker('kimi-for-coding',value)
        self.assertEqual([w['kind']for w in row['windows']],['short','weekly','month_total','month_code'])
        self.assertEqual([w['label']for w in row['windows']],['5小时','每周','总额度','月度Code'])
        value['account']['stale']=True
        row=normalize_worker('kimi-for-coding',value)
        self.assertNotIn('month_total',[w['kind']for w in row['windows']])

if __name__=='__main__':unittest.main()
