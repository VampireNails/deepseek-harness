import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,resolve,sep} from 'node:path';
import {apply} from '../index.js';
const deferred=()=>{let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};};
const tick=()=>new Promise(done=>setImmediate(done));
const transcript={events:[{type:'user/message',data:{content:[{type:'text',text:'Remember my lifelong preference for concise reports.'}],source:{kind:'user'}}}]};
async function fixture(body){
  const dir=mkdtempSync(join(tmpdir(),'quiet-lifetime-')),previous=process.env.DSH_HOME;
  process.env.DSH_HOME=dir;
  const handlers=new Map(),disposers=[],services=new Map();
  const ctx={provide:(key,value)=>services.set(key,value),
    on:(event,handler)=>{handlers.set(event,handler);return ()=>handlers.delete(event);},
    effect:fn=>{const disposer=fn();if(typeof disposer==='function')disposers.push(disposer);},
    sessionQuery:{readSession:async()=>transcript},
    subagents:{getProvider:()=>({name:'spawn'}),start:async()=>{throw new Error('unconfigured test provider');}}};
  const start=()=>apply(ctx,{cooldownSec:0,minUserChars:8,reflectTimeoutMs:10000,autoApproveMemoryWrites:true});
  const fire=()=>handlers.get('agent/turn-stopping')({agent:{session:{id:'parent'}},turn:1});
  const unload=async()=>{await Promise.all(disposers.splice(0).map(dispose=>dispose()));};
  try{await body({ctx,start,fire,unload,services});}
  finally{await unload();if(previous===undefined)delete process.env.DSH_HOME;else process.env.DSH_HOME=previous;
    assert.ok(resolve(dir).startsWith(resolve(tmpdir())+sep));rmSync(dir,{recursive:true,force:true});}
}
test('unload during transcript preparation cannot start a new reflection',()=>fixture(async({ctx,start,fire,unload})=>{
  const reading=deferred();let starts=0;
  ctx.sessionQuery.readSession=()=>reading.promise;ctx.subagents.start=async()=>{starts++;};
  start();fire();await unload();reading.resolve(transcript);await tick();assert.equal(starts,0);
}));
test('unload disposes a run that publishes after shutdown and issues no grant',()=>fixture(async({ctx,start,fire,unload,services})=>{
  const publication=deferred(),result=deferred(),child={};let disposed=0,signal;
  ctx.subagents.start=async(_name,request)=>{signal=request.signal;return publication.promise;};
  start();fire();await tick();let closed=false;
  const closing=unload().then(()=>{closed=true;});await tick();assert.equal(signal.aborted,true);assert.equal(closed,false);
  publication.resolve({localAgent:child,result:result.promise,dispose:async()=>{disposed++;result.resolve({});}});
  await closing;assert.equal(disposed,1);
  assert.equal(await services.get('quietMemoryAuthority').authorize({agent:child,name:'memory_write'}),false);
}));
test('unload waits for asynchronous disposal already begun by completion',()=>fixture(async({ctx,start,fire,unload})=>{
  const result=deferred(),disposal=deferred();let disposing=false,closed=false;
  ctx.subagents.start=async()=>({localAgent:{},result:result.promise,
    dispose:async()=>{disposing=true;await disposal.promise;}});
  start();fire();await tick();result.resolve({});await tick();assert.equal(disposing,true);
  const closing=unload().then(()=>{closed=true;});await tick();assert.equal(closed,false);
  disposal.resolve();await closing;assert.equal(closed,true);
}));
test('unload aborts and disposes a published reflection',()=>fixture(async({ctx,start,fire,unload})=>{
  const result=deferred();let disposed=0,signal;
  ctx.subagents.start=async(_name,request)=>{signal=request.signal;return {localAgent:{},result:result.promise,
    dispose:async()=>{disposed++;result.resolve({});}};};
  start();fire();await tick();await unload();assert.equal(signal.aborted,true);assert.ok(disposed>=1);
}));
