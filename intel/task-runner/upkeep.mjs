import {createHash,randomUUID} from 'node:crypto';

const fail=code=>Object.assign(new Error(code),{code});
const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
const seqOf=(value,id)=>Object.hasOwn(value,id)?value[id]:-1;
function cursors(value){
  if(!object(value)||Object.entries(value).some(([id,seq])=>!id||id.length>1024||!Number.isSafeInteger(seq)||seq< -1))throw fail('UPKEEP_INVALID_STATE');
}
function state(value){
  if(!object(value)||value.version!==1)throw fail('UPKEEP_INVALID_STATE');
  cursors(value.cursors);
  for(const key of ['backoffMs','nextDueAtMs','lastNowMs'])if(!Number.isSafeInteger(value[key])||value[key]<0)throw fail('UPKEEP_INVALID_STATE');
  if(value.pending!==undefined&&value.pending!==null){
    const pending=value.pending;
    if(!object(pending)||typeof pending.runId!=='string'||!/^[-a-zA-Z0-9._:]{1,256}$/.test(pending.runId)||
      !/^[a-f0-9]{64}$/.test(pending.batchHash))throw fail('UPKEEP_INVALID_STATE');
    if(pending.taskId!==undefined&&!['memory_upkeep','evolve_upkeep'].includes(pending.taskId))throw fail('UPKEEP_INVALID_STATE');
    cursors(pending.proposedCursors);
    for(const [id,seq] of Object.entries(value.cursors))if(seqOf(pending.proposedCursors,id)<seq)throw fail('UPKEEP_INVALID_STATE');
  }
}
const verified=result=>result?.complete===true&&result.failedWrites===0&&Array.isArray(result.writeIds)&&
  result.writeIds.every(id=>Number.isSafeInteger(id)&&id>0);

/**
 * Admit only new human input and retain unresolved side effects across cron invocations.
 * The caller holds the task lock; source returns complete raw cuts, and inspect verifies persisted native tool outcomes.
 * @param options - Validated limits, clock, durable state, read-only source, model dispatch, run inspection and private-free audit.
 * @returns A prompt-free outcome; source, dispatch, verification and persistence failures reject with stable codes.
 */
export async function runUpkeep({config,nowMs,taskId,stateStore,source,execute,inspect,record}){
  try{
    if(!Number.isSafeInteger(nowMs)||nowMs<0||!object(config))throw fail('UPKEEP_INVALID_CONFIG');
    for(const key of ['initialBackoffMs','maxBackoffMs','maxBatchMessages','maxBatchChars'])
      if(!Number.isSafeInteger(config[key])||config[key]<1)throw fail('UPKEEP_INVALID_CONFIG');
    if(config.maxBackoffMs<config.initialBackoffMs||config.maxBackoffMs>86400000||config.maxBatchMessages>100||config.maxBatchChars>262144)
      throw fail('UPKEEP_INVALID_CONFIG');
    if(taskId!==undefined&&!['memory_upkeep','evolve_upkeep'].includes(taskId))throw fail('UPKEEP_INVALID_CONFIG');
    const previous=await stateStore.read();
    if(previous!==null)state(previous);
    const audit=async(kind,extra={})=>{await record({kind,...extra});return {kind,...extra};};
    if(previous&&nowMs<previous.lastNowMs)return await audit('upkeep-clock-rollback');
    async function commit(proposedCursors,backoffMs){
      const next={version:1,cursors:proposedCursors,backoffMs,nextDueAtMs:nowMs+backoffMs,lastNowMs:nowMs};
      state(next);await stateStore.write(next);
    }
    if(previous?.pending){
      let receipt;
      try{receipt=await inspect({runId:previous.pending.runId,...previous.pending.taskId!==undefined?{taskId:previous.pending.taskId}:{}});}catch{throw fail('UPKEEP_PREVIOUS_RUN_UNRESOLVED');}
      if(!verified(receipt))throw fail('UPKEEP_PREVIOUS_RUN_UNRESOLVED');
      await commit(previous.pending.proposedCursors,0);
      return await audit('upkeep-recovered',{batchHash:previous.pending.batchHash,writes:receipt.writeIds.length});
    }
    if(previous&&nowMs<previous.nextDueAtMs)return await audit('upkeep-backoff',{nextDueAtMs:previous.nextDueAtMs});
    const cut=await source.capture({cursors:previous?.cursors??{},bootstrap:previous===null,
      maxBatchMessages:config.maxBatchMessages,maxBatchChars:config.maxBatchChars});
    if(!object(cut)||!Array.isArray(cut.messages))throw fail('UPKEEP_INVALID_SOURCE');
    cursors(cut.cursors);
    for(const [id,seq] of Object.entries(previous?.cursors??{}))if(seqOf(cut.cursors,id)<seq)throw fail('UPKEEP_SOURCE_REGRESSED');
    if(previous===null){
      await commit(cut.cursors,config.initialBackoffMs);
      return await audit('upkeep-bootstrap',{counts:{sessions:Object.keys(cut.cursors).length,messages:0}});
    }
    let chars=0;
    const keys=new Set();
    for(const message of cut.messages){
      if(!object(message)||typeof message.sessionId!=='string'||!Number.isSafeInteger(message.seq)||message.seq<0||
        typeof message.text!=='string'||!message.text.trim()||message.seq<=seqOf(previous.cursors,message.sessionId)||
        message.seq>seqOf(cut.cursors,message.sessionId))throw fail('UPKEEP_INVALID_SOURCE');
      const key=JSON.stringify([message.sessionId,message.seq]);
      if(keys.has(key))throw fail('UPKEEP_INVALID_SOURCE');keys.add(key);chars+=message.text.length;
    }
    if(cut.messages.length>config.maxBatchMessages||chars>config.maxBatchChars)throw fail('UPKEEP_INVALID_SOURCE');
    const counts={messages:cut.messages.length,chars};
    for(const key of ['sessions','scannedSessions','filteredSessions','filteredEvents','newMessages','remainingMessages'])
      if(Number.isSafeInteger(cut.counts?.[key])&&cut.counts[key]>=0)counts[key]=cut.counts[key];
    if(!cut.messages.length){
      const backoffMs=Math.min(config.maxBackoffMs,previous.backoffMs?previous.backoffMs*2:config.initialBackoffMs);
      await commit(cut.cursors,backoffMs);
      return await audit('upkeep-no-signal',{counts,nextDueAtMs:nowMs+backoffMs});
    }
    const batchHash=createHash('sha256').update(JSON.stringify(cut.messages)).digest('hex');
    const runId=randomUUID();
    await stateStore.write({...previous,pending:{runId,batchHash,proposedCursors:cut.cursors,...taskId!==undefined?{taskId}:{}}});
    await audit('upkeep-signals',{batchHash,counts});
    const exit=await execute({messages:cut.messages,batchHash,runId});
    if(exit!==0)throw fail('UPKEEP_EXECUTION_FAILED');
    const receipt=await inspect({runId,...taskId!==undefined?{taskId}:{}});
    if(!verified(receipt))throw fail('UPKEEP_RUN_UNVERIFIED');
    await commit(cut.cursors,0);
    return await audit('upkeep-processed',{batchHash,counts,writes:receipt.writeIds.length});
  }catch(error){
    if(typeof error?.code==='string'&&/^UPKEEP_[A-Z0-9_]{1,64}$/.test(error.code))throw fail(error.code);
    throw fail('UPKEEP_FAILED');
  }
}
