'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const Account=require('../web/kimi-account-view.js');
const View=require('../web/task-view.js');
const Setup=require('../web/setup.js');
test('OAuth success with only weekly/short windows never fabricates account total',()=>{
 const value=Account.view({status:{authenticated:true},usage:{state:'ok',windows:[{name:'limit5h',remaining_percent:99.99},{name:'limit7d',remaining_percent:15.38}],weekly_applicability:'applicable',account_binding:{state:'verified'}}});
 assert.equal(value.authenticated,true);assert.equal(value.hasTotal,false);
 assert.deepEqual(value.windows.map(w=>w.kind),['short','weekly']);
});
test('real total zero stays zero while missing and malformed numbers stay unknown',()=>{
 const value=Account.view({usage:{stale:true,windows:[{kind:'month_total',remaining_percent:0},{kind:'month_code',remaining_percent:null},{kind:'weekly',remaining_percent:NaN},{kind:'short',remaining_percent:101}]}});
 assert.equal(value.hasTotal,true);assert.equal(value.stale,true);assert.equal(value.windows.length,1);assert.equal(value.windows[0].remaining_percent,0);
});
test('new plan with no weekly window retains its separate applicability and identity state',()=>{
 const value=Account.view({usage:{weekly_applicability:'not_applicable',account_binding:{state:'mismatch'},windows:[{kind:'month_total',remaining_percent:66.73}],account:{nickname:'name',token:'secret',phone:'private'}}});
 assert.equal(value.weeklyApplicability,'not_applicable');assert.equal(value.binding,'mismatch');assert.deepEqual(value.account,{nickname:'name'});
});
test('login links are first-party only and include only the validated temporary device code',()=>{
 const value=Account.safeLogin({state:'pending',verification_url:'https://www.kimi.com/code/authorize_device?token=secret',user_code:'ABCD-1234',access_token:'secret',interval:1});
 assert.equal(value.url,'https://www.kimi.com/code/authorize_device?user_code=ABCD-1234');assert.equal(value.interval,5);assert.equal(JSON.stringify(value).includes('secret'),false);
 assert.equal(Account.safeLogin({verification_url:'https://evil.test/code',user_code:'<script>'}).url,null);
 assert.equal(Account.safeLogin({verification_url:'https://www.kimi.com.evil.test/code'}).url,null);
});
test('Ultra tier state and history preserve actual historical model identity',()=>{
 const task={tier:'ultra',current_model:'kimi-for-coding/k3.1-pro',tier_change:{from_tier:'deep',to_tier:'ultra',status:'applied'}};
 assert.match(View.tierHTML(task,{tr:(k,v={})=>k+(v.tier?':'+v.tier:''),esc:String}),/tier.ultra/);
 assert.equal(View.currentModel({actual_models:['kimi-for-coding/k3']}),'kimi-for-coding/k3');
});
test('new catalog excludes retired K3 models but retains the current Ultra model',()=>{
 const catalog=Setup.normalizeCatalog({providers:[{id:'kimi-for-coding',models:[{id:'k3'},{id:'k3-256k'},{id:'k3.1-pro'}]},{id:'volcengine-agent-plan',models:[{id:'kimi-k3'}]}]});
 assert.deepEqual(catalog[0].models.map(x=>x.id),['k3.1-pro']);assert.deepEqual(catalog[1].models,[]);
});
test('keeping a legacy three-tier setup round-trips without inventing an Ultra route',()=>{
 const original={revision:1,max_parallel_per_owner:4,profiles:{fallback:{model:'deepseek/deepseek-flash',enabled:true}},routing:{fast:'fallback',background:'fallback',deep:'fallback'}};
 const draft=Setup.createDraft(original,{providers:[{id:'deepseek',connected:true,models:[{id:'deepseek-flash'}]}]});
 const payload=Setup.buildPayload(draft);
 assert.deepEqual(payload.routing,original.routing);
 assert.equal(payload.routing.ultra,undefined);
});
test('POST login envelope is unwrapped while GET login status stays compatible',()=>{
 const flow={state:'pending',verification_url:'https://www.kimi.com/code/authorize_device',user_code:'ABCD-1234',interval:5};
 assert.deepEqual(Account.safeLogin({start:{state:'ready'},login:flow}),Account.safeLogin(flow));
});
test('web total remains connected and explicit when the independent OAuth service is down',()=>{
 const value=Account.view({status:{authenticated:false,state:'unavailable'},usage:{state:'ok',stale:false,account_binding:{state:'verified'},oauth_account:{state:'unavailable'},web_account:{state:'ok',source:'kimi_web_membership',account_binding:{state:'verified'}},windows:[{name:'monthTotal',kind:'month_total',remaining_percent:66.73,source:'kimi_web_membership'}]}});
 assert.equal(value.authenticated,true);assert.equal(value.oauthAuthenticated,false);assert.equal(value.webConnected,true);assert.equal(value.hasTotal,true);assert.equal(value.windows[0].source,'kimi_web_membership');
});
test('new weekly reserve takes precedence in review without rewriting legacy setting on keep',()=>{
 const original={revision:1,max_parallel_per_owner:4,kimi_reserve_percent:0,kimi_weekly_reserve_percent:30,profiles:{fallback:{model:'deepseek/deepseek-flash',enabled:true}},routing:{fast:'fallback',background:'fallback',deep:'fallback'}};
 const draft=Setup.createDraft(original,{providers:[{id:'deepseek',connected:true,models:[{id:'deepseek-flash'}]}]});
 assert.equal(draft.kimiReservePercent,30);
 const payload=Setup.buildPayload(draft);
 assert.equal(payload.kimi_reserve_percent,0);assert.equal(payload.kimi_weekly_reserve_percent,30);
});
test('monthly subscription expiry is never rendered as a quota reset',()=>{
 const expiry=Account.windowTiming({kind:'month_total',resets_at:null,expires_at:'2026-11-18T06:47:51Z'});
 assert.equal(expiry.kind,'expiry');assert.equal(expiry.at,Date.parse('2026-11-18T06:47:51Z'));
 assert.equal(Account.windowTiming({resets_at:'2026-10-27T03:47:50Z',expires_at:'2026-11-18T06:47:51Z'}).kind,'reset');
 assert.equal(Account.windowTiming({resets_at:null,expires_at:null}).kind,'unknown');
});
