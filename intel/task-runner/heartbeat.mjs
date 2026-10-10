import {randomUUID} from 'node:crypto';
import {createUpkeepStateStore} from './upkeep-state.mjs';

const fail=code=>Object.assign(Error(code),{code});
const hash=value=>typeof value==='string'&&/^[a-f0-9]{64}$/.test(value);
const time=value=>Number.isSafeInteger(value)&&value>=0;
function validate(state){
  if(!state||state.version!==1||!hash(state.signalId)||typeof state.healthy!=='boolean'||
    !['lastNowMs','nextDueAtMs','backoffMs'].every(k=>time(state[k]))||
    state.nextDueAtMs!==state.lastNowMs+state.backoffMs||
    state.pending!==null&&(!state.pending||state.pending.signalId!==state.signalId||typeof state.pending.runId!=='string'||!/^[-A-Za-z0-9._:]{1,256}$/.test(state.pending.runId)))throw fail('HEARTBEAT_INVALID_STATE');
}

/**
 * Reuse the private atomic/fsynced checkpoint mechanism under the heartbeat flock.
 * @param file - Absolute private state path, isolated by the caller's source scope.
 * @returns Durable read/write operations with heartbeat storage failure codes.
 */
export function createHeartbeatStateStore(file){
  const store=createUpkeepStateStore(file);
  const wrap=async operation=>{try{return await operation();}catch(error){throw fail(error.code?.startsWith('UPKEEP_')?error.code.replace('UPKEEP_','HEARTBEAT_'):'HEARTBEAT_STATE_FAILED');}};
  return {read:()=>wrap(()=>store.read()),write:value=>wrap(()=>store.write(value))};
}

/**
 * Preflight every invocation before admitting changed/recovered signals or a due repeated anomaly.
 * The caller holds one scope-specific flock through capture, durable pending write, dispatch and completion.
 * Unknown source data rejects; pending identical side effects are never automatically replayed.
 * @param options - Clock, validated config, safe capture, durable state, model dispatch and audit.
 * @returns Quiet outcome or original model exit, with safe signal/run identities.
 */
export async function runHeartbeat({config,nowMs,source,stateStore,execute,record}){
  const previous=await stateStore.read();if(previous!==null)validate(previous);
  const cut=await source.capture();
  if(!cut||cut.version!==1||cut.complete!==true||cut.observedAtMs!==nowMs||!hash(cut.signalId)||!hash(cut.checklistId)||
    typeof cut.empty!=='boolean'||typeof cut.healthy!=='boolean'||!Array.isArray(cut.signals)||
    cut.empty&&(!cut.healthy||cut.signals.length)||!cut.empty&&(cut.signals.length!==6||cut.signals.some(x=>!hash(x.id)||!['healthy','anomaly'].includes(x.status))||cut.healthy!==cut.signals.every(x=>x.status==='healthy')))throw fail('HEARTBEAT_INVALID_SOURCE');
  const same=previous?.signalId===cut.signalId;
  const clockRollback=previous!==null&&nowMs<previous.lastNowMs;
  const logicalNow=Math.max(nowMs,previous?.lastNowMs??0);
  const info={signalId:cut.signalId,checklistId:cut.checklistId,observedAtMs:nowMs,healthy:cut.healthy,scopeSignals:cut.signals.map(({source,id,status})=>({source,id,status})),clockRollback};
  const audit=async(kind,extra={})=>{const row={kind,...info,...extra};await record(row);return row;};
  const commit=async(backoffMs,pending=null)=>stateStore.write({version:1,signalId:cut.signalId,healthy:cut.healthy,lastNowMs:logicalNow,nextDueAtMs:logicalNow+backoffMs,backoffMs,pending});
  if(same&&previous.pending){await audit('heartbeat-unresolved',{runId:previous.pending.runId,reason:'previous-run-unresolved'});throw fail('HEARTBEAT_PREVIOUS_RUN_UNRESOLVED');}
  if(cut.empty||cut.healthy&&(!previous||same)){
    await commit(0);return await audit(cut.empty?'heartbeat-empty-checklist':same?'heartbeat-no-change':'heartbeat-healthy',{reason:cut.empty?'empty-checklist':same?'healthy-unchanged':'healthy-baseline',modelExecutions:0});
  }
  if(same){
    if(clockRollback){await audit('heartbeat-clock-rollback',{reason:'clock-rollback',modelExecutions:0});throw fail('HEARTBEAT_CLOCK_ROLLBACK');}
    if(nowMs<previous.nextDueAtMs)return await audit('heartbeat-backoff',{reason:'same-anomaly-backoff',nextDueAtMs:previous.nextDueAtMs,modelExecutions:0});
  }
  const reason=!previous?'new-anomaly':same?'repeated-anomaly-due':cut.healthy?'recovered':'signal-changed';
  if(previous?.pending)await audit('heartbeat-pending-superseded',{reason:'new-signal',previousRunId:previous.pending.runId,previousSignalId:previous.pending.signalId});
  const runId=randomUUID();
  // Pending is durable before launch. Crash/failed completion never permits the same signal to replay.
  const backoffMs=same?Math.min(config.maxBackoffMs,Math.max(config.initialBackoffMs,previous.backoffMs*2)):config.initialBackoffMs;
  await commit(backoffMs,{runId,signalId:cut.signalId});
  await audit('heartbeat-signals',{reason,runId,modelExecutions:0});
  const exit=await execute({runId,cut,reason});
  if(!Number.isInteger(exit)||exit<0||exit>255)throw fail('HEARTBEAT_EXECUTION_INVALID');
  if(exit===0)await commit(backoffMs);
  return await audit('heartbeat-processed',{reason,runId,exit,modelExecutions:1});
}
