'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const View = require('../web/task-view.js');
const tick = () => new Promise(resolve => setImmediate(resolve));
const esc = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const display = { tr: key => key, esc, number: String, clock: String };

test('usage keeps unknown distinct from zero and never adds reasoning twice', () => {
 const tasks = [{id:'a',usage:{total:100,input:70,output:30,reasoning:10}},{id:'b',usage:{total:0}},{id:'c',tokens:{input:900}}];
 assert.deepEqual(View.aggregate(tasks), {total:100,known:2,count:3});
 assert.deepEqual(View.aggregate(tasks,new Map([['a',{usage:{total:120}}]])),{total:120,known:2,count:3});
 assert.equal(View.aggregate([{id:'x'}]).total,null);
 assert.match(View.usageHTML({total:0},display),/<dd>0<\/dd>/);
 assert.match(View.usageHTML({},display),/<dd>—<\/dd>/);
});

test('activity escapes every untrusted field and marks a stale sample', () => {
 const html=View.activityHTML({stale:true,error:'<secret>',source:'saved',has_more:true,events:[{id:'" onclick="bad',status:'evil',label:'<img>',text:'<script>bad()</script>',time:2}]},display);
 assert.ok(!html.includes('<script>'));
 assert.ok(!html.includes('<img>'));
 assert.match(html,/&lt;script&gt;/);
 assert.match(html,/activity\.stale/);
 assert.match(html,/activity\.recentOnly/);
});

test('polling cancels a closed task and ignores late replies', async () => {
 let resolve,signal;
 const c=View.createController({changed:()=>{},request:(_p,options)=>{signal=options.signal;return new Promise(r=>resolve=r);}});
 c.open('a');c.close();assert.equal(signal.aborted,true);
 resolve({task:{id:'a'}});await tick();
 assert.equal(c.state().selected,null);assert.equal(c.cache.has('a'),false);
});

test('switching tasks ignores superseded responses; double click collapses', async () => {
 const replies={};
 const c=View.createController({changed:()=>{},request:p=>new Promise(r=>replies[p]=r)});
 c.open('a');c.open('b');replies['/console-api/task/b']({task:{id:'b'}});await tick();
 replies['/console-api/task/a']({task:{id:'a'}});await tick();
 assert.equal(c.state().detail.task.id,'b');assert.equal(c.cache.has('a'),false);
 c.open('b');assert.equal(c.state().selected,null);
});

test('polls do not overlap, pause while hidden, and recover without losing evidence', async () => {
 let calls=0,visible=false,resolve;
 const c=View.createController({changed:()=>{},visible:()=>visible,request:()=>{calls++;return new Promise(r=>resolve=r);}});
 c.open('a');assert.equal(calls,0);
 visible=true;const first=c.refresh();c.refresh();assert.equal(calls,1);
 resolve({task:{id:'a'},usage:{total:5}});await first;
 visible=false;await c.refresh();assert.equal(calls,1);
 let fails=true;
 const recovering=View.createController({changed:()=>{},request:()=>fails?Promise.reject(Error('offline')):Promise.resolve({task:{id:'x'}})});
 recovering.open('x');await tick();assert.equal(recovering.state().error,'offline');
 fails=false;await recovering.refresh();assert.equal(recovering.state().error,null);
 fails=true;await recovering.refresh();assert.equal(recovering.state().detail.task.id,'x');
});

test('completed details stop polling but can be explicitly refreshed', async () => {
 let calls=0;
 const c=View.createController({changed:()=>{},request:async()=>{calls++;return {task:{id:'a',status:'completed'}};}});
 c.open('a');await tick();await c.refresh();assert.equal(calls,1);
 await c.refresh(true);assert.equal(calls,2);
 c.close();c.open('a');await tick();assert.equal(calls,3);
});

// Minimal DOM adapter tests the real app's row reconciliation without a browser,
// services, credentials or model calls. It does not assert visual rendering.
class Element {
 constructor(name='div'){this.name=name;this.children=[];this.parentNode=null;this.dataset={};this.hidden=false;this.innerHTML='';this.value='';this.textContent='';this.listeners={};this.classList={toggle(){},add(){}};}
 append(node){this.insertBefore(node,null);}
 insertBefore(node,before){node.remove();const at=before?this.children.indexOf(before):this.children.length;if(at<0)throw Error('invalid anchor');this.children.splice(at,0,node);node.parentNode=this;}
 remove(){if(this.parentNode){const a=this.parentNode.children;a.splice(a.indexOf(this),1);this.parentNode=null;}}
 get firstElementChild(){return this.children[0]||null;}
 get nextElementSibling(){if(!this.parentNode)return null;const a=this.parentNode.children;return a[a.indexOf(this)+1]||null;}
 contains(node){return !!node&&(node===this||this.children.some(c=>c.contains(node)));}
 addEventListener(type,fn){this.listeners[type]=fn;}
 querySelector(){return null;}
 querySelectorAll(){return [];}
 getAttribute(key){return this[key]||null;}
 focus(){}
 scrollIntoView(){throw Error('Task expansion must not scroll the page');}
}
function appHarness(){
 const elements=new Map();const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
 const document={hidden:false,activeElement:null,getElementById:get,createElement:n=>new Element(n),querySelector:get,querySelectorAll:()=>[],addEventListener(){}};
 const fixtureTasks=['a','b','c'].map(id=>({id,title:id,group_id:'g',group_title:'Group',status:'running',mode:'read',usage:{total:10}}));
 let revision=0;
 const fetch=async url=>({ok:true,json:async()=>url==='/console-api/state'?{tasks:fixtureTasks,quota:{},profiles:{},pool_healthy:true}:url==='/console-api/auth/status'?{authenticated:true,username:'fixture'}:{task:fixtureTasks.find(t=>url.endsWith(t.id)),activity:{source:'live',events:[{id:'e',text:'Update '+(++revision),status:'running'}]},usage:{total:20}}});
 const ctx={document,window:{TaskView:View,getSelection:()=>null,location:{replace(){throw Error('unexpected redirect');}}},TaskView:View,fetch,setInterval(){},Date,Map,Set,AbortController,console};
 vm.createContext(ctx);vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8'),ctx);
 return {ctx,get,run:source=>vm.runInContext(source,ctx)};
}

test('real task list inserts detail immediately after selected row and keeps its node on refresh', async () => {
 const h=appHarness();await tick();
 h.run("details('b')");await tick();
 const tbody=h.get('task-rows');
 assert.equal(tbody.children.length,4);
 assert.match(tbody.children[1].innerHTML,/data-detail="b"/);
 assert.equal(tbody.children[2].className,'task-detail-row');
 assert.match(tbody.children[3].innerHTML,/data-detail="c"/);
 const panel=tbody.children[2];const before=h.get('detail-body').innerHTML;
 await h.run('refresh()');await tick();
 assert.equal(tbody.children[2],panel);
 assert.notEqual(h.get('detail-body').innerHTML,before);
 assert.match(tbody.children[1].innerHTML,/aria-expanded="true"/);
 h.run("details('b')");assert.equal(tbody.children.length,3);
});

test('filtering away the selected row stops its detail and preserves the filtered summary', async () => {
 const h=appHarness();await tick();h.run("details('b')");await tick();
 h.get('search').value='c';h.run('renderTasks()');
 assert.equal(h.run('selectedTask'),null);
 assert.equal(h.get('task-rows').children.length,1);
 assert.match(h.get('task-rows').children[0].innerHTML,/data-detail="c"/);
});
