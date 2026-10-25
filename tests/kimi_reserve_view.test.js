'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const Account=require('../web/kimi-account-view.js');
const source=fs.readFileSync(path.join(__dirname,'../web/manage.js'),'utf8');
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const reset='2026-10-27T03:47:50Z';
function translations(locale){const context={window:{navigator:{languages:[locale]},document:null}};vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/i18n.js'),'utf8'),context);return context.window.I18n.t;}
function policy(overrides={}){return {reserve_percent:30,effective_reserve_percent:17.5,released_percent:12.5,emergency_reserve_percent:5,dynamic_enabled:true,release_state:'releasing',remaining_percent:15.38,resets_at:reset,remaining_hours:42,...overrides};}
test('policy readouts use backend thresholds and released percentage points, never a quota balance',()=>{
 const tr=translations('en');
 const html=Account.weeklyReserveHTML(policy(),{tr,esc,time:()=> 'weekly reset'});
 assert.match(html,/Base \/ maximum/);assert.match(html,/Current protection/);
 assert.match(html,/30%/);assert.match(html,/17.5%/);assert.match(html,/12.5 percentage points/);
 assert.match(html,/not the available quota balance/);assert.doesNotMatch(html,/15.38/);
 assert.match(html,/Next weekly reset: weekly reset/);
 const changed=Account.weeklyReserveHTML(policy({remaining_percent:80,effective_reserve_percent:12.14,released_percent:17.86,remaining_hours:24}),{tr,esc});
 assert.match(changed,/12.14%/);assert.match(changed,/17.86 percentage points/);assert.doesNotMatch(changed,/80%/);
});
test('pause, first-half and disabled states remain explicit in Chinese and missing fields stay unknown',()=>{
 const tr=translations('zh-CN');
 for(const state of ['fixed','first_half','ultra_demand','weekly_unknown','reset_unknown','demand_unknown']){
  const html=Account.weeklyReserveHTML(policy({effective_reserve_percent:30,released_percent:0,release_state:state}),{tr,esc});
  assert.match(html,/30%/);assert.match(html,/0 个百分点/);assert.doesNotMatch(html,/kimiReserve\./);
  if(state.includes('unknown')||state==='ultra_demand')assert.match(html,/暂停释放/);
 }
 assert.equal(Account.weeklyReserveHTML(undefined,{tr,esc}),'');
 const missing=Account.weeklyReserveHTML({release_state:'reset_unknown',expires_at:'2026-11-18T06:47:51Z'},{tr,esc,time:()=> 'MONTHLY EXPIRY'});
 assert.doesNotMatch(missing,/NaN|null|0%|MONTHLY EXPIRY/);assert.match(missing,/周刷新未知/);
});
test('management defaults dynamic release on, preserves explicit false and saves a boolean with the base',()=>{
 const cells={'kimi-reserve':{value:''},'kimi-reserve-dynamic':{checked:false}};
 const context={$:id=>cells[id]};vm.createContext(context);
 vm.runInContext(source.slice(source.indexOf('function loadKimiWeeklyReserveSettings('),source.indexOf('function renderRouting(')),context);
 context.loadKimiWeeklyReserveSettings({});
 assert.equal(cells['kimi-reserve'].value,30);assert.equal(cells['kimi-reserve-dynamic'].checked,true);
 context.loadKimiWeeklyReserveSettings({kimi_weekly_reserve_percent:30,kimi_weekly_reserve_dynamic:false});
 assert.equal(cells['kimi-reserve-dynamic'].checked,false);
 cells['kimi-reserve'].value='35';
 const payload=JSON.parse(JSON.stringify(context.kimiWeeklyReserveSettingsPayload()));
 assert.deepEqual(payload,{kimi_reserve_percent:35,kimi_weekly_reserve_percent:35,kimi_weekly_reserve_dynamic:false});
 cells['kimi-reserve-dynamic'].checked=true;
 assert.equal(context.kimiWeeklyReserveSettingsPayload().kimi_weekly_reserve_dynamic,true);
});
test('account refresh renders current policy, then a later refresh reflects paused release without deriving a reset from expiry',async()=>{
 const cells=new Map(),get=id=>{if(!cells.has(id))cells.set(id,{textContent:'',innerHTML:'',hidden:false,disabled:false});return cells.get(id);};
 let next=policy(),requests=0;
 const payload=()=>({status:{authenticated:true},usage:{state:'ok',windows:[],account_binding:{state:'verified'}},weekly_reserve:next});
 const context={window:{KimiAccountView:Account},KimiAccountView:Account,$:get,I18n:null,esc,tr:translations('en'),recordTime:()=> 'weekly reset',resetTime:()=> 'reset',quotaTrack:()=>'',request:async()=>{requests++;return payload();},Promise};
 vm.createContext(context);vm.runInContext(source.slice(source.indexOf('let kimiAccountLoginTimer='),source.indexOf('function showKimiAccountLogin(')),context);
 await context.loadKimiAccount();
 assert.equal(get('kimi-weekly-reserve').hidden,false);assert.match(get('kimi-weekly-reserve').innerHTML,/17.5%/);
 next=policy({effective_reserve_percent:30,released_percent:0,release_state:'ultra_demand'});
 await context.loadKimiAccount();
 assert.equal(requests,2);assert.match(get('kimi-weekly-reserve').innerHTML,/Release paused: Ultra/);assert.match(get('kimi-weekly-reserve').innerHTML,/0 percentage points/);assert.doesNotMatch(get('kimi-weekly-reserve').innerHTML,/17.5%/);
 context.renderKimiWeeklyReserve(undefined);assert.equal(get('kimi-weekly-reserve').hidden,true);
});
test('the real settings save handler includes the dynamic-release boolean',async()=>{
 const cells=new Map(),get=id=>{if(!cells.has(id))cells.set(id,{value:'1',checked:false,disabled:false,textContent:''});return cells.get(id);};
 let saved;
 const context={$:get,modelSettings:{revision:2},tr:key=>key,readRoutingEditor:()=>[{},{}],readProfiles:()=>({}),readBudgetSignals:()=>({}),mutate:async(path,body)=>{saved=body;return body;},loadSettings:async()=>{},refresh:async()=>{}};
 vm.createContext(context);
 vm.runInContext(source.slice(source.indexOf('function loadKimiWeeklyReserveSettings('),source.indexOf('function renderRouting(')),context);
 vm.runInContext(source.slice(source.indexOf("$('save-settings').onclick="),source.indexOf("$('new-task').onclick=")),context);
 get('kimi-reserve').value='30';get('kimi-reserve-dynamic').checked=false;
 await get('save-settings').onclick();
 assert.equal(saved.kimi_weekly_reserve_percent,30);assert.equal(saved.kimi_weekly_reserve_dynamic,false);
 get('kimi-reserve-dynamic').checked=true;
 await get('save-settings').onclick();
 assert.equal(saved.kimi_weekly_reserve_dynamic,true);assert.equal(get('save-settings').disabled,false);
});
test('a plan without a verified weekly limit explicitly marks weekly protection not applicable',()=>{
 const record=policy({release_state:'not_applicable',effective_reserve_percent:0,released_percent:0});
 assert.equal(Account.weeklyReserve(record).state,'not_applicable');
 const html=Account.weeklyReserveHTML(record,{tr:translations('zh-CN'),esc});
 assert.match(html,/该套餐无周额度限制，周预留不适用/);
 assert.doesNotMatch(html,/30%|kimiReserve\.state\.unknown|保持基础|下次周刷新/);
});
