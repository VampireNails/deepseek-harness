/** Host-owned raw reads; cold Session query observations contain synthetic interruption closers. */
const fail=code=>Object.assign(new Error(code),{code});
function freeze(value){
  if(value&&typeof value==='object'&&!Object.isFrozen(value)){
    for(const child of Object.values(value))freeze(child);Object.freeze(value);
  }
  return value;
}
function lease(header,inheritedEventCount,events,close=async()=>{}){
  let disposed;
  return Object.freeze({header,inheritedEventCount,events,cursor:events.at(-1)?.seq?? -1,
    [Symbol.asyncDispose](){if(!disposed)disposed=Promise.resolve().then(close);return disposed;}});
}

/**
 * Read exact live snapshots or official read-only persistence slices without making cold Agents live.
 * @param ctx - Existing Host context with sessions, query and persistence providers.
 * @param options - Optional cancellation and configured compressed artifact byte limit.
 * @returns A list/observe adapter whose cursors contain only actual raw events.
 */
export function createUpkeepQuery(ctx,{signal,maxSessionBytes}={}){
  const query=ctx.get('sessionQuery'),persistence=ctx.get('sessionPersistence');
  if(!query||!persistence||!ctx.sessions)throw fail('UPKEEP_SOURCE_UNAVAILABLE');
  if(maxSessionBytes!==undefined&&(!Number.isSafeInteger(maxSessionBytes)||maxSessionBytes<1))throw fail('UPKEEP_INVALID_CONFIG');
  return {
    listSessions(){signal?.throwIfAborted();return query.listSessions(signal);},
    async observeSession(id,options={}){
      const cancellation=options.signal??signal;cancellation?.throwIfAborted();
      const live=ctx.sessions.get(id);
      if(live){
        const events=Object.freeze([...live.snapshotEvents()]);
        return lease(live.header,live.inheritedEventCount,events);
      }
      if(maxSessionBytes!==undefined){
        const snapshot=await persistence.stat(id,{signal:cancellation});
        if(!snapshot)throw fail('UPKEEP_SOURCE_MISSING');
        if(!Number.isSafeInteger(snapshot.sizeBytes)||snapshot.sizeBytes>maxSessionBytes)throw fail('UPKEEP_SOURCE_TOO_LARGE');
      }
      const handle=await persistence.open(id,'read',{signal:cancellation});
      try{
        const snapshot=await handle.read(0,undefined,{signal:cancellation});
        cancellation?.throwIfAborted();
        const header=freeze(structuredClone(handle.header));
        const events=freeze(snapshot.eventState==='shared-frozen'?[...snapshot.events]:structuredClone(snapshot.events));
        return lease(header,handle.inheritedEventCount,events,()=>handle.close());
      }catch(error){
        try{await handle.close();}catch(closeError){throw new AggregateError([error,closeError],'UPKEEP_SOURCE_READ_FAILED',{cause:error});}
        throw error;
      }
    },
  };
}
