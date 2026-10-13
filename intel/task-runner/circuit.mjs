import {createUpkeepStateStore} from './upkeep-state.mjs';

const fail=code=>Object.assign(Error(code),{code});
const id=value=>typeof value==='string'&&/^[A-Za-z0-9_.:-]{1,128}$/.test(value);
const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);

/** Validate deployment failure threshold before task execution; absent config retains older deployments. */
export function circuitConfig(value){
  if(value===undefined)return null;
  if(!object(value)||typeof value.enabled!=='boolean'||!Number.isSafeInteger(value.threshold)||value.threshold<1||value.threshold>1000||
    Object.keys(value).some(key=>!['enabled','threshold'].includes(key)))throw fail('CIRCUIT_INVALID_CONFIG');
  return value.enabled?value:null;
}

/** Reuse the owner-only fsynced JSON store under the existing task flock. */
export function createCircuitStateStore(file){
  const store=createUpkeepStateStore(file);
  const call=async fn=>{try{return await fn();}catch(error){throw fail(error.code?.startsWith('UPKEEP_')?error.code.replace('UPKEEP_','CIRCUIT_'):'CIRCUIT_STORAGE_FAILED');}};
  return {read:()=>call(()=>store.read()),write:state=>call(()=>store.write(state))};
}

/**
 * Count final run outcomes in flock admission order, independently of attempts and JobLog projections.
 * Legacy history never seeds a streak. Unfinished admissions fail closed; release cannot replay them.
 * @param options - Task/scope, validated threshold, private store and durable event writer.
 * @returns Admission, pending/completion, outbox retry and exact-opening-run release operations.
 */
export function createCircuit({taskId,scope,config,store,emit}){
  if(!id(taskId)||!['production','test'].includes(scope))throw fail('CIRCUIT_INVALID_CONFIG');
  const initial=()=>({version:1,taskId,scope,failures:0,pending:null,last:null,openRunId:null,outbox:null});
  const validRun=value=>object(value)&&id(value.runId)&&['ok','fail','quiet'].includes(value.outcome);
  async function read(){
    const s=await store.read()??initial();
    if(!object(s)||s.version!==1||s.taskId!==taskId||s.scope!==scope||!Number.isSafeInteger(s.failures)||s.failures<0||
      s.pending!==null&&!id(s.pending)||s.last!==null&&!validRun(s.last)||s.openRunId!==null&&!id(s.openRunId)||
      s.openRunId!==null&&(s.pending!==null||s.last?.runId!==s.openRunId||s.last?.outcome!=='fail'||s.failures<1)||
      s.outbox!==null&&(!object(s.outbox)||s.outbox.runId!==s.openRunId||!Number.isSafeInteger(s.outbox.threshold)||s.outbox.threshold<1||s.outbox.threshold>1000||s.failures<s.outbox.threshold))
      throw fail('CIRCUIT_INVALID_STATE');
    return s;
  }
  async function flush(){
    const s=await read();if(!s.outbox)return;
    emit({type:'task.circuit_open',severity:'high',title:'任务连续失败，已熔断',
      detail:`taskId=${taskId}; runId=${s.outbox.runId}; failures=${s.failures}; threshold=${s.outbox.threshold}; inspect route-runs.jsonl before release`,
      source:'runner',taskId,scope,runId:s.outbox.runId,code:'TASK_CIRCUIT_OPEN'});
    s.outbox=null;await store.write(s);
  }
  async function admit(){
    await flush();const s=await read();
    if(s.pending)throw fail('CIRCUIT_RUN_UNRESOLVED');
    return s.openRunId===null;
  }
  async function begin(runId){
    if(!id(runId))throw fail('CIRCUIT_INVALID_RUN');
    if(!await admit())throw fail('CIRCUIT_OPEN');
    const s=await read();if(s.last?.runId===runId)throw fail('CIRCUIT_RUN_REUSED');
    s.pending=runId;await store.write(s);
  }
  async function complete(runId,outcome){
    if(!validRun({runId,outcome}))throw fail('CIRCUIT_INVALID_RUN');
    const s=await read();
    if(s.pending===null&&s.last?.runId===runId&&s.last.outcome===outcome)return;
    if(s.pending!==runId)throw fail('CIRCUIT_RUN_MISMATCH');
    s.pending=null;s.last={runId,outcome};
    if(outcome==='ok')s.failures=0;
    if(outcome==='fail')s.failures++;
    if(outcome==='fail'&&s.failures>=config.threshold){s.openRunId=runId;s.outbox={runId,threshold:config.threshold};}
    await store.write(s);
  }
  async function release(openRunId){
    await flush();const s=await read();
    if(s.pending)throw fail('CIRCUIT_RUN_UNRESOLVED');
    if(!s.openRunId||s.openRunId!==openRunId)throw fail('CIRCUIT_RELEASE_MISMATCH');
    s.openRunId=null;s.failures=0;await store.write(s);
  }
  return {admit,begin,complete,flush,release};
}
