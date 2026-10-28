'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const Account=require('../web/kimi-account-view.js');
const source=fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
const NOW=Date.parse('2026-10-25T06:00:00Z');
const EXPIRY='2026-11-18T06:47:51Z';

function endpointWindows(){
  return [
    {name:'limit5h',remaining_percent:94,remaining:0.94,limit:1,duration_minutes:300,resets_at:'2026-10-25T08:00:00Z',valid:true},
    {name:'limit7d',remaining_percent:10,remaining:0.1,limit:1,duration_minutes:10080,resets_at:'2026-10-27T03:47:50Z',valid:true}
  ];
}
function accountRecord(overrides={}){
  return {
    state:'ok',source:'kimi_code_account',sampled_at:NOW/1000,stale:false,
    account_binding:{state:'verified'},weekly_applicability:'applicable',
    windows:[
      {name:'monthTotal',kind:'month_total',remaining_percent:65.66,remaining:0.6566,limit:1,resets_at:null,expires_at:EXPIRY,valid:true,source:'kimi_web_membership'},
      {name:'limit7d',kind:'weekly',remaining_percent:9.876,duration_minutes:10080,resets_at:'2026-10-27T03:47:50Z',valid:true},
      {name:'limit5h',kind:'short',remaining_percent:94.2,duration_minutes:300,resets_at:'2026-10-25T08:00:00Z',valid:true}
    ],
    ...overrides
  };
}
function cardContext(){
  const context=vm.createContext({
    I18n:null,data:{quota:{},tier_guidance:{}},
    tr:(key,variables)=>variables?key+'('+Object.values(variables).join('|')+')':key,
    esc:value=>String(value??''),fixed2:value=>Number(value).toFixed(2),number:value=>String(value),
    window:{KimiAccountView:Account}
  });
  for(const name of ['clock','resetDate','resetCountdown','resetTime','accountPercent','expiryTime',
    'kimiTotalMissing','kimiCardWindows','estimatedRange','windowRunway','expectedRemaining',
    'quotaPace','quotaTrack','windowName','quotaWindowTiming','quotaWindows']){
    const definition=source.match(new RegExp('^function '+name+'\\(.*$','m'));
    assert.ok(definition,name+' is available in app.js');
    vm.runInContext(definition[0],context);
  }
  return context;
}
function rowFor(html,labelKey){
  const index=html.indexOf(labelKey);
  assert.ok(index>=0,labelKey+' row is rendered');
  const start=html.lastIndexOf('<article',index),end=html.indexOf('</article>',index);
  return html.slice(start,end);
}

test('verified fresh account total leads the card and deduplicates weekly and five-hour windows',()=>{
  const merged=Account.kimiQuotaWindows(endpointWindows(),accountRecord(),NOW);
  assert.deepEqual(merged.map(w=>w.kind),['month_total','short','weekly']);
  assert.equal(merged[0].remaining_percent,65.66);
  assert.equal(merged[0].account_window,true);
  assert.equal(merged[0].account_stale,false);
  assert.equal(merged.filter(w=>w.kind==='weekly').length,1);
  assert.equal(merged.find(w=>w.kind==='weekly').remaining_percent,9.876);
  assert.equal(merged.find(w=>w.kind==='short').remaining_percent,94.2);
});

test('card shows the real total with two decimals, an explicit expiry and the account sample',()=>{
  const context=cardContext();
  const merged=context.kimiCardWindows(endpointWindows(),accountRecord({sampled_at:Date.now()/1000}));
  assert.equal(context.kimiTotalMissing(merged),false);
  const html=context.quotaWindows(merged);
  const total=rowFor(html,'quota.accountTotal');
  assert.match(total,/65\.66%/);
  assert.doesNotMatch(total,/<strong>66%<\/strong>/);
  assert.match(total,/quota\.expiresAt/);
  assert.doesNotMatch(total,/quota\.resetBeijing|quota\.resetDual|reset\.unknown/);
  assert.match(total,/quota\.sampleSuffix/);
  assert.doesNotMatch(total,/quota\.staleSuffix/);
  assert.match(total,/quota\.state\.available/);
  assert.doesNotMatch(total,/quota\.paceUnknown|quota\.paceCritical|quota\.paceTight/);
  assert.equal((html.match(/<span>quota\.weekly<\/span>/g)||[]).length,1);
});

test('stale totals stay visible but explicitly stale, without overriding the endpoint windows',()=>{
  const stale=accountRecord({stale:true});
  const merged=Account.kimiQuotaWindows(endpointWindows(),stale,NOW);
  const total=merged.find(w=>w.kind==='month_total');
  assert.equal(total.account_stale,true);
  assert.equal(merged.find(w=>w.name==='limit7d').remaining_percent,10);
  const html=cardContext().quotaWindows(merged);
  const row=rowFor(html,'quota.accountTotal');
  assert.match(row,/quota\.staleSuffix/);
  assert.match(row,/hostov\.quotaStale/);
  assert.doesNotMatch(row,/quota\.state\.available/);
  assert.match(html,/65\.66%/);
});

test('an unverified or mismatched binding and an unavailable account never merge account windows',()=>{
  for(const record of [accountRecord({account_binding:{state:'unverified'}}),
    accountRecord({account_binding:{state:'mismatch'}}),accountRecord({state:'unavailable'}),null]){
    const merged=Account.kimiQuotaWindows(endpointWindows(),record,NOW);
    assert.deepEqual(merged.map(w=>w.name),['limit5h','limit7d']);
    assert.equal(cardContext().kimiTotalMissing(merged),true);
  }
});

test('missing total reports the existing totalMissing label while a real zero total stays zero',()=>{
  const context=cardContext();
  const withoutTotal=accountRecord({windows:accountRecord().windows.filter(w=>w.kind!=='month_total')});
  const missing=context.kimiCardWindows(endpointWindows(),withoutTotal);
  assert.equal(context.kimiTotalMissing(missing),true);
  assert.equal(context.kimiTotalMissing([]),true);
  assert.match(context.quotaWindows(missing),/quota\.fiveHour/);
  assert.equal(context.kimiTotalMissing([{name:'monthTotal',remaining_percent:0}]),false);

  const zero=accountRecord({windows:[{kind:'month_total',remaining_percent:0,expires_at:EXPIRY,valid:true}]});
  const merged=context.kimiCardWindows(endpointWindows(),zero);
  assert.equal(context.kimiTotalMissing(merged),false);
  const html=context.quotaWindows(merged);
  assert.match(html,/>0\.00%</);
  assert.match(html,/quota\.paceExhausted/);
  assert.doesNotMatch(rowFor(html,'quota.accountTotal'),/quota\.state\.available/);
});

test('old, missing and future account samples keep the total visible but explicitly stale',()=>{
  const context=cardContext();
  for(const sampled of ['2026-10-20T00:00:00Z',undefined,Date.parse('2026-10-27T00:00:00Z')/1000]){
    const merged=context.kimiCardWindows(endpointWindows(),accountRecord({sampled_at:sampled}));
    assert.equal(merged.find(w=>w.kind==='month_total').account_stale,true);
    const row=rowFor(context.quotaWindows(merged),'quota.accountTotal');
    assert.match(row,/65\.66%/);
    assert.match(row,/hostov\.quotaStale/);
    assert.match(row,/quota\.staleSuffix/);
    assert.doesNotMatch(row,/quota\.state\.available/);
  }
});

test('invalid, expired and past-reset account windows are dropped instead of fabricated',()=>{
  const bad=accountRecord({windows:[
    {kind:'month_total',remaining_percent:65,valid:false},
    {kind:'month_total',remaining_percent:Number.NaN},
    {kind:'month_total',remaining_percent:101},
    {kind:'month_total',remaining_percent:65,expires_at:'2026-10-01T00:00:00Z'},
    {kind:'month_total',remaining_percent:65,resets_at:'2026-10-01T00:00:00Z'},
    {kind:'month_total',remaining_percent:65,expired:true},
    {kind:'weekly',remaining_percent:50,valid:false}
  ]});
  const merged=Account.kimiQuotaWindows(endpointWindows(),bad,NOW);
  assert.equal(merged.some(w=>w.kind==='month_total'),false);
  assert.equal(merged.find(w=>w.name==='limit7d').remaining_percent,10);
});

test('both monthly pools lead the card in order and deduplicate by kind',()=>{
  const record=accountRecord({windows:[
    {kind:'month_total',remaining_percent:65.66,expires_at:EXPIRY,valid:true},
    {kind:'month_code',remaining_percent:12.5,resets_at:'2026-11-01T00:00:00Z',valid:true},
    {kind:'month_total',remaining_percent:64,expires_at:EXPIRY,valid:true},
    {kind:'weekly',remaining_percent:9.876,duration_minutes:10080,resets_at:'2026-10-27T03:47:50Z',valid:true}
  ]});
  const merged=Account.kimiQuotaWindows(endpointWindows(),record,NOW);
  assert.deepEqual(merged.slice(0,2).map(w=>w.kind),['month_total','month_code']);
  assert.equal(merged.filter(w=>w.kind==='month_total').length,1);
  assert.equal(merged.find(w=>w.kind==='month_total').remaining_percent,64);
  const html=cardContext().quotaWindows(merged);
  assert.match(rowFor(html,'quota.codeMonthly'),/quota\.resetBeijing|quota\.resetDual/);
});

test('non-Kimi windows keep integer rendering and no account-only markup',()=>{
  const html=cardContext().quotaWindows([
    {name:'AFPFiveHour',remaining_percent:94.6,duration_minutes:300,resets_at:'2026-10-25T08:00:00Z',valid:true}
  ]);
  assert.match(html,/95%/);
  assert.doesNotMatch(html,/94\.6%/);
  assert.doesNotMatch(html,/account-window|quota\.sampleSuffix|quota\.expiresAt/);
});
