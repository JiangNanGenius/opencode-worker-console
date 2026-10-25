'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/manage.js'), 'utf8');
const pure = source.slice(source.indexOf('function defaultLadder('), source.indexOf('function captureRoutingEditor('));

function stage(profiles, curve, enabled = true) {
  return {
    querySelectorAll: () => profiles.map(profile => ({ querySelector: selector => ({value: selector.includes('profile') ? profile : '1'}) })),
    querySelector: selector => selector.includes('adaptive') ? {checked: enabled} : selector.includes('curve-strength') ? (curve == null ? null : {value: String(curve)}) : {value: '2:1, 1:1'}
  };
}
function read(stages) {
  const context = {tr: key => key, $: id => id === 'routing-policy-enabled' ? {checked: true} : {
    querySelector: selector => ({querySelectorAll: () => stages[selector.match(/"(.*?)"/)[1]] || []})
  }};
  vm.createContext(context); vm.runInContext(pure, context);
  return JSON.parse(JSON.stringify(context.readRoutingEditor()));
}
test('saving unrelated settings preserves three-provider and two-provider curves', () => {
  const [policy, dynamics] = read({fast: [stage(['fallback'], null, false)], background: [stage(['kimi', 'aliyun', 'ark'], 2)], deep: [stage(['kimi-k3', 'aliyun-max'], 3)]});
  assert.equal(policy.fast[0][0].profile, 'fallback');
  assert.equal(policy.background[0].length, 3);
  assert.deepEqual(dynamics, {background: {'0': {curve: {strength: 2}}}, deep: {'0': {curve: {strength: 3}}}});
});
test('legacy pair ladders remain ladders and explicit disable removes only that curve', () => {
  const [, dynamics] = read({background: [stage(['kimi', 'ark'], null)], deep: [stage(['kimi', 'aliyun', 'ark'], 2, false)]});
  assert.deepEqual(dynamics, {background: {'0': {ladder: [[2, 1], [1, 1]]}}});
});
test('saving other routing settings preserves the dedicated Ultra stage', () => {
  const [policy, dynamics] = read({fast:[stage(['fallback'],null,false)],ultra:[stage(['ultra-reasoning'],null,false)]});
  assert.deepEqual(policy.ultra,[[{profile:'ultra-reasoning',weight:1}]]);
  assert.equal(dynamics.ultra,undefined);
});
