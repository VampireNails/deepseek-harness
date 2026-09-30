import {test} from 'node:test';
import assert from 'node:assert/strict';
import {QuietMemoryAuthority} from '../src/memory-authority.js';
import {ReflectionLifetime} from '../src/reflection-lifetime.js';

const execution=(agent,name='memory_write',signal=new AbortController().signal)=>({agent,name,signal});
const childOf=parentSession=>({session:{header:{parentSession}}});
test('published children and ordinary callers do not wait for a slow sibling',async()=>{
  const authority=new QuietMemoryAuthority(),child=childOf('parent');
  authority.begin(new AbortController().signal,'parent')({localAgent:child});
  const finishSlow=authority.begin(new AbortController().signal,'other');
  const timely=promise=>Promise.race([promise,new Promise((_,reject)=>setTimeout(()=>reject(new Error('unrelated publication stalled execution')),100))]);
  try{
    assert.equal(await timely(authority.authorize(execution(child))),true);
    assert.equal(await timely(authority.authorize(execution({}))),false);
    assert.equal(await timely(authority.authorize(execution(childOf('unrelated')))),false);
  }finally{finishSlow();}
});
test('early child resumes at its own publication while a same-parent sibling remains pending',async()=>{
  const authority=new QuietMemoryAuthority(),child=childOf('parent');
  const finish=authority.begin(new AbortController().signal,'parent');
  const slow=authority.begin(new AbortController().signal,'parent');
  const waiting=authority.authorize(execution(child));finish({localAgent:child});
  try{assert.equal(await Promise.race([waiting,new Promise((_,reject)=>setTimeout(()=>reject(new Error('sibling blocked child')),100))]),true);}
  finally{slow();}
});
test('lifetime unload aborts owned work, disposes published and late-published runs',async()=>{
  const lifetime=new ReflectionLifetime();let disposed=0;
  const published=lifetime.begin(10000),pending=lifetime.begin(10000);
  assert.equal(await lifetime.publish(published,{dispose:async()=>{disposed++;}}),true);
  const closing=lifetime.close();
  assert.equal(published.controller.signal.aborted,true);assert.equal(pending.controller.signal.aborted,true);
  assert.equal(lifetime.begin(10000),null);
  assert.equal(await lifetime.publish(pending,{dispose:async()=>{disposed++;}}),false);
  assert.equal(disposed,2);lifetime.release(pending);lifetime.release(published);await closing;
});
test('only the exact published Quiet child receives two memory writes',async()=>{
  const authority=new QuietMemoryAuthority();
  const child={},signal=new AbortController().signal;
  authority.begin(signal)({localAgent:child});
  assert.equal(await authority.authorize(execution({label:'quiet-moment reflection'})),false);
  assert.equal(await authority.authorize(execution(child,'file_write')),false);
  assert.equal(await authority.authorize(execution(child)),true);
  assert.equal(await authority.authorize(execution(child)),true);
  assert.equal(await authority.authorize(execution(child)),false);
});
test('early tool execution waits for publication without granting an impostor',async()=>{
  const authority=new QuietMemoryAuthority(),child=childOf('parent');
  const finish=authority.begin(new AbortController().signal,'parent');
  const actual=authority.authorize(execution(child));
  const impostor=authority.authorize(execution({}));
  finish({localAgent:child});
  assert.equal(await actual,true);assert.equal(await impostor,false);
});
test('failed, remote, cancelled, released and unloaded runs fail closed',async()=>{
  const authority=new QuietMemoryAuthority(),child={},controller=new AbortController();
  const failure=authority.begin(controller.signal);const waiting=authority.authorize(execution(child));
  failure();assert.equal(await waiting,false);
  authority.begin(controller.signal)({localAgent:undefined});
  assert.equal(await authority.authorize(execution(child)),false);
  authority.begin(controller.signal)({localAgent:child});
  const cancelled=new AbortController();cancelled.abort();
  assert.equal(await authority.authorize(execution(child,'memory_write',cancelled.signal)),false);
  controller.abort();assert.equal(await authority.authorize(execution(child)),false);
  authority.begin(new AbortController().signal)({localAgent:child});authority.release(child);
  assert.equal(await authority.authorize(execution(child)),false);
  authority.begin(new AbortController().signal)({localAgent:child});authority.close();
  assert.equal(await authority.authorize(execution(child)),false);
});
test('unload and cancellation unblock an execution waiting for publication',async()=>{
  const authority=new QuietMemoryAuthority(),child=childOf('parent');
  authority.begin(new AbortController().signal,'parent');
  const waiting=authority.authorize(execution(child));authority.close();
  assert.equal(await waiting,false);
  const other=new QuietMemoryAuthority(),controller=new AbortController();
  const finish=other.begin(new AbortController().signal,'parent');
  const cancelled=other.authorize(execution(child,'memory_write',controller.signal));
  controller.abort();assert.equal(await cancelled,false);finish();
});
