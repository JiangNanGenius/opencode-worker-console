'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');

function load(names) {
  const context = vm.createContext({
    tr: (key, vars) => ({'quota.seatDetails': 'Seat details ({n})', 'quota.unknown': 'unknown',
      'quota.seatExpired': 'expired'}[key] || key).replace('{n}', vars && vars.n !== undefined ? vars.n : ''),
    esc: value => String(value ?? ''),
    number: value => String(value),
    I18n: null,
    TaskView: { epochSeconds: value => typeof value === 'number' ? value : null },
  });
  for (const name of names) {
    const definition = source.match(new RegExp('^function ' + name + '\\(q\\).*$', 'm'));
    assert.ok(definition, name + ' is available');
    vm.runInContext(definition[0], context);
  }
  return context;
}

const context = load(['alibabaQuotaBalance', 'alibabaSeatDetails']);

// One combined Credits window is the subscription total: the scarcest single
// seat (13%) can no longer be reported as the Alibaba balance.
const combined = {
  source: 'openapi_live', sampled_at: 123, available: true,
  windows: [{ name: 'Credits(pool)', valid: true, limit: 317742.000000,
    remaining: 230769.237225, remaining_percent: 72.628, seat_count: 2,
    resets_at: '2026-11-16T10:00:00+00:00' }],
  seat_groups: [
    { name: 'Credits(pro)', seat_type: 'pro', valid: true, limit: 100000,
      remaining: 13027.237225, remaining_percent: 13.027 },
    { name: 'Credits(max)', seat_type: 'max', valid: true, limit: 217742,
      remaining: 217742, remaining_percent: 100 },
  ],
};

test('combined seat ledger reports the summed balance, not the minimum seat', () => {
  assert.equal(context.alibabaQuotaBalance(combined), 230769.237225);
  assert.notEqual(context.alibabaQuotaBalance(combined), 13027.237225);
});

test('optional seat detail renders both seats with the common aggregate intact', () => {
  const html = context.alibabaSeatDetails(combined);
  assert.match(html, /Seat details \(2\)/);
  assert.match(html, /pro/);
  assert.match(html, /max/);
  assert.match(html, /13%/);
  assert.match(html, /100%/);
  assert.doesNotMatch(html, /72%/, 'the aggregated window is rendered once, not as a seat row');
});

test('degraded seat detail stays unknown and keeps a known limit', () => {
  const degraded = JSON.parse(JSON.stringify(combined));
  degraded.seat_groups[1] = { name: 'Credits(max)', seat_type: 'max', valid: false,
    limit: 217742, remaining: null, remaining_percent: null, credits_service_degraded: true };
  const html = context.alibabaSeatDetails(degraded);
  assert.match(html, /unknown/);
  assert.match(html, /217742 Credits/);
});

test('legacy single-seat payload renders no seat detail', () => {
  const single = { windows: [{ name: 'Credits(pro)', valid: true, limit: 100000,
    remaining: 80000, remaining_percent: 80 }] };
  assert.equal(context.alibabaSeatDetails(single), '');
  assert.equal(context.alibabaSeatDetails({}), '');
  assert.equal(context.alibabaQuotaBalance(single), 80000);
});
