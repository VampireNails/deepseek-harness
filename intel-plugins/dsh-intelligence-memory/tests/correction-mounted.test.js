import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {Context} from '@deepseek-ai/cordis';
import ToolRuntime from '@deepseek-ai/dsh-tools';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import Agents from '@deepseek-ai/dsh-agent';
import SessionStore,{Session,SessionId} from '@deepseek-ai/dsh-session';
import Projections from '@deepseek-ai/dsh-session-projection';
import {scopeTarget} from '@deepseek-ai/dsh-scope';
import * as Memory from '../index.js';

test('mounted Memory service excludes invalidated originals from recall and new references, preserves explicit history and unloads',async()=>{
  const directory=mkdtempSync(join(tmpdir(),'memory-mounted-')),ctx=new Context();
  try {
    await ctx.plugin(SessionStore);await ctx.plugin(SystemPrompt);
    await ctx.plugin(ToolRuntime,{mode:'native'});await ctx.plugin(Agents);await ctx.plugin(Projections);
    const fiber=await ctx.plugin(Memory,{dataDir:directory,recallLimit:3,minQueryChars:2});
    const written=await ctx.tools.execute({callId:'write-fixture',name:'memory_write',arguments:{text:'anonymous favorite cobalt color',kind:'preference'},signal:new AbortController().signal});
    assert.equal(written.isError,false);
    const manager=ctx.get('memoryManagement'),references=ctx.get('memoryReferences');
    assert.equal(manager.available,true);const id=manager.list().items[0].id;
    assert.equal(references.validate([id]),true);
    const session=Session.create(SessionId('mounted-memory-fixture'));
    session.append('turn/start',{turn:1});const agent={session};
    const recall=()=>ctx.waterfall(scopeTarget(agent,agent),'agent/pre-step',
      {agent,turn:1,step:1,signal:new AbortController().signal},async()=>({kind:'allow',messages:[{role:'user',content:'cobalt color'}]}));
    assert.equal((await recall()).messages.length,2);
    manager.change({id,action:'invalidate',expectedRevision:0,reason:'user correction',mutationId:'00000000-0000-4000-8000-000000000003'});
    assert.equal(references.validate([id]),false);assert.equal(manager.list().items.length,0);
    assert.equal((await recall()).messages.length,1);
    assert.equal(manager.detail(id).record.text,'anonymous favorite cobalt color');
    assert.equal(manager.detail(id).record.active,false);
    const history=await ctx.tools.execute({callId:'history-fixture',name:'memory_history',arguments:{id},signal:new AbortController().signal});
    assert.equal(history.isError,false);assert.match(history.content[0].text,/anonymous favorite cobalt color/);
    await fiber.dispose();assert.equal(ctx.get('memoryManagement'),undefined);
    assert.throws(()=>manager.list());
    const reopened=new DatabaseSync(join(directory,'memory.db'),{readOnly:true});
    try{assert.equal(reopened.prepare('SELECT count(*) AS n FROM memories').get().n,1);assert.equal(reopened.prepare('SELECT count(*) AS n FROM memory_audit').get().n,1);}finally{reopened.close();}
  } finally {await ctx.fiber.dispose();rmSync(directory,{recursive:true});}
});
