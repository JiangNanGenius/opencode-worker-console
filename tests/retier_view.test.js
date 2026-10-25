'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const View = require('../web/task-view.js');
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const display = {
  esc, clock: value => String(value ?? ''),
  tr: (key, vars = {}) => key + (vars.tier ? ':' + vars.tier : '')
};

test('a pending lower tier does not replace the observed effective tier or current model', () => {
  const task = {tier:'deep',requested_tier:'fast',current_model:'provider/current',
    tier_change:{request_id:'phase-2',from_tier:'deep',to_tier:'fast',status:'pending'}};
  const html = View.tierHTML(task, display);
  assert.match(html,/tier\.current:tier\.deep/);
  assert.match(html,/tier\.change\.pending:tier\.fast/);
  assert.doesNotMatch(html,/tier\.change\.applied/);
  assert.equal(View.currentModel(task),'provider/current');
  assert.equal(task.tier,'deep');
});

test('uncertain and rejected requests never appear applied', () => {
  for (const status of ['uncertain','rejected']) {
    const html = View.tierHTML({tier:'normal',tier_change:{to_tier:'deep',status}},display);
    assert.match(html,new RegExp('tier\\.change\\.'+status));
    assert.doesNotMatch(html,/tier\.change\.applied/);
  }
});

test('applied tier and history are distinct from requested changes and safe for untrusted reasons', () => {
  const task = {tier:'fast',tier_change:{request_id:'phase-2',from_tier:'deep',to_tier:'fast',status:'applied',applied_at:9,reason:'<img onerror="bad()">'},
    tier_history:[{request_id:'phase-1',from_tier:'normal',to_tier:'deep',status:'applied',created_at:1},
      {request_id:'phase-2',from_tier:'deep',to_tier:'fast',status:'pending',created_at:2}]};
  const html = View.tierHistoryHTML(task, display);
  assert.equal((html.match(/<li>/g)||[]).length,2);
  assert.ok(html.indexOf('tier.deep → tier.fast')<html.indexOf('tier.normal → tier.deep'));
  assert.match(html,/&lt;img onerror=&quot;bad\(\)&quot;&gt;/);
  assert.doesNotMatch(html,/<img/);
  assert.match(html,/<time>9<\/time>/);
  assert.doesNotMatch(html,/tier\.state\.pending/);
  assert.match(html,/data-section="tiers"/);
});

test('legacy tasks and invalid tier data do not fabricate a pending change', () => {
  assert.equal(View.tierChangeHTML({requested_tier:'fast'},display),'');
  assert.equal(View.tierHistoryHTML({},display),'');
  assert.equal(View.tierHTML({tier:'<script>',tier_change:{to_tier:'fast',status:'fake'}},display),'');
  assert.equal(View.tierChangeHTML({tier_change:{to_tier:'<img>',status:'pending'}},display),'');
});
