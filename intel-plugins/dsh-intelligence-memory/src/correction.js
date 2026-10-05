import {randomUUID} from 'node:crypto';
import {defineTool} from '@deepseek-ai/dsh-tools';

const actions=new Map([['memory_invalidate','invalidate'],['memory_restore','restore']]);
const fail=code=>Object.assign(new Error(code),{code});

function checkedRecord(retriever,args,action) {
  if(!retriever.validity.prepared)throw fail('MEMORY_STATE_UNAVAILABLE');
  if(!args||!Number.isSafeInteger(args.expectedRevision)||args.expectedRevision<0||
      typeof args.reason!=='string'||!args.reason.trim()||args.reason.length>2048||
      /[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(args.reason))throw fail('MEMORY_INVALID_ARGUMENT');
  const record=retriever.describe(args.id);
  if(!record)throw fail('MEMORY_NOT_FOUND');
  if(record.revision!==args.expectedRevision)throw fail('MEMORY_REVISION_CONFLICT');
  if(record.active===(action==='restore'))throw fail('MEMORY_STATE_CONFLICT');
  return record;
}

function publicFailure(error) {
  return fail(typeof error?.code==='string'&&/^MEMORY_[A-Z_]+$/.test(error.code)?error.code:'MEMORY_OPERATION_FAILED');
}

function mutationTool(retriever,name,action) {
  return defineTool({
    name,
    description:action==='invalidate'
      ? '将错误的长期记忆标记为失效，保留原文与审计；必须先读取当前状态版本并经用户批准。'
      : '恢复已失效的长期记忆，保留原文与审计；必须先读取当前状态版本并经用户批准。',
    parameters:{
      id:{type:'number',required:true,description:'原记忆 ID'},
      expectedRevision:{type:'number',required:true,description:'memory_history 返回的当前 revision'},
      reason:{type:'string',required:true,description:'用户纠错或恢复的具体原因'},
    },
    output:{schema:{type:'json'},render:(_args,value)=>[{type:'text',text:`记忆 #${value.record.id} 已${value.record.active?'恢复':'失效'}，版本 ${value.record.revision}；原文和审计已保留。`}]},
    async execute(args,exec) {
      try {
        if(!exec.agent)throw fail('MEMORY_APPROVAL_REQUIRED');
        checkedRecord(retriever,args,action);
        return retriever.change({...args,action,mutationId:randomUUID()},
          {source:'approved-tool',sessionId:exec.agent.session.id,signal:exec.signal});
      } catch(error) {throw publicFailure(error);}
    },
  });
}

/** Register explicit history reads and user-approved validity changes as owned effects. */
export function registerMemoryCorrection(ctx,retriever) {
  ctx.effect(()=>ctx.on('tools/pre-execute',async(exec,next)=>{
    const decision=await next(), action=actions.get(exec.name);
    if(!action||decision.kind==='deny'||decision.kind==='cancel')return decision;
    try {
      const record=checkedRecord(retriever,exec.arguments,action);
      const reason=[`确认${action==='invalidate'?'失效':'恢复'}记忆 #${record.id}（版本 ${record.revision}）`,
        `原文：\n${record.text}`,`原因：\n${exec.arguments.reason.trim()}`];
      if(decision.kind==='ask'&&decision.reason)reason.push(decision.reason);
      return {kind:'ask',reason:reason.join('\n\n')};
    } catch(error) {return {kind:'deny',reason:publicFailure(error).message};}
  },{prepend:true}));
  ctx.effect(function*(){
    yield ctx.tools.register(defineTool({
      name:'memory_history',
      description:'显式读取记忆原文、有效状态、当前 revision 和最近 50 次纠错审计；包括已失效记忆。',
      parameters:{id:{type:'number',required:true,description:'原记忆 ID'}},
      output:{schema:{type:'json'},render:(_args,value)=>[{type:'text',text:JSON.stringify(value)}]},
      async execute(args){
        try{return {record:retriever.describe(args.id),history:retriever.history(args.id)};}
        catch(error){throw publicFailure(error);}
      },
    }));
    for(const [name,action] of actions)yield ctx.tools.register(mutationTool(retriever,name,action));
  },'intelMemory.correction');
}

/** The paired server supplies the actor; wire arguments never choose audit identity. */
export function memoryManagement(retriever) {
  const protect=fn=>(...args)=>{try{return fn(...args);}catch(error){throw publicFailure(error);}};
  return {
    available:retriever.validity.prepared,
    list:protect(args=>retriever.list(args)),
    detail:protect(id=>({record:retriever.describe(id),history:retriever.history(id)})),
    change:protect((args,signal)=>retriever.change(args,{source:'paired-client',signal})),
  };
}
