import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,mkdirSync,writeFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {AutomationSessionStore} from '../../dsh-intelligence-evolution/src/session-origin.js';
import {aggregateSessions} from '../src/aggregator.js';

const startMs=Date.parse('2026-10-13T16:00:00Z'),endMs=startMs+6*3600000;
function fixture(t){
  const home=mkdtempSync(join(tmpdir(),'scope-usage-'));
  const root=join(home,'sessions','--root--'),joblog=join(home,'intel-joblog');mkdirSync(root,{recursive:true});
  const store=new AutomationSessionStore(joblog);
  const db=new DatabaseSync(join(joblog,'job_history.sqlite'));
  t.after(()=>{store.close();db.close();rmSync(home,{recursive:true,force:true});});
  db.exec('CREATE TABLE runs(scope TEXT,runId TEXT,row TEXT); CREATE TABLE latest(scope TEXT,id TEXT,row TEXT); PRAGMA user_version=1');
  const logs=new Map();
  function log(id,header={},usageEvents){
    const file=join(root,id,'session.v4.jsonl.zstd');mkdirSync(join(root,id));writeFileSync(file,'');
    const rows=[{type:'session',version:4,id,createdAt:startMs,isSeeded:false,...header},
      {seq:0,type:'user/message',time:startMs,data:{content:[{text:'Heartbeat name is not scope'}]}},
      ...(usageEvents??[{seq:1,type:'assistant/message',time:startMs,data:{usage:{inputTokens:10,outputTokens:3,cacheReadTokens:4}}}])];
    logs.set(file,rows.map(JSON.stringify).join('\n'));return rows;
  }
  function identity(id,taskId,runId){store.record(id,{taskId,runId,runDate:'2026-10-14'});}
  function completion(runId,id,scope,table='runs'){
    const row=JSON.stringify({runId,id,scope});db.prepare(table==='runs'?'INSERT INTO runs VALUES(?,?,?)':'INSERT INTO latest VALUES(?,?,?)').run(scope,table==='runs'?runId:id,row);
  }
  const aggregate=()=>aggregateSessions(root,7,file=>logs.get(file),{window:{startMs,endMs}});
  return {home,root,db,logs,log,identity,completion,aggregate};
}
function conserved(v){for(const bucket of [v.byTask,v.byScope,v.byDay,v.byModel])for(const field of Object.keys(v.details))assert.equal(Object.values(bucket).reduce((n,x)=>n+x[field],0),v.details[field]);for(const bucket of [v.byTask,v.byScope])assert.equal(Object.values(bucket).reduce((n,x)=>n+x.sessions,0),v.sessions);}

test('task and scope use exact immutable session/run joins, separating production, test and unknown for the same task',t=>{
  const f=fixture(t);for(const [id,scope] of [['a','production'],['b','test'],['c',null]]){f.log(id);f.identity(id,'test_named_task','run-'+id);if(scope)f.completion('run-'+id,'test_named_task',scope);}
  // Latest and history may repeat one completion; they do not add tokens or sessions.
  f.completion('run-a','test_named_task','production','latest');
  const v=f.aggregate();assert.deepEqual(Object.values(v.byTask).map(x=>x.scope).sort(),['production','test','unclassified']);
  assert.equal(v.byScope.production.sessions,1);assert.equal(v.byScope.unknown.sessions,1);assert.equal(v.taskUsage.scopeAvailable,true);
  assert.equal(v.taskUsage.scopeCoverage,'partial');assert.deepEqual(v.window,{startMs,endMs,timeZone:'UTC',basis:'assistant-message-time',endExclusive:true});conserved(v);
});
test('same run in both scopes and disagreement about task stay conflict without discarding usage',t=>{
  const f=fixture(t);f.log('a');f.identity('a','task_a','run-a');f.completion('run-a','task_a','test');f.completion('run-a','task_a','production');
  let v=f.aggregate();assert.equal(Object.values(v.byTask)[0].scopeStatus,'conflict');assert.equal(v.byScope.conflict.input,10);conserved(v);
  f.db.exec('DELETE FROM runs');f.completion('run-a','another_task','production');v=f.aggregate();assert.equal(Object.values(v.byTask)[0].classification,'conflict');conserved(v);
});
test('native subagent lineage inherits task/scope but fork prefix is owned by its original session only',t=>{
  const f=fixture(t);f.log('parent');f.identity('parent','task_a','run-a');f.completion('run-a','task_a','production');
  const inherited={seq:1,type:'assistant/message',time:startMs,data:{usage:{inputTokens:999,outputTokens:999}}};
  f.log('child',{origin:'subagent',parentSession:'parent',isSeeded:true},[inherited,{seq:2,type:'session/end-seed',time:startMs,data:{inherited:true}},
    {...inherited,seq:3,data:{usage:{inputTokens:5,outputTokens:2,cacheReadTokens:1}}}]);
  const v=f.aggregate();assert.equal(v.sessions,2);assert.equal(v.details.input,15);assert.equal(v.byScope.production.input,15);assert.equal(Object.values(v.byTask)[0].id,'task_a');conserved(v);
});
test('unseeded end-seed markers keep live usage; closed window excludes its upper boundary and ignores mtime',t=>{
  const f=fixture(t);f.log('a',{},[{seq:1,type:'assistant/message',time:startMs-1,data:{usage:{inputTokens:999,outputTokens:999}}},
    {seq:2,type:'session/end-seed',time:startMs,data:{}},
    {seq:3,type:'assistant/message',time:startMs,data:{usage:{inputTokens:10,outputTokens:3}}},
    {seq:4,type:'assistant/message',time:endMs,data:{usage:{inputTokens:999,outputTokens:999}}}]);
  const v=f.aggregate();assert.equal(v.details.input,10);assert.equal(v.byScope.unknown.input,10);conserved(v);
});
test('broken seeded ownership or lineage cannot become an exact scope',t=>{
  const f=fixture(t);f.log('a',{isSeeded:true});assert.throws(f.aggregate,/TOKENLOG_LOG_INVALID/);
  f.logs.clear();f.log('b',{origin:'subagent',parentSession:'missing'});rmSync(join(f.root,'a'),{recursive:true});
  const v=f.aggregate();assert.equal(Object.values(v.byTask)[0].classification,'unknown');assert.equal(v.byScope.unknown.sessions,1);
});
test('uncommitted completion and a reopened reader retain unknown until atomic completion commits',t=>{
  const f=fixture(t);f.log('a');f.identity('a','task_a','run-a');
  f.db.exec('BEGIN IMMEDIATE');f.completion('run-a','task_a','production');
  assert.equal(f.aggregate().byScope.unknown.sessions,1);f.db.exec('ROLLBACK');assert.equal(f.aggregate().byScope.unknown.sessions,1);
  f.completion('run-a','task_a','production');assert.equal(f.aggregate().byScope.production.sessions,1);
});
test('cyclic subagent ancestry is conflict, ordinary forks do not inherit task and missing stable headers fail',t=>{
  const f=fixture(t);f.log('a',{origin:'subagent',parentSession:'b'});f.log('b',{origin:'subagent',parentSession:'a'});
  assert.equal(f.aggregate().byScope.conflict.sessions,2);
  f.logs.clear();rmSync(join(f.root,'a'),{recursive:true});rmSync(join(f.root,'b'),{recursive:true});
  f.log('root');f.identity('root','task_a','run-a');f.completion('run-a','task_a','production');f.log('fork',{parentSession:'root'});
  assert.equal(f.aggregate().byScope.unknown.sessions,1);
  const file=join(f.root,'fork','session.v4.jsonl.zstd');f.logs.set(file,JSON.stringify({type:'assistant/message',time:startMs,data:{usage:{inputTokens:1,outputTokens:1}}}));
  assert.throws(f.aggregate,/TOKENLOG_LOG_INVALID/);
});
test('an unmatched seeded fork cannot infer its own task from the inherited parent prompt',t=>{
  const f=fixture(t);f.log('fork',{isSeeded:true,parentSession:'unavailable-parent'},[
    {seq:2,type:'session/end-seed',time:startMs,data:{inherited:true}},
    {seq:3,type:'assistant/message',time:startMs,data:{usage:{inputTokens:10,outputTokens:3}}}]);
  const v=f.aggregate();assert.equal(Object.values(v.byTask)[0].classification,'unknown');assert.equal(v.byScope.unknown.sessions,1);
});
test('malformed completion data fails without heuristic fallback and recovers after the owned corrupt row is removed',t=>{
  const f=fixture(t);f.log('a');f.identity('a','task_a','run-a');f.db.prepare('INSERT INTO runs VALUES(?,?,?)').run('production','run-a','{partial');
  assert.throws(f.aggregate,/TOKENLOG_METADATA_INVALID/);f.db.exec('DELETE FROM runs');assert.equal(f.aggregate().byScope.unknown.sessions,1);
});
