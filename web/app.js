const $ = (id) => document.getElementById(id);
const esc = (x) => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const I18n = window.I18n;
const tr = (key, vars) => (I18n ? I18n.t(key, vars) : key);
const fixed2 = (value) => (I18n ? I18n.number(Number(value), {minimumFractionDigits:2, maximumFractionDigits:2}) : Number(value).toFixed(2));
function profileMeta(id) { if(id==='fallback')return ['DeepSeek Flash',tr('profile.fallback'),'fast']; if(id==='fast-code')return ['DeepSeek Flash',tr('profile.fallback'),'fast']; if(id==='senior-code')return ['Kimi K2.8',tr('profile.generic'),'senior']; if(id==='deep-research')return ['Kimi K3',tr('profile.deep'),'deep']; return null; }
function taskTierMeta(task) { const tier=task.tier||(task.complexity==='deep'?'deep':task.urgency==='fast'?'fast':null); if(tier==='deep')return [tr('profile.deep'),'deep']; if(tier==='fast')return [tr('profile.fast.generic'),'fast']; if(tier==='normal')return [tr('profile.generic'),'senior']; return null; }
const activeStates = ['starting','running','stopping','uncertain'];
const attentionStates = ['failed','needs_attention','timed_out','uncertain'];
const terminalStates = new Set(['completed','failed','cancelled','timed_out','needs_attention']);
const externalIcon = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6 3H3v10h10v-3M9 3h4v4M7 9l6-6"/></svg>';
const folderIcon = '<svg viewBox="0 0 18 18" aria-hidden="true"><path d="M2 5h5l2 2h7v8H2zM2 5V3h5l2 2h7v2"/></svg>';
let data = {tasks:[],quota:{}}, selectedGroup = '', selectedFilter = 'all', selectedTask = null, loading = false;
let selectedTaskIds = new Set(), visibleSelectableIds = [];
function setHTML(el, value) { const changed=el.innerHTML !== value; if (changed) el.innerHTML = value; return changed; }
function reducedMotion(){return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches===true;}
function clock(seconds) { return seconds ? (I18n ? I18n.time(seconds,{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'}) : new Date(seconds * 1000).toLocaleTimeString('en-GB',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'})) : '—'; }
function resetDate(value) { if (!value) return null; const d=new Date(typeof value==='number'?(value<1e12?value*1000:value):value);return Number.isNaN(d.getTime())?null:d; }
function resetCountdown(d){const mins=Math.max(0,Math.ceil((d.getTime()-Date.now())/60000));return mins>=60?tr('quota.resetsInHours',{h:Math.floor(mins/60),m:mins%60}):tr('quota.resetsInMinutes',{m:mins});}
function estimatedRange(w){const e=w?.consumption_estimate||{},hours=e.hours==null?NaN:Number(e.hours),reset=resetDate(w?.resets_at);if(e.idle)return tr('quota.rangePaused');if(!Number.isFinite(hours))return tr('quota.rangeCollecting');if(reset&&hours>=(reset.getTime()-Date.now())/3600000)return tr('quota.rangeToReset');if(hours>=48)return tr('quota.rangeDays',{d:I18n?I18n.number(Math.round(hours/24)):Math.round(hours/24)});return tr('quota.rangeHours',{h:I18n?I18n.number(Math.max(1,Math.round(hours))):Math.max(1,Math.round(hours))});}
function estimatedBalanceRange(q){const rows=(q?.balances||[]).map(b=>b.consumption_estimate).filter(Boolean);if(!rows.length)return tr('quota.rangeCollecting');const active=rows.filter(e=>!e.idle&&Number.isFinite(Number(e.hours)));if(active.length){const hours=Math.min(...active.map(e=>Number(e.hours)));if(hours>=48)return tr('quota.rangeDays',{d:I18n?I18n.number(Math.round(hours/24)):Math.round(hours/24)});return tr('quota.rangeHours',{h:I18n?I18n.number(Math.max(1,Math.round(hours))):Math.max(1,Math.round(hours))});}return rows.some(e=>e.idle)?tr('quota.rangePaused'):tr('quota.rangeCollecting');}
function resetTime(value) { const d=resetDate(value);if(!d)return tr('reset.unknown');const opts={month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false};const beijing=I18n?I18n.date(d,{...opts,timeZone:'Asia/Shanghai'}):d.toLocaleString('zh-CN',{...opts,timeZone:'Asia/Shanghai'});const zone=Intl.DateTimeFormat().resolvedOptions().timeZone||'Local';const local=I18n?I18n.date(d,opts):d.toLocaleString();return zone==='Asia/Shanghai'?tr('quota.resetBeijing',{time:beijing,left:resetCountdown(d)}):tr('quota.resetDual',{beijing,local,zone,left:resetCountdown(d)}); }
function windowRunway(w){const p=Number(w?.remaining_percent),d=resetDate(w?.resets_at),duration=Number(w?.duration_minutes),estimate=w?.consumption_estimate||{};if(!Number.isFinite(p)||!d||d.getTime()<=Date.now())return null;const resetHours=(d.getTime()-Date.now())/3600000,estimatedHours=estimate.hours==null?NaN:Number(estimate.hours);if(estimate.idle)return 4;if(Number.isFinite(estimatedHours)&&resetHours>0)return Math.max(0,Math.min(4,estimatedHours/resetHours));if(!Number.isFinite(duration)||duration<=0)return null;const fraction=Math.max(0,Math.min(1,(d.getTime()-Date.now())/(duration*60000)));return fraction>0?Math.max(0,Math.min(4,p/100/fraction)):null;}
function expectedRemaining(w){const d=resetDate(w?.resets_at),duration=Number(w?.duration_minutes);if(!d||!Number.isFinite(duration)||duration<=0)return null;return Math.max(0,Math.min(100,(d.getTime()-Date.now())/(duration*60000)*100));}
function quotaPace(w){const p=Number(w?.remaining_percent),runway=windowRunway(w),threshold=(data.tier_guidance?.runway_threshold_percent??75)/100;if(Number.isFinite(p)&&p<=0)return ['exhausted',tr('quota.paceExhausted')];if(runway==null)return ['unknown',tr('quota.paceUnknown')];if(runway<.35)return ['critical',tr('quota.paceCritical')];if(runway<threshold)return ['tight',tr('quota.paceTight')];if(runway<1.15)return ['on-track',tr('quota.paceOnTrack')];return ['healthy',tr('quota.paceHealthy')];}
function quotaTrack(p,label){const value=Number.isFinite(Number(p))?Math.max(0,Math.min(100,Number(p))):0;return `<svg class="quota-track" viewBox="0 0 100 8" role="img" aria-label="${esc(label)}"><rect class="quota-track-bg" x="0" y="1" width="100" height="6" rx="3"/><rect class="quota-track-fill" x="0" y="1" width="${value}" height="6" rx="3"/></svg>`;}
const PoolModel = {
  number(value){const n=Number(value);return typeof value==='number'&&Number.isFinite(n)&&n>=0?n:null;},
  component(raw,key,label){
    raw=raw&&typeof raw==='object'?raw:{};
    const amount=this.number(raw.amount);
    const known=amount!==null&&['ok','stale','unavailable'].includes(raw.status);
    return {key,label,known,amount:known?amount:0,sourceAmount:this.number(raw.source_amount),
      unit:raw.unit||'',currency:raw.currency||'',status:known?raw.status:(raw.status==='unknown'?'unknown':'missing'),
      stale:raw.stale===true,resetsAt:raw.resets_at||null,excluded:Array.isArray(raw.excluded_currencies)?raw.excluded_currencies:[]};
  },
  view(source){
    source=source&&typeof source==='object'?source:{};
    const balance=this.component(source.components?.balance,'balance',tr('quota.poolBalance'));
    const plan=this.component(source.components?.plan,'plan',tr('quota.poolPlan'));
    const components=[balance,plan];
    const known=components.filter(x=>x.known);
    const total=known.reduce((sum,x)=>sum+x.amount,0);
    const stale=known.some(x=>x.stale);
    const blocked=known.some(x=>x.status==='unavailable');
    const allZero=known.length&&known.every(x=>x.amount===0);
    const state=!known.length?'unknown':allZero?'unavailable':stale||blocked||known.length<components.length?'partial':'ready';
    return {balance,plan,components,known,total,unit:source.unit||'AFP-equivalent',state,stale,
      complete:source.complete===true&&known.length===components.length};
  }
};
function formatPoolTotal(value){return number(Math.round(value));}
function animatePoolValue(el,to){
  const from=Number(el.dataset.value);
  el.dataset.value=String(to);
  if(!Number.isFinite(from)||from===to||reducedMotion()||!window.requestAnimationFrame){el.textContent=formatPoolTotal(to);return;}
  const start=performance.now(),duration=520;
  cancelAnimationFrame(el._poolFrame);
  const step=now=>{const p=Math.min(1,(now-start)/duration),ease=1-Math.pow(1-p,3);el.textContent=formatPoolTotal(from+(to-from)*ease);if(p<1)el._poolFrame=requestAnimationFrame(step);};
  el._poolFrame=requestAnimationFrame(step);
}
function poolComponentValue(x){
  if(!x.known)return '—';
  if(x.key==='balance')return (x.currency==='CNY'?'¥':'')+(I18n?I18n.number(x.sourceAmount,{minimumFractionDigits:2,maximumFractionDigits:2}):x.sourceAmount.toFixed(2));
  return formatPoolTotal(x.sourceAmount??x.amount)+' AFP';
}
function poolComponentDetail(x){
  if(!x.known)return tr(x.status==='unknown'?'quota.poolUnknown':'quota.poolUnavailable');
  const prefix=x.key==='balance'?'≈ ':'';
  return prefix+formatPoolTotal(x.amount)+' '+x.unit+(x.stale?' · '+tr('quota.state.stale'):'');
}
function poolStateText(model){
  if(!model.known.length)return tr('quota.poolUnknown');
  if(model.state==='unavailable')return tr('quota.poolUnavailable');
  if(model.state==='stale')return tr('quota.poolStale');
  if(model.state==='partial')return tr('quota.poolPartial');
  return tr('quota.poolReady');
}
function renderPoolBar(model){
  const el=$('pool-bar');if(!el)return;
  const desired=model.known.filter(x=>x.amount>0).map(x=>({key:x.key,share:x.amount/model.total*100}));
  const current=[...el.children].filter(node=>node.dataset.segment);
  if(current.length===desired.length&&current.every((node,i)=>node.dataset.segment===desired[i].key)){
    desired.forEach((x,i)=>current[i].style.width=x.share.toFixed(3)+'%');
    return;
  }
  el.setAttribute('role','img');el.setAttribute('aria-label',tr('quota.poolBarAria',{total:formatPoolTotal(model.total),unit:model.unit}));
  el.innerHTML=desired.map((x,i)=>`<span class="work-pool-segment ${x.key}" data-segment="${x.key}" style="width:${x.share.toFixed(3)}%;animation-delay:${i*70}ms"></span>`).join('');
}
function renderPool(providers){
  const block=document.querySelector('.pool-block');
  const relevant=providers.has('deepseek')||providers.has('volcengine-agent-plan');
  block.hidden=!relevant;
  if(!relevant)return;
  const model=PoolModel.view(data.economics?.work_pool);
  const previousState=block.dataset.poolState;
  block.dataset.poolState=model.state;
  const state=$('pool-state');
  state.textContent=poolStateText(model);
  state.className='quota-state '+(model.state==='ready'?'healthy':model.state==='unavailable'?'critical':model.state==='unknown'?'':'tight');
  if(previousState&&previousState!==model.state&&!reducedMotion()){block.classList.remove('pool-state-changed');void block.offsetWidth;block.classList.add('pool-state-changed');setTimeout(()=>block.classList.remove('pool-state-changed'),650);}
  setHTML($('pool-summary'),`<div class="pool-total"><strong id="pool-total-value">0</strong><span>${esc(tr('quota.poolTotalUnit',{unit:model.unit}))}</span></div>`);
  animatePoolValue($('pool-total-value'),model.total);
  renderPoolBar(model);
  setHTML($('pool-components'),model.components.map(x=>`<div class="pool-component ${x.key}"><i class="component-dot ${x.key}" aria-hidden="true"></i><span class="component-copy"><strong>${esc(x.label)}</strong><small>${esc(poolComponentValue(x))}</small></span><span class="component-normalized">${esc(poolComponentDetail(x))}</span></div>`).join(''));
  const notes=[];
  if(model.balance.excluded.length)notes.push(tr('quota.poolExcludedCurrency',{currencies:model.balance.excluded.join(', ')}));
  if(providers.has('kimi-for-coding'))notes.push(tr('quota.poolKimiSeparate'));
  notes.push(tr('quota.poolNormalization'));
  $('pool-next').textContent=notes.join(' · ');
}
function duration(item) { if (!item.started_at) return tr('duration.waiting'); const s = Math.max(0, Math.round((item.finished_at || Date.now()/1000)-item.started_at)); if(s<60)return tr('duration.seconds',{n:s}); if(s<3600)return tr('duration.minutes',{m:Math.floor(s/60),s:s%60}); return tr('duration.hours',{h:Math.floor(s/3600),m:Math.floor(s%3600/60)}); }
let toastTimer=null;
function showToast(title,message,tone='success',sticky=false){const toast=$('toast');if(!toast)return;clearTimeout(toastTimer);$('toast-title').textContent=title;$('toast-message').textContent=message||'';toast.className='toast '+tone;toast.hidden=false;if(!sticky)toastTimer=setTimeout(()=>{toast.hidden=true;},9000);}
function taskCleanupResult(result){const bytes=Number(result?.freed_bytes)||0,size=bytes>=2**30?fixed2(bytes/2**30)+' GiB':fixed2(bytes/2**20)+' MiB';const message=tr('tasks.cleanupResult',{deleted:result?.deleted||0,sessions:result?.sessions_deleted||0,worktrees:result?.worktrees_released||0,retained:result?.retained_for_review?.length||0,size});$('task-cleanup-message').textContent=message;showToast(tr('tasks.cleanupDone'),message,(result?.errors||[]).length?'warning':'success');}
function status(item) { const key='status.'+item.status; const text=I18n&&I18n.has(key)?tr(key):item.status; return `<span class="status ${esc(item.status)}"><i></i>${esc(text)}</span>`; }
function grouped() { const m=new Map(); for(const t of data.tasks){if(!m.has(t.group_id))m.set(t.group_id,{name:t.group_title,tasks:[]});m.get(t.group_id).tasks.push(t);} return m; }
function renderGroups() { let html=`<button class="nav-button ${!selectedGroup?'selected':''}" data-group="" aria-pressed="${!selectedGroup}">${folderIcon}<span class="nav-name">${tr('nav.allTasks')}</span><span class="nav-count">${data.tasks.length}</span></button>`;
 for(const [id,g] of grouped())html+=`<button class="nav-button ${id===selectedGroup?'selected':''}" data-group="${esc(id)}" aria-pressed="${id===selectedGroup}">${folderIcon}<span class="nav-name" title="${esc(g.name)}">${esc(g.name)}</span><span class="nav-count">${g.tasks.length}</span></button>`;setHTML($('groups'),html); }
function windowName(w){if(w.name==='overall')return tr('quota.weekly');if(w.name==='AFPFiveHour')return tr('quota.fiveHour');if(w.name==='AFPDaily')return tr('quota.daily');if(w.name==='AFPWeekly')return tr('quota.weekly');if(w.name==='AFPMonthly')return tr('quota.monthly');return w.duration_minutes?tr('quota.hoursWindow',{h:w.duration_minutes/60}):tr('quota.limitWindow');}
function quotaWindows(windows,showValues=false){return windows.length?windows.map(w=>{const p=w.remaining_percent,name=windowName(w),runway=windowRunway(w),expected=expectedRemaining(w),pace=quotaPace(w),display=p==null?'—':(I18n?I18n.number(Math.round(p)):Math.round(p))+'%';const delta=Number.isFinite(Number(p))&&Number.isFinite(expected)?Number(p)-expected:null;const comparison=delta==null?tr('quota.expectedRemaining'):tr(delta>=0?'quota.paceAhead':'quota.paceBehind',{value:I18n?I18n.number(Math.abs(Math.round(delta))):Math.abs(Math.round(delta))});return `<article class="quota-window ${pace[0]}"><div class="window-heading"><span>${esc(name)}</span><strong>${esc(display)}</strong></div>${quotaTrack(p,tr('quota.remainingAria',{name}))}<div class="quota-window-status"><strong class="quota-pace-label">${esc(pace[1])}</strong><span>${esc(estimatedRange(w))}</span></div><div class="quota-window-reset">${esc(resetTime(w.resets_at))}</div><details class="window-details"><summary>${esc(tr('table.details'))}</summary><div>${esc(comparison)}${runway==null?'':` · ${esc(fixed2(runway))}×`}${showValues&&w.remaining!=null?` · ${esc(number(Math.round(w.remaining)))} / ${esc(number(Math.round(w.limit)))} AFP`:''}</div></details></article>`}).join(''):'<p class="muted">'+tr('quota.unknown')+'</p>';}
function renderQuota(){const providers=new Set(Object.values(data.profiles||{}).map(p=>p.model.split('/')[0]));const hasArk=providers.has('volcengine-agent-plan');document.querySelector('.deepseek-block').hidden=!providers.has('deepseek');document.querySelector('.kimi-block').hidden=!providers.has('kimi-for-coding');document.querySelector('.ark-block').hidden=!hasArk;document.querySelector('.quota-section').hidden=!providers.has('deepseek')&&!providers.has('kimi-for-coding')&&!hasArk;const ds=data.quota.deepseek||{}, k=data.quota['kimi-for-coding']||{},ark=data.quota['volcengine-agent-plan']||{};
 renderPool(providers);
 $('ds-balance').textContent=ds.balances?.length?ds.balances.map(b=>(b.currency==='CNY'?'¥':b.currency==='USD'?'$':b.currency+' ')+fixed2(b.remaining)).join(' / '):'—';
 $('ds-range').textContent=estimatedBalanceRange(ds);
 $('ds-state').textContent=ds.state==='auth_error'?tr('quota.state.authError'):ds.available===false?tr('quota.state.unavailable'):ds.stale?tr('quota.state.stale'):ds.available===true?tr('quota.state.available'):tr('quota.state.unknown');
 $('ds-time').textContent=ds.sampled_at?tr('quota.sampledAt',{time:clock(ds.sampled_at)})+(ds.stale?tr('quota.lastSuccess'):''):tr('quota.noSample');
 const windows=k.windows||[];setHTML($('kimi-windows'),quotaWindows(windows));
 const monthlyExhausted=k.billing?.reason==='monthly_usage_limit';const endpointEmpty=k.billing?.telemetry_available===false||(!k.billing&&k.available===false)||windows.some(w=>w.valid!==false&&w.remaining===0);
 $('kimi-time').textContent=tr('quota.kimiShared')+(k.sampled_at?tr('quota.sampleSuffix',{time:clock(k.sampled_at)}):'')+(k.stale?tr('quota.staleSuffix'):'')+(endpointEmpty?' · '+tr('quota.state.unavailable'):monthlyExhausted?' · '+tr('quota.monthlyExhausted'):k.state==='auth_error'?tr('quota.authErrorSuffix'):k.available===false?' · '+tr('quota.state.unavailable'):'');
 $('kimi-time').classList.toggle('danger-text',monthlyExhausted);
 const reset=k.monthly_reset;const next=$('kimi-monthly-next');next.hidden=!(reset?.enabled&&reset.next_reset_at);next.textContent=next.hidden?'':tr('quota.nextMonthlyReset',{time:resetTime(reset.next_reset_at),zone:reset.timezone});
 setHTML($('ark-windows'),quotaWindows(ark.windows||[],true));$('ark-state').textContent=ark.state==='no_credential'?tr('quota.state.notConfigured'):ark.state==='auth_error'?tr('quota.state.authError'):ark.available===false?tr('quota.state.unavailable'):ark.stale?tr('quota.state.stale'):ark.available===true?tr('quota.state.available'):tr('quota.state.unknown');$('ark-time').textContent=(ark.plan?.type?ark.plan.type+' · ':'')+(ark.sampled_at?tr('quota.sampledAt',{time:clock(ark.sampled_at)}):tr('quota.noSample'));
}
function ordered(items){const ids=new Set(items.map(t=>t.id)),out=[],seen=new Set();function visit(t,depth){if(seen.has(t.id))return;seen.add(t.id);out.push([t,depth]);items.filter(x=>x.parent_task_id===t.id).forEach(x=>visit(x,depth+1));}items.filter(t=>!ids.has(t.parent_task_id)).forEach(t=>visit(t,0));items.forEach(t=>visit(t,0));return out;}
const detailPanel = $('detail');
const inlineDetailRow = document.createElement('tr');
inlineDetailRow.className = 'task-detail-row';
const inlineDetailCell = document.createElement('td');
inlineDetailCell.colSpan = 7;
inlineDetailRow.append(inlineDetailCell);
const taskRows = new Map();
const number = value => I18n ? I18n.number(value) : Number(value).toLocaleString();
const detailController = TaskView.createController({
 request,
 visible: () => !document.hidden && !$('view-tasks').hidden,
 changed: state => { selectedTask = state.selected; renderTasks(); renderInlineDetail(state); }
});
function renderTasks(){
 const q=$('search').value.trim().toLowerCase();
 let list=data.tasks.filter(t=>!selectedGroup||t.group_id===selectedGroup);
 const total=list.length;
 $('tasks-heading').textContent=selectedGroup?(grouped().get(selectedGroup)?.name||tr('common.primaryTask')):tr('nav.allTasks');
 $('counts').textContent=tr('counts.summary',{active:list.filter(t=>activeStates.includes(t.status)).length,queued:list.filter(t=>t.status==='queued').length});
 list=list.filter(t=>(selectedFilter==='all'||selectedFilter==='active'&&activeStates.includes(t.status)||selectedFilter==='attention'&&attentionStates.includes(t.status)||selectedFilter==='completed'&&t.status==='completed')&&(!q||[t.title,t.profile,data.profiles?.[t.profile]?.label,data.profiles?.[t.profile]?.model,t.group_title,t.id].join(' ').toLowerCase().includes(q)));
 selectedTaskIds=new Set([...selectedTaskIds].filter(id=>data.tasks.some(t=>t.id===id&&terminalStates.has(t.status))));
 visibleSelectableIds=list.filter(t=>terminalStates.has(t.status)).map(t=>t.id);
 if(selectedTask&&!list.some(t=>t.id===selectedTask)){detailController.close();return;}
 const overrides=new Map();
 if(selectedTask&&detailController.state().detail)overrides.set(selectedTask,detailController.state().detail);
 const usage=TaskView.aggregate(list,overrides);
 $('usage-summary').textContent=tr('usage.filtered',{total:usage.total==null?'—':number(usage.total),known:usage.known,count:usage.count});
 const rows=[];
 for(const [t,depth] of ordered(list)){
  const p=[...(profileMeta(t.profile)||(t.profile?[t.actual_models?.join(', ')||t.profile,tr('profile.generic'),'senior']:[tr('profile.awaiting'),tr('profile.auto'),'senior']))];
  if(data.profiles?.[t.profile]?.label)p[0]=data.profiles[t.profile].label;
  const tier=taskTierMeta(t);if(tier){p[1]=tier[0];p[2]=tier[1];}
  const expanded=selectedTask===t.id;
  const usage=overrides.get(t.id)?.usage||TaskView.usageOf(t);
  let row=taskRows.get(t.id);
  if(!row){row=document.createElement('tr');taskRows.set(t.id,row);}
  row.className='task-row'+(expanded?' expanded':'');
  const focus=document.activeElement;
  const focusTarget=row.contains(focus)?focus.getAttribute('data-focus'):null;
  const toggle=`data-detail="${esc(t.id)}" aria-expanded="${expanded}"${expanded?' aria-controls="detail"':''}`;
  const canDelete=terminalStates.has(t.status);
  setHTML(row,`<td class="task-select-cell"><input type="checkbox" data-task-select="${esc(t.id)}" aria-label="${esc(tr('tasks.selectTask',{title:t.title}))}" ${selectedTaskIds.has(t.id)?'checked':''} ${canDelete?'':'disabled'}></td><td><button class="task-title task-toggle" data-focus="title" ${toggle}>${depth?'<span class="parent-indicator">└</span>':''}${esc(t.title)}</button><div class="task-meta">${esc(t.group_title)}${t.parent_task_id?tr('common.subtask'):''} · ${t.mode==='write'?tr('mode.write'):tr('mode.read')}${t.workspace==='isolated'?tr('mode.isolated'):''}</div></td><td><div class="worker-name"><i class="model-dot ${p[2]}"></i><div>${esc(p[0])}<small>${esc(p[1])}</small></div></div></td><td>${status(t)}</td><td class="duration">${duration(t)}</td><td class="task-token-cell">${TaskView.numeric(usage.total)?esc(number(usage.total)):'—'}</td><td><div class="task-row-actions">${t.session_url?`<a class="native-task-link" data-focus="native" href="${esc(t.session_url)}" target="_blank" rel="noopener" aria-label="${esc(tr('detail.openTask'))}" title="${esc(tr('detail.openTask'))}">${externalIcon}</a>`:''}${canDelete?`<button class="task-delete-button" data-delete-task="${esc(t.id)}" aria-label="${esc(tr('tasks.deleteOne',{title:t.title}))}" title="${esc(tr('tasks.deleteOne',{title:t.title}))}">×</button>`:''}<button class="detail-button" data-focus="toggle" ${toggle} aria-label="${esc(tr(expanded?'detail.close':'task.viewDetails',{title:t.title}))}"><svg viewBox="0 0 18 18" aria-hidden="true"><path d="m7 4 5 5-5 5"/></svg></button></div></td>`);
  if(focusTarget&&!row.contains(document.activeElement))row.querySelector('[data-focus="'+focusTarget+'"]').focus({preventScroll:true});
  rows.push(row);
  if(expanded){inlineDetailCell.append(detailPanel);rows.push(inlineDetailRow);}
 }
 const body=$('task-rows');
 const wanted=new Set(rows);
 for(const node of Array.from(body.children))if(!wanted.has(node))node.remove();
 let cursor=body.firstElementChild;
 for(const row of rows){if(row!==cursor)body.insertBefore(row,cursor);cursor=row.nextElementSibling;}
 if(!rows.length)setHTML(body,`<tr><td colspan="7" class="empty">${data.tasks.length?tr('tasks.emptyFiltered'):tr('tasks.emptyNone')}</td></tr>`);
 for(const id of taskRows.keys())if(!data.tasks.some(t=>t.id===id))taskRows.delete(id);
 detailPanel.hidden=!selectedTask;
 if(!selectedTask&&detailPanel.parentNode!==$('view-tasks'))$('view-tasks').append(detailPanel);
 $('visible-count').textContent=tr('tasks.visibleCount',{visible:list.length,total});
 const selectAll=$('select-visible-tasks');
 const selectedVisible=visibleSelectableIds.filter(id=>selectedTaskIds.has(id)).length;
 selectAll.checked=visibleSelectableIds.length>0&&selectedVisible===visibleSelectableIds.length;
 selectAll.indeterminate=selectedVisible>0&&selectedVisible<visibleSelectableIds.length;
 selectAll.disabled=!visibleSelectableIds.length;
 $('delete-selected-tasks').disabled=!selectedTaskIds.size;
 $('clear-completed-tasks').disabled=!data.tasks.some(t=>terminalStates.has(t.status));
 $('selected-task-count').textContent=selectedTaskIds.size?tr('tasks.selected',{n:selectedTaskIds.size}):tr('tasks.selectedNone');
}
async function request(path,options){const response=await fetch(path,options);if(response.status===401){window.location.replace('/console-login');throw new Error(tr('auth.expired'));}if(!response.ok){let body={};try{body=await response.json();}catch{}throw new Error(body.error||tr('error.unavailable'));}return response.json();}
function showError(message){$('error').textContent=message;$('error').hidden=false;}
window.addEventListener?.('error',event=>showError(tr('tasks.partialError',{message:event.message||tr('error.unavailable')})));
window.addEventListener?.('unhandledrejection',event=>showError(tr('tasks.partialError',{message:event.reason?.message||String(event.reason||tr('error.unavailable'))})));
async function refresh(){if(loading)return;loading=true;try{data=await request('/console-api/state');const errors=[];for(const [name,fn] of [['tasks',renderTasks],['groups',renderGroups],['quota',renderQuota]]){try{fn();}catch(error){errors.push(name+': '+(error.message||String(error)));if(name==='tasks')setHTML($('task-rows'),`<tr><td colspan="7" class="empty">${esc(tr('tasks.renderError',{message:error.message||String(error)}))}</td></tr>`);}}$('health').classList.toggle('offline',!data.pool_healthy);$('health').innerHTML=`<i></i>${data.pool_healthy?tr('health.online'):tr('health.offline')}`;$('updated').textContent=tr('updated.at',{time:clock(data.updated_at)});$('pool-limit').textContent=tr('pool.limit',{n:data.max_parallel_per_owner});if(errors.length)showError(tr('tasks.partialError',{message:errors.join(' · ')}));else $('error').hidden=true;}catch(e){showError(e.message);$('health').classList.add('offline');$('health').innerHTML='<i></i>'+tr('health.disconnected');setHTML($('task-rows'),`<tr><td colspan="7" class="empty">${esc(tr('tasks.renderError',{message:e.message}))}</td></tr>`);}finally{loading=false;detailController.refresh();}}
function listHTML(items,empty){return items?.length?'<ul>'+items.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':'<p class="muted">'+empty+'</p>';}
function renderInlineDetail(state){
 if(!state.selected)return;
 $('close-detail').textContent=tr('detail.close');
 $('refresh-detail').textContent=tr('activity.refresh');
 const d=state.detail;
 $('detail-title').textContent=d?.task?.title||data.tasks.find(t=>t.id===state.selected)?.title||tr('detail.title');
 const feedback=$('detail-feedback');
 feedback.hidden=!state.error&&(!state.pending||!!d);
 $('refresh-detail').disabled=state.pending;
 feedback.textContent=state.error?tr('activity.refreshError',{message:state.error}):tr('activity.refreshing');
 if(!d){delete $('detail-body').dataset.rendered;setHTML($('detail-body'),`<p class="muted">${esc(state.error?tr('activity.retryHint'):tr('detail.loading'))}</p>`);return;}
 const t=d.task,r=d.report||{};
 const options={tr,esc,number,clock};
 const actions=`<div class="task-detail-actions">${['running','uncertain'].includes(t.status)?`<button class="primary-button" data-steer-task="${esc(t.id)}">${tr('detail.steer')}</button>`:''}${['queued',...activeStates].includes(t.status)?`<button class="secondary-button" data-cancel-task="${esc(t.id)}">${tr('detail.stop')}</button>`:''}${d.session_url?`<a class="detail-link" href="${esc(d.session_url)}" target="_blank" rel="noopener">${tr('detail.openTask')}</a>`:''}</div>`;
 const body=$('detail-body');
 const selection=window.getSelection?.();
 if(selection&&!selection.isCollapsed&&body.contains(selection.anchorNode))return;
 const sameTask=body.dataset.task===state.selected;
 const oldActivity=body.querySelector('.activity-scroll');
 const scroll=sameTask?(oldActivity?.scrollTop||0):0;
 const followNewest=!sameTask||!oldActivity||scroll<24;
 body.dataset.task=state.selected;
 const focusedSection=document.activeElement?.closest('details')?.dataset.section;
 const activityFocused=document.activeElement===body.querySelector('.activity-scroll');
 const sections=new Set(sameTask?Array.from(body.querySelectorAll('details[open]')).map(el=>el.dataset.section):[]);
 const html=`<div class="task-current-status">${status(t)}<span class="muted">${esc(t.actual_models?.join(', ')||data.profiles?.[t.profile]?.label||t.profile||'')}</span><span class="muted">${esc(t.fallback_used?tr('task.fallbackUsed'):(t.routing_notice||t.reason||t.queue_reason||t.group_title||''))}</span></div>${actions}<div class="task-monitor">${TaskView.activityHTML(d.activity,options)}${TaskView.usageHTML(d.usage,options)}</div><details class="task-evidence" data-section="brief"><summary>${tr('detail.objective')} · ${tr('detail.acceptance')}</summary><h3>${tr('detail.objective')}</h3><p>${esc(d.objective)}</p>${t.targets?.length?`<h3>${tr('field.targets')}</h3>${listHTML(t.targets,'')}`:''}<h3>${tr('detail.acceptance')}</h3>${listHTML(d.acceptance,tr('detail.noAcceptance'))}<div class="detail-code">${esc(t.id)}<br>${esc(t.directory||t.source_dir)}</div></details><details class="task-evidence" data-section="results"><summary>${tr('detail.workerSummary')} · ${tr('detail.evidence')}</summary><p>${esc(r.summary||t.summary||tr('detail.summaryPlaceholder'))}</p><h3>${tr('detail.evidence')}</h3>${listHTML(r.evidence,tr('detail.noEvidence'))}<h3>${tr('detail.tests')}</h3>${listHTML(r.tests,tr('detail.noTests'))}<h3>${tr('detail.unresolved')}</h3>${listHTML(r.unresolved,tr('detail.unresolvedDefault'))}</details>${d.errors?.length?'<section class="task-errors"><h3>'+tr('detail.errors')+'</h3>'+listHTML(d.errors.map(e=>[e.source,e.code,e.message,e.command?tr('detail.command')+e.command:'',e.exit_code!=null?tr('detail.exitCode')+e.exit_code:''].filter(Boolean).join(' · ')),'')+'</section>':''}${d.pending?.length?'<h3>'+tr('detail.pending')+'</h3>'+listHTML(d.pending.map(p=>JSON.stringify(p)),''):''}${t.guidance?.length?'<h3>'+tr('detail.guidance')+'</h3>'+listHTML(t.guidance.map(g=>g.status+' · '+g.text),''):''}`;
 // Preserve reading position and disclosure state during a live refresh.
 if(body.dataset.rendered===html)return;
 body.innerHTML=html;body.dataset.rendered=html;
 for(const section of body.querySelectorAll('details'))section.open=sections.has(section.dataset.section);
 const activity=body.querySelector('.activity-scroll');if(activity)activity.scrollTop=followNewest?0:scroll;
 if(focusedSection)body.querySelector('details[data-section="'+focusedSection+'"] summary')?.focus({preventScroll:true});
 else if(activityFocused)activity?.focus({preventScroll:true});
}
function details(id){detailController.open(id);}
$('groups').addEventListener('click',e=>{const b=e.target.closest('[data-group]');if(!b)return;selectedGroup=b.dataset.group;renderGroups();renderTasks();});
$('filters').addEventListener('click',e=>{const b=e.target.closest('[data-filter]');if(!b)return;selectedFilter=b.dataset.filter;for(const x of $('filters').querySelectorAll('button')){x.classList.toggle('selected',x===b);x.setAttribute('aria-pressed',String(x===b));}renderTasks();});
$('search').addEventListener('input',renderTasks);
$('select-visible-tasks').addEventListener('change',e=>{for(const id of visibleSelectableIds)e.target.checked?selectedTaskIds.add(id):selectedTaskIds.delete(id);renderTasks();});
$('task-rows').addEventListener('change',e=>{const box=e.target.closest('[data-task-select]');if(!box)return;box.checked?selectedTaskIds.add(box.dataset.taskSelect):selectedTaskIds.delete(box.dataset.taskSelect);renderTasks();});
$('task-rows').addEventListener('click',e=>{const remove=e.target.closest('[data-delete-task]');if(remove){const task=data.tasks.find(t=>t.id===remove.dataset.deleteTask);openAction(tr('dialog.deleteTasks'),`<p>${esc(tr('dialog.deleteTasksHint',{n:1}))}</p><p><strong>${esc(task?.title||remove.dataset.deleteTask)}</strong></p>`,async()=>{showToast(tr('tasks.cleanupRunning'),tr('tasks.cleanupRunningHint'),'busy',true);const result=await mutate('/console-api/tasks/manage',{action:'delete',ids:[remove.dataset.deleteTask],discard_cancelled_worktrees:true});selectedTaskIds.delete(remove.dataset.deleteTask);await refresh();taskCleanupResult(result);});return;}const b=e.target.closest('[data-detail]');if(b)details(b.dataset.detail);});
$('delete-selected-tasks').addEventListener('click',()=>{const ids=[...selectedTaskIds];if(!ids.length)return;openAction(tr('dialog.deleteTasks'),`<p>${esc(tr('dialog.deleteTasksHint',{n:ids.length}))}</p>`,async()=>{showToast(tr('tasks.cleanupRunning'),tr('tasks.cleanupRunningHint'),'busy',true);const result=await mutate('/console-api/tasks/manage',{action:'delete',ids,discard_cancelled_worktrees:true});selectedTaskIds.clear();await refresh();taskCleanupResult(result);});});
$('clear-completed-tasks').addEventListener('click',()=>{const n=data.tasks.filter(t=>terminalStates.has(t.status)).length;if(!n){showToast(tr('tasks.cleanupDone'),tr('tasks.cleanupNothing'),'success');return;}openAction(tr('dialog.clearCompleted'),`<p>${esc(tr('dialog.clearCompletedHint',{n}))}</p>`,async()=>{showToast(tr('tasks.cleanupRunning'),tr('tasks.cleanupRunningHint'),'busy',true);const result=await mutate('/console-api/tasks/manage',{action:'clear_finished',discard_cancelled_worktrees:true});selectedTaskIds.clear();await refresh();taskCleanupResult(result);});});
$('close-detail').addEventListener('click',()=>{const id=selectedTask;detailController.close();taskRows.get(id)?.querySelector('[data-focus=title]')?.focus({preventScroll:true});});
$('refresh-detail').addEventListener('click',()=>detailController.refresh(true));
$('refresh-quota').addEventListener('click',async()=>{const b=$('refresh-quota');b.disabled=true;try{await request('/console-api/quota',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});await refresh();}catch(e){$('error').textContent=e.message;$('error').hidden=false;}finally{b.disabled=false;}});
$('toast-close').addEventListener('click',()=>{$('toast').hidden=true;clearTimeout(toastTimer);});
function initAmbientParticles(){
  const canvas=$('ambient-particles');
  if(!canvas||!canvas.getContext)return;
  const ctx=canvas.getContext('2d');
  let particles=[],raf=0,last=0,width=0,height=0,dpr=1;
  const reduced=()=>reducedMotion();
  function make(){
    const count=Math.max(8,Math.min(26,Math.round(width*height/72000)));
    particles=Array.from({length:count},()=>({x:Math.random()*width,y:Math.random()*height,
      r:1+Math.random()*1.8,vx:-.04+Math.random()*.08,vy:-.03+Math.random()*.06,
      alpha:.035+Math.random()*.055}));
  }
  function resize(){
    dpr=Math.min(window.devicePixelRatio||1,2);width=window.innerWidth||0;height=window.innerHeight||0;
    canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);
    canvas.style.width=width+'px';canvas.style.height=height+'px';make();
  }
  function draw(now){
    raf=0;
    if(document.hidden||reduced())return;
    if(now-last<42){raf=requestAnimationFrame(draw);return;}
    const dt=Math.min(2,(now-(last||now))/16.7);last=now;
    ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,width,height);
    for(const p of particles){
      p.x+=p.vx*dt;p.y+=p.vy*dt;
      if(p.x<-6)p.x=width+6;if(p.x>width+6)p.x=-6;if(p.y<-6)p.y=height+6;if(p.y>height+6)p.y=-6;
      ctx.beginPath();ctx.arc(p.x,p.y,p.r,0,Math.PI*2);ctx.fillStyle=`rgba(23,107,93,${p.alpha})`;ctx.fill();
    }
    raf=requestAnimationFrame(draw);
  }
  function start(){if(reduced()||raf)return;if(!width)resize();last=0;raf=requestAnimationFrame(draw);}
  function stop(){cancelAnimationFrame(raf);raf=0;ctx.setTransform(1,0,0,1,0,0);ctx.clearRect(0,0,canvas.width,canvas.height);}
  window.addEventListener('resize',()=>{resize();start();},{passive:true});
  document.addEventListener('visibilitychange',()=>document.hidden?stop():start());
  window.matchMedia?.('(prefers-reduced-motion: reduce)').addEventListener?.('change',e=>e.matches?stop():start);
  start();
}
initAmbientParticles();
refresh();setInterval(()=>{if(!document.hidden)refresh();},4000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
document.addEventListener('i18n:change',()=>{renderGroups();renderQuota();renderTasks();$('health').innerHTML='<i></i>'+(data.pool_healthy?tr('health.online'):tr('health.offline'));if(data.updated_at)$('updated').textContent=tr('updated.at',{time:clock(data.updated_at)});if(data.max_parallel_per_owner!=null)$('pool-limit').textContent=tr('pool.limit',{n:data.max_parallel_per_owner});if(selectedTask)renderInlineDetail(detailController.state());});

$('sign-out').addEventListener('click',async()=>{const b=$('sign-out');b.disabled=true;try{await request('/console-api/auth/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});window.location.replace('/console-login');}catch(e){$('error').textContent=e.message;$('error').hidden=false;}finally{b.disabled=false;}});
request('/console-api/auth/status').then(s=>{if(!s.authenticated){window.location.replace('/console-login');return;}$('account-name').textContent=s.username||'';}).catch(()=>{});
