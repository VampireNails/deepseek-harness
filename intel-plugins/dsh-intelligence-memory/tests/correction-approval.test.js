import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, rmSync, readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {Context} from '@deepseek-ai/cordis';
import ToolRuntime from '@deepseek-ai/dsh-tools';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import ApprovalService from '@deepseek-ai/dsh-user-approval';
import {Session, SessionId} from '@deepseek-ai/dsh-session';
import {openStore} from '../src/store.js';
import {FtsRetriever} from '../src/retriever.js';
import {registerMemoryCorrection} from '../src/correction.js';

async function fixture(t, {approval=true, outcome}={}) {
  const directory=mkdtempSync(join(tmpdir(),'memory-approval-'));
  const db=openStore(directory), retriever=new FtsRetriever(db);
  const {id}=retriever.add({text:'anonymous original full memory',kind:'fact'});
  const ctx=new Context();
  await ctx.plugin(SystemPrompt);
  await ctx.plugin(ToolRuntime,{mode:'native'});
  if(approval)await ctx.plugin(ApprovalService,{policy:'ask'});
  registerMemoryCorrection(ctx,retriever);
  const asks=[];
  if(outcome)ctx.on('approval/request',async request=>{asks.push(request);return typeof outcome==='function'?outcome(request):outcome;});
  const session=Session.create(SessionId('memory-approval-fixture'));
  session.append('turn/start',{turn:1});
  const agent={session};
  const run=(options={})=>ctx.tools.execute({callId:'memory-fixture-call',name:'memory_invalidate',arguments:{id,expectedRevision:0,reason:'the user corrected this fact'},agent,signal:new AbortController().signal,...options});
  t.after(async()=>{await ctx.fiber.dispose();db.close();rmSync(directory,{recursive:true});});
  return {ctx,db,retriever,id,asks,agent,run};
}

test('actual tool approval includes full original text and reason; grant invalidates with trusted audit',async t=>{
  const f=await fixture(t,{outcome:'allowed-once'});
  const original=f.db.prepare('SELECT * FROM memories').all();
  const result=await f.run();
  assert.equal(result.isError,false);assert.equal(f.asks.length,1);
  assert.match(f.asks[0].reason,/anonymous original full memory/);
  assert.match(f.asks[0].reason,/the user corrected this fact/);
  const expected=readFileSync(new URL('./expected/correction-approved.txt',import.meta.url),'utf8');
  assert.equal(f.asks[0].reason+'\n\n---\n\n'+result.content[0].text+'\n',expected);
  const events=Array.from({length:f.agent.session.seq},(_,seq)=>f.agent.session.eventAt(seq));
  assert.equal(events.find(event=>event.type==='approval/asked').data.reason,f.asks[0].reason);
  assert.equal(events.find(event=>event.type==='approval/decided').data.outcome,'allowed-once');
  assert.equal(f.retriever.get(f.id),null);
  assert.deepEqual(f.db.prepare('SELECT * FROM memories').all(),original);
  assert.equal(f.retriever.history(f.id)[0].source,'approved-tool');
  const restore=await f.run({name:'memory_restore',arguments:{id:f.id,expectedRevision:1,reason:'the original fact is valid'}});
  assert.equal(restore.isError,false);assert.equal(f.retriever.get(f.id).text,'anonymous original full memory');
  assert.equal(f.retriever.history(f.id).length,2);
});

for(const scenario of [{approval:false},{outcome:'rejected'},{outcome:'cancelled'},{},{outcome:'allowed-once',noAgent:true}]) {
  test('approval refuses mutation '+JSON.stringify(scenario),async t=>{
    const f=await fixture(t,scenario);
    const before=f.db.prepare('SELECT * FROM memories_fts').all();
    const result=await f.run(scenario.noAgent?{agent:undefined}:{});
    assert.equal(result.isError,true);assert.equal(f.retriever.describe(f.id).revision,0);
    assert.equal(f.retriever.history(f.id).length,0);
    assert.deepEqual(f.db.prepare('SELECT * FROM memories_fts').all(),before);
    if(scenario.noAgent)assert.equal(f.asks.length,0);
  });
}

test('cancellation while the actual approval is pending prevents a late grant from changing memory',async t=>{
  let entered,release;const waiting=new Promise(resolve=>{entered=resolve;});
  const f=await fixture(t,{outcome:()=>{entered();return new Promise(resolve=>{release=resolve;});}});
  const controller=new AbortController(), pending=f.run({signal:controller.signal});
  await waiting;controller.abort();release('allowed-once');
  assert.equal((await pending).isError,true);
  assert.equal(f.retriever.describe(f.id).revision,0);assert.equal(f.retriever.history(f.id).length,0);
});

test('revision changed during approval refuses the stale action and preserves the newer audit',async t=>{
  const f=await fixture(t,{outcome:()=>{
    f.retriever.change({id:f.id,action:'invalidate',expectedRevision:0,reason:'separate confirmed action',mutationId:'00000000-0000-4000-8000-000000000001'},{source:'paired-client'});
    return 'allowed-once';
  }});
  const result=await f.run();assert.equal(result.isError,true);
  assert.equal(f.retriever.history(f.id).length,1);assert.equal(f.retriever.history(f.id)[0].source,'paired-client');
});

test('a preceding denial and a following cancellation are preserved without asking or writing',async t=>{
  const f=await fixture(t,{outcome:'allowed-once'});
  const remove=f.ctx.on('tools/pre-execute',async()=>({kind:'deny',reason:'existing denial'}),{prepend:true});
  assert.equal((await f.run()).isError,true);assert.equal(f.asks.length,0);remove();
  f.ctx.on('tools/pre-execute',async()=>({kind:'cancel'}));
  assert.equal((await f.run()).isError,true);assert.equal(f.asks.length,0);
  assert.equal(f.retriever.describe(f.id).revision,0);
});
