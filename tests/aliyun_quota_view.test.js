'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync(require('node:path').join(__dirname,'../web/app.js'),'utf8');
const context = vm.createContext({tr:key=>key,clock:value=>String(value),esc:value=>String(value),I18n:null,
  expiryTime:value=>'expiry:'+String(value),resetTime:value=>'reset:'+String(value),
  TaskView:{epochSeconds:value=>typeof value==='number'?value:(typeof value==='string'&&value?Date.parse(value)/1000:null)}});
for (const name of ['alibabaQuotaBalance','alibabaQuotaStamp','alibabaAddonNote','quotaWindowTiming']) {
  const definition = source.match(new RegExp('^function '+name+'\\(.*$', 'm'));
  assert.ok(definition, name+' is available');
  vm.runInContext(definition[0],context);
}

test('live API Credits are displayed without requiring a manual snapshot', () => {
  const value={source:'openapi_live',sampled_at:123,windows:[
    {valid:true,remaining:80000,remaining_percent:80}]};
  assert.equal(context.alibabaQuotaBalance(value),80000);
  assert.match(context.alibabaQuotaStamp(value),/quota.liveApi/);
});

test('last live value wins over an older manual anchor, including genuine zero', () => {
  assert.equal(context.alibabaQuotaBalance({windows:[{valid:true,remaining:0,remaining_percent:0}],
    snapshot:{remaining:99000}}),0);
  assert.equal(context.alibabaQuotaBalance({snapshot:{remaining:1234}}),1234);
  assert.equal(context.alibabaQuotaBalance({windows:[{valid:false,remaining:999,remaining_percent:null}]}),null);
});

test('failed refresh preserves the original timestamp and shows update status', () => {
  const stamp=context.alibabaQuotaStamp({source:'openapi_live',sampled_at:123,live_error:{state:'auth_error'}});
  assert.match(stamp,/123/);
  assert.match(stamp,/quota.refreshErrorShort/);
  assert.match(context.alibabaQuotaStamp({state:'no_credential'}),/quota.aliyunAuthNeeded/);
});

// Prepaid seat Credits have an expiry, but no target to last until that date.
test('prepaid pace displays availability rather than renewal sufficiency', () => {
  const w={remaining_percent:72,capacity_forecast:{runway:1}};
  const scope=vm.createContext({data:{quota:{ali:{prepaid:true,windows:[w]}}},
    tr:key=>key,windowRunway:()=>1});
  vm.runInContext(source.match(/^function quotaPace\(w\).*$/m)[0],scope);
  assert.equal(scope.quotaPace(w)[1],'quota.state.available');
  w.remaining_percent=0;
  assert.equal(scope.quotaPace(w)[1],'quota.paceExhausted');
});


test('personal percentage and addon do not masquerade as total Credits', () => {
  const value={plan_edition:'personal',source:'personal_console',sampled_at:123,
    windows:[{name:'Monthly',unit:'percent',remaining_percent:100,valid:true},
      {name:'Addon',remaining:5000,limit:20000,remaining_percent:25,valid:true}]};
  assert.equal(context.alibabaQuotaBalance(value),null);
  assert.match(context.alibabaQuotaStamp(value),/quota.liveApi.*123/);
  assert.doesNotMatch(context.alibabaQuotaStamp({plan_edition:'personal',source:'personal_console',
    state:'unknown',live_error:{kind:'quota_auth'}}),/quota.liveApi/);
});


test('failed or stale personal records still surface an explicit addon unknown notice', () => {
  const stale={state:'unknown',plan_edition:'personal',
    addon:{known:true,remaining:100,limit:200,used:100,active_count:1,remaining_percent:50}};
  assert.match(context.alibabaAddonNote(stale),/quota.addonUnknown/);
  const retained={state:'ok',plan_edition:'personal',stale:true,
    addon:{known:true,remaining:100,limit:200,used:100,active_count:1,remaining_percent:50}};
  assert.match(context.alibabaAddonNote(retained),/quota.addonUnknown/);
  const failed={state:'ok',plan_edition:'personal',live_error:{kind:'quota_auth'},
    addon:{known:true,remaining:100,limit:200,used:100,active_count:1,remaining_percent:50}};
  assert.match(context.alibabaAddonNote(failed),/quota.addonUnknown/);
  const empty={state:'ok',plan_edition:'personal',
    addon:{known:true,remaining:0,limit:200,used:200,active_count:0,remaining_percent:0}};
  assert.equal(context.alibabaAddonNote(empty),'');
});

test('active addon window shows nearest expiry instead of a reset unknown', () => {
  assert.match(context.quotaWindowTiming({name:'Addon',nearest_expires_at:'2026-11-28T16:00:00+00:00'}),
    /quota.addonNearestExpiry/);
  assert.match(context.quotaWindowTiming({name:'Addon',expires_at:1795881600}),
    /quota.addonNearestExpiry/);
  assert.match(context.quotaWindowTiming({name:'Addon'}),/quota.addonExpiryUnknown/);
  assert.match(context.quotaWindowTiming({name:'Monthly',resets_at:123}),/reset:123/);
});

test('addon stats stay visible while the monthly base is positive and never sum units', () => {
  const value={state:'ok',plan_edition:'personal',source:'personal_console',sampled_at:123,
    addon:{known:true,unit:'credits',remaining:19643.114675,limit:20000,used:356.885325,
      active_count:1,remaining_percent:98.215573375,nearest_expires_at:'2026-11-28T16:00:00+00:00'},
    windows:[{name:'Monthly',unit:'percent',remaining_percent:97.5,valid:true}]};
  const html=context.alibabaAddonNote(value);
  assert.match(html,/quota.addonPool/);
  assert.match(html,/Credits/);
  assert.match(html,/quota.addonUsed/);
  assert.match(html,/quota.addonPacks/);
  assert.match(html,/quota.addonNearestExpiry/);
  assert.doesNotMatch(html,/quota.addonExpiryUnknown/);
});

test('known exhausted addon disappears from the current card', () => {
  const value={state:'ok',addon:{known:true,remaining:0,limit:20000,used:20000,
    active_count:0,remaining_percent:0},windows:[]};
  assert.equal(context.alibabaAddonNote(value),'');
});

test('unknown or failed addon query is shown as unknown, never zero', () => {
  const value={state:'ok',addon:{known:false},windows:[]};
  assert.match(context.alibabaAddonNote(value),/quota.addonUnknown/);
  assert.equal(context.alibabaAddonNote({state:'ok',windows:[]}),'');
  assert.equal(context.alibabaAddonNote({state:'unknown'}),'');
});

test('inconsistent nearest expiry is flagged unknown without hiding observed credits', () => {
  const html=context.alibabaAddonNote({state:'ok',addon:{known:true,remaining:500,limit:20000,
    used:19500,active_count:3,remaining_percent:2.5,inconsistent:true},windows:[]});
  assert.match(html,/quota.addonInconsistent/);
  assert.match(html,/Credits/);
});

test('addon without a supplied nearest expiry says unknown instead of inventing one', () => {
  const html=context.alibabaAddonNote({state:'ok',addon:{known:true,remaining:5,limit:10,
    used:5,active_count:1,remaining_percent:50},windows:[]});
  assert.match(html,/quota.addonExpiryUnknown/);
});

test('each sequential plan renders only its own addon metadata', () => {
  const seen=[];
  const scope=vm.createContext({tr:key=>key,esc:value=>String(value),
    alibabaQuotaStamp:q=>q.source||'unknown',quotaWindows:()=> 'window',
    alibabaAddonNote:q=>{seen.push(q.addon||null);return 'addon';}});
  vm.runInContext(source.match(/^function alibabaPlanWindows\(q\).*$/m)[0],scope);
  const addon={known:true,remaining:10,limit:100,used:90,active_count:1,remaining_percent:10};
  const value={plan_edition:'sequential',active_plan:'team',windows:[],
    plan_quotas:{team:{source:'openapi_live',windows:[]},
      personal:{source:'personal_console',windows:[],addon}}};
  scope.alibabaPlanWindows(value);
  assert.deepEqual(seen,[null,addon]);
});

test('sequential plans render Team first and Personal standby without combining units', () => {
  const seen=[];
  const scope=vm.createContext({tr:key=>key,esc:value=>String(value),
    alibabaQuotaStamp:q=>q.source||'unknown',quotaWindows:rows=>{seen.push(rows);return 'window';}});
  vm.runInContext(source.match(/^function alibabaPlanWindows\(q\).*$/m)[0],scope);
  const team=[{name:'Credits(max)',remaining:2000,remaining_percent:1,valid:true}];
  const personal=[{name:'Monthly',unit:'percent',remaining_percent:99,valid:true}];
  const value={plan_edition:'sequential',active_plan:'team',
    windows:[{remaining:999999,remaining_percent:100,valid:true}],
    plan_quotas:{team:{source:'openapi_live',windows:team},personal:{source:'personal_console',windows:personal}}};
  const html=scope.alibabaPlanWindows(value);
  assert.ok(html.indexOf('quota.teamFirst')<html.indexOf('quota.personalStandby'));
  assert.match(html,/quota.planActive/);
  assert.deepEqual(seen,[team,personal]);
  assert.equal(context.alibabaQuotaBalance(value),null);
});


test('terminal plans are omitted from the sequential card, live siblings stay', () => {
  const seen=[];
  const scope=vm.createContext({tr:key=>key,esc:value=>String(value),
    alibabaQuotaStamp:q=>q.source||'unknown',quotaWindows:rows=>{seen.push(rows);return 'window';}});
  vm.runInContext(source.match(/^function alibabaPlanWindows\(q\).*$/m)[0],scope);
  const team=[{name:'Credits(max)',remaining:0,remaining_percent:0,valid:true,display_hidden:true}];
  const personal=[{name:'Monthly',unit:'percent',remaining_percent:97.48,valid:true}];
  const value={plan_edition:'sequential',active_plan:'personal',windows:[],
    plan_quotas:{team:{source:'openapi_live',display_hidden:true,windows:team},
      personal:{source:'personal_console',display_hidden:false,windows:personal}}};
  const html=scope.alibabaPlanWindows(value);
  assert.doesNotMatch(html,/quota.teamFirst/);
  assert.match(html,/quota.personalStandby/);
  assert.deepEqual(seen,[personal]);

  value.plan_quotas.personal.display_hidden=true;
  assert.equal(scope.alibabaPlanWindows(value),'');
});

test('Team exhaustion is not total exhaustion while Personal remains available', () => {
  const scope=vm.createContext({tr:key=>key,quotaPace:()=>['on-track','ready']});
  vm.runInContext(source.slice(source.indexOf('function planQuotaState('),source.indexOf('function balanceQuotaState(')),scope);
  const value={state:'ok',plan_edition:'sequential',available:true,windows:[],
    plan_quotas:{team:{available:false,windows:[{remaining_percent:0,valid:true}]},
      personal:{available:true,windows:[{remaining_percent:99,valid:true}]}}};
  assert.equal(scope.planQuotaState(value).tone,'healthy');
  assert.equal(scope.planQuotaState({...value,available:false}).tone,'critical');
  assert.equal(scope.planQuotaState({...value,available:null,stale:true}).tone,'unknown');
});
