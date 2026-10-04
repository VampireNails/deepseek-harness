import {TASKS} from '../../intel-plugins/dsh-intelligence-evolution/src/tasks.js';

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
  const template=TASK_ALIASES[id]??(Object.hasOwn(TASKS,id)?id:null);
  const prompt=template?TASKS[template].prompt:customPrompt;
  if(typeof prompt!=='string'||!prompt.trim())throw Error('custom task needs an explicit prompt');
  return {id,template,prompt,...selected,
    reason:route?'configured-task':'unconfigured-task',fallback:route?.fallback,
    retrySafe:route?.retrySafe===true};
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
export async function runTask(config,id,customPrompt,execute,record){
  const plan=resolveTask(config,id,customPrompt);
  const info={taskId:id,provider:plan.provider,model:plan.model,transport:plan.transport,template:plan.template,reason:plan.reason};
  await record({kind:'start',...info});
  const exit=await execute(plan);
  await record({kind:'finish',...info,exit});
  if(exit===0||!plan.fallback)return exit;
  if(!plan.retrySafe){await record({kind:'retry-refused',...info,primaryExit:exit});return exit;}
  const next={...plan,...resolveRoute(config,plan.fallback,'fallback route')};
  await record({kind:'fallback',taskId:id,provider:next.provider,model:next.model,transport:next.transport,primaryExit:exit});
  const fallbackExit=await execute(next);
  await record({kind:'finish',taskId:id,provider:next.provider,model:next.model,transport:next.transport,exit:fallbackExit});
  return fallbackExit;
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
