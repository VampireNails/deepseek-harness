import {validateReceipt} from './failure.mjs';

const limit=8192;
const counters=['sessions','steps','requests','tools','outputs','retries'];

/**
 * Read bounded private receipt lines without retaining output or arbitrary JSON fields.
 * Any invalid line invalidates replay evidence; observed effects survive later resets.
 * @param plan - Expected run, attempt and requested route.
 * @returns A per-process pipe reader and its final safe observations.
 */
export function createRuntimeObserver(plan){
  let buffer=Buffer.alloc(0),invalid=false,receipt,receiptIssue='none',runtimeStage='unobserved';
  const maxima=Object.fromEntries(counters.map(key=>[key,0]));
  let streamOutput=false,toolFailed=false,modelMismatch=false;
  return {
    get retainedBytes(){return buffer.length;},
    invalidate(issue='fd-error'){invalid=true;receiptIssue=issue;buffer=Buffer.alloc(0);},
    push(chunk){
      if(invalid)return;
      // Slice before copying: even a very long chunk cannot grow the retained line.
      for(let offset=0;offset<chunk.length;){
        const newline=chunk.indexOf(10,offset),end=newline<0?chunk.length:newline;
        if(buffer.length+end-offset>limit){this.invalidate('line-limit');return;}
        buffer=Buffer.concat([buffer,chunk.subarray(offset,end)]);
        if(newline<0)return;
        let next;
        try{next=validateReceipt(JSON.parse(buffer.toString('utf8')),plan);}
        catch(error){this.invalidate('malformed-json');return;}
        buffer=Buffer.alloc(0);
        if(!next){this.invalidate('identity-or-fields');return;}
        for(const key of counters)maxima[key]=Math.max(maxima[key],next[key]);
        streamOutput ||= next.streamOutput;toolFailed ||= next.toolFailed===true;
        modelMismatch ||= next.requests>0&&!next.modelMatches;
        receipt={...next,...maxima,streamOutput,modelMatches:next.modelMatches&&!modelMismatch,...(toolFailed?{toolFailed:true}:{})};
        runtimeStage=receipt.complete?'ended':receipt.outputs||streamOutput?'output':receipt.tools?'tool':receipt.requests?'request':receipt.sessions?'session':'activated';
        offset=newline+1;
      }
    },
    finish(){
      if(buffer.length)this.invalidate('unterminated');
      return {runtimeStage,receiptIssue,...(invalid?{receiptStatus:'invalid',...(receipt?{receipt:{...receipt,complete:false}}:{})}:!receipt?{receiptStatus:'missing'}:
        {receipt,receiptStatus:receipt.complete?'complete':'incomplete'})};
    },
  };
}
