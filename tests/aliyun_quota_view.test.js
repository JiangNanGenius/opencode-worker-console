'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync(require('node:path').join(__dirname,'../web/app.js'),'utf8');
const context = vm.createContext({tr:key=>key,clock:value=>String(value),
  TaskView:{epochSeconds:value=>typeof value==='number'?value:null}});
for (const name of ['alibabaQuotaBalance','alibabaQuotaStamp']) {
  const definition = source.match(new RegExp('^function '+name+'\\(q\\).*$', 'm'));
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
