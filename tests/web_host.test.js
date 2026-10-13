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
