const byteLimit=65536,windowLimit=128;
const hints=['none','plugin-startup','fd-write'];
const stages=['unobserved','activated','session','request','tool','output','ended'];
const issues=['none','fd-error','line-limit','malformed-json','identity-or-fields','unterminated'];

/**
 * Drain stderr, retaining only a bounded match window and fixed diagnostic hints.
 * Text is never returned or persisted; bytes beyond the cap are neither counted nor scanned.
 * @returns A streaming observer whose finish clears its transient match window.
 */
export function createStderrObserver(){
  let bytes=0,capped=false,window=Buffer.alloc(0),hint='none';
  return {
    get retainedBytes(){return window.length;},
    push(chunk){
      const take=Math.min(chunk.length,byteLimit-bytes);capped ||= take<chunk.length;bytes+=take;
      for(let offset=0;offset<take;offset+=windowLimit){
        window=Buffer.concat([window,chunk.subarray(offset,Math.min(take,offset+windowLimit))]);
        const text=window.toString('ascii');
        if(text.includes('plugin tree failed to load')||text.includes('Failed plugins ('))hint='plugin-startup';
        if(text.includes('INTEL_RUNTIME_FD_WRITE_FAILED'))hint='fd-write';
        window=window.subarray(Math.max(0,window.length-windowLimit));
      }
      if(bytes===byteLimit)window=Buffer.alloc(0);
    },
    finish(){window=Buffer.alloc(0);return {bytes,capped,hint};},
  };
}

/**
 * Select fixed process observations for route audits, JobLog detail and failure events.
 * Hints are unauthenticated stderr observations and never establish cause or replay safety.
 * @param result - Child process observations; arbitrary text and unknown fields are discarded.
 * @returns A bounded ASCII summary, with absent or malformed observations marked unknown.
 */
export function processDiagnostic(result){
  const status=['missing','invalid','incomplete','complete'].includes(result.receiptStatus)?result.receiptStatus:'unknown';
  const issue=issues.includes(result.receiptIssue)?result.receiptIssue:'none';
  const stage=stages.includes(result.runtimeStage)?result.runtimeStage:'unobserved';
  const stderr=result.stderr,bytes=Number.isSafeInteger(stderr?.bytes)&&stderr.bytes>=0&&stderr.bytes<=byteLimit?stderr.bytes:'unknown';
  const hint=hints.includes(stderr?.hint)?stderr.hint:'none';
  return `receipt=${status}; receiptIssue=${issue}; runtime=${stage}; stderrBytes=${bytes}; stderrCapped=${stderr?.capped===true}; stderrHint=${hint}`;
}
