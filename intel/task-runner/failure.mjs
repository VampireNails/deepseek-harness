const codes={QUOTA:'quota',RATE_LIMIT:'rate-limit',AUTH:'authentication',MISSING_CREDENTIAL:'authentication',
  INVALID_CREDENTIAL:'authentication',SERVER:'service',NETWORK:'network',TRANSPORT:'network',STREAM_CLOSED:'network',TIMEOUT:'timeout',
  NO_ADAPTER:'model-config',NO_MODEL:'model-config',INVALID_REQUEST:'model-config',
  CONTEXT_WINDOW_EXCEEDED:'model-config',UNSUPPORTED_REASONING_EFFORT:'model-config'};

/**
 * Project only trusted runtime facts; text and process exit 1 never establish quota.
 * @param result - Process exit, signal and optional complete runtime receipt.
 * @returns A prompt-free stable classification with replay evidence.
 */
export function classifyFailure(result){
  if(result.launchCode)return {code:'TASK_LAUNCH_'+result.launchCode,category:'runner-error',result:'unknown',preExecution:false};
  if(result.exit===124)return {code:'TASK_TIMEOUT',category:'timeout',result:'unknown',preExecution:false};
  if(result.signal)return {code:'TASK_SIGNAL',category:'signal',result:'unknown',preExecution:false};
  if(result.exit===65)return {code:'TASK_NO_VISIBLE_OUTPUT',category:'no-visible-output',result:'completed',preExecution:false};
  const receipt=result.receipt;
  if(!receipt?.complete)return {code:'TASK_RESULT_UNKNOWN',category:'unknown',result:'unknown',preExecution:false};
  if(receipt.toolFailed)return {code:'TASK_TOOL_FAILED',category:'tool',result:'partial',preExecution:false};
  const failure=receipt.failure,category=codes[failure?.code]??'unknown';
  const code=category==='unknown'?'TASK_RESULT_UNKNOWN':category==='model-config'?'MODEL_CONFIG':'MODEL_'+category.toUpperCase().replaceAll('-','_');
  const preExecution=receipt.sessions===1&&receipt.steps===1&&receipt.requests===1&&receipt.tools===0&&receipt.outputs===0&&
    receipt.streamOutput===false&&receipt.retries===0&&receipt.modelMatches===true&&receipt.end==='error'&&
    (category==='quota'&&[402,429].includes(failure.status)||category==='rate-limit'&&failure.status===429);
  return {code,category,result:preExecution?'rejected':receipt.tools||receipt.outputs||receipt.streamOutput?'partial':'unknown',preExecution,
    providerCode:category==='unknown'?'UNKNOWN':failure?.code,status:failure?.status};
}

/**
 * Validate the private child receipt against this invocation's identity.
 * @param value - JSON decoded from the child-owned receipt.
 * @param plan - Runner attempt identity.
 * @returns Validated safe facts, or no evidence for malformed or stale receipts.
 */
export function validateReceipt(value,plan){
  if(value?.version!==1||value.runId!==plan.runId||value.attempt!==plan.attempt||
    !['sessions','steps','requests','tools','outputs','retries'].every(key=>Number.isSafeInteger(value[key])&&value[key]>=0)||
    typeof value.complete!=='boolean'||typeof value.streamOutput!=='boolean')return undefined;
  if(value.failure!==undefined&&(typeof value.failure!=='object'||value.failure===null||
    typeof value.failure.code!=='string'||value.failure.status!==undefined&&(!Number.isInteger(value.failure.status)||value.failure.status<100||value.failure.status>599)))return undefined;
  const modelMatches=value.provider===plan.provider&&value.model===plan.model;
  return {...value,modelMatches,provider:modelMatches?plan.provider:undefined,model:modelMatches?plan.model:undefined,
    failure:value.failure?{code:Object.hasOwn(codes,value.failure.code)?value.failure.code:'UNKNOWN',status:value.failure.status}:undefined};
}
