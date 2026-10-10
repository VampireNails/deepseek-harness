import {test} from 'node:test';
import assert from 'node:assert/strict';
import {runTask} from '../runner.mjs';
import {classifyFailure} from '../failure.mjs';

const config={timeoutMs:1000,defaultRoute:'primary',routes:{primary:{provider:'p',model:'a'},backup:{provider:'q',model:'b'}},
  tasks:{custom:{route:'primary',fallback:'backup',retrySafe:false}},fallback:{enabled:true,maxAttempts:2,totalTimeoutMs:2000}};
const receipt={version:1,sessions:1,steps:1,requests:1,tools:0,outputs:0,retries:0,complete:true,streamOutput:false,modelMatches:true,
  provider:'p',model:'a',end:'error',failure:{code:'QUOTA',status:402}};
test('reliable initial quota rejection permits one bounded fallback for a side-effecting task',async()=>{
  const audit=[],attempts=[];
  const exit=await runTask(config,'custom','private prompt',async plan=>{
    attempts.push({...plan});return attempts.length===1?{exit:1,receipt}:{exit:0};
  },row=>audit.push(row),{runId:'routing-id',runDate:'2026-10-10'});
  assert.equal(exit,0);assert.equal(attempts.length,2);assert.equal(attempts[1].model,'b');
  assert.equal(attempts[1].runId,'routing-id');assert.equal(audit.at(-1).attempts,2);
  assert.equal(audit.filter(row=>row.kind==='finish').length,1);
  assert.equal(audit.find(row=>row.kind==='fallback').fallbackReason,'pre-execution-rejection');
  assert.equal(JSON.stringify(audit).includes('private prompt'),false);
});
test('tool quota text, partial output, late refusal, network and unknown process results cannot replay',async()=>{
  for(const altered of [{tools:1,toolFailed:true},{outputs:1},{streamOutput:true},{steps:2},{retries:1},
    {failure:{code:'QUOTA'}},{failure:{code:'NETWORK'}},{failure:{code:'SERVER'}},{failure:{code:'SERVER',status:503}},{complete:false}]){
    const calls=[],audit=[];
    const exit=await runTask({...config,tasks:{custom:{...config.tasks.custom,retrySafe:true}}},'custom','text',async plan=>{calls.push(plan);return {exit:1,receipt:{...receipt,...altered}};},row=>audit.push(row));
    assert.equal(exit,1);assert.equal(calls.length,1);assert.equal(audit.at(-1).kind,'finish');
    assert.equal(audit.some(row=>row.kind==='retry-refused'),true);
  }
  for(const result of [{exit:124,receipt},{exit:1,signal:'SIGTERM',receipt},{exit:1},{exit:65,receipt}]){
    assert.equal(classifyFailure(result).preExecution,false);
  }
});
test('authentication, rate limit, quota, service, config, tool and unknown have stable safe codes',()=>{
  for(const [code,status,category,safe] of [['AUTH',401,'authentication',false],['RATE_LIMIT',429,'rate-limit',true],
    ['QUOTA',402,'quota',true],['SERVER',503,'service',false],['NO_MODEL',undefined,'model-config',false],
    ['SECRET_PATH',undefined,'unknown',false]]){
    const actual=classifyFailure({exit:1,receipt:{...receipt,failure:{code,status}}});
    assert.equal(actual.category,category);assert.equal(actual.preExecution,safe);
    assert.equal(JSON.stringify(actual).includes('SECRET_PATH'),false);
  }
});
test('disabled or absent fallback config preserves one process and original exit',async()=>{
  for(const fallback of [undefined,{enabled:false}]){
    let calls=0;
    const exit=await runTask({...config,fallback},'custom','text',async()=>{calls++;return {exit:1,receipt};},()=>{});
    assert.equal(exit,1);assert.equal(calls,1);
  }
});
test('attempt and total deadline limits stop a reliable refusal without amplification',async()=>{
  for(const [maxAttempts,ticks,reason] of [[1,[0,0,0],'attempt-limit'],[2,[0,0,2000],'deadline-exhausted']]){
    const audit=[];let calls=0,index=0;
    const exit=await runTask({...config,fallback:{...config.fallback,maxAttempts}},'custom','text',async()=>{calls++;return {exit:3,receipt};},
      row=>audit.push(row),{monotonicNow:()=>ticks[Math.min(index++,ticks.length-1)]});
    assert.equal(exit,3);assert.equal(calls,1);assert.equal(audit.find(row=>row.kind==='retry-refused').fallbackReason,reason);
  }
});
test('a failed fallback has one final identity and its own failure classification',async()=>{
  const audit=[];let calls=0;
  const exit=await runTask(config,'custom','text',async()=>++calls===1?{exit:3,receipt}:{exit:7,receipt:{...receipt,
    provider:'q',model:'b',failure:{code:'AUTH',status:401}}},row=>audit.push(row),{runId:'stable-final'});
  assert.equal(exit,7);assert.equal(calls,2);assert.equal(audit.at(-1).code,'MODEL_AUTHENTICATION');
  assert.equal(audit.at(-1).runId,'stable-final');assert.equal(audit.at(-1).provider,'q');
});
test('fallback launch failure finalizes the second attempt instead of reusing primary quota',async()=>{
  let calls=0;const audit=[];
  const exit=await runTask(config,'custom','text',async()=>{
    if(++calls===1)return {exit:7,receipt};throw Object.assign(Error('private path'),{code:'ENOENT'});
  },row=>audit.push(row));
  assert.equal(exit,1);assert.equal(audit.at(-1).attempts,2);assert.equal(audit.at(-1).code,'TASK_LAUNCH_ENOENT');
  assert.equal(audit.at(-1).model,'b');assert.equal(JSON.stringify(audit).includes('private path'),false);
});
test('signal and timeout preserve independent original process exit observations',async()=>{
  for(const result of [{exit:1,processExit:null,signal:'SIGTERM'},{exit:124,processExit:0,signal:null}]){
    const audit=[];await runTask(config,'custom','text',async()=>result,row=>audit.push(row));
    assert.equal(audit.at(-1).processExit,result.processExit);assert.equal(audit.at(-1).signal,result.signal);
    assert.equal(audit.at(-1).attempts,1);
  }
});
