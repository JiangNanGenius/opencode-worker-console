/* Account quota display helpers. Never infer a monthly pool from weekly quota. */
(function(root){
  'use strict';
  const windowKinds = new Map([['monthTotal','month_total'],['monthCode','month_code'],['limit7d','weekly'],['limit5h','short']]);
  const windowLabels = {month_total:'quota.accountTotal',month_code:'quota.codeMonthly',weekly:'quota.weekly',short:'quota.fiveHour'};
  function view(payload={}) {
    const status=payload.status||{},usage=payload.usage||{},binding=usage.account_binding?.state||'unverified',web=usage.web_account||{},oauth=usage.oauth_account||{},oauthAuthenticated=status.authenticated===true,webConnected=web.state==='ok'&&web.account_binding?.state==='verified';
    const windows=(usage.windows||[]).filter(w=>w&&w.valid!==false&&typeof w.remaining_percent==='number'&&Number.isFinite(w.remaining_percent)&&w.remaining_percent>=0&&w.remaining_percent<=100).map(w=>({...w,kind:w.kind||windowKinds.get(w.name)})).filter(w=>windowLabels[w.kind]);
    return {authenticated:oauthAuthenticated||webConnected,oauthAuthenticated,oauthState:oauth.state||status.state||'unavailable',webState:web.state||'not_connected',webConnected,state:status.state||'unavailable',binding,windows,stale:usage.stale===true,hasTotal:windows.some(w=>w.kind==='month_total'),weeklyApplicability:usage.weekly_applicability||'unknown',account:Object.fromEntries(['nickname','userLevelName'].filter(key=>typeof usage.account?.[key]==='string').map(key=>[key,usage.account[key]])),sampledAt:usage.sampled_at||null};
  }
  function weeklyReserve(record) {
    if(!record||typeof record!=='object'||Array.isArray(record))return null;
    const percent=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0&&value<=100?value:null;
    const states=new Set(['fixed','first_half','releasing','ultra_demand','weekly_unknown','reset_unknown','demand_unknown','not_applicable']);
    return {base:percent(record.reserve_percent),effective:percent(record.effective_reserve_percent),released:percent(record.released_percent),emergency:percent(record.emergency_reserve_percent),dynamicEnabled:typeof record.dynamic_enabled==='boolean'?record.dynamic_enabled:null,state:states.has(record.release_state)?record.release_state:'unknown',reset:windowTiming({resets_at:record.resets_at}),remainingHours:typeof record.remaining_hours==='number'&&Number.isFinite(record.remaining_hours)&&record.remaining_hours>=0?record.remaining_hours:null};
  }
  function weeklyReserveHTML(record,{tr,esc,number=String,time=String}) {
    const value=weeklyReserve(record);if(!value)return '';
    if(value.state==='not_applicable')return `<h4>${esc(tr('kimiReserve.heading'))}</h4><p class="muted">${esc(tr('kimiReserve.state.not_applicable'))}</p>`;
    const metric=(key,amount,points=false)=>amount==null?'':`<div><span>${esc(tr(key))}</span><strong>${esc(tr(points?'kimiReserve.points':'kimiReserve.percent',{value:number(amount)}))}</strong></div>`;
    const refresh=value.reset.kind==='reset'?tr('kimiReserve.nextReset',{time:time(value.reset.at)}):tr('kimiReserve.resetUnknown');
    return `<h4>${esc(tr('kimiReserve.heading'))}</h4><div class="kimi-reserve-metrics">${metric('kimiReserve.base',value.base)}${metric('kimiReserve.effective',value.effective)}${metric('kimiReserve.released',value.released,true)}</div><p class="muted">${esc(tr('kimiReserve.state.'+value.state))}</p><p class="muted">${esc(refresh)}</p><p class="muted">${esc(tr('kimiReserve.releasedNote'))}</p>`;
  }
  function safeLogin(result={}) {
    result=result.login&&typeof result.login==='object'?result.login:result;
    let url=null;
    try{const u=new URL(result.verification_url);if(u.protocol==='https:'&&['www.kimi.com','kimi.com','www.kimi.ai','kimi.ai','auth.kimi.com','auth.kimi.ai'].includes(u.hostname)&&!u.username&&!u.password)url=u.origin+u.pathname;}catch{}
    const code=typeof result.user_code==='string'&&/^[a-z0-9-]{1,32}$/i.test(result.user_code)?result.user_code:null;
    if(url&&code){const complete=new URL(url);complete.searchParams.set('user_code',code);url=complete.href;}
    return {state:result.state||'unknown',url,code,interval:Math.max(5,Math.min(300,Number(result.interval)||5)),expiresAt:result.expires_at||null};
  }
  function windowTiming(window={}) {
    const timestamp=value=>{if(value==null||value===''||typeof value==='boolean')return null;const ms=typeof value==='number'?(value<1e11?value*1000:value):Date.parse(value);return Number.isFinite(ms)&&ms>0?ms:null;};
    const reset=timestamp(window.resets_at),expiry=timestamp(window.expires_at);
    return reset!=null?{kind:'reset',at:reset}:expiry!=null?{kind:'expiry',at:expiry}:{kind:'unknown',at:null};
  }
  function windowKey(window) {
    if(!window||typeof window!=='object')return '';
    const kind=window.kind||windowKinds.get(window.name);
    if(windowLabels[kind])return kind;
    const duration=Number(window.duration_minutes);
    if(window.name==='overall'||duration===10080)return 'weekly';
    if(duration===300)return 'short';
    return window.name?'name:'+window.name:duration>0?'duration:'+duration:'';
  }
  function kimiQuotaWindows(endpointWindows=[],account=null,now=Date.now()) {
    const rows=(Array.isArray(endpointWindows)?endpointWindows:[]).filter(w=>w&&typeof w==='object');
    const byKey=new Map(),order=[],leading=[];
    for(const row of rows){const key=windowKey(row);if(!key||byKey.has(key))continue;byKey.set(key,row);order.push(key);}
    if(!account||typeof account!=='object'||Array.isArray(account)||account.state!=='ok')return order.map(key=>byKey.get(key));
    const binding=account.account_binding&&typeof account.account_binding==='object'?account.account_binding:{};
    if(binding.state!=='verified')return order.map(key=>byKey.get(key));
    const timestamp=value=>{if(value==null||value===''||typeof value==='boolean')return null;const ms=typeof value==='number'?(value<1e11?value*1000:value):Date.parse(value);return Number.isFinite(ms)&&ms>0?ms:null;};
    const stale=account.stale===true;
    const sampled=typeof account.sampled_at==='number'&&Number.isFinite(account.sampled_at)?account.sampled_at:null;
    // Freshness mirrors the host view merge: only a current sample may override
    // the endpoint's own weekly/five-hour windows. A stale monthly pool is still
    // shown, but explicitly marked stale instead of silently advertised.
    const fresh=!stale&&sampled!=null&&now-sampled*1000>=0&&now-sampled*1000<=900000;
    for(const row of account.windows||[]){
      if(!row||typeof row!=='object'||Array.isArray(row))continue;
      const kind=row.kind||windowKinds.get(row.name);
      if(!windowLabels[kind])continue;
      const percent=row.remaining_percent;
      if(typeof percent!=='number'||!Number.isFinite(percent)||percent<0||percent>100)continue;
      if(row.valid===false||row.expired===true)continue;
      const reset=timestamp(row.resets_at),expiry=timestamp(row.expires_at);
      if(reset!=null&&reset<=now)continue;
      if(expiry!=null&&expiry<=now)continue;
      const merged={...row,kind,account_window:true,account_stale:stale||!fresh,account_sampled_at:sampled};
      const key=windowKey(merged);
      const monthly=kind==='month_total'||kind==='month_code';
      if(byKey.has(key)){
        if(monthly){
          const index=order.indexOf(key);
          if(index>=0)order.splice(index,1);
          if(!leading.includes(key))leading.push(key);
          byKey.set(key,merged);
        }else if(fresh){
          byKey.set(key,merged);
        }
      }else if(monthly){
        // Month pools are the primary limit: they lead the card.
        byKey.set(key,merged);
        if(!leading.includes(key))leading.push(key);
      }else if(fresh){
        byKey.set(key,merged);order.push(key);
      }
    }
    return [...leading,...order.filter(key=>!leading.includes(key))].map(key=>byKey.get(key));
  }
  const api={view,safeLogin,windowLabels,windowTiming,windowKey,kimiQuotaWindows,weeklyReserve,weeklyReserveHTML};
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.KimiAccountView=api;
})(typeof window!=='undefined'?window:globalThis);
