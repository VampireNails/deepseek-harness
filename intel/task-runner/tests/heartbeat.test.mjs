import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {runHeartbeat} from '../heartbeat.mjs';

const config={initialBackoffMs:10,maxBackoffMs:40};
const id=n=>n.toString(16).padStart(64,'0');
function fixture(){
  let state=null,cut;const rows=[],runs=[];
  const f={rows,runs,get state(){return state;},set state(value){state=value;},
    observe(n,healthy=false){cut={version:1,complete:true,observedAtMs:0,signalId:id(n),checklistId:id(9),healthy,empty:false,signals:['service','listener','disk','memory','journal','cron'].map((source,i)=>({source,id:id(n+i),status:healthy||i!==0?'healthy':'anomaly'}))};},
    async run(now,extra={}){cut.observedAtMs=now;return runHeartbeat({config,nowMs:now,source:{capture:async()=>structuredClone(cut)},stateStore:{read:async()=>structuredClone(state),write:async value=>{state=structuredClone(value);}},execute:async batch=>{runs.push(batch);return 0;},record:async row=>rows.push(row),...extra});}};
  f.observe(1);return f;
}
test('same anomaly doubles due intervals to the configured cap; healthy numeric noise is outside identity',async()=>{
  const f=fixture();await f.run(100);assert.equal(f.runs.length,1);await f.run(109);assert.equal(f.runs.length,1);await f.run(110);assert.equal(f.state.nextDueAtMs,130);
  await f.run(130);assert.equal(f.state.nextDueAtMs,170);await f.run(170);assert.equal(f.state.nextDueAtMs,210);assert.equal(f.runs.length,4);
  f.observe(2,true);await f.run(171);assert.equal(f.runs.length,5);await f.run(172);assert.equal(f.runs.length,5);
});
test('clock rollback fails same anomaly but does not hide a new anomaly or recovery',async()=>{
  const f=fixture();await f.run(100);await assert.rejects(f.run(90),{code:'HEARTBEAT_CLOCK_ROLLBACK'});assert.equal(f.runs.length,1);
  f.observe(2);await f.run(80);assert.equal(f.runs.length,2);assert.equal(f.state.lastNowMs,100);f.observe(3,true);await f.run(70);assert.equal(f.runs.length,3);assert.equal(f.rows.at(-1).clockRollback,true);
});
test('crash after admission and nonzero execution persist pending; new signal supersedes it without replay',async()=>{
  const f=fixture();await assert.rejects(f.run(100,{execute:async()=>{throw Error('interrupted');}}));const pending=f.state.pending;
  await assert.rejects(f.run(110),{code:'HEARTBEAT_PREVIOUS_RUN_UNRESOLVED'});assert.equal(f.state.pending.runId,pending.runId);
  f.observe(2);const failed=await f.run(111,{execute:async()=>8});assert.equal(failed.exit,8);assert.ok(f.state.pending);assert.equal(f.rows.find(r=>r.kind==='heartbeat-pending-superseded').previousRunId,pending.runId);
  f.observe(3,true);await f.run(112);assert.equal(f.state.pending,null);
});
test('invalid/partial sources and corrupted state never advance or dispatch',async()=>{
  const f=fixture();await f.run(100);const before=structuredClone(f.state);
  await assert.rejects(f.run(101,{source:{capture:async()=>({complete:false})}}),{code:'HEARTBEAT_INVALID_SOURCE'});assert.deepEqual(f.state,before);assert.equal(f.runs.length,1);
  f.state={version:1};await assert.rejects(f.run(102),{code:'HEARTBEAT_INVALID_STATE'});
  f.state={...before,nextDueAtMs:before.nextDueAtMs+1};await assert.rejects(f.run(102),{code:'HEARTBEAT_INVALID_STATE'});
});
test('pending persistence and admission audit failures prevent model dispatch',async()=>{
  const f=fixture();await assert.rejects(f.run(100,{stateStore:{read:async()=>null,write:async()=>{throw Error('fsync refused');}}}));assert.equal(f.runs.length,0);
  await assert.rejects(f.run(100,{record:async()=>{throw Error('audit refused');}}));assert.equal(f.runs.length,0);assert.ok(f.state.pending);
});
test('stable prompt-free heartbeat decision output matches the owner-local snapshot',async()=>{
  const f=fixture();f.observe(1,true);await f.run(100);await f.run(101);f.observe(2);await f.run(102);await f.run(103);f.observe(3,true);await f.run(104);
  const shown=f.rows.map(({kind,reason,observedAtMs,modelExecutions,exit})=>JSON.stringify({kind,reason,observedAtMs,...modelExecutions!==undefined?{modelExecutions}:{},...exit!==undefined?{exit}:{}})).join('\n')+'\n';
  assert.equal(shown,await readFile(new URL('./expected/heartbeat-decisions.txt',import.meta.url),'utf8'));
});
