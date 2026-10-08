import {test} from 'node:test';
import assert from 'node:assert/strict';
import {runTask} from '../runner.mjs';

const config={initialBackoffMs:100,maxBackoffMs:400,maxBatchMessages:3,maxBatchChars:1024};
const committed={version:1,cursors:{'anonymous-session-a':10,'anonymous-session-b':4},
  backoffMs:0,nextDueAtMs:0,lastNowMs:0};
const proposedCursors={'anonymous-session-a':11,'anonymous-session-b':5};
const messages=[
  {sessionId:'anonymous-session-a',seq:11,messageId:'private-message-marker-a',text:'private upkeep text marker alpha'},
  {sessionId:'anonymous-session-b',seq:5,messageId:'private-message-marker-b',text:'private upkeep text marker beta'},
];
const signal={cursors:proposedCursors,messages,counts:{sessions:2,messages:2,chars:61}};
const complete={complete:true,failedWrites:0,writeIds:[]};
const clone=value=>structuredClone(value);
test('source authorship labels survive dispatch while unknown legacy inputs remain eligible',async()=>{
  const {runUpkeep}=await import('../upkeep.mjs');
  const f=fixture();
  f.captureResult.messages[0].inputOrigin={kind:'human',basis:'host-message-attribution'};
  f.captureResult.messages[1].inputOrigin={kind:'unknown',basis:'user-channel-only'};
  await f.invoke(runUpkeep);
  assert.deepEqual(f.executions[0].messages,f.captureResult.messages);
  assertPromptFree(f.audit);
});
test('machine or invalid authorship on the source wire cannot enter a model batch or advance checkpoints',async()=>{
  const {runUpkeep}=await import('../upkeep.mjs');
  for(const origin of [{kind:'machine',basis:'host-message-attribution'},{kind:'user',basis:'user-channel-only'},
    {kind:'human',basis:'user-channel-only'},{kind:'unknown',basis:'host-message-attribution'},null]){
    const f=fixture();f.captureResult.messages[0].inputOrigin=origin;
    await assert.rejects(()=>f.invoke(runUpkeep),{code:'UPKEEP_INVALID_SOURCE'});
    assert.equal(f.executions.length,0);assert.deepEqual(f.readState(),committed);
  }
});

function fixture(initial=committed){
  let state=clone(initial);
  const captured=[],executions=[],inspections=[],writes=[],audit=[];
  const f={captured,executions,inspections,writes,audit,readState:()=>clone(state),
    captureResult:clone(signal),executeResult:0,inspectResult:clone(complete),
    source:{async capture(request){captured.push(clone(request));return clone(f.captureResult);}},
    async execute(request){executions.push(clone(request));return f.executeResult;},
    async inspect(request){inspections.push(clone(request));return clone(f.inspectResult);},
    async record(entry){audit.push(clone(entry));},
    stateStore:{async read(){return clone(state);},async write(next){writes.push(clone(next));state=clone(next);}},
  };
  f.invoke=(runUpkeep,nowMs=1000)=>runUpkeep({config,nowMs,stateStore:f.stateStore,source:f.source,
    execute:request=>f.execute(request),inspect:request=>f.inspect(request),record:entry=>f.record(entry)});
  return f;
}

function assertPromptFree(audit){
  const output=JSON.stringify(audit);
  for(const marker of ['private upkeep text marker','private-message-marker','anonymous-session-a','anonymous-session-b'])
    assert.equal(output.includes(marker),false,'Audit must not contain captured text, messages or session identifiers');
}

function stableFailure(error){
  assert.match(error.code??'',/^UPKEEP_[A-Z0-9_]+$/,'Upkeep failures must expose a stable error code');
  assert.equal(error.message.includes('private'),false,'Failure messages must not expose private provider text');
  return true;
}

test('baseline raw runner dispatches upkeep twice despite two empty successful executions',async()=>{
  const routes={defaultRoute:'fixture',routes:{fixture:{provider:'anonymous-provider',model:'anonymous-model'}},
    tasks:{evolve_upkeep:{route:'fixture',retrySafe:false,visibleOutput:'optional'}}};
  const seen=[],audit=[];
  for(const runId of ['anonymous-baseline-first','anonymous-baseline-second']){
    const exit=await runTask(routes,'evolve_upkeep',undefined,async plan=>{seen.push(plan);return 0;},entry=>audit.push(entry),
      {runId,runDate:'2026-10-05'});
    assert.equal(exit,0);
  }
  assert.equal(seen.length,2,'The existing raw runner has no empty-signal admission or persisted backoff');
  assert.deepEqual(seen.map(plan=>plan.template),['memory_upkeep','memory_upkeep']);
  assert.equal(audit.filter(entry=>entry.kind==='start').length,2);
  assert.equal(audit.filter(entry=>entry.kind==='finish').length,2);
});

test('first run bootstraps an acknowledged cut without executing historical messages',async()=>{
  const {runUpkeep}=await import('../upkeep.mjs');
  const f=fixture(null);
  await f.invoke(runUpkeep,1000);
  assert.equal(f.captured.length,1);
  assert.equal(f.captured[0].bootstrap,true);
  assert.deepEqual(f.captured[0].cursors,{});
  assert.equal(f.captured[0].maxBatchMessages,config.maxBatchMessages);
  assert.equal(f.captured[0].maxBatchChars,config.maxBatchChars);
  assert.equal(f.executions.length,0);
  assert.equal(f.inspections.length,0);
  assert.equal(f.writes.length,1);
  const state=f.readState();
  assert.equal(state.version,1);
  assert.deepEqual(state.cursors,proposedCursors);
  assert.equal(state.lastNowMs,1000);
  assert.equal(state.pending??null,null);
  assertPromptFree(f.audit);
});

test('bootstrap capture or durable checkpoint failure cannot launch the model or invent state',async t=>{
  const {runUpkeep}=await import('../upkeep.mjs');
  for(const failure of ['capture','write'])await t.test(failure,async()=>{
    const f=fixture(null);
    if(failure==='capture')f.source.capture=async()=>{throw Error('private capture payload marker');};
    else f.stateStore.write=async()=>{throw Error('private durable store marker');};
    await assert.rejects(()=>f.invoke(runUpkeep),stableFailure);
    assert.equal(f.readState(),null);
    assert.equal(f.executions.length,0);
    assert.equal(f.inspections.length,0);
    assertPromptFree(f.audit);
  });
});

test('empty cuts persist exponential capped backoff and equality reopens capture without a model',async()=>{
  const {runUpkeep}=await import('../upkeep.mjs');
  const f=fixture();
  f.captureResult={cursors:clone(committed.cursors),messages:[],counts:{sessions:2,messages:0,chars:0}};
  for(const [nowMs,backoff] of [[1000,100],[1100,200],[1300,400],[1700,400]]){
    const calls=f.captured.length;
    await f.invoke(runUpkeep,nowMs);
    assert.equal(f.captured.length,calls+1,'Capture is eligible at exact nextDueAtMs');
    const state=f.readState();
    assert.deepEqual(state.cursors,committed.cursors);
    assert.equal(state.backoffMs,backoff);
    assert.equal(state.nextDueAtMs,nowMs+backoff);
    assert.equal(state.lastNowMs,nowMs);
    const writes=f.writes.length;
    await f.invoke(runUpkeep,nowMs+backoff-1);
    assert.equal(f.captured.length,calls+1,'An early backoff invocation must avoid the source');
    assert.equal(f.writes.length,writes,'An early invocation must not change the durable checkpoint');
    assert.equal(f.executions.length,0);
    assert.equal(f.inspections.length,0);
  }
  assert.ok(f.audit.some(entry=>entry.counts?.messages===0||entry.messages===0),'Empty work must record prompt-free counts');
  assertPromptFree(f.audit);
});

test('clock rollback cannot regress the committed cut or its eligibility timestamp',async()=>{
  const {runUpkeep}=await import('../upkeep.mjs');
  const f=fixture();
  f.captureResult={cursors:clone(committed.cursors),messages:[],counts:{sessions:2,messages:0,chars:0}};
  await f.invoke(runUpkeep,1000);
  const before=f.readState(),writes=f.writes.length,captures=f.captured.length;
  await f.invoke(runUpkeep,900);
  assert.deepEqual(f.readState(),before);
  assert.equal(f.writes.length,writes);
  assert.equal(f.captured.length,captures);
  assert.equal(f.executions.length,0);
  assertPromptFree(f.audit);
});

test('a signal durably stores pending identity before execute and commits only after that run is verified',async()=>{
  const {runUpkeep}=await import('../upkeep.mjs');
  const f=fixture();
  let pendingAtExecute;
  const execute=f.execute;
  f.execute=async request=>{
    pendingAtExecute=f.readState().pending;
    assert.deepEqual(f.readState().cursors,committed.cursors,'Committed cursors cannot move before side effects are verified');
    assert.equal(f.writes.length,1,'Pending identity must be durable before launching execute');
    assert.deepEqual(pendingAtExecute.proposedCursors,proposedCursors);
    assert.match(pendingAtExecute.runId,/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
    assert.match(request.batchHash,/^[a-f0-9]{64}$/);
    assert.equal(request.batchHash,pendingAtExecute.batchHash);
    assert.deepEqual(request.messages,messages);
    return execute(request);
  };
  await f.invoke(runUpkeep,1000);
  assert.equal(f.captured[0].bootstrap,false);
  assert.deepEqual(f.captured[0].cursors,committed.cursors);
  assert.equal(f.captured[0].maxBatchMessages,config.maxBatchMessages);
  assert.equal(f.captured[0].maxBatchChars,config.maxBatchChars);
  assert.equal(f.executions.length,1);
  assert.deepEqual(f.inspections,[{runId:pendingAtExecute.runId}]);
  assert.equal(f.writes.length,2);
  assert.deepEqual(f.readState().cursors,proposedCursors);
  assert.equal(f.readState().pending??null,null);
  assert.equal(f.readState().backoffMs,0);
  assert.equal(f.readState().lastNowMs,1000);
  assertPromptFree(f.audit);
});

test('execution and verification failures retain old committed cursors plus pending identity without an immediate replay',async t=>{
  const {runUpkeep}=await import('../upkeep.mjs');
  const cases=[
    ['execute throws',f=>{f.execute=async request=>{f.executions.push(clone(request));throw Error('private execute text marker');};},0],
    ['nonzero exit',f=>{f.executeResult=7;},0],
    ['inspection throws',f=>{f.inspect=async request=>{f.inspections.push(clone(request));throw Error('private inspection text marker');};},1],
    ['incomplete receipt',f=>{f.inspectResult={...complete,complete:false};},1],
    ['failed memory write',f=>{f.inspectResult={...complete,failedWrites:1};},1],
    ['missing receipt',f=>{f.inspectResult=null;},1],
  ];
  for(const [label,prepare,inspectionCount] of cases)await t.test(label,async()=>{
    const f=fixture();prepare(f);
    await assert.rejects(()=>f.invoke(runUpkeep),stableFailure);
    assert.deepEqual(f.readState().cursors,committed.cursors);
    assert.deepEqual(f.readState().pending.proposedCursors,proposedCursors);
    assert.equal(f.executions.length,1);
    assert.equal(f.inspections.length,inspectionCount);
    assert.equal(f.writes.length,1,'A failed run must not commit its proposed cursor');
    assertPromptFree(f.audit);
  });
});

test('a later invocation recovers a verified pending run without capturing or executing its messages again',async()=>{
  const {runUpkeep}=await import('../upkeep.mjs');
  const pending={runId:'11111111-1111-4111-8111-111111111111',batchHash:'a'.repeat(64),proposedCursors:clone(proposedCursors)};
  const f=fixture({...clone(committed),pending});
  await f.invoke(runUpkeep,1000);
  assert.deepEqual(f.inspections,[{runId:pending.runId}]);
  assert.equal(f.captured.length,0);
  assert.equal(f.executions.length,0);
  assert.equal(f.writes.length,1);
  assert.deepEqual(f.readState().cursors,proposedCursors);
  assert.equal(f.readState().pending??null,null);
  assertPromptFree(f.audit);
});

test('unresolved prior pending work refuses a new capture or execution and preserves durable state',async t=>{
  const {runUpkeep}=await import('../upkeep.mjs');
  const pending={runId:'22222222-2222-4222-8222-222222222222',batchHash:'b'.repeat(64),proposedCursors:clone(proposedCursors)};
  for(const result of [null,{...complete,complete:false},{...complete,failedWrites:1},'throw'])await t.test(JSON.stringify(result),async()=>{
    const initial={...clone(committed),pending:clone(pending)},f=fixture(initial);
    if(result==='throw')f.inspect=async request=>{f.inspections.push(clone(request));throw Error('private prior-run receipt marker');};
    else f.inspectResult=result;
    await assert.rejects(()=>f.invoke(runUpkeep),error=>{
      stableFailure(error);assert.equal(error.code,'UPKEEP_PREVIOUS_RUN_UNRESOLVED');return true;
    });
    assert.deepEqual(f.inspections,[{runId:pending.runId}]);
    assert.equal(f.captured.length,0);
    assert.equal(f.executions.length,0);
    assert.equal(f.writes.length,0);
    assert.deepEqual(f.readState(),initial);
    assertPromptFree(f.audit);
  });
});

test('failed pending persistence stops execute and keeps the committed checkpoint unchanged',async()=>{
  const {runUpkeep}=await import('../upkeep.mjs');
  const f=fixture();
  f.stateStore.write=async()=>{throw Error('private pending-state write marker');};
  await assert.rejects(()=>f.invoke(runUpkeep),stableFailure);
  assert.deepEqual(f.readState(),committed);
  assert.equal(f.executions.length,0);
  assert.equal(f.inspections.length,0);
  assertPromptFree(f.audit);
});

test('failed final checkpoint persistence leaves durable pending for recovery instead of reexecuting side effects',async()=>{
  const {runUpkeep}=await import('../upkeep.mjs');
  const f=fixture(),write=f.stateStore.write;
  let attempts=0;
  f.stateStore.write=async next=>{
    if(++attempts===2)throw Error('private final checkpoint marker');
    return write(next);
  };
  await assert.rejects(()=>f.invoke(runUpkeep),stableFailure);
  const pending=f.readState().pending;
  assert.ok(pending);
  assert.deepEqual(f.readState().cursors,committed.cursors);
  assert.equal(f.executions.length,1);
  assert.equal(f.inspections.length,1);
  await f.invoke(runUpkeep,1001);
  assert.equal(f.captured.length,1,'Pending recovery must not capture a fresh source batch');
  assert.equal(f.executions.length,1,'A completed side effect must not be replayed after state-write failure');
  assert.deepEqual(f.inspections,[{runId:pending.runId},{runId:pending.runId}]);
  assert.deepEqual(f.readState().cursors,proposedCursors);
  assert.equal(f.readState().pending??null,null);
  assertPromptFree(f.audit);
});
