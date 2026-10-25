'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const manage = fs.readFileSync(path.join(__dirname, '../web/manage.js'), 'utf8');
const i18n = fs.readFileSync(path.join(__dirname, '../web/i18n.js'), 'utf8');

test('prepaid is exposed in the existing budget dropdown without extra fields', () => {
  assert.match(manage, /\[['"]prepaid['"],\s*['"]budget\.prepaid['"]\]/);
  assert.match(manage, /mode==='ignore'\|\|mode==='prepaid'\)\{result\[provider\]=\{mode\};continue;\}/);
});

test('prepaid label is present in all five existing languages', () => {
  assert.equal((i18n.match(/'budget\.prepaid'/g) || []).length, 5);
  for (const label of ['Prepaid refillable credit', '可充值的预付额度', '可儲值的預付額度',
                       'チャージ可能な前払いクレジット', '충전 가능한 선불 크레딧']) {
    assert.ok(i18n.includes(label), label);
  }
});
