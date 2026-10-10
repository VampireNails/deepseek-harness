import {writeSync} from 'node:fs';

export const name='intel-task-runtime';
export const inject=['agents','sessions','agentDefaultModel'];

/**
 * Observe a runner-owned one-shot profile without retaining model or tool content.
 * The receipt is invalidated by any additional session, tool, output or retry.
 * @param ctx - Owning profile context.
 * @returns Nothing; registrations belong to the profile lifetime.
 */
export function apply(ctx){
  if(process.env.INTEL_RUNTIME_FD!=='3')return;
  const state={version:1,runId:process.env.INTEL_RUN_ID,attempt:Number(process.env.INTEL_RUN_ATTEMPT),
    sessions:0,steps:0,requests:0,tools:0,outputs:0,streamOutput:false,retries:0,complete:false};
  const expected={provider:process.env.INTEL_ROUTE_PROVIDER,model:process.env.INTEL_ROUTE_MODEL};
  let root,writeFailed=false;
  function save(){
    try{writeSync(3,JSON.stringify(state)+'\n');}
    catch(error){writeFailed=true;}
  }
  const selection=ctx.agentDefaultModel.currentSelection();
  const defaultMatches=selection.provider===expected.provider&&selection.model===expected.model;
  ctx.effect(()=>ctx.on('session/created',session=>{
    state.sessions++;if(!root)root=session.id;else state.complete=false;save();
  },{global:true}));
  ctx.effect(()=>ctx.on('session/event',(session,event)=>{
    if(session.id!==root){state.complete=false;save();return;}
    const {type,data}=event;
    state.complete=false;
    if(type==='step/start')state.steps++;
    if(type==='request/header'){
      state.requests++;state.provider=data.header.config.provider;state.model=data.header.config.model;
      state.modelMatches=state.provider===expected.provider&&state.model===expected.model;
    }
    if(type==='tool/call'||type==='tool/ptc-dispatch-start')state.tools++;
    if(type==='tool/result'&&data.message?.isError===true)state.toolFailed=true;
    if(type==='assistant/message')state.outputs++;
    if(type==='llm/retry')state.retries++;
    if(type==='assistant/attempt'){
      state.streamOutput ||= data.stream.some(record=>record.type!=='chunk'||
        !['finish','usage'].includes(record.chunk.type));
      const finish=data.stream.findLast(record=>record.type==='chunk'&&record.chunk.type==='finish')?.chunk;
      if(finish?.kind==='error'){
        const failure=finish.failure;
        state.failure={code:failure.code,status:failure.status};
      }
    }
    if(type==='turn/end'){
      state.end=data.reason.kind;
      if(data.reason.kind==='error'&&!state.failure)state.failure={code:data.reason.error.code,status:data.reason.error.status};
      state.complete=!writeFailed;
    }
    save();
  },{global:true}));
  ctx.effect(()=>ctx.on('agent/request',async (payload,next)=>{
    const selected=await next();
    if(!defaultMatches||selected.provider!==expected.provider||selected.model!==expected.model){
      state.failure={code:'NO_MODEL'};save();throw Error('RUNNER_MODEL_PATCH_MISMATCH');
    }
    return selected;
  },{global:true,prepend:true}));
  ctx.effect(()=>ctx.on('agent/request-error',async (payload,next)=>{
    const recovery=await next();
    return process.env.INTEL_DISABLE_PROVIDER_RETRY==='1'?undefined:recovery;
  },{global:true,prepend:true}));
  ctx.provide('intelTaskRuntime',{ready:true});
}
