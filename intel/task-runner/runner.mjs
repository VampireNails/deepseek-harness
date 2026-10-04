import {TASKS} from '../../intel-plugins/dsh-intelligence-evolution/src/tasks.js';
import {randomUUID} from 'node:crypto';

/** Production cron aliases to the evolution module's template names. */
export const TASK_ALIASES=Object.freeze({evolve_upkeep:'memory_upkeep',evolve_studying:'studying',
  evolve_ideas:'idea_curation',evolve_dreaming:'dreaming',evolve_skill_review:'skill_review',
  evolve_goal_review:'goal_review',heartbeat:'heartbeat',evolve_goal_act:'goal_act'});

function resolveRoute(config,id,label='route'){
  const selected=config.routes?.[id];
  if(!selected||typeof selected.provider!=='string'||!selected.provider||
    typeof selected.model!=='string'||!selected.model||
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
 * @param execute - Launches a resolved profile request and returns its exit code.
 * @param record - Persists a prompt-free route audit record.
 * @returns The final process exit code; launch and audit errors reject.
 */
export async function runTask(config,id,customPrompt,execute,record,options={}){
  const plan=resolveTask(config,id,customPrompt);
  const timeZone=options.timeZone??config.timeZone??'Asia/Shanghai';
  const runDate=options.runDate??new Intl.DateTimeFormat('en-CA',{timeZone,year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  const runId=options.runId??randomUUID();
  if(typeof runId!=='string'||!runId||runId.length>256||!/^\d{4}-\d{2}-\d{2}$/.test(runDate))throw Error('invalid run identity');
  Object.assign(plan,{runId,runDate,timeZone});
  if(plan.visibleOutput==='required') plan.prompt += `\n\n可见交付要求：本轮任务 ${id}，日期 ${runDate}（${timeZone}）。结束前用 feed_post 发布具体结论和下一步；引用记忆时把 memory_search/memory_write 返回的实际 ID 放入 references。长报告可用 artifact_save，同时在 Feed 中写清结论。无资料时如实发布本次检查范围与未发现新信号，禁止编造来源。任务身份由 runner 注入，不要自行填入或猜测 ID。仅回复聊天或写记忆不算完成。`;
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
  async function finish(attempt,exit){
    const identity={...info,provider:attempt.provider,model:attempt.model,transport:attempt.transport};
    if(exit===0&&plan.visibleOutput==='required'){
      const outputs=await options.inspectVisibleOutputs(attempt);
      if(!Array.isArray(outputs)||!outputs.some(output=>output?.taskId===id&&output?.runId===runId&&output?.runDate===runDate&&hasContent(output))){
        await record({kind:'no-visible-output',...identity});
        await record({kind:'finish',...identity,exit:65,processExit:0});
        return 65;
      }
    }
    await record({kind:'finish',...identity,exit});
    return exit;
  }
  const exit=await finish(plan,await execute(plan));
  if(exit===65)return exit; // Never replay completed side effects to manufacture output.
  if(exit===0||!plan.fallback)return exit;
  if(!plan.retrySafe){await record({kind:'retry-refused',...info,primaryExit:exit});return exit;}
  const next={...plan,...resolveRoute(config,plan.fallback,'fallback route')};
  await record({kind:'fallback',...info,provider:next.provider,model:next.model,transport:next.transport,primaryExit:exit});
  return await finish(next,await execute(next));
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
