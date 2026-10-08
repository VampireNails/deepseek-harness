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

test('mounted recall retains a late subject and reports bounded query rejection without private text', async t => {
  const directory = mkdtempSync(join(tmpdir(), 'memory-recall-bounds-'));
  const ctx = new Context();
  t.after(async () => { try { await ctx.fiber.dispose(); } finally { rmSync(directory, { recursive: true }); } });
  const warnings = [];
  ctx.logger.exporter({ levels: { default: 3 }, export(message) { if (message.name === Memory.name && message.type === 'warn') warnings.push(message); } });
  await ctx.plugin(SessionStore); await ctx.plugin(SystemPrompt);
  await ctx.plugin(ToolRuntime, { mode: 'native' }); await ctx.plugin(Agents); await ctx.plugin(Projections);
  await ctx.plugin(Memory, { dataDir: directory, maxQueryChars: 256, maxQueryTerms: 32 });
  const written = await ctx.tools.execute({callId:'recall-bounds-write',name:'memory_write',
    arguments:{text:'heliostat calibration handbook'},signal:new AbortController().signal});
  assert.equal(written.isError, false);
  const session = Session.create(SessionId('mounted-recall-bounds'));
  session.append('turn/start', {turn:1});
  const agent = {session};
  const recall = query => ctx.waterfall(scopeTarget(agent, agent), 'agent/pre-step',
    {agent,turn:1,step:1,signal:new AbortController().signal}, async () => ({kind:'allow',messages:[{role:'user',content:query}]}));
  const long = await recall('please review our previous project notes and find heliostat calibration handbook');
  assert.equal(long.messages.length, 2);
  assert.equal(long.messages[1].source.kind, Memory.name);
  assert.match(long.messages[1].content[0].text, /heliostat calibration handbook/);
  const privateText = 'private-fixture-query-'.repeat(20);
  assert.equal((await recall(privateText)).messages.length, 1);
  assert.equal(warnings.length, 1);
  assert.match(JSON.stringify(warnings[0].args), /maxQueryChars=256/);
  assert.doesNotMatch(JSON.stringify(warnings[0].args), /private-fixture-query/);
  const overflow = Array.from({length:33}, (_, i) => 'term' + i).join(' ');
  const rejected = await ctx.tools.execute({callId:'recall-bounds-search',name:'memory_search',
    arguments:{query:overflow},signal:new AbortController().signal});
  assert.equal(rejected.isError, true);
  assert.match(rejected.content[0].text, /maxQueryTerms=32/);
  assert.equal((await recall('heliostat calibration handbook')).messages.length, 2);
});

test('mounted recall and native search reject candidate byte overflow without private content and recover after invalidation', async t => {
  const directory = mkdtempSync(join(tmpdir(), 'memory-candidate-bounds-')), ctx = new Context();
  t.after(async () => { try { await ctx.fiber.dispose(); } finally { rmSync(directory, { recursive: true }); } });
  const warnings = [];
  ctx.logger.exporter({ levels: { default: 3 }, export(message) { if (message.name === Memory.name && message.type === 'warn') warnings.push(message); } });
  await ctx.plugin(SessionStore); await ctx.plugin(SystemPrompt);
  await ctx.plugin(ToolRuntime, { mode: 'native' }); await ctx.plugin(Agents); await ctx.plugin(Projections);
  await ctx.plugin(Memory, { dataDir: directory, maxCandidateBytes: 64 });
  const execute = (name, args) => ctx.tools.execute({ callId: name + JSON.stringify(args).length, name, arguments: args, signal: new AbortController().signal });
  assert.equal((await execute('memory_write', { text: 'opal notebook' })).isError, false);
  const privateText = 'opal private-candidate-fixture '.repeat(8);
  assert.equal((await execute('memory_write', { text: privateText })).isError, false);
  const session = Session.create(SessionId('mounted-candidate-bounds'));
  session.append('turn/start', { turn: 1 }); const agent = { session };
  const recall = () => ctx.waterfall(scopeTarget(agent, agent), 'agent/pre-step',
    { agent, turn: 1, step: 1, signal: new AbortController().signal }, async () => ({ kind: 'allow', messages: [{ role: 'user', content: 'opal' }] }));
  assert.equal((await recall()).messages.length, 1);
  assert.equal(warnings.length, 1);
  assert.match(JSON.stringify(warnings[0].args), /maxCandidateBytes=64/);
  assert.doesNotMatch(JSON.stringify(warnings[0].args), /opal|private-candidate-fixture/);
  const rejected = await execute('memory_search', { query: 'opal' });
  assert.equal(rejected.isError, true);
  assert.match(rejected.content[0].text, /maxCandidateBytes=64/);
  assert.doesNotMatch(rejected.content[0].text, /private-candidate-fixture/);
  const manager = ctx.get('memoryManagement');
  const big = manager.list().items.find(row => row.preview.includes('private-candidate-fixture'));
  assert.ok(big);
  manager.change({ id: big.id, action: 'invalidate', expectedRevision: 0, reason: 'Synthetic candidate correction', mutationId: '00000000-0000-4000-8000-000000000019' });
  const allowed = await recall();
  assert.equal(allowed.messages.length, 2);
  assert.match(allowed.messages[1].content[0].text, /opal notebook/);
  const recovered = await execute('memory_search', { query: 'opal' });
  assert.equal(recovered.isError, false);
  assert.match(recovered.content[0].text, /opal notebook/);
  assert.equal(manager.detail(big.id).record.text, privateText.trim());
});

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
