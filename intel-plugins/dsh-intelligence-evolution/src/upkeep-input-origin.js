/** Host-owned authorship assignments; message text and working directories cannot establish authorship. */
const unknown=()=>({kind:'unknown',basis:'user-channel-only'});
const fail=()=>Object.assign(Error('UPKEEP_INVALID_INPUT_ORIGINS'),{code:'UPKEEP_INVALID_INPUT_ORIGINS'});
const validId=value=>typeof value==='string'&&value.length>0&&value.length<=1024;

/**
 * Validate private Host assignments and classify immutable session/message identities.
 * Machine session scopes exclude controlled acceptance roots; human assignments match one owned event exactly.
 * @param options - Machine-only session IDs and exact human/machine message assignments verified by the Host owner.
 * @returns Classifier returning unknown for every identity without an explicit assignment.
 */
export function createUpkeepInputOrigin({machineSessionIds=[],messageOrigins=[]}={}){
  if(!Array.isArray(machineSessionIds)||!Array.isArray(messageOrigins)||
    machineSessionIds.length>10000||messageOrigins.length>10000)throw fail();
  const machines=new Set(),messages=new Map();
  for(const id of machineSessionIds){
    if(!validId(id)||machines.has(id))throw fail();
    machines.add(id);
  }
  for(const row of messageOrigins){
    if(!row||!validId(row.sessionId)||!validId(row.messageId)||!Number.isSafeInteger(row.seq)||row.seq<0||
      !['human','machine'].includes(row.kind)||machines.has(row.sessionId)&&row.kind==='human')throw fail();
    const key=JSON.stringify([row.sessionId,row.seq,row.messageId]);
    if(messages.has(key))throw fail();
    messages.set(key,row.kind);
  }
  return (sessionId,event)=>{
    if(machines.has(sessionId))return {kind:'machine',basis:'host-machine-session'};
    const kind=event&&messages.get(JSON.stringify([sessionId,event.seq,event.data?.id]));
    return kind?{kind,basis:'host-message-attribution'}:unknown();
  };
}
