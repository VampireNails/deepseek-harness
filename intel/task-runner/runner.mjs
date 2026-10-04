import {TASKS} from '../../intel-plugins/dsh-intelligence-evolution/src/tasks.js';

export const TASK_ALIASES=Object.freeze({evolve_upkeep:'memory_upkeep',evolve_studying:'studying',
  evolve_ideas:'idea_curation',evolve_dreaming:'dreaming',evolve_skill_review:'skill_review',
  evolve_goal_review:'goal_review',heartbeat:'heartbeat',evolve_goal_act:'goal_act'});

/** Resolve a runtime template and explicit provider/model; invalid routes fail loudly. */
export function resolveTask(config,id,customPrompt){
  if(!/^[a-z][a-z0-9_-]{0,79}$/.test(id))throw Error('invalid task id');
  const route=config.tasks?.[id],routeId=route?.route??config.defaultRoute;
  const selected=config.routes?.[routeId];
  if(!selected||typeof selected.provider!=='string'||!selected.provider||
    typeof selected.model!=='string'||!selected.model)throw Error('invalid route');
  if(route?.fallback!==undefined){
    const fallback=config.routes?.[route.fallback];
    if(!fallback||typeof fallback.provider!=='string'||!fallback.provider||
      typeof fallback.model!=='string'||!fallback.model)throw Error('invalid fallback route');
  }
  const template=TASK_ALIASES[id]??(Object.hasOwn(TASKS,id)?id:null);
  const prompt=template?TASKS[template].prompt:customPrompt;
  if(typeof prompt!=='string'||!prompt.trim())throw Error('custom task needs an explicit prompt');
  return {id,template,prompt,provider:selected.provider,model:selected.model,
    reason:route?'configured-task':'unconfigured-task',fallback:route?.fallback,
    retrySafe:route?.retrySafe===true};
}

/** Record requested routes and process results; do not retry tasks that may have side effects. */
export async function runTask(config,id,customPrompt,execute,record){
  const plan=resolveTask(config,id,customPrompt);
  const info={taskId:id,provider:plan.provider,model:plan.model,template:plan.template,reason:plan.reason};
  await record({kind:'start',...info});
  const exit=await execute(plan);
  await record({kind:'finish',...info,exit});
  if(exit===0||!plan.fallback)return exit;
  if(!plan.retrySafe){await record({kind:'retry-refused',...info,primaryExit:exit});return exit;}
  const fallback=config.routes?.[plan.fallback];
  if(!fallback||typeof fallback.provider!=='string'||!fallback.provider||
    typeof fallback.model!=='string'||!fallback.model)throw Error('invalid fallback route');
  const next={...plan,provider:fallback.provider,model:fallback.model};
  await record({kind:'fallback',taskId:id,provider:next.provider,model:next.model,primaryExit:exit});
  const fallbackExit=await execute(next);
  await record({kind:'finish',taskId:id,provider:next.provider,model:next.model,exit:fallbackExit});
  return fallbackExit;
}

/** Preview a restricted migration; leave custom jobs and compound shell commands intact. */
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
