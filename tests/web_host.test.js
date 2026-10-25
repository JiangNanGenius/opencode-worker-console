'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const View = require('../web/host.js');

test('format helpers render unavailable for null, never coerce to zero', () => {
  assert.equal(View.formatPercent(null), '—');
  assert.equal(View.formatPercent(undefined), '—');
  assert.equal(View.formatPercent(''), '—');
  assert.equal(View.formatPercent(true), '—');
  assert.equal(View.formatPercent(0), '0%');
  assert.equal(View.formatRate(null), '—');
  assert.equal(View.formatRate(0), '0 B/s');
  assert.equal(View.formatBytes(null), '—');
  assert.equal(View.formatBytes(0), '0 B');
  assert.equal(View.formatTemp(null), '—');
  assert.equal(View.formatTemp(44.44), '44.4 °C');
  assert.equal(View.formatDuration(null), '—');
  assert.equal(View.formatDuration(65), '1 m 5 s');
});

test('splitGaps breaks series at missing samples instead of zero filling', () => {
  const points = [{ t: 0, v: 1 }, { t: 2, v: 2 }, { t: 20, v: 3 }, { t: 22, v: 4 }];
  const segments = View.splitGaps(points, 2);
  assert.equal(segments.length, 2);
  assert.deepEqual(segments[0].map(p => p.v), [1, 2]);
  assert.deepEqual(segments[1].map(p => p.v), [3, 4]);
});

test('chartSVG renders empty for no data and a line for points', () => {
  assert.match(View.chartSVG([], { label: 'cpu' }), /<svg class="hostov-svg"/);
  const svg = View.chartSVG([{ t: 0, v: 5 }, { t: 2, v: 9 }], { label: 'cpu', interval: 2 });
  assert.match(svg, /class="hostov-line"/);
  assert.match(svg, /class="hostov-dot"/);
});

test('task rows show real progress only and escape untrusted fields', () => {
  const item = { id: 'a', title: '<img src=x>', project: 'p', status: 'running', model: 'm1',
                started_at: 100, progress: { percent: 42, phase: 'build' } };
  const html = View.taskRowHTML(item, 200);
  assert.ok(!html.includes('<img'));
  assert.match(html, /&lt;img/);
  assert.match(html, /42/);
  assert.match(html, /m1/);
  assert.match(html, /1 m 40 s/); // elapsed from started_at
  const noProgress = View.taskRowHTML({ id: 'b', title: 'x', status: 'running' }, 200);
  assert.ok(!noProgress.includes('hostov-taskbar'));
  const objectRun = View.taskRowHTML({ id: 'c', title: 'x', status: 'running',
                                       progress: { github_run: { url: 'https://github.com/a/b/actions/runs/1' } } }, 200);
  assert.ok(!objectRun.includes('href'));   // object-shaped run is never rendered
  const realRun = View.taskRowHTML({ id: 'd', title: 'x', status: 'running',
                                     progress: { percent: 5, github_run: 'https://github.com/a/b/actions/runs/9' } }, 200);
  assert.match(realRun, /href="https:\/\/github\.com\/a\/b\/actions\/runs\/9"/);
  const jsRun = View.taskRowHTML({ id: 'e', title: 'x', status: 'running',
                                   progress: { github_run: 'javascript:alert(1)' } }, 200);
  assert.ok(!jsRun.includes('href'));
});

test('tasksHTML merges codex and opencode items with sources', () => {
  const html = View.tasksHTML({ codex: { items: [{ id: 't1', title: 'c', status: 'running' }] },
                                opencode: { items: [{ id: 'j1', title: 'o', status: 'running' }] } }, 100);
  assert.match(html, /Codex/);
  assert.match(html, /OpenCode/);
  assert.equal(View.tasksHTML({ codex: { items: [] }, opencode: { items: [] } }), '<p class="muted">—</p>');
});

test('quota rows show worst window and balance values', () => {
  const withWindow = View.quotaRowHTML({ id: 'kimi', name: 'Kimi', state: 'ok',
    windows: [{ label: '5h', remaining_percent: 83 }], balances: [] });
  assert.match(withWindow, /83%/);
  const withBalance = View.quotaRowHTML({ id: 'deepseek', name: 'DeepSeek', state: 'ok',
    windows: [], balances: [{ currency: 'CNY', remaining: 12.5 }] });
  assert.match(withBalance, /¥12\.50/);
  const down = View.quotaRowHTML({ id: 'codex', name: 'Codex', state: 'unavailable' });
  assert.match(down, /unavailable/);
});

test('disk rows mark external volumes and capacity', () => {
  const html = View.diskRowHTML({ name: 'USB', mountpoint: '/Volumes/USB', internal: false,
                                  total_gb: 100, free_gb: 50, used_percent: 50 });
  assert.match(html, /external/);
  assert.match(html, /50%/);
  const internal = View.diskRowHTML({ name: 'Mac SSD', mountpoint: '/System/Volumes/Data', internal: true,
                                      total_gb: 460, free_gb: 100, used_percent: 78 });
  assert.ok(!internal.includes('external'));
});

test('service rows show alive state', () => {
  assert.match(View.serviceRowHTML({ name: 'pool', alive: true, pid: 7 }), /alive/);
  assert.match(View.serviceRowHTML({ name: 'pool', alive: false }), /down/);
});

test('process rows expose TERM and explicit KILL with identity tokens', () => {
  const html = View.processRowHTML({ pid: 42, name: 'worker', cpu_percent: 5, mem_percent: 2,
                                     rss_bytes: 1024, started_at: 777 }, true);
  assert.match(html, /data-proc-term="42"/);
  assert.match(html, /data-proc-kill="42"/);
  assert.match(html, /data-proc-started="777"/);
  const readOnly = View.processRowHTML({ pid: 42, name: 'w', cpu_percent: 5, mem_percent: 2,
                                         rss_bytes: 1, started_at: 1 }, false);
  assert.ok(!readOnly.includes('data-proc-term'));
});

test('operation status distinguishes accepted from completed', () => {
  assert.equal(View.opStatusLabel({ status: 'accepted' }), 'accepted…');
  assert.equal(View.opStatusLabel({ status: 'dispatched' }), 'dispatched…');
  assert.equal(View.opStatusLabel({ status: 'completed' }), 'completed');
  assert.equal(View.opStatusLabel({ status: 'failed' }), 'failed');
});

test('availability notes explain disabled controls', () => {
  assert.equal(View.availabilityNote(true, 'x'), '');
  assert.match(View.availabilityNote(false, 'helper missing'), /helper missing/);
});


test('quota ignores unknown windows and shows the limiting window reset', () => {
  const html = View.quotaRowHTML({id:'kimi',state:'ok',windows:[
    {remaining_percent:null}, {remaining_percent:38,resets_at:1800000000}
  ]});
  assert.match(html,/38%/);
  assert.match(html,/↻/);
});

test('quota rows surface exhaustion, snapshots and Credits amounts honestly', () => {
  const t = (key) => ({ 'hostov.quotaExhausted': 'exhausted', 'hostov.quotaStale': 'stale',
                        'hostov.quotaSnapshotStale': 'snapshot stale' }[key] || key);
  const blocked = View.quotaRowHTML({ id: 'kimi-for-coding', name: 'Kimi', state: 'ok',
    available: false, billing: { reason: 'monthly_usage_limit', monthly_exhausted: true },
    windows: [{ remaining_percent: 10, resets_at: 1800000000 }], balances: [] }, t);
  assert.match(blocked, /exhausted/);
  assert.match(blocked, /critical/);
  const snapshot = View.quotaRowHTML({ id: 'bailian-token-plan', name: '阿里百炼', state: 'ok',
    available: null, stale: true, snapshot: { freshness: 'stale', expired: false },
    windows: [{ remaining_percent: 99.9, remaining: 99926.94, limit: 100000, unit: 'credits',
                resets_at: 1800000000 }], balances: [] }, t);
  assert.match(snapshot, /99,926\.94 Credits/);
  assert.match(snapshot, /snapshot stale/);
  const expired = View.quotaRowHTML({ id: 'bailian-token-plan', state: 'ok', available: null,
    snapshot: { freshness: 'fresh', expired: true }, windows: [], balances: [] }, t);
  assert.match(expired, /snapshot stale/);
  assert.match(expired, /—/);
  const unknown = View.quotaRowHTML({ id: 'bailian-token-plan', state: 'ok', available: null,
    windows: [], balances: [] }, t);
  assert.doesNotMatch(unknown, /0%/);
  assert.match(unknown, /—/);
});

test('quota rows keep live percentages when no exhaustion is reported', () => {
  const healthy = View.quotaRowHTML({ id: 'deepseek', name: 'DeepSeek', state: 'ok',
    windows: [{ remaining_percent: 42 }], balances: [] });
  assert.match(healthy, /42%/);
  assert.match(healthy, /healthy/);
});

test('Codex tasks expose exact relationships as escaped read-only details without invented progress', () => {
  const group = {state:'ok', items:[
    {id:'owner',title:'<Owner>',status:'running',model:'model',workers:[{id:'job-1',title:'<Worker>',status:'completed'}]},
    {id:'child',parent_thread_id:'owner',title:'Child',status:'unknown'}],
    missing_owners:[{id:'missing',status:'unknown',workers:[{id:'job-2',title:'Other',status:'running'}]}]};
  const html = View.codexHTML(group, 100);
  assert.match(html,/data-codex-thread="owner"/);
  assert.match(html,/data-codex-thread="child"/);
  assert.match(html,/&lt;Owner&gt;/);
  assert.match(html,/&lt;Worker&gt;/);
  assert.match(html,/data-host-task="job-1"/);
  assert.match(html,/Owner task unavailable/);
  assert.ok(!html.includes('progressbar'));
  assert.ok(!html.includes('data-cancel-task'));
  assert.ok(!html.includes('data-delete-task'));
  const combined = View.tasksHTML({codex:group,opencode:{items:[{id:'job-1',title:'duplicate',status:'running'}]}},100);
  assert.ok(!combined.includes('duplicate'));
});

test('Codex task source reports unavailable and stale independently of empty content', () => {
  assert.match(View.codexHTML({state:'unavailable',items:[]}),/unavailable or out of date/);
  assert.match(View.codexHTML({state:'ok',stale:true,items:[]}),/unavailable or out of date/);
  assert.match(View.codexHTML({state:'ok',items:[]}),/No recent local Codex tasks/);
  assert.match(View.tasksHTML({opencode:{state:'ok',stale:true,items:[]}}),/OpenCode task status/);
});

test('Kimi overview highlights account total while preserving tighter weekly and short limits',()=>{
 const quota={id:'kimi-for-coding',name:'Kimi',state:'ok',available:true,windows:[
  {kind:'short',remaining_percent:99.94,source:'kimi_code_account'},
  {kind:'weekly',remaining_percent:15.37,source:'kimi_code_account'},
  {kind:'month_total',remaining_percent:66.73,source:'kimi_web_membership'}]};
 const html=View.quotaRowHTML(quota,k=>k);
 assert.match(html,/<strong>66[.,]73%<\/strong>/);
 assert.match(html,/Kimi · quota.accountTotal/);
 assert.match(html,/quota.weekly 15[.,]37%/);
 assert.match(html,/quota.fiveHour 99[.,]94%/);
 assert.match(html,/hostov-quota tight/);
 assert.match(html,/aria-valuenow="66.73"/);
 assert.doesNotMatch(html,/kimiAccount.totalMissing/);
 const zero=View.quotaRowHTML({...quota,windows:quota.windows.map(w=>w.kind==='month_total'?{...w,remaining_percent:0}:w)},k=>k);
 assert.match(zero,/<strong>0%<\/strong>/);assert.match(zero,/hostov-quota critical/);
});

test('missing Kimi total labels the actual weekly balance and never calls it total quota',()=>{
 const html=View.quotaRowHTML({id:'kimi-for-coding',name:'Kimi',state:'ok',windows:[{kind:'weekly',remaining_percent:15.37},{kind:'short',remaining_percent:99.94}]},k=>k);
 assert.match(html,/<strong>15[.,]37%<\/strong>/);
 assert.match(html,/Kimi · quota.weekly/);
 assert.match(html,/kimiAccount.totalMissing/);
 assert.doesNotMatch(html,/Kimi · quota.accountTotal/);
 const other=View.quotaRowHTML({id:'other',name:'Other',state:'ok',windows:[{kind:'month_total',remaining_percent:66.73},{kind:'weekly',remaining_percent:15.37}]},k=>k);
 assert.match(other,/<strong>15%<\/strong>/);
 assert.doesNotMatch(other,/Other · quota.accountTotal/);
});
test('Kimi monthly subscription expiry is labeled expiry instead of a reset arrow',()=>{
 const html=View.quotaRowHTML({id:'kimi-for-coding',name:'Kimi',state:'ok',windows:[{kind:'month_total',remaining_percent:66.73,resets_at:null,expires_at:'2026-11-18T06:47:51Z'}]},(key,v={})=>key+(v.time?':'+v.time:''));
 assert.match(html,/quota.expiresAt:/);assert.doesNotMatch(html,/↻/);
});
