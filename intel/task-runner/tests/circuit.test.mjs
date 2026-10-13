import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createCircuit,circuitConfig} from '../circuit.mjs';
import {runHeartbeat} from '../heartbeat.mjs';

function fixture(){
  let value=null;const events=[];
  const store={read:async()=>structuredClone(value),write:async state=>{value=structuredClone(state);}};
  const load=()=>createCircuit({taskId:'evolve_ideas',scope:'test',config:circuitConfig({enabled:true,threshold:3}),store,emit:entry=>{events.push(entry);return entry;}});
  return {load,events,store};
}
test('counts complete runs once, ignores quiet and resets on success',async()=>{
  const f=fixture();let c=f.load();
  for(const runId of ['r1','r2']){assert.equal(await c.admit(),true);await c.begin(runId);await c.complete(runId,'fail');await c.complete(runId,'fail');}
  assert.equal((await f.store.read()).failures,2);
  await c.begin('quiet');await c.complete('quiet','quiet');
  assert.equal((await f.store.read()).failures,2);
  await c.begin('success');await c.complete('success','ok');
  assert.equal((await f.store.read()).failures,0);
  for(const id of ['r3','r4','r5']){c=f.load();await c.begin(id);await c.complete(id,'fail');}
  c=f.load();assert.equal(await c.admit(),false);assert.equal(f.events.length,1);
  assert.equal(f.events[0].runId,'r5');assert.match(f.events[0].detail,/failures=3/);
  await c.admit();assert.equal(f.events.length,1);
  await assert.rejects(c.release('r4'),{code:'CIRCUIT_RELEASE_MISMATCH'});
  await c.release('r5');assert.equal(await c.admit(),true);
  await assert.rejects(c.begin('r5'),{code:'CIRCUIT_RUN_REUSED'});
  await c.begin('next-schedule');await c.complete('next-schedule','ok');
});
test('pending crash and damaged state fail closed without guessing a failure count',async()=>{
  const f=fixture();await f.load().begin('uncertain');
  await assert.rejects(f.load().admit(),{code:'CIRCUIT_RUN_UNRESOLVED'});
  await assert.rejects(f.load().release('uncertain'),{code:'CIRCUIT_RUN_UNRESOLVED'});
  await assert.rejects(f.load().complete('other','fail'),{code:'CIRCUIT_RUN_MISMATCH'});
  assert.equal((await f.store.read()).failures,0);
  await f.store.write({version:1});await assert.rejects(f.load().admit(),{code:'CIRCUIT_INVALID_STATE'});
});
test('durable outbox survives failed event append and retries the same identity',async()=>{
  const f=fixture();const config=circuitConfig({enabled:true,threshold:1});
  let calls=0;const emit=entry=>{calls++;if(calls===1)throw Error('disk');f.events.push(entry);};
  let c=createCircuit({taskId:'evolve_ideas',scope:'test',config,store:f.store,emit});
  await c.begin('r1');await c.complete('r1','fail');
  await assert.rejects(c.flush());assert.equal((await f.store.read()).openRunId,'r1');
  c=createCircuit({taskId:'evolve_ideas',scope:'test',config,store:f.store,emit});
  assert.equal(await c.admit(),false);assert.equal(f.events[0].runId,'r1');assert.equal((await f.store.read()).outbox,null);
});
test('configuration rejects invalid and unknown tuning fields early',()=>{
  for(const x of [{enabled:true,threshold:0},{enabled:true,threshold:1.5},{enabled:true,threshold:3,cooldown:1},{threshold:3}])
    assert.throws(()=>circuitConfig(x),{code:'CIRCUIT_INVALID_CONFIG'});
  assert.equal(circuitConfig(undefined),null);
});
test('open model circuit still captures actual heartbeat sources without changing pending or paying for feedback',async()=>{
  let captures=0,writes=0,executions=0;const records=[];
  const hash='a'.repeat(64),previous={version:1,signalId:hash,healthy:false,lastNowMs:1,nextDueAtMs:2,backoffMs:1,pending:{signalId:hash,runId:'old-run'}};
  const result=await runHeartbeat({nowMs:10,config:{initialBackoffMs:1,maxBackoffMs:4},
    stateStore:{read:async()=>previous,write:async()=>writes++},admit:async()=>false,
    source:{capture:async()=>{captures++;return {version:1,complete:true,observedAtMs:10,signalId:hash,checklistId:hash,empty:false,healthy:false,
      signals:['service','listener','disk','memory','journal','cron'].map(source=>({source,id:hash,status:'anomaly'}))};}},
    execute:async()=>{executions++;return 0;},record:async row=>records.push(row)});
  assert.equal(captures,1);assert.equal(writes,0);assert.equal(executions,0);assert.equal(result.exit,78);
  assert.equal(records[0].scopeSignals[0].source,'service');assert.equal(previous.pending.runId,'old-run');
});
