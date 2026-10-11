import assert from 'node:assert/strict';
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { LlmAdapter, createUserMessage } from '@deepseek-ai/dsh-llm';
import { FtsRetriever } from '../../src/retriever.js';
export const name='memory-growth-probe';
export const inject=['tools','sessions','agents','llm','sessionProjections','userProfile','soul'];
export async function apply(ctx) {
  const proof={runtime:{pid:process.pid,node:process.version,dataHome:process.env.DSH_HOME}};
  try {
    const deadline=Date.now()+10000;
    while(!ctx.tools.get('memory_write')) {
      if(Date.now()>deadline)throw Error('Memory tool registration did not settle');
      await new Promise(resolve=>setTimeout(resolve,10));
    }
    const session=ctx.sessions.create(),agent={session}, other={session:ctx.sessions.create()};
    const call=(name,args,actor=agent)=>ctx.tools.execute({callId:name+Math.random(),name,arguments:args,agent:actor,signal:new AbortController().signal});
    const input={text:'用户偏好简洁中文',kind:'preference'};
    const first=await call('memory_write',input),repeat=await call('memory_write',input);
    proof.initial={first,repeat};
    assert.equal(first.isError,false); assert.equal(repeat.isError,false);
    assert.equal(repeat.value.id,first.value.id); assert.equal(repeat.value.deduplicated,true);
    assert.match(repeat.content[0].text,/未新增版本或更新时间/);
    assert.equal(first.value.capacity.rows,1); assert.equal(repeat.value.capacity.rows,1);
    const different=await call('memory_write',input,other); assert.notEqual(different.value.id,first.value.id);
    assert.equal(different.value.capacity.warning,true); assert.match(different.content[0].text,/已达到提示阈值/);
    const event1=await call('memory_write',{text:'相同事件发生',kind:'event'}),event2=await call('memory_write',{text:'相同事件发生',kind:'event'});
    assert.notEqual(event1.value.id,event2.value.id);
    const direct=args=>ctx.tools.execute({callId:'unknown-'+Math.random(),name:'memory_write',arguments:args,signal:new AbortController().signal});
    const null1=await direct({text:'来源未知的原文'}),null2=await direct({text:'来源未知的原文'});
    assert.notEqual(null1.value.id,null2.value.id);
    const db=new DatabaseSync(join(process.env.DSH_HOME,'intel-memory','memory.db'));
    try {
      assert.equal(db.prepare('SELECT session_id FROM memories WHERE id=?').get(first.value.id).session_id,session.id);
      assert.equal(db.prepare('SELECT session_id FROM memories WHERE id=?').get(different.value.id).session_id,other.session.id);
      const original=new FtsRetriever(db),old=original.add({text:'默认无容量提示',sessionId:'synthetic-fixture'});
      assert.equal(old.capacity,undefined);
      db.exec('BEGIN IMMEDIATE');
      try {const failed=await call('memory_write',{text:'PRIVATE locked body'});assert.equal(failed.isError,true);assert.equal(failed.error.info.code,'MEMORY_WRITE_FAILED');assert.doesNotMatch(JSON.stringify(failed),/PRIVATE/);proof.lockFailure=failed;}
      finally {db.exec('ROLLBACK');}
      proof.originals=db.prepare('SELECT id,kind,session_id,created_at FROM memories ORDER BY id').all();
    } finally {db.close();}
    const search=await call('memory_search',{query:'中文',limit:5}); assert.equal(search.isError,false);assert.ok(search.value.results.some(row=>row.id===first.value.id));assert.match(search.content[0].text,/记忆容量/);
    const read=await call('memory_read',{id:first.value.id}); assert.equal(read.isError,false);assert.equal(read.value.text,input.text);
    const tooLong=await call('memory_search',{query:'PRIVATE'.repeat(10000)});assert.equal(tooLong.isError,true);assert.equal(tooLong.error.info.code,'MEMORY_QUERY_TOO_LARGE');assert.doesNotMatch(JSON.stringify(tooLong),/PRIVATE/);
    proof.tools={first,repeat,different,event1,event2,null1,null2,search,read,tooLong};
    const user=text=>createUserMessage({content:[{type:'text',text}],source:{kind:'user'}});
    class Replay extends LlmAdapter {
      requests=[];
      async resolveModel(provider,model){return {provider,id:model,name:model};}
      async *stream(options) {
        this.requests.push(options);
        const block=this.requests.length===1?{type:'tool-call',id:'later-step',name:'memory_read',arguments:JSON.stringify({id:first.value.id})}:{type:'text',text:'DONE'};
        yield {type:'block-start',index:0,blockType:block.type};
        if(block.type==='tool-call')yield {type:'tool-call-delta',index:0,id:block.id,name:block.name,argumentsDelta:block.arguments};
        else yield {type:'text-delta',index:0,text:block.text};
        yield {type:'block-end',index:0,block};yield {type:'finish',reason:{kind:block.type==='tool-call'?'tool-calls':'stop'}};
      }
    }
    const replay=new Replay(),disposeAdapter=ctx.llm.registerAdapter(['t15-replay'],replay);
    const handle=await ctx.agents.create({sessionId:'t15-durable-facts',agentOptions:{provider:'t15-replay',model:'keyless'}});
    try {
      const idle=new Promise(resolve=>{const dispose=ctx.on('agent/status',({agent:subject,status})=>{if(subject===handle.agent&&status==='idle'){dispose();resolve();}});});
      handle.agent.followup(user('中文'));await idle;
      assert.equal(replay.requests.length,2);
      const injections=handle.agent.session.ownEvents().filter(e=>e.type==='user/message'&&e.data.source?.kind==='dsh-intelligence-memory');
      assert.equal(injections.length,1);
      const original=injections[0].data;
      proof.loopDebug={requests:replay.requests,injections};
      for(const request of replay.requests)assert.ok(request.messages.some(m=>m.id===original.id&&m.content[0].text.includes(input.text)));
      const additional={};
      for(const [kind,text] of [['dsh-intelligence-profile','Synthetic profile fact: prefers annotated calculations.'],['dsh-intelligence-soul','Synthetic soul rule: retain observed evidence.']]) {
        const entries=handle.agent.session.ownEvents().filter(e=>e.type==='user/message'&&e.data.source?.kind===kind);
        assert.equal(entries.length,1);
        for(const request of replay.requests)assert.ok(request.messages.some(m=>m.id===entries[0].data.id&&m.content[0].text.includes(text)));
        additional[kind]=entries[0].data;
      }
      await ctx.sessions.flush(handle.agent.session);
      proof.loop={requestCount:2,firstInjectionCount:1,injection:original,additional,modelMessages:replay.requests.map(request=>request.messages),events:handle.agent.session.ownEvents()};
    } finally {await handle.dispose();disposeAdapter();}
    writeFileSync(process.env.T15_RECEIPT,JSON.stringify({ok:true,...proof})+'\n');
  } catch(error) {writeFileSync(process.env.T15_RECEIPT,JSON.stringify({ok:false,...proof,message:error.message,stack:error.stack})+'\n');}
}
