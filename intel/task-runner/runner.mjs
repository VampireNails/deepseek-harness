import {TASKS} from '../../intel-plugins/dsh-intelligence-evolution/src/tasks.js';
import {randomUUID} from 'node:crypto';
import {classifyFailure} from './failure.mjs';

/** Production cron aliases to the evolution module's template names. */
export const TASK_ALIASES=Object.freeze({evolve_upkeep:'memory_upkeep',evolve_studying:'studying',
  evolve_ideas:'idea_curation',evolve_dreaming:'dreaming',evolve_skill_review:'skill_review',
  evolve_goal_review:'goal_review',heartbeat:'heartbeat',evolve_goal_act:'goal_act'});

function resolveRoute(config,id,label='route'){
  const selected=config.routes?.[id];
  if(!selected||typeof selected.provider!=='string'||!/^[-a-zA-Z0-9_.]{1,100}$/.test(selected.provider)||
    typeof selected.model!=='string'||!/^[-a-zA-Z0-9_.:/]{1,160}$/.test(selected.model)||
    (selected.transport!==undefined&&!['sse','websocket','websocket-cached','auto'].includes(selected.transport)))
    throw Error('invalid '+label);
  return {provider:selected.provider,model:selected.model,transport:selected.transport};
}

/**
 * Resolve a runtime template and explicit provider/model; invalid routes fail loudly.
 * @param config - Deployment routes and task assignments.
 * @param id - Registered cron task ID or explicit custom task name.
 * @param customPrompt - Required only for a custom task.
 * @returns The resolved template and requested model.
 */
export function resolveTask(config,id,customPrompt){
  if(!/^[a-z][a-z0-9_-]{0,79}$/.test(id))throw Error('invalid task id');
  const route=config.tasks?.[id],routeId=route?.route??config.defaultRoute;
  const selected=resolveRoute(config,routeId);
  if(route?.fallback!==undefined)resolveRoute(config,route.fallback,'fallback route');
  if(route?.retrySafe!==undefined&&typeof route.retrySafe!=='boolean')throw Error('invalid retrySafe');
  if(config.fallback?.enabled!==undefined&&typeof config.fallback.enabled!=='boolean')throw Error('invalid fallback enabled');
  if(config.fallback?.enabled&&(!Number.isSafeInteger(config.fallback.maxAttempts)||config.fallback.maxAttempts<1||config.fallback.maxAttempts>2||
    !Number.isSafeInteger(config.fallback.totalTimeoutMs)||config.fallback.totalTimeoutMs<1000||config.fallback.totalTimeoutMs>600000))
    throw Error('invalid fallback limits');
  if(route?.visibleOutput!==undefined&&!['required','optional'].includes(route.visibleOutput))throw Error('invalid visible output policy');
  if(route?.deduplicateDaily!==undefined&&typeof route.deduplicateDaily!=='boolean')throw Error('invalid daily deduplication policy');
  const template=TASK_ALIASES[id]??(Object.hasOwn(TASKS,id)?id:null);
  const prompt=template?TASKS[template].prompt:customPrompt;
  if(typeof prompt!=='string'||!prompt.trim())throw Error('custom task needs an explicit prompt');
  return {id,template,prompt,...selected,
    reason:route?'configured-task':'unconfigured-task',fallback:route?.fallback,
    retrySafe:route?.retrySafe===true,visibleOutput:route?.visibleOutput??'optional',deduplicateDaily:route?.deduplicateDaily===true};
}

/**
 * Record requested routes and process results; do not retry tasks that may have side effects.
 * @param config - Deployment routing configuration.
 * @param id - Task identifier.
 * @param customPrompt - Explicit custom task prompt, if needed.
 * @param execute - Launches a resolved profile request and returns an exit or safe runtime observations.
 * @param record - Persists a prompt-free route audit record.
 * @returns The final process exit code; launch failures finalize as runner errors and audit errors reject.
 */
export async function runTask(config,id,customPrompt,execute,record,options={}){
  const plan=resolveTask(config,id,customPrompt);
  const timeZone=options.timeZone??config.timeZone??'Asia/Shanghai';
  const runDate=options.runDate??new Intl.DateTimeFormat('en-CA',{timeZone,year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  const runId=options.runId??randomUUID();
  if(typeof runId!=='string'||!runId||runId.length>256||!/^\d{4}-\d{2}-\d{2}$/.test(runDate))throw Error('invalid run identity');
  Object.assign(plan,{runId,runDate,timeZone});
  const artifactGuidance=plan.template==='dreaming'?'':'长报告可用 artifact_save，同时在 Feed 中写清结论。';
  if(plan.visibleOutput==='required') plan.prompt += `\n\n可见交付要求：本轮任务 ${id}，日期 ${runDate}（${timeZone}）。结束前用 feed_post 发布具体结论和下一步；引用记忆时把 memory_search/memory_write 返回的实际 ID 放入 references。${artifactGuidance}无资料时如实发布本次检查范围与未发现新信号，禁止编造来源。任务身份由 runner 注入，不要自行填入或猜测 ID。仅回复聊天或写记忆不算完成。`;
  if(plan.visibleOutput==='required'&&typeof options.inspectVisibleOutputs!=='function')throw Error('visible output inspector required');
  const info={taskId:id,runId,runDate,timeZone,provider:plan.provider,model:plan.model,transport:plan.transport,template:plan.template,reason:plan.reason};
  await record({kind:'start',...info});
  const hasContent=output=>typeof output?.body==='string'&&output.body.trim()||typeof output?.content==='string'&&output.content.trim();
  if(plan.visibleOutput==='required'&&plan.deduplicateDaily){
    const previous=(await options.inspectVisibleOutputs(plan)).find(output=>output?.taskId===id&&output?.runDate===runDate&&hasContent(output)&&
      ['feed','artifact'].some(channel=>output.idempotencyKey===`${channel}:${id}:${runDate}`));
    if(previous){
      await record({kind:'already-published',...info,publishedRunId:previous.runId});
      await record({kind:'finish',...info,exit:0,reused:true});
      return 0;
    }
  }
  async function finish(attempt,result,attempts,failure){
    const exit=result.exit;
    const identity={...info,provider:attempt.provider,model:attempt.model,transport:attempt.transport};
    if(exit===0&&plan.visibleOutput==='required'){
      const outputs=await options.inspectVisibleOutputs(attempt);
      if(!Array.isArray(outputs)||!outputs.some(output=>output?.taskId===id&&output?.runId===runId&&output?.runDate===runDate&&hasContent(output))){
        await record({kind:'no-visible-output',...identity});
        await record({kind:'finish',...identity,exit:65,processExit:0,attempts,code:'TASK_NO_VISIBLE_OUTPUT',category:'no-visible-output'});
        return 65;
      }
    }
    await record({kind:'finish',...identity,exit,processExit:Object.hasOwn(result,'processExit')?result.processExit:exit,signal:result.signal,attempts,fallbackReason,...failure,
      actualProvider:result.receipt?.provider,actualModel:result.receipt?.model});
    return exit;
  }
  const enabled=config.fallback?.enabled===true;
  const now=options.monotonicNow??(()=>performance.now()),deadline=now()+(enabled?config.fallback.totalTimeoutMs:config.timeoutMs??600000);
  let attempt={...plan,attempt:1},result,failure,fallbackReason;
  for(;;){
    const timeoutMs=Math.max(1,Math.min(config.timeoutMs??600000,Math.floor(deadline-now())));
    let raw;
    try{raw=await execute({...attempt,timeoutMs,disableProviderRetry:enabled});}
    catch(error){raw={exit:1,launchCode:['ENOENT','EACCES','EPIPE'].includes(error.code)?error.code:'UNKNOWN'};}
    result=typeof raw==='number'?{exit:raw}:raw;
    if(!Number.isSafeInteger(result?.exit)||result.exit<0||result.exit>255)throw Error('invalid process result');
    failure=result.exit===0?undefined:classifyFailure(result);
    await record({kind:'attempt',...info,provider:attempt.provider,model:attempt.model,attempt:attempt.attempt,exit:result.exit,...failure,
      actualProvider:result.receipt?.provider,actualModel:result.receipt?.model});
    if(result.exit===0)break;
    const refusal=!enabled?'fallback-disabled':!plan.fallback?'fallback-unconfigured':attempt.attempt>=config.fallback.maxAttempts?'attempt-limit':
      deadline-now()<1?'deadline-exhausted':!failure.preExecution?'unsafe-or-unknown-result':undefined;
    if(refusal){
      fallbackReason=refusal;
      if(plan.fallback)await record({kind:'retry-refused',...info,attempt:attempt.attempt,primaryExit:result.exit,fallbackReason:refusal,...failure});
      break;
    }
    const next={...plan,...resolveRoute(config,plan.fallback,'fallback route'),attempt:attempt.attempt+1};
    fallbackReason='pre-execution-rejection';
    await record({kind:'fallback',...info,provider:next.provider,model:next.model,transport:next.transport,primaryExit:result.exit,
      attempt:next.attempt,fallbackReason:'pre-execution-rejection',...failure});
    attempt=next;
  }
  return await finish(attempt,result,attempt.attempt,failure);
}

/**
 * Preview a restricted migration; leave custom jobs and compound shell commands intact.
 * @param text - Existing crontab text.
 * @param runnerPath - Absolute executable path without shell metacharacters.
 * @returns Replacement text and count; never writes crontab.
 */
export function migrateCrontab(text,runnerPath){
  if(!/^\/[a-zA-Z0-9_./-]+$/.test(runnerPath))throw Error('invalid runner path');
  let changed=0;
  const migrated=text.split('\n').map(line=>{
    const match=line.match(/^(\s*\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+)\/root\/intel\/bin\/intel-task\.sh\s+([a-z][a-z0-9_-]*)\s+(?:"[^"$`]*"|'[^']*')\s*$/);
    if(!match||(!TASK_ALIASES[match[2]]&&!Object.hasOwn(TASKS,match[2])))return line;
    changed++;return match[1]+runnerPath+' '+match[2];
  });
  return {text:migrated.join('\n'),changed};
}
