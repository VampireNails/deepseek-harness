const categories=['success','quota','rate-limit','authentication','service','network','model-config','tool','timeout','signal','no-visible-output','runner-error','unknown'];
const codes=['OK','MODEL_QUOTA','MODEL_RATE_LIMIT','MODEL_AUTHENTICATION','MODEL_SERVICE','MODEL_NETWORK','MODEL_CONFIG','TASK_TOOL_FAILED','TASK_TIMEOUT','TASK_SIGNAL','TASK_NO_VISIBLE_OUTPUT','TASK_RESULT_UNKNOWN','TASK_LAUNCH_ENOENT','TASK_LAUNCH_EACCES','TASK_LAUNCH_EPIPE','TASK_LAUNCH_UNKNOWN'];
const providerCodes=['QUOTA','RATE_LIMIT','AUTH','MISSING_CREDENTIAL','INVALID_CREDENTIAL','SERVER','NETWORK','TRANSPORT','STREAM_CLOSED','TIMEOUT','NO_ADAPTER','NO_MODEL','INVALID_REQUEST','CONTEXT_WINDOW_EXCEEDED','UNSUPPORTED_REASONING_EFFORT','UNKNOWN'];
const retryReasons=['none','fallback-disabled','fallback-unconfigured','attempt-limit','deadline-exhausted','unsafe-or-unknown-result','pre-execution-rejection'];

/**
 * Validate persisted/wire task facts and omit unknown fields; absence is historical unknown.
 * This presentation data never authorizes replay.
 * @param value - Optional structured runner diagnostic.
 * @returns Selected safe fields; malformed present data throws.
 */
export function taskDiagnostic(value){
  if(value===undefined)return undefined;
  if(!value||typeof value!=='object'||Array.isArray(value)||value.version!==1||
    !categories.includes(value.category)||!codes.includes(value.code)||!['completed','rejected','partial','unknown'].includes(value.result)||
    !Number.isSafeInteger(value.attempts)||value.attempts<1||value.attempts>2||!retryReasons.includes(value.retryReason))
    throw Error('INVALID_TASK_DIAGNOSTIC');
  const out={version:1,category:value.category,code:value.code,result:value.result,attempts:value.attempts,retryReason:value.retryReason};
  for(const key of ['provider','model','actualProvider','actualModel']){
    if(value[key]===undefined)continue;
    const pattern=key.endsWith('Provider')||key==='provider'?/^[-a-zA-Z0-9_.]{1,100}$/:/^[-a-zA-Z0-9_.:/]{1,160}$/;
    if(typeof value[key]!=='string'||!pattern.test(value[key]))throw Error('INVALID_TASK_DIAGNOSTIC');
    out[key]=value[key];
  }
  if(value.providerCode!==undefined){
    if(!providerCodes.includes(value.providerCode))throw Error('INVALID_TASK_DIAGNOSTIC');
    out.providerCode=value.providerCode;
  }
  if(value.httpStatus!==undefined){
    if(!Number.isInteger(value.httpStatus)||value.httpStatus<100||value.httpStatus>599)throw Error('INVALID_TASK_DIAGNOSTIC');
    out.httpStatus=value.httpStatus;
  }
  return out;
}
