'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const View = require('../web/memory.js');

test('pageCount and clampPage keep pagination inside real bounds', () => {
  assert.equal(View.pageCount(0, 25), 1);
  assert.equal(View.pageCount(2127, 25), 86);
  assert.equal(View.pageCount(2127, 50), 43);
  assert.equal(View.clampPage(200, 86), 86);
  assert.equal(View.clampPage(0, 86), 1);
  assert.equal(View.clampPage('3', 86), 3);
});

test('latest-response race protection drops a superseded response', () => {
  const state = View.createState();
  const first = View.beginRequest(state, 'page');
  const second = View.beginRequest(state, 'page');
  assert.equal(View.acceptResult(state, 'page', first, { total: 1 }), null);
  assert.deepEqual(View.acceptResult(state, 'page', second, { total: 2 }), { total: 2 });
  const detail = View.beginRequest(state, 'detail');
  assert.equal(View.acceptResult(state, 'page', detail, { total: 3 }), null);
  assert.deepEqual(View.acceptResult(state, 'detail', detail, { id: 'x' }), { id: 'x' });
});

test('levelOf reports real native levels and never invents missing summaries', () => {
  const file = {
    kind: 'file', content: 'full body',
    levels: {
      l0: { available: false, reason: 'not ready', uri: 'viking://user/default/memories/experiences' },
      l1: { available: true, text: 'Overview text', uri: 'viking://user/default/memories/experiences', aggregate: true },
      l2: { available: true, chars: 9 }
    }
  };
  assert.deepEqual(View.levelOf(file, 'l0'), { state: 'notReady' });
  assert.deepEqual(View.levelOf(file, 'l1'),
    { state: 'available', text: 'Overview text', uri: 'viking://user/default/memories/experiences', aggregate: true });
  assert.equal(View.levelOf(file, 'l2').state, 'available');
  assert.equal(View.levelOf(file, 'l2').text, 'full body');
  assert.deepEqual(View.levelOf({ levels: {} }, 'l0'), { state: 'pending' });
  assert.deepEqual(View.levelOf({ levels: { l0: { available: false, error: 'boom' } } }, 'l0'),
    { state: 'error', error: 'boom' });
  assert.deepEqual(View.levelOf({ kind: 'directory', levels: { l2: { available: false, reason: 'directory' } } }, 'l2'),
    { state: 'directory' });
  assert.equal(View.levelOf({ levels: { l0: { available: false } } }, 'l0').state, 'missing');
});

test('previewOf bounds row previews and falls back to sanitized content', () => {
  assert.equal(View.previewOf({ preview: '  spaced   preview  ' }), 'spaced preview');
  assert.equal(View.previewOf({ content: 'x'.repeat(900) }).length, 360);
  assert.equal(View.previewOf(null), '');
  assert.equal(View.previewOf({}), '');
});

test('sessionURL only links a server-verified same-origin session', () => {
  assert.equal(View.sessionURL({ session: { verified: true, url: '/abc/session/ses_1' } }), '/abc/session/ses_1');
  assert.equal(View.sessionURL({ session: { verified: false, url: '/abc/session/ses_1' } }), null);
  assert.equal(View.sessionURL({ session: { verified: true, url: 'https://evil.example/x' } }), null);
  assert.equal(View.sessionURL({ session: { verified: true, url: 'javascript:alert(1)' } }), null);
  assert.equal(View.sessionURL({ session: { verified: true, url: '//evil.example/x' } }), null);
  assert.equal(View.sessionURL({ session: { verified: true } }), null);
  assert.equal(View.sessionURL(null), null);
});

test('formatBytes renders real measurements and unavailable values, never zero', () => {
  assert.equal(View.formatBytes(null), '—');
  assert.equal(View.formatBytes(undefined), '—');
  assert.equal(View.formatBytes(0), '0 B');
  assert.equal(View.formatBytes(101210719), '96.5 MB');
  assert.equal(View.formatBytes(943718), '922 KB');
  assert.equal(View.formatBytes(2 * 1024 ** 3), '2.00 GB');
});

test('capacityState separates logical estimate from the rolling ceiling', () => {
  const state = View.capacityState({
    logical: { bytes: 29879664, records: 2127 }, capacity_bytes: 1073741824,
    over_capacity: false, preview: { candidates: 0, candidate_bytes: 0 }
  });
  assert.equal(state.state, 'ready');
  assert.equal(state.over, false);
  assert.ok(Math.abs(state.ratio - 0.02783) < 0.01);
  assert.equal(state.candidates, 0);
  assert.equal(View.capacityState(null).state, 'pending');
});

test('queueTone reports busy, failed and idle from real counts', () => {
  assert.equal(View.queueTone({ pending: 2, running: 0, failed: 0 }), 'busy');
  assert.equal(View.queueTone({ pending: 0, running: 1, failed: 0 }), 'busy');
  assert.equal(View.queueTone({ pending: 0, running: 0, failed: 1 }), 'warning');
  assert.equal(View.queueTone({ pending: 0, running: 0, failed: 0 }), 'idle');
  assert.equal(View.queueTone(null), 'unknown');
});

test('inspection titles use real content instead of project paths or boilerplate', () => {
  assert.equal(View.titleOf({ content: '## Request\n\nFix the parser\n\n## Outcome', displayName: '/tmp/project' }), 'Fix the parser');
  assert.equal(View.titleOf({ content: '# RouteSelectionPolicy\nDetails' }), 'RouteSelectionPolicy');
  assert.equal(View.titleOf({ uri: 'viking://user/default/memories/entities/component.md' }), 'component.md');
  assert.equal(View.previewOf({ preview: '## Request Fix the parser ## Outcome Passed' }), 'Fix the parser  Passed');
  assert.equal(View.capacityState({ logical: { bytes: 0 }, capacity_bytes: 1024 }).ratio, 0);
});

test('every memory view translation key exists in the English bundle', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'memory.js'), 'utf8');
  const i18n = fs.readFileSync(path.join(__dirname, '..', 'web', 'i18n.js'), 'utf8');
  const keys = new Set();
  const pattern = /tr\('([^']+)'/g;
  let match;
  while ((match = pattern.exec(source))) {
    const key = match[1];
    if (key.startsWith('memory.') && !key.endsWith('.')) keys.add(key);
  }
  // Constructed keys are checked for each real branch value.
  ['memory.tab.l0', 'memory.tab.l1', 'memory.tab.l2',
   'memory.commit.completed', 'memory.commit.running', 'memory.commit.pending',
   'memory.commit.failed', 'memory.commit.cancelled', 'memory.commit.unknown',
   'memory.source.legacy', 'memory.source.bridge', 'memory.source.openviking',
   'memory.sort.newest', 'memory.sort.oldest', 'memory.sort.name', 'memory.sort.size'
  ].forEach(key => keys.add(key));
  for (const key of keys) assert.ok(i18n.includes("'" + key + "'"), 'missing translation ' + key);
});

test('every memory view element id exists in the console page', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'memory.js'), 'utf8');
  const page = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
  const ids = new Set();
  const pattern = /\$\('([a-zA-Z-]+)'\)/g;
  let match;
  while ((match = pattern.exec(source))) ids.add(match[1]);
  assert.ok(ids.size > 10);
  for (const id of ids) assert.ok(page.includes('id="' + id + '"'), 'missing element #' + id);
});
