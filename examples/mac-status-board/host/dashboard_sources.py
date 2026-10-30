"""Read-only dashboard adapters. Never dispatch tasks or alter provider routing."""
import argparse, contextlib, concurrent.futures, datetime, json, math, pathlib, sqlite3, subprocess, sys, time, re
HOME = pathlib.Path.home()
WORKER = HOME / 'Developer/opencode-worker-console/scripts'
STATE = HOME / '.local/state/delegate-opencode'
CODEXBAR = '/Applications/CodexBar.app/Contents/Helpers/CodexBarCLI'
MAX_TASKS = 12
PRIORITY = {'attention': 0, 'running': 1, 'unknown': 2, 'completed': 3}

def epoch(value):
    if isinstance(value, (float,int)) and not isinstance(value,bool): return float(value)
    if isinstance(value,str):
        try:return datetime.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
        except ValueError:pass
    return 0

def bounded(value, maximum):
    return ''.join(c for c in str(value or '') if c >= ' ' and c != '\x7f')[:maximum]

def display_title(value, fallback="未命名任务"):
    text=bounded(value,96)
    if re.search(r"密码|password|api[_ -]?key|bearer |sk-[A-Za-z0-9]|https?://",text,re.I):return fallback
    return text or fallback

def sort_tasks(items):
    items.sort(key=lambda t:(PRIORITY.get(t['status'],2),-t.get('updated_at',0),t['id']))
    return items[:MAX_TASKS]

def classify(status, updated, now, completed=0, active_age=None):
    if status in ('completed','done'):
        return 'completed' if completed and 0 <= now-completed <= 300 else None
    if status in ('cancelled','interrupted','timed_out'):return None
    if status in ('failed','needs_attention','queued','waiting','awaiting_input'):
        return 'attention' if 0 <= now-updated <= 86400 else None
    if status in ('inProgress','running','starting','stopping','retry'):
        if now-updated > 86400:return None
        return 'unknown' if active_age is not None and active_age > 120 else 'running'
    return None

def ro_db(path):
    con=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=1)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    return con

class CodexTasks:
    def __init__(self, root=None):
        self.root=pathlib.Path(root or HOME/'.codex');self.tails={}
    def activity(self, path, now):
        """Incremental event tail, timestamps inside records; never file mtime as status."""
        path=pathlib.Path(path)
        cache=self.tails.setdefault(str(path),{'offset':0,'at':0,'event':None,'carry':b''})
        try:
            with path.open('rb') as f:
                size=f.seek(0,2)
                if size<cache['offset']:cache.update(offset=0,at=0,event=None,carry=b'')
                # Bootstrap from a bounded tail, then read only appended bytes.
                start=max(cache['offset'],size-262144)
                f.seek(start)
                if start>cache['offset']:f.readline();cache['carry']=b''
                raw=cache['carry']+f.read(262144);cache['offset']=f.tell()
            lines=raw.split(b'\n');cache['carry']=lines.pop()
            for line in lines:
                try:d=json.loads(line)
                except (ValueError,UnicodeError):continue
                if d.get('type')=='event_msg':
                    at=epoch(d.get('timestamp'));p=d.get('payload',{})
                    if at>=cache['at']:cache['at']=at
                    if p.get('type') in ('task_started','task_complete','turn_aborted'):
                        cache['event']=p['type']
            return cache
        except OSError:return {'at':0,'event':None}
    def collect(self, now=None):
        now=now or time.time();items=[]
        with contextlib.closing(ro_db(self.root/'state_5.sqlite')) as meta, contextlib.closing(ro_db(self.root/'thread_history_1.sqlite')) as history:
            threads=meta.execute('SELECT id,name,title,cwd,rollout_path,updated_at FROM threads WHERE archived=0 AND updated_at>? ORDER BY updated_at DESC LIMIT 100',(now-86400,)).fetchall()
            for t in threads:
                turn=history.execute('SELECT status,started_at,completed_at FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1',(t['id'],)).fetchone()
                if not turn:continue
                activity=self.activity(t['rollout_path'],now)
                updated=max(t['updated_at'],activity['at'])
                status=turn['status'];completed=turn['completed_at'] or 0
                # An explicit end event wins over a lagging in-progress projection.
                if status=='inProgress' and activity['event']=='task_complete':status='completed';completed=activity['at']
                if status=='inProgress' and activity['event']=='turn_aborted':status='interrupted'
                state=classify(status,updated,now,completed,now-(activity['at'] or turn['started_at'] or 0))
                if state:items.append(dict(id=t['id'],title=display_title(t['name'] or t['title']),project=('桌面信息牌' if pathlib.Path(t['cwd']).name=='https-40le-com-archives-13-html' else bounded(pathlib.Path(t['cwd']).name,36)),status=state,started_at=turn['started_at'] or 0,ended_at=completed,updated_at=updated))
        return dict(state='ok',sampled_at=now,total=len(items),active=sum(t['status']!='completed' for t in items),items=sort_tasks(items))

def task_model(record):
    # History is an unordered accounting set, not the current execution model.
    value=record.get('current_model')
    if not value:
        actual=record.get('actual_models')
        if isinstance(actual,list) and len(actual)==1:value=actual[0]
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_.:/+\-]{1,160}',value):
        return '模型未知'
    return bounded(value.rsplit('/',1)[-1],80) or '模型未知'

def opencode_tasks(now=None,state_dir=None):
    now=now or time.time();items=[];root=pathlib.Path(state_dir or STATE)/'tasks'
    if not root.is_dir():raise FileNotFoundError('Worker task store missing')
    for path in root.glob('*.json'):
        try:t=json.loads(path.read_text())
        except (OSError,ValueError):continue
        updated=epoch(t.get('updated_at'));completed=epoch(t.get('finished_at'))
        observed=epoch(t.get('last_observed_at')) or updated
        status=classify(t.get('status'),updated,now,completed,now-observed)
        if status == "running" and t.get("status") == "running":items.append(dict(id=bounded(t.get('id'),64),title=display_title(t.get('title')),project=bounded(t.get('group_title') or pathlib.Path(t.get('source_dir','')).name,36),status=status,model=task_model(t),started_at=epoch(t.get('started_at')),ended_at=completed,updated_at=updated))
    return dict(state='ok',sampled_at=now,total=len(items),active=sum(t['status']!='completed' for t in items),items=sort_tasks(items))

def valid_number(x):return isinstance(x,(float,int)) and not isinstance(x,bool) and math.isfinite(x)

def window_label(minutes):
    return {300:'5小时',1440:'每日',10080:'每周',43200:'每月'}.get(minutes,'额度')

WINDOW_UNITS={'bailian-token-plan':'credits','aliyun-team':'credits','volcengine-agent-plan':'afp'}

def terminal_exhausted(value,provider):
    """Shared display rule; fail open (keep the row) if the worker module is absent."""
    try:
        roots=[pathlib.Path(__file__).resolve().parents[3]/'scripts',WORKER]
        for root in roots:
            if (root/'allowance_visibility.py').is_file():
                if str(root) not in sys.path:sys.path.insert(0,str(root))
                import allowance_visibility
                return allowance_visibility.terminal_exhausted(value,provider=provider)
    except Exception:pass
    return False

def kimi_window_key(window):
    kind=window.get('kind')
    if kind in ('short','weekly','month_total','month_code'):return kind
    name=window.get('name');minutes=window.get('duration_minutes')
    if name in ('limit5h','limit_5h') or minutes==300:return 'short'
    if name in ('limit7d','limit_7d','overall') or minutes==10080:return 'weekly'
    return 'name:'+str(name) if name else 'duration:'+str(minutes)

def kimi_windows(value):
    """Use only supplied, verified fresh account metadata; never query it here."""
    rows=[dict(w) for w in value.get('windows',[]) if isinstance(w,dict) and w.get('valid') is True
          and valid_number(w.get('remaining_percent')) and 0<=w['remaining_percent']<=100]
    account=value.get('account');now=time.time()
    if isinstance(account,dict):
        stamp=epoch(account.get('sampled_at'));binding=account.get('account_binding')
        if (account.get('state')=='ok' and not account.get('stale') and
                isinstance(binding,dict) and binding.get('state')=='verified' and
                stamp and 0<=now-stamp<=900):
            merged={kimi_window_key(w):w for w in rows}
            for w in account.get('windows',[]):
                if not isinstance(w,dict):continue
                remaining=w.get('remaining_percent');reset=epoch(w.get('resets_at'));expires=epoch(w.get('expires_at'))
                if (w.get('valid') is True and not w.get('expired') and valid_number(remaining) and
                        0<=remaining<=100 and (not reset or reset>now) and (not expires or expires>now)):
                    merged[kimi_window_key(w)]=dict(w)
            rows=list(merged.values())
    keyed={kimi_window_key(w):w for w in rows}
    ordered=[keyed.pop(kind) for kind in ('short','weekly','month_total','month_code') if kind in keyed]
    return (ordered+list(keyed.values()))[:4]

def normalize_worker(provider,value):
    names={'kimi-for-coding':('kimi','Kimi'),'volcengine-agent-plan':('ark','火山方舟'),'deepseek':('deepseek','DeepSeek'),
           'bailian-token-plan':('aliyun','阿里百炼'),'aliyun-team':('aliyun-team','阿里团队'),
           'aliyun-personal':('aliyun-personal','阿里个人')}
    value=value if isinstance(value,dict) else {}
    ident,name=names[provider]
    result=dict(id=ident,name=name,state=value.get('state','unavailable'),sampled_at=epoch(value.get('sampled_at')),windows=[],balances=[])
    validUntil=epoch(value.get('valid_until'))
    if validUntil>result['sampled_at']:result['valid_until']=validUntil
    windows=kimi_windows(value) if provider=='kimi-for-coding' else value.get('windows',[])
    for w in windows[:4]:
        if not isinstance(w,dict):continue
        remaining=w.get('remaining_percent')
        if w.get('valid') and valid_number(remaining) and 0<=remaining<=100:
            label=window_label(w.get('duration_minutes'))
            kind=kimi_window_key(w) if provider=='kimi-for-coding' else w.get('kind')
            if provider=='kimi-for-coding':label={'short':'5小时','weekly':'每周','month_total':'总额度','month_code':'月度Code'}.get(kind,label)
            if provider=='aliyun-personal' or value.get('plan_edition')=='personal':
                minutes=w.get('duration_minutes')
                if (w.get('name')=='Monthly' or kind in ('monthly','month_total') or
                        valid_number(minutes) and minutes>=28*1440):label='每月'
            row=dict(label=label,remaining_percent=round(remaining,3 if provider in ('kimi-for-coding','aliyun-personal') else 1),resets_at=epoch(w.get('resets_at')))
            if provider=='kimi-for-coding':
                row['kind']=kind
                if w.get('expires_at'):row['expires_at']=epoch(w['expires_at'])
            if w.get('name')=='Addon' or w.get('addon') is True:
                row['addon']=True;row['label']='用量包'
                expiry=epoch(w.get('nearest_expires_at') or w.get('expires_at'))
                if expiry:row['expires_at']=expiry
            amount,limit=w.get('remaining'),w.get('limit')
            if valid_number(amount):row['remaining']=round(amount,3)
            if valid_number(limit):row['limit']=round(limit,3)
            own_unit=w.get('unit')
            unit=own_unit if isinstance(own_unit,str) and own_unit else (None if value.get('plan_edition')=='personal' else WINDOW_UNITS.get(provider))
            if unit and (own_unit or 'remaining' in row):row['unit']=bounded(unit,16)
            result['windows'].append(row)
    addon=value.get('addon') if isinstance(value.get('addon'),dict) else None
    if addon is not None and (provider=='aliyun-personal' or value.get('plan_edition')=='personal'):
        # Display-only bundle statistics; unknown stays unknown, known-exhausted hides.
        known=addon.get('known') is True
        # Expose incomplete bundle telemetry on the existing firmware's status
        # line while preserving known monthly values and real availability.
        if not known or addon.get('inconsistent') is True:result['state']='unknown'
        remaining,limit=addon.get('remaining'),addon.get('limit')
        percent=addon.get('remaining_percent')
        visible=(known and valid_number(remaining) and remaining>0 and
                 valid_number(percent) and 0<=percent<=100)
        projected=any(r.get('addon') is True for r in result['windows'])
        if visible and not projected and len(result['windows'])<4:
            row=dict(label='用量包',remaining_percent=round(percent,3),resets_at=0,addon=True,
                     remaining=round(remaining,3))
            if valid_number(limit) and limit>0:
                row['limit']=round(limit,3);row['unit']='credits'
            expiry=epoch(addon.get('nearest_expires_at'))
            if expiry:row['expires_at']=expiry
            result['windows'].append(row)
        meta={k:addon[k] for k in ('known','remaining','limit','used','active_count','remaining_percent')
              if k in addon}
        expiry=epoch(addon.get('nearest_expires_at'))
        if expiry:meta['nearest_expires_at']=expiry
        if addon.get('inconsistent') is True:meta['inconsistent']=True
        result['addon']=meta
    for b in value.get('balances',[])[:2]:
        if valid_number(b.get('remaining')):result['balances'].append(dict(currency=bounded(b.get('currency'),8),remaining=b['remaining']))
    if 'available' in value:
        # Unknown availability stays an explicit null so it never reads as healthy.
        result['available']=value['available'] if isinstance(value['available'],bool) else None
    if value.get('stale') is True or isinstance(value.get('live_error'),dict):result['stale']=True
    if value.get('source') in ('openapi_live','personal_console'):result['source']=value['source']
    return result

def worker_rows(provider,value):
    """Fresh sequential fetches contain independent products, never one balance."""
    if provider=='bailian-token-plan' and isinstance(value,dict) and value.get('plan_edition')=='sequential':
        plans=value.get('plan_quotas') or {};rows=[]
        for edition in ('team','personal'):
            plan=plans.get(edition) if isinstance(plans,dict) else None
            plan=dict(plan) if isinstance(plan,dict) else {'state':'unknown','available':None}
            plan['plan_edition']=edition
            if terminal_exhausted(plan,'aliyun-'+edition):continue
            row=normalize_worker('aliyun-'+edition,plan)
            row['plan_edition']='sequential'
            if value.get('active_plan') in ('team','personal'):
                row['plan_slot']='active' if value['active_plan']==edition else 'standby'
            rows.append(row)
        return rows
    if terminal_exhausted(value,provider):return []
    return [normalize_worker(provider,value)]

def normalize_codex(data,now=None):
    now=now or time.time()
    row=next((v for v in data if v.get('provider')=='codex'),{}) if isinstance(data,list) else data
    usage=row.get('usage') or {};result=dict(id='codex',name='Codex',state='unavailable',sampled_at=0,windows=[],balances=[])
    if row.get('error'):return result
    for key in ('primary','secondary','tertiary'):
        w=usage.get(key) or {};used=w.get('usedPercent')
        if valid_number(used) and 0<=used<=100:
            minutes=w.get('windowMinutes') or w.get('windowDurationMins')
            result['windows'].append(dict(label=window_label(minutes),remaining_percent=round(100-used,1),resets_at=epoch(w.get('resetsAt'))))
    if result['windows']:result.update(state='ok',sampled_at=epoch(usage.get('updatedAt')) or now)
    return result

def codex_quota():
    # OAuth reader avoids the installed CLI adapter's obsolete `untrusted` approval flag.
    result=subprocess.run([CODEXBAR,'usage','--provider','codex','--source','oauth','--format','json','--no-credits'],capture_output=True,text=True,timeout=35)
    return normalize_codex(json.loads(result.stdout))

def worker_quota(provider):
    sys.path.insert(0,str(WORKER));import quota
    # fetch_one is account telemetry only; refresh() also changes routing circuits and is intentionally not used.
    rows=worker_rows(provider,quota.fetch_one(provider))
    return rows if len(rows)>1 else rows[0]

def quota_snapshot():
    out=[]
    jobs=[('codex',codex_quota,None),('kimi',worker_quota,'kimi-for-coding'),('ark',worker_quota,'volcengine-agent-plan'),
          ('deepseek',worker_quota,'deepseek'),('aliyun',worker_quota,'bailian-token-plan')]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures=[(name,pool.submit(fn,*([arg] if arg else []))) for name,fn,arg in jobs]
        for name,future in futures:
            try:
                result=future.result()
                out.extend(result if isinstance(result,list) else [result])
            except Exception:out.append(dict(id=name,name={'ark':'火山方舟','kimi':'Kimi','codex':'Codex','deepseek':'DeepSeek','aliyun':'阿里百炼'}[name],state='unavailable',sampled_at=0,windows=[],balances=[]))
    return out

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--quotas',action='store_true');a=p.parse_args()
    if a.quotas:print(json.dumps(quota_snapshot(),ensure_ascii=False))
    else:print(json.dumps({'codex':CodexTasks().collect(),'opencode':opencode_tasks()},ensure_ascii=False))
