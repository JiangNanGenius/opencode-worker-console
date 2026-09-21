'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const View = require('../web/task-view.js');
const tick = () => new Promise(resolve => setImmediate(resolve));
const esc = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const display = { tr: key => key, esc, number: String, clock: String };

test('usage keeps unknown distinct from zero and never adds reasoning twice', () => {
 const tasks = [{id:'a',usage:{total:100,input:70,output:30,reasoning:10}},{id:'b',usage:{total:0}},{id:'c',tokens:{input:900}}];
 assert.deepEqual(View.aggregate(tasks), {total:100,known:2,count:3});
 assert.deepEqual(View.aggregate(tasks,new Map([['a',{usage:{total:120}}]])),{total:120,known:2,count:3});
 assert.equal(View.aggregate([{id:'x'}]).total,null);
 assert.match(View.usageHTML({total:0},display),/<dd>0<\/dd>/);
 assert.match(View.usageHTML({},display),/<dd>—<\/dd>/);
});

test('usage keeps the task total and shows actual model segments after a switch', () => {
 const usage={total:140,input:14,output:7,reasoning:7,cache_read:112,cache_write:0,complete:true,
   by_model:[{model:'ark/evolving',total:100},{model:'ark/auto',total:40}]};
 const html=View.usageHTML(usage,display);
 assert.match(html,/usage\.byModel/);
 assert.match(html,/ark\/evolving/);
 assert.match(html,/100/);
 assert.match(html,/ark\/auto/);
 assert.match(html,/40/);
});

test('activity escapes every untrusted field and marks a stale sample', () => {
 const html=View.activityHTML({stale:true,error:'<secret>',source:'saved',has_more:true,events:[{id:'" onclick="bad',status:'evil',label:'<img>',text:'<script>bad()</script>',time:2}]},display);
 assert.ok(!html.includes('<script>'));
 assert.ok(!html.includes('<img>'));
 assert.match(html,/&lt;script&gt;/);
 assert.match(html,/activity\.stale/);
 assert.match(html,/activity\.recentOnly/);
});

test('activity shows the newest progress first without mutating the source events', () => {
 const events=[{id:'old',status:'completed',text:'older',time:1},{id:'new',status:'running',text:'newer',time:2}];
 const html=View.activityHTML({events},display);
 assert.ok(html.indexOf('newer') < html.indexOf('older'));
 assert.deepEqual(events.map(event=>event.id),['old','new']);
});

test('truncated activity expands to the complete cached message and can collapse again', () => {
 const activity={events:[{id:'evt-long',type:'assistant',label:'assistant',status:'completed',text:'preview…',truncated:true,time:2}]};
 const collapsed=View.activityHTML(activity,{...display,taskId:'job-1'});
 assert.match(collapsed,/data-expand-activity="evt-long"/);
 assert.match(collapsed,/aria-expanded="false"/);
 assert.match(collapsed,/activity\.expand/);
 const key='job-1:evt-long';
 const expanded=View.activityHTML(activity,{...display,taskId:'job-1',fullMessages:new Map([[key,'complete <tail>']]),expandedMessages:new Set([key])});
 assert.match(expanded,/complete &lt;tail&gt;/);
 assert.match(expanded,/aria-expanded="true"/);
 assert.match(expanded,/activity\.collapse/);
 assert.doesNotMatch(expanded,/complete <tail>/);
});

test('polling cancels a closed task and ignores late replies', async () => {
 let resolve,signal;
 const c=View.createController({changed:()=>{},request:(_p,options)=>{signal=options.signal;return new Promise(r=>resolve=r);}});
 c.open('a');c.close();assert.equal(signal.aborted,true);
 resolve({task:{id:'a'}});await tick();
 assert.equal(c.state().selected,null);assert.equal(c.cache.has('a'),false);
});

test('switching tasks ignores superseded responses; double click collapses', async () => {
 const replies={};
 const c=View.createController({changed:()=>{},request:p=>new Promise(r=>replies[p]=r)});
 c.open('a');c.open('b');replies['/console-api/task/b']({task:{id:'b'}});await tick();
 replies['/console-api/task/a']({task:{id:'a'}});await tick();
 assert.equal(c.state().detail.task.id,'b');assert.equal(c.cache.has('a'),false);
 c.open('b');assert.equal(c.state().selected,null);
});

test('polls do not overlap, pause while hidden, and recover without losing evidence', async () => {
 let calls=0,visible=false,resolve;
 const c=View.createController({changed:()=>{},visible:()=>visible,request:()=>{calls++;return new Promise(r=>resolve=r);}});
 c.open('a');assert.equal(calls,0);
 visible=true;const first=c.refresh();c.refresh();assert.equal(calls,1);
 resolve({task:{id:'a'},usage:{total:5}});await first;
 visible=false;await c.refresh();assert.equal(calls,1);
 let fails=true;
 const recovering=View.createController({changed:()=>{},request:()=>fails?Promise.reject(Error('offline')):Promise.resolve({task:{id:'x'}})});
 recovering.open('x');await tick();assert.equal(recovering.state().error,'offline');
 fails=false;await recovering.refresh();assert.equal(recovering.state().error,null);
 fails=true;await recovering.refresh();assert.equal(recovering.state().detail.task.id,'x');
});

test('completed details stop polling but can be explicitly refreshed', async () => {
 let calls=0;
 const c=View.createController({changed:()=>{},request:async()=>{calls++;return {task:{id:'a',status:'completed'}};}});
 c.open('a');await tick();await c.refresh();assert.equal(calls,1);
 await c.refresh(true);assert.equal(calls,2);
 c.close();c.open('a');await tick();assert.equal(calls,3);
});

test('detail shows seeded task immediately and a timeout releases loading state', async () => {
 const c=View.createController({changed:()=>{},timeoutMs:5,translate:key=>key==='detail.slow'?'Live progress took too long':key,request:(_path,{signal})=>new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>{const e=Error('aborted');e.name='AbortError';reject(e);} ))});
 c.open('a',{task:{id:'a',status:'running'},activity:{events:[]},usage:{total:7}});
 assert.equal(c.state().detail.task.id,'a');
 await new Promise(resolve=>setTimeout(resolve,15));
 assert.equal(c.state().pending,false);
 assert.equal(c.state().detail.usage.total,7);
 assert.match(c.state().error,/too long/);
});

// Minimal DOM adapter tests the real app's row reconciliation without a browser,
// services, credentials or model calls. It does not assert visual rendering.
class Element {
  constructor(name='div'){this.name=name;this.children=[];this.parentNode=null;this.dataset={};this.attrs={};this.hidden=false;this.innerHTML='';this.value='';this.textContent='';this.style={};this.listeners={};this.classList={toggle(){},add(){},remove(){}};}
  append(node){this.insertBefore(node,null);}
  insertBefore(node,before){node.remove();const at=before?this.children.indexOf(before):this.children.length;if(at<0)throw Error('invalid anchor');this.children.splice(at,0,node);node.parentNode=this;}
  remove(){if(this.parentNode){const a=this.parentNode.children;a.splice(a.indexOf(this),1);this.parentNode=null;}}
  get firstElementChild(){return this.children[0]||null;}
  get nextElementSibling(){if(!this.parentNode)return null;const a=this.parentNode.children;return a[a.indexOf(this)+1]||null;}
  contains(node){return !!node&&(node===this||this.children.some(c=>c.contains(node)));}
  addEventListener(type,fn){this.listeners[type]=fn;}
  querySelector(){return null;}
  querySelectorAll(){return [];}
  getAttribute(key){return Object.prototype.hasOwnProperty.call(this.attrs,key)?this.attrs[key]:(this[key]??null);}
  setAttribute(key,value){this.attrs[key]=value;}
  focus(){}
  scrollIntoView(){throw Error('Task expansion must not scroll the page');}
}
function appHarness(){
 const elements=new Map();const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
 const document={hidden:false,activeElement:null,getElementById:get,createElement:n=>new Element(n),querySelector:get,querySelectorAll:()=>[],addEventListener(){}};
 const fixtureTasks=['a','b','c'].map(id=>({id,title:id,group_id:'g',group_title:'Group',status:'running',mode:'read',usage:{total:10}}));
 fixtureTasks[2].profile='ark-k3';fixtureTasks[2].tier='deep';fixtureTasks[2].actual_models=['volcengine-agent-plan/kimi-k3'];
 let revision=0;
 const profiles={'ark-k3':{label:'Ark Agent Plan · Kimi K3',model:'volcengine-agent-plan/kimi-k3'}};
 const fetch=async url=>({ok:true,json:async()=>url==='/console-api/state'?{tasks:fixtureTasks,quota:{},profiles,pool_healthy:true}:url==='/console-api/auth/status'?{authenticated:true,username:'fixture'}:{task:fixtureTasks.find(t=>url.endsWith(t.id)),activity:{source:'live',events:[{id:'e',text:'Update '+(++revision),status:'running'}]},usage:{total:20}}});
 const ctx={document,window:{TaskView:View,getSelection:()=>null,location:{replace(){throw Error('unexpected redirect');}}},TaskView:View,fetch,setInterval(){},setTimeout(){return 1;},clearTimeout(){},Date,Map,Set,AbortController,console};
 vm.createContext(ctx);vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8'),ctx);
 return {ctx,get,run:source=>vm.runInContext(source,ctx)};
}

test('real task list inserts detail immediately after selected row and keeps its node on refresh', async () => {
 const h=appHarness();await tick();
 h.run("details('b')");await tick();
 const tbody=h.get('task-rows');
 assert.equal(tbody.children.length,4);
 assert.match(tbody.children[1].innerHTML,/data-detail="b"/);
 assert.equal(tbody.children[2].className,'task-detail-row');
 assert.match(tbody.children[3].innerHTML,/data-detail="c"/);
 const panel=tbody.children[2];const before=h.get('detail-body').innerHTML;
 await h.run('refresh()');await tick();
 assert.equal(tbody.children[2],panel);
 assert.notEqual(h.get('detail-body').innerHTML,before);
 assert.match(tbody.children[1].innerHTML,/aria-expanded="true"/);
 h.run("details('b')");assert.equal(tbody.children.length,3);
});

test('filtering away the selected row stops its detail and preserves the filtered summary', async () => {
 const h=appHarness();await tick();h.run("details('b')");await tick();
 h.get('search').value='c';h.run('renderTasks()');
 assert.equal(h.run('selectedTask'),null);
 assert.equal(h.get('task-rows').children.length,1);
 assert.match(h.get('task-rows').children[0].innerHTML,/data-detail="c"/);
});

test('task tier labels custom and Ark profiles instead of assuming every unknown profile is general', async () => {
 const h=appHarness();await tick();
 const row=h.get('task-rows').children[2].innerHTML;
 assert.match(row,/Ark Agent Plan · Kimi K3/);
 assert.match(row,/profile\.deep/);
 assert.match(row,/model-dot deep/);
 assert.doesNotMatch(row,/profile\.generic/);
});

test('quota range distinguishes paused sampling, collection and burn estimates', async () => {
 const h=appHarness();await tick();
 assert.equal(h.run("estimatedRange({consumption_estimate:{idle:true}})"),'quota.rangePaused');
 assert.equal(h.run("estimatedRange({consumption_estimate:{hours:null}})"),'quota.rangeCollecting');
 assert.equal(h.run("estimatedRange({consumption_estimate:{hours:12}})"),'quota.rangeHours');
 assert.equal(h.run("estimatedBalanceRange({balances:[{consumption_estimate:{hours:31,idle:false}}]})"),'quota.rangeHours');
 assert.equal(h.run("estimatedBalanceRange({balances:[{consumption_estimate:{idle:true}}]})"),'quota.rangePaused');
 assert.equal(h.run("quotaPace({remaining_percent:70,duration_minutes:10080,resets_at:new Date(Date.now()+100*3600000).toISOString(),consumption_estimate:{hours:40}})[0]"),'on-track');
 assert.doesNotMatch(h.run("quotaTrack(70,'remaining')"),/line|pace-marker/);
  assert.match(h.run("quotaWindows([{name:'AFPFiveHour',remaining_percent:70,duration_minutes:300,resets_at:new Date(Date.now()+3600000).toISOString()}],true)"),/quota-window-reset/);
  const details=h.run("quotaWindows([{name:'AFPFiveHour',remaining_percent:70,duration_minutes:300,resets_at:new Date(Date.now()+3600000).toISOString(),consumption_estimate:{hours:1}},{name:'AFPWeekly',remaining_percent:60,duration_minutes:10080,resets_at:new Date(Date.now()+100*3600000).toISOString(),consumption_estimate:{hours:20}}],true)");
  assert.equal((details.match(/quota\.bottleneckRunway/g)||[]).length,1);
});

test('every provider header exposes a quota-level state', async () => {
  const h=appHarness();await tick();
  h.run(`data.profiles={ds:{model:"deepseek/flash"},kimi:{model:"kimi-for-coding/k3"},ark:{model:"volcengine-agent-plan/k3"}};
    data.quota={deepseek:{available:true,balances:[{remaining:8,currency:"CNY"}]},
      "kimi-for-coding":{available:false,windows:[{name:"overall",valid:true,remaining:0,remaining_percent:0}]},
      "volcengine-agent-plan":{available:true,windows:[{name:"AFPWeekly",valid:true,remaining_percent:8,duration_minutes:10080,resets_at:new Date(Date.now()+100*3600000).toISOString(),consumption_estimate:{hours:10}}]}};
    data.tier_guidance={runway_threshold_percent:38,budget_signals:{deepseek:{low:true}}};renderQuota()`);
  assert.equal(h.get('ds-state').textContent,'quota.state.nearExhausted');
  assert.equal(h.get('kimi-state').textContent,'quota.state.exhausted');
  assert.equal(h.get('ark-state').textContent,'quota.state.nearExhausted');
  assert.match(h.get('ds-state').className,/tight/);
  assert.match(h.get('kimi-state').className,/critical/);
});

test('work pool uses fitted runtime, with DeepSeek as a small observed share', async () => {
  const h=appHarness();await tick();
  h.run('data.quota={deepseek:{available:true,balances:[{remaining:50,currency:"CNY"}]},"kimi-for-coding":{available:false,windows:[{name:"overall",remaining_percent:0}]},"volcengine-agent-plan":{available:true,windows:[{name:"AFPWeekly",remaining_percent:44}]}};data.tier_guidance={conservation_level:2};data.economics={work_pool:{capacity:321.667,total:80,remaining_percent:24.87,refills:[{provider:"kimi",resets_at:new Date(Date.now()+72000000).toISOString(),projected_remaining_percent:66}],components:{balance:{capacity:30,amount:25,remaining_percent:83.333},kimi:{capacity:166.667,amount:0,remaining_percent:0},plan:{capacity:125,amount:55,remaining_percent:44}}}};data.profiles={ds:{model:"deepseek/flash"},kimi:{model:"kimi-for-coding/k3"},ark:{model:"volcengine-agent-plan/k3"}};renderQuota()');
  const bar=h.get('pool-bar');
  assert.match(bar.innerHTML,/data-segment="kimi"[^>]*width:0\.000%/);
  assert.match(bar.innerHTML,/data-segment="ark"[^>]*width:17\.098%/);
  assert.match(bar.innerHTML,/data-segment="deepseek"[^>]*width:7\.772%/);
  assert.equal(bar.getAttribute('aria-label'),'quota.poolCapacityAria');
  assert.match(h.get('pool-summary').innerHTML,/25%/);
  const components=h.get('pool-components').innerHTML;
  assert.match(components,/¥50\.00/);
  assert.doesNotMatch(components,/¥50\.00 · \d+%/);
  assert.match(components,/quota\.weekly · 0%/);
  assert.match(components,/quota\.weekly · 44%/);
  assert.doesNotMatch(h.get('pool-summary').innerHTML+components,/AFP-equivalent|≈/);
  assert.equal(h.get('pool-state').textContent,'quota.conservationLevel2');
  assert.match(h.get('pool-next').innerHTML,/quota\.poolEndurance/);
  assert.match(h.get('pool-next').innerHTML,/quota\.poolRunwayDays/);
  assert.match(h.get('pool-next').innerHTML,/quota\.poolNextRefillLabel/);
  assert.match(h.get('pool-next').innerHTML,/quota\.poolProjected/);
});

test('work pool remains visible for a Kimi-only installation', async () => {
  const h=appHarness();await tick();
  h.run('data.quota={"kimi-for-coding":{available:true,windows:[{name:"overall",remaining_percent:75}]}};data.economics={work_pool:{capacity:168,total:126,remaining_percent:75,components:{kimi:{capacity:168,amount:126,remaining_percent:75}}}};data.profiles={kimi:{model:"kimi-for-coding/k3"}};renderQuota()');
  assert.match(h.get('pool-summary').innerHTML,/75%/);
  assert.match(h.get('pool-components').innerHTML,/quota\.kimiPlan/);
});

test('Kimi reset restores its fitted capacity to the total work pool', async () => {
  const h=appHarness();await tick();
  h.run('data.quota={"kimi-for-coding":{available:true,windows:[{name:"overall",remaining_percent:100}]}};data.economics={work_pool:{capacity:168,total:168,remaining_percent:100,components:{kimi:{capacity:168,amount:168,remaining_percent:100}}}};data.profiles={kimi:{model:"kimi-for-coding/k3"}};renderQuota()');
  assert.match(h.get('pool-summary').innerHTML,/100%/);
  assert.match(h.get('pool-bar').innerHTML,/data-segment="kimi"[^>]*width:100\.000%/);
});


test('cleanup result stays visible in the task row and raises a success toast', async () => {
 const h=appHarness();await tick();
 h.run("taskCleanupResult({deleted:3,sessions_deleted:2,worktrees_released:1,freed_bytes:1048576,retained_for_review:[],errors:[]})");
 assert.equal(h.get('toast').hidden,false);
 assert.equal(h.get('toast-title').textContent,'tasks.cleanupDone');
 assert.equal(h.get('task-cleanup-message').textContent,'tasks.cleanupResult');
});
