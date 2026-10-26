#!/usr/bin/env python3
"""Independent source schedules; publishing must never block acquisition."""
import argparse,base64,copy,json,pathlib,subprocess,sys,threading,time,socket,signal,urllib.request,urllib.parse
from status_agent import collect
from dashboard_sources import CodexTasks,opencode_tasks
from telemetry_server import TelemetryServer,sanitize_snapshot
ROOT=pathlib.Path(__file__).resolve().parent.parent

class Dashboard:
    def __init__(self):
        self.lock=threading.Lock();self.stop=threading.Event()
        self.value={'schema_version':2,'system':{},'tasks':{
            'codex':{'state':'unavailable','sampled_at':0,'items':[],'total':0},
            'opencode':{'state':'unavailable','sampled_at':0,'items':[],'total':0}},'quotas':[]}
    def snapshot(self):
        with self.lock:d=copy.deepcopy(self.value)
        time.tzset()  # Re-read the Mac timezone, including changes while this process runs.
        d['unix_time']=int(time.time())
        d['utc_offset_seconds']=time.localtime(d['unix_time']).tm_gmtoff
        return d
    def system_loop(self):
        while not self.stop.is_set():
            start=time.monotonic()
            try:
                value=collect();value.pop('cmd',None);value['sampled_at']=time.time()
                with self.lock:self.value['system']=value
            except Exception:pass  # Original sampled_at remains, so stale data cannot look fresh.
            self.stop.wait(max(.1,2-(time.monotonic()-start)))
    def task_loop(self):
        codex=CodexTasks()
        while not self.stop.is_set():
            start=time.monotonic()
            for name,fn in [('codex',codex.collect),('opencode',opencode_tasks)]:
                try:value=fn()
                except Exception:
                    with self.lock:value=dict(self.value['tasks'][name],state='unavailable')
                with self.lock:self.value['tasks'][name]=value
            self.stop.wait(max(.1,3-(time.monotonic()-start)))
    def quota_once(self):
        r=subprocess.run([sys.executable,str(ROOT/'host/dashboard_sources.py'),'--quotas'],capture_output=True,text=True,timeout=45,check=True)
        values=json.loads(r.stdout)
        with self.lock:
            previous={v['id']:v for v in self.value['quotas']}
            for v in values:
                if v['state']!='ok' and v['id'] in previous:
                    old=previous[v['id']];v.update(windows=old['windows'],balances=old['balances'],sampled_at=old['sampled_at'])
            self.value['quotas']=values
    def quota_loop(self):
        while not self.stop.is_set():
            try:self.quota_once()
            except Exception:
                with self.lock:
                    for q in self.value['quotas']:q['state']='unavailable'
            self.stop.wait(300)
    def start(self):
        for fn in [self.system_loop,self.task_loop,self.quota_loop]:
            threading.Thread(target=fn,daemon=True).start()

class SharedDashboard:
    """Read the console's atomic snapshot without running duplicate collectors."""
    def __init__(self, path):
        self.path=path
        self.last={'schema_version':2,'system':{},'tasks':{},'quotas':[]}
    def snapshot(self):
        try:
            value=json.loads(self.path.read_text())
            if value.get('schema_version')==2 and isinstance(value.get('system'),dict):
                self.last=value
        except (OSError,ValueError):
            pass
        value=copy.deepcopy(self.last)
        # Preserve source sample times; an unavailable bridge must look stale.
        time.tzset()
        value['unix_time']=int(time.time())
        value['utc_offset_seconds']=time.localtime().tm_gmtoff
        return value
    def start(self):
        pass

class BoardSource:
    """Prefer the bridge's published snapshot; the local collectors cover only its absence.
    Re-checked every cycle, so a freshly deployed bridge needs no agent restart and the
    legacy quota adapters cannot keep making duplicate account API calls."""
    def __init__(self, path):
        self.path=path;self.shared=SharedDashboard(path);self.local=None;self.using=None;self.lock=threading.Lock()
    def snapshot(self):
        with self.lock:return self._snapshot()
    def _snapshot(self):
        if self.path.is_file():
            if self.local is not None:
                self.local.stop.set();self.local=None
                print('Board source switched to the shared bridge snapshot',flush=True)
            elif self.using!='shared':
                print('Board source: shared bridge snapshot',flush=True)
            self.using='shared'
            return self.shared.snapshot()
        if self.local is None:
            self.local=Dashboard();self.local.start()
            if self.using is not None:print('Shared bridge snapshot missing; resuming local collectors',flush=True)
            self.using='local'
        return self.local.snapshot()
    def start(self):
        pass

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--snapshot',type=pathlib.Path)
    parser.add_argument('--url',default='http://mac-status-board.local')
    parser.add_argument('--shared',type=pathlib.Path,default=pathlib.Path.home()/'.local/state/delegate-opencode/host-board.json')
    parser.add_argument('--no-tcp',action='store_true',help='Use legacy HTTP push only')
    args=parser.parse_args()
    shared=args.shared
    if args.snapshot:
        if shared.is_file():
            # Preview and diagnostics must use the latest producer data, not a second opinion.
            args.snapshot.write_text(json.dumps(SharedDashboard(shared).snapshot(),ensure_ascii=False,indent=2)+'\n');return
        d=Dashboard()
        system=collect();system.pop('cmd',None);system['sampled_at']=time.time();d.value['system']=system
        d.value['tasks']={'codex':CodexTasks().collect(),'opencode':opencode_tasks()};d.quota_once()
        args.snapshot.write_text(json.dumps(d.snapshot(),ensure_ascii=False,indent=2)+'\n');return
    key=(ROOT/'private/ota-token').read_text().strip();auth='Basic '+base64.b64encode(('admin:'+key).encode()).decode()
    # Prefer the known LAN address to avoid mDNS resolution delaying clock sync.
    urls=[args.url]
    try:
        parsed=urllib.parse.urlsplit(args.url)
        address=socket.gethostbyname(parsed.hostname)
        resolved=urllib.parse.urlunsplit((parsed.scheme,address+(':'+str(parsed.port) if parsed.port else ''),parsed.path,parsed.query,''))
        if resolved!=args.url:urls.insert(0,resolved)
    except OSError:pass
    d=BoardSource(shared)
    connected=False;d.start()
    server=None
    if not args.no_tcp:
        try:
            server=TelemetryServer(d.snapshot,key.encode()).start()
            print('LAN telemetry listening on TCP/UDP 8642',flush=True)
        except OSError:
            print('LAN telemetry unavailable; retaining HTTP push',flush=True)
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
    try:
      while True:
          start=time.monotonic()
          if server is not None and server.active_client():
              time.sleep(1);continue
          payload=json.dumps(sanitize_snapshot(d.snapshot()),ensure_ascii=False,separators=(',',':')).encode()
          if len(payload)<=24576:
              for url in urls.copy():
                  try:
                      req=urllib.request.Request(url+'/stats',data=payload,headers={'Content-Type':'application/json','Authorization':auth})
                      with urllib.request.urlopen(req,timeout=3) as r:
                          if r.status!=200:raise RuntimeError('Rejected')
                      if not connected:print('Telemetry connected',flush=True)
                      connected=True;urls.remove(url);urls.insert(0,url);break
                  except Exception:
                      if connected:print('Telemetry disconnected',flush=True)
                      connected=False
          time.sleep(max(.1,1-(time.monotonic()-start)))
    finally:
        if server is not None:server.close()
if __name__=='__main__':main()
