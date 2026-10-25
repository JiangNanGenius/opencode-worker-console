'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
const motion=source.slice(source.indexOf('let motionSequence='),source.indexOf('function clock('));
const settle=()=>new Promise(resolve=>setImmediate(resolve));
function harness(){
 const errors=[],document={documentElement:{dataset:{}},startViewTransition(update){
  const updateCallbackDone=Promise.resolve().then(update);
  const ready=updateCallbackDone.then(()=>{throw Object.assign(new Error('Transition was skipped. New ViewTransition started'),{name:'AbortError'});});
  const finished=updateCallbackDone.then(()=>undefined);
  return {ready,finished,updateCallbackDone};
 }};
 const context={document,Promise,reducedMotion:()=>false,setTimeout,showError:error=>errors.push(error),tr:(key,vars={})=>vars.message||key};
 vm.createContext(context);vm.runInContext(motion,context);
 return {context,document,errors};
}
test('overlapping skipped animations do not report module failure and updates still run',async()=>{
 const h=harness();let updates=0;
 h.context.motionTransition('page',()=>{updates++;});
 h.context.motionTransition('page',()=>{updates++;});
 await settle();
 assert.equal(updates,2);assert.deepEqual(h.errors,[]);
 assert.deepEqual(h.document.documentElement.dataset,{});
});
test('consuming animation-ready rejection does not hide a failed business update',async()=>{
 const h=harness();
 h.context.motionTransition('page',()=>{throw new Error('Real update failed');});
 await settle();
 assert.deepEqual(h.errors,['Real update failed']);
 assert.deepEqual(h.document.documentElement.dataset,{});
});
