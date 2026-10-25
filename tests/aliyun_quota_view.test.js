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
