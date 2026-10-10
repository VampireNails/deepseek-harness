import assert from 'node:assert/strict';
import {writeFileSync,readFileSync,mkdirSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import fs from 'node:fs';
import {syncBuiltinESMExports} from 'node:module';
import {LlmAdapter,createUserMessage} from '@deepseek-ai/dsh-llm';
import {CronStore} from '../../src/store.js';
import {CronScheduler} from '../../src/scheduler.js';
import {DatabaseSync} from 'node:sqlite';
import {FtsRetriever} from '../../../dsh-intelligence-memory/src/retriever.js';
export const name='intel-tool-errors-probe';
export const inject=['tools','intelGoals','sessions','agents','llm'];
export async function apply(ctx){
 const records=[],failures=[];
 try{
  const session=ctx.sessions.create(),agent={session};
  const call=async(label,name,args,code)=>{
   const result=await ctx.tools.execute({callId:label,name,arguments:args,agent,signal:new AbortController().signal});
   records.push({label,name,result});
   try{assert.equal(result.isError,!!code);if(code){assert.equal(result.error.info?.code,code);assert.match(result.content[0].text,new RegExp(code));}assert.doesNotMatch(JSON.stringify(result),/PRIVATE_MARKER/);}
   catch(error){failures.push({label,message:error.message});}
   return result;
  };
  await call('cron-invalid','cron_create',{name:'n',prompt:'p',schedule:'wrong'},'CRON_INVALID_SCHEDULE');
  await call('cron-missing','cron_delete',{ref:'missing'},'CRON_NOT_FOUND');
  await call('cron-short','cron_create',{name:'n',prompt:'p',schedule:'每1分钟'},'CRON_INVALID_INTERVAL');
  await call('cron-empty','cron_create',{name:'n',prompt:' ',schedule:'每5分钟'},'CRON_INVALID_PROMPT');
  await call('goal-title','goal_create',{title:' '},'GOALS_INVALID_TITLE');
  await call('goal-missing','goal_progress',{ref:'missing',text:'p'},'GOALS_NOT_FOUND');
  await call('goal-status','goal_list',{status:'PRIVATE_MARKER'},'GOALS_INVALID_STATUS');
  await call('goal-empty-progress','goal_progress',{ref:'g1',text:' '},'GOALS_INVALID_PROGRESS');
  await call('memory-empty','memory_write',{text:' '},'MEMORY_INVALID_TEXT');
  await call('event-invalid','sysevents_emit',{type:' ',title:'t'},'SYSEVENTS_INVALID_ENTRY');
  const cronFile=join(process.env.DSH_HOME,'intel-cron','cron.json');writeFileSync(cronFile,'[broken');
  await call('cron-corrupt','cron_list',{},'CRON_INVALID_DATA');assert.equal(readFileSync(cronFile,'utf8'),'[broken');rmSync(cronFile);
  mkdirSync(ctx.intelGoals.mdFile);
  const partial=await call('goal-partial','goal_create',{title:'recorded'},'GOALS_PROJECTION_FAILED');
  assert.equal(ctx.intelGoals.load().goals.length,1);
  assert.match(partial.content[0].text,/JSON 已提交/);
  const listing=await call('goal-partial-list','goal_list',{},'GOALS_PROJECTION_FAILED');assert.match(listing.content[0].text,/recorded/);assert.match(listing.content[0].text,/Markdown 未同步/);
  rmSync(ctx.intelGoals.mdFile,{recursive:true});await call('goal-recovered','goal_list',{});
  await call('goal-created-second','goal_create',{title:'recorded second'});
  await call('goal-ambiguous','goal_close',{ref:'recorded'},'GOALS_AMBIGUOUS_REF');
  await call('goal-closed','goal_close',{ref:'g1'});
  await call('goal-already-closed','goal_close',{ref:'g1'},'GOALS_ALREADY_CLOSED');
  const goalFile=ctx.intelGoals.file,goalBytes=readFileSync(goalFile);writeFileSync(goalFile,'broken PRIVATE_MARKER');await call('goal-corrupt','goal_list',{},'GOALS_INVALID_DATA');assert.equal(readFileSync(goalFile,'utf8'),'broken PRIVATE_MARKER');writeFileSync(goalFile,goalBytes);
  const read=fs.readFileSync;fs.readFileSync=(file,...rest)=>{if(file===goalFile)throw Object.assign(Error('PRIVATE_MARKER'),{code:'EACCES'});return read(file,...rest);};syncBuiltinESMExports();
  try{const r=await call('goal-permission','goal_list',{},'GOALS_READ_FAILED');assert.match(r.content[0].text,/errno=EACCES/);}finally{fs.readFileSync=read;syncBuiltinESMExports();}
  const flush=ctx.sessions.flush,initialSchedules=session.ownEvents().filter(e=>e.type==='schedule/change').length;let failed=false;
  ctx.sessions.flush=async target=>{if(!failed&&target===session&&session.ownEvents().filter(e=>e.type==='schedule/change').length>initialSchedules){failed=true;throw Object.assign(Error('PRIVATE_MARKER'),{code:'EIO'});}return flush.call(ctx.sessions,target);};
  try{const r=await call('cron-partial','cron_create',{name:'pending',prompt:'p',schedule:'每5分钟'},'CRON_SCHEDULE_FAILED');assert.match(r.content[0].text,/committed=true/);assert.match(r.content[0].text,/errno=EIO/);}finally{ctx.sessions.flush=flush;}
  const cronStore=new CronStore(join(process.env.DSH_HOME,'intel-cron')),scheduler=new CronScheduler({store:cronStore,ctx});const before=session.ownEvents().filter(e=>e.type==='schedule/change').length;await scheduler.reconcile(agent);assert.equal(session.ownEvents().filter(e=>e.type==='schedule/change').length,before);
  await call('cron-created-second','cron_create',{name:'pending second',prompt:'p',schedule:'每5分钟'});
  await call('cron-ambiguous','cron_delete',{ref:'pending'},'CRON_AMBIGUOUS_REF');
  await call('cron-deleted','cron_delete',{ref:'c2'});
  for(let n=1;n<20;n++)await call('cron-fill-'+n,'cron_create',{name:'fill'+n,prompt:'p',schedule:'每5分钟'});
  const capacity=await call('cron-capacity','cron_create',{name:'denied',prompt:'p',schedule:'每5分钟'},'CRON_CAPACITY_REACHED');assert.match(capacity.content[0].text,/已用 20\/20/);
  const jobs=cronStore.load();cronStore.save([...jobs,{...jobs.at(-1),id:'c22',scheduleId:'cron-c22'}]);const exceeded=await call('cron-over-capacity','cron_create',{name:'denied',prompt:'p',schedule:'每5分钟'},'CRON_CAPACITY_REACHED');assert.match(exceeded.content[0].text,/已用 21\/20/);cronStore.save(jobs);
  const memory=await call('memory-created','memory_write',{text:'safe memory'});assert.deepEqual(memory.meta,{memoryWrite:{ok:true,id:1}});assert.equal(memory.value.id,1);
  assert.equal((await call('memory-missing','memory_read',{id:999})).value.found,false);
  assert.deepEqual((await call('memory-no-match','memory_search',{query:'unmatchedfixture'})).value.results,[]);
  for(const [method,name,args,code] of [['get','memory_read',{id:1},'MEMORY_READ_FAILED'],['search','memory_search',{query:'safe'},'MEMORY_SEARCH_FAILED'],['add','memory_write',{text:'safe'},'MEMORY_WRITE_FAILED']]){
   const original=FtsRetriever.prototype[method];FtsRetriever.prototype[method]=()=>{throw Object.assign(Error('PRIVATE_MARKER /secret prompt stack'),{code:'PRIVATE_MARKER',errno:'EIO',committed:true});};
   try{const r=await call('memory-private-'+method,name,args,code);assert.doesNotMatch(r.content[0].text,/JSON 已提交|committed=true|errno=/);}finally{FtsRetriever.prototype[method]=original;}
  }
  const memoryDb=new DatabaseSync(join(process.env.DSH_HOME,'intel-memory','memory.db'));memoryDb.exec('BEGIN IMMEDIATE');try{await call('memory-write-locked','memory_write',{text:'PRIVATE_MARKER'},'MEMORY_WRITE_FAILED');}finally{memoryDb.exec('ROLLBACK');memoryDb.close();}
  const write=fs.writeSync;fs.writeSync=()=>{throw Object.assign(Error('PRIVATE_MARKER'),{code:'ENOSPC'});};syncBuiltinESMExports();
  try{await call('event-write-failed','sysevents_emit',{type:'test.failure',title:'safe'},'SYSEVENTS_WRITE_FAILED');}finally{fs.writeSync=write;syncBuiltinESMExports();}
  await call('event-created','sysevents_emit',{type:'test.success',title:'safe'});
  const sync=fs.fsyncSync;fs.fsyncSync=()=>{throw Object.assign(Error('PRIVATE_MARKER'),{code:'EIO',committed:true});};syncBuiltinESMExports();
  try{const r=await call('event-sync-partial','sysevents_emit',{type:'test.partial',title:'safe'},'SYSEVENTS_WRITE_FAILED');assert.doesNotMatch(r.content[0].text,/JSON 已提交|committed=true/);}finally{fs.fsyncSync=sync;syncBuiltinESMExports();}
  const listed=await call('event-partial-confirmed','sysevents_list',{});assert.match(listed.content[0].text,/test.partial/);
  const eventFile=join(process.env.DSH_HOME,'intel-system-events','events.jsonl'),eventBytes=readFileSync(eventFile);writeFileSync(eventFile,'broken PRIVATE_MARKER');await call('event-corrupt','sysevents_list',{},'SYSEVENTS_INVALID_DATA');writeFileSync(eventFile,eventBytes);
  const original=ctx.intelGoals.logProgress;
  ctx.intelGoals.logProgress=()=>{throw Object.assign(Error('PRIVATE_MARKER /secret prompt stack'),{code:'PRIVATE_MARKER',errno:'PRIVATE_MARKER',committed:true});};
  try{const r=await call('goal-private','goal_progress',{ref:'g1',text:'p'},'GOALS_OPERATION_FAILED');assert.doesNotMatch(r.content[0].text,/JSON 已提交|committed=true|errno=/);}finally{ctx.intelGoals.logProgress=original;}
  const aborted=new AbortController();aborted.abort(Error('PRIVATE_MARKER'));const cancelled=await ctx.tools.execute({callId:'cancelled',name:'cron_delete',arguments:{ref:'c1'},agent,signal:aborted.signal});assert.equal(cancelled.isError,true);assert.equal(cronStore.load().length,20);records.push({label:'pre-dispatch-cancel',name:'cron_delete',result:cancelled});
  await ctx.sessions.flush(session);
  const script=[{id:'snapshot-cron',name:'cron_delete',args:{ref:'missing'}},{id:'snapshot-goal',name:'goal_progress',args:{ref:'missing',text:'p'}},{id:'snapshot-memory-failure',name:'memory_write',args:{text:' '}},{id:'snapshot-memory-success',name:'memory_write',args:{text:'recorded memory'}},{id:'snapshot-event',name:'sysevents_emit',args:{type:' ',title:'t'}}];
  class Replay extends LlmAdapter{
   requests=[];
   async resolveModel(provider,model){return {provider,id:model,name:model};}
   async *stream(options){this.requests.push(options);const call=script.shift();if(call){const block={type:'tool-call',id:call.id,name:call.name,arguments:JSON.stringify(call.args)};yield {type:'block-start',index:0,blockType:'tool-call'};yield {type:'tool-call-delta',index:0,id:block.id,name:block.name,argumentsDelta:block.arguments};yield {type:'block-end',index:0,block};yield {type:'finish',reason:{kind:'tool-calls'}};}else{yield {type:'block-start',index:0,blockType:'text'};yield {type:'text-delta',index:0,text:'DONE'};yield {type:'block-end',index:0,block:{type:'text',text:'DONE'}};yield {type:'finish',reason:{kind:'stop'}};}}
  }
  const replay=new Replay(),disposeAdapter=ctx.llm.registerAdapter(['t09-replay'],replay);
  const handle=await ctx.agents.create({sessionId:'t09-recorded-session',agentOptions:{provider:'t09-replay',model:'keyless'}});
  try{
   const idle=new Promise(resolve=>{const dispose=ctx.on('agent/status',({agent:subject,status})=>{if(subject===handle.agent&&status==='idle'){dispose();resolve();}});});
   handle.agent.followup(createUserMessage({content:[{type:'text',text:'Verify recorded intel errors.'}],source:{kind:'user'}}));await idle;
   const events=handle.agent.session.ownEvents().filter(e=>e.type==='tool/result');records.push({label:'loop-diagnostic',types:handle.agent.session.ownEvents().map(e=>e.type),events:events.map(e=>e.data),requests:replay.requests.length});assert.equal(events.length,5);assert.deepEqual(events.map(e=>e.data.message.isError),[true,true,true,false,true]);
   const modelResults=handle.agent.session.deriveMessages().filter(m=>m.role==='tool');assert.equal(modelResults.length,5);assert.deepEqual(modelResults.map(m=>m.isError),[true,true,true,false,true]);
   assert.deepEqual(events[3].data.meta,{memoryWrite:{ok:true,id:2}});assert.equal(events[0].data.error.code,'CRON_NOT_FOUND');assert.equal(events[1].data.error.code,'GOALS_NOT_FOUND');
   records.push({label:'actual-loop',events:events.map(e=>e.data),modelResults});await ctx.sessions.flush(handle.agent.session);
  }finally{await handle.dispose();disposeAdapter();}
  writeFileSync(process.env.T09_RECEIPT,JSON.stringify({ok:failures.length===0,records,failures})+'\n');
 }catch(error){writeFileSync(process.env.T09_RECEIPT,JSON.stringify({ok:false,records,failures,message:error.message,stack:error.stack})+'\n');}
}
