'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const View = require('../web/host.js');

test('host meters represent the quantity without CSP-blocked style attributes', () => {
  for (const percent of [0, 2.5, 10, 50, 100]) {
    const html = View.meterHTML('hostov-track', percent, 'quota:test', '', 'Allowance');
    assert.match(html, new RegExp('aria-valuenow="' + percent + '"'));
    assert.match(html, new RegExp('data-meter-percent="' + percent + '"'));
    assert.doesNotMatch(html, /\sstyle=/);
  }
  assert.equal(View.meterHTML('hostov-track', null, 'unknown'), '');
  assert.equal(View.meterHTML('hostov-track', NaN, 'unknown'), '');
  assert.match(View.meterHTML('hostov-track', 120, 'over'), /aria-valuenow="100"/);
  assert.match(View.meterHTML('hostov-track', -1, 'under'), /aria-valuenow="0"/);
});

test('live update starts at the visible fraction and transitions to the new value', () => {
  const oldFill = {dataset: {hostMeter:'quota:test'},
    parentElement:{getBoundingClientRect:()=>({width:200})},
    getBoundingClientRect:()=>({width:80})};
  const nextFill = {dataset:{hostMeter:'quota:test',meterPercent:'25'},style:{},isConnected:true};
  let replaced = false;
  const container = {
    querySelectorAll:()=>replaced ? [nextFill] : [oldFill],
    set innerHTML(value) {replaced = true;},
    get offsetWidth() {return 200;}
  };
  const original = global.requestAnimationFrame;
  let frame;
  global.requestAnimationFrame = callback => {frame = callback;};
  try {
    View.renderMeterHTML(container, 'new markup');
    assert.equal(nextFill.style.transform,'scaleX(0.4)');
    frame();
    assert.equal(nextFill.style.transform,'scaleX(0.25)');
  } finally {
    if (original) global.requestAnimationFrame = original;
    else delete global.requestAnimationFrame;
  }
});

test('missing disk use and missing task progress never render full meters', () => {
  assert.doesNotMatch(View.diskRowHTML({name:'unknown',used_percent:null}), /hostov-disk-bar/);
  assert.doesNotMatch(View.taskRowHTML({title:'unknown',progress:{percent:'invalid'}}), /hostov-taskbar/);
});
