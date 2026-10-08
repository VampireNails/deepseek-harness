/** Raw Session cuts for incremental upkeep; the Host adapter must omit synthetic closers. */
const fail=code=>Object.assign(new Error(code),{code});
const seqOf=(cursors,id)=>Object.hasOwn(cursors,id)?cursors[id]:-1;
const unknownOrigin=()=>({kind:'unknown',basis:'user-channel-only'});
async function dispose(lease){
  if(typeof lease[Symbol.asyncDispose]==='function')await lease[Symbol.asyncDispose]();
  else if(typeof lease[Symbol.dispose]==='function')await lease[Symbol.dispose]();
  else throw fail('UPKEEP_SOURCE_LEASE_INVALID');
}
function validateCut(lease,id,previous=-1){
  if(!lease||lease.header?.id!==id||!Number.isSafeInteger(lease.cursor)||lease.cursor< -1||
    !Number.isSafeInteger(lease.inheritedEventCount)||lease.inheritedEventCount<0||lease.inheritedEventCount>lease.cursor+1||
    previous>lease.cursor)throw fail('UPKEEP_SOURCE_INVALID_CUT');
}
function textOf(event){
  if(event.type!=='user/message'||event.data?.source?.kind!=='user')return null;
  const content=event.data.content;
  const text=typeof content==='string'?content:Array.isArray(content)?content.filter(block=>block?.type==='text'&&typeof block.text==='string')
    .map(block=>block.text).join('\n'):'';
  return text.trim()?text:null;
}

/**
 * Capture acknowledged raw cursors and a bounded batch of original user-channel text with authorship labels.
 * Inherited events, canonical children and authoritative automation are excluded; overflow stays unacknowledged.
 * @param query - Host-owned raw-cut list/observe adapter.
 * @param automationRows - Trusted task/run attribution read before and revalidated after capture.
 * @param request - Previous cursors, explicit bootstrap and validated batch limits.
 * @param inputOrigin - Host-owned immutable identity classifier, unavailable to socket callers; defaults to unknown.
 * @returns Private messages and cursors plus content-free counts; caller commits only after verified handling.
 */
export async function captureUpkeepSource(query,automationRows,request,inputOrigin=unknownOrigin){
  const cursors={...request.cursors},messages=[];
  const counts={scannedSessions:0,newMessages:0,filteredSessions:0,filteredEvents:0};
  if(typeof request.bootstrap!=='boolean'||!Number.isSafeInteger(request.maxBatchMessages)||request.maxBatchMessages<1||
    !Number.isSafeInteger(request.maxBatchChars)||request.maxBatchChars<1)throw fail('UPKEEP_INVALID_SOURCE_REQUEST');
  const automatic=new Set(automationRows.map(row=>row.sessionId));
  const records=await query.listSessions();
  const seen=new Set();let chars=0,blocked=false;
  for(const record of [...records].sort((a,b)=>a.header.id<b.header.id?-1:a.header.id>b.header.id?1:0)){
    const id=record.header.id;
    if(seen.has(id))throw fail('UPKEEP_SOURCE_DUPLICATE');seen.add(id);
    if(blocked)continue;
    const lease=await query.observeSession(id,{projectionMode:'none'});
    if(!lease)throw fail('UPKEEP_SOURCE_MISSING');
    try{
      const previous=seqOf(request.cursors,id);validateCut(lease,id,previous);counts.scannedSessions++;
      const filtered=lease.header.origin==='subagent'||automatic.has(id)||inputOrigin(id).kind==='machine';
      if(filtered)counts.filteredSessions++;
      if(request.bootstrap||filtered){Object.defineProperty(cursors,id,{value:lease.cursor,enumerable:true,writable:true,configurable:true});continue;}
      for(const event of lease.events){
        if(event.seq<=previous)continue;
        if(event.seq>lease.cursor||!Number.isSafeInteger(event.seq))throw fail('UPKEEP_SOURCE_INVALID_CUT');
        const origin=inputOrigin(id,event);
        const text=event.seq<lease.inheritedEventCount||origin.kind==='machine'?null:textOf(event);
        if(text!==null){
          if(text.length>request.maxBatchChars)throw fail('UPKEEP_SOURCE_MESSAGE_TOO_LARGE');
          if(messages.length>=request.maxBatchMessages||chars+text.length>request.maxBatchChars){blocked=true;break;}
          messages.push({sessionId:id,seq:event.seq,messageId:event.data.id,text,inputOrigin:origin});chars+=text.length;counts.newMessages++;
        }else counts.filteredEvents++;
        Object.defineProperty(cursors,id,{value:event.seq,enumerable:true,writable:true,configurable:true});
      }
      if(!blocked)Object.defineProperty(cursors,id,{value:lease.cursor,enumerable:true,writable:true,configurable:true});
    }finally{await dispose(lease);}
  }
  return {cursors,messages,counts};
}

/**
 * Verify one exactly attributed root's completed turn and native memory outcomes.
 * Rendered success strings cannot establish write success; canonical metadata is required.
 * @param query - Host raw-cut adapter.
 * @param automationRows - Trusted immutable task/run attribution.
 * @param identity - Exact runner task and run ID.
 * @returns Content-free completion, failed-write count and actual memory IDs.
 */
export async function inspectUpkeepRun(query,automationRows,identity){
  const listed=new Map((await query.listSessions()).map(record=>[record.header.id,record.header]));
  const ids=[...new Set(automationRows.filter(row=>row.taskId===identity.taskId&&row.runId===identity.runId&&
    listed.get(row.sessionId)?.origin!=='subagent').map(row=>row.sessionId))];
  const report={complete:false,failedWrites:0,writeIds:[]};
  if(ids.length!==1)return report;
  const id=ids[0],lease=await query.observeSession(id,{projectionMode:'none'});
  if(!lease)throw fail('UPKEEP_SOURCE_MISSING');
  try{
    validateCut(lease,id);
    const calls=new Map(),settled=new Set();
    let openTurn=null,hadTurn=false,valid=true;
    for(const event of lease.events){
      if(event.seq<lease.inheritedEventCount)continue;
      const data=event.data;
      if(event.type==='turn/start'){
        if(openTurn!==null)valid=false;openTurn=data.turn;hadTurn=true;
      }else if(event.type==='turn/end'){
        if(openTurn!==data.turn||data.reason?.kind!=='completed')valid=false;openTurn=null;
      }else if(event.type==='tool/call'){
        if(openTurn!==data.turn||calls.has(data.callId)||!['memory_search','memory_write'].includes(data.name))valid=false;
        calls.set(data.callId,{name:data.name,turn:data.turn,step:data.step});
      }else if(event.type==='tool/result'){
        const callId=data.message?.toolCallId,call=calls.get(callId);
        if(!call||settled.has(callId)||call.turn!==data.turn||call.step!==data.step||data.message?.source?.callId!==callId){valid=false;continue;}
        settled.add(callId);
        if(data.message.isError!==false)valid=false;
        if(call.name==='memory_write'){
          const write=data.meta?.memoryWrite;
          if(data.message.isError!==false||write?.ok!==true||!Number.isSafeInteger(write.id)||write.id<1){report.failedWrites++;valid=false;}
          else report.writeIds.push(write.id);
        }
      }
    }
    report.complete=valid&&hadTurn&&openTurn===null&&calls.size===settled.size;
    report.writeIds=[...new Set(report.writeIds)];
    return report;
  }finally{await dispose(lease);}
}
