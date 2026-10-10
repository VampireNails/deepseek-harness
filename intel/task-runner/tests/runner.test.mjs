import {test} from 'node:test';
import assert from 'node:assert/strict';
import {resolveTask,runTask,migrateCrontab} from '../runner.mjs';
import {readFileSync} from 'node:fs';

const config={defaultRoute:'flash',routes:{flash:{provider:'deepseek-official',model:'deepseek-flash'},
  chat:{provider:'openai-codex',model:'gpt-5.5'}},tasks:{evolve_upkeep:{route:'chat',fallback:'flash',retrySafe:false}}};
test('both dreaming aliases dispatch one Feed requirement without permitting a prohibited Artifact call',async()=>{
  for(const id of ['evolve_dreaming','dreaming']){
    let prompt;
    const configured={...config,tasks:{[id]:{route:'chat',visibleOutput:'required'}}};
    const exit=await runTask(configured,id,undefined,async plan=>{prompt=plan.prompt;return 1;},()=>{},
      {runId:'isolated-dreaming-prompt',runDate:'2026-10-08',inspectVisibleOutputs:async()=>[]});
    assert.equal(exit,1);
    assert.match(prompt,/预留最后 1 次给 feed_post，不额外调用 artifact_save/);
    assert.doesNotMatch(prompt,/长报告可用 artifact_save/);
    assert.match(prompt,/结束前用 feed_post 发布具体结论和下一步/);
  }
});
test('default task routing preserves the deployed nine-task provider assignments',()=>{
  const deployed=JSON.parse(readFileSync(new URL('../routes.json',import.meta.url),'utf8'));
  for(const id of ['evolve_upkeep','heartbeat'])assert.equal(resolveTask(deployed,id).provider,'deepseek-official');
  for(const id of ['morning_briefing','evolve_studying','evolve_ideas','evolve_dreaming','evolve_skill_review','evolve_goal_review','evolve_goal_act'])
    {const plan=resolveTask(deployed,id,'brief');assert.equal(plan.provider,'openai-codex');assert.equal(plan.transport,'sse');}
});
test('deployed side-effecting tasks retain uncertain primary failure despite configured fallback',async()=>{
  const deployed=JSON.parse(readFileSync(new URL('../routes.json',import.meta.url),'utf8'));
  for(const [id,assignment] of Object.entries(deployed.tasks)){
    assert.equal(assignment.retrySafe,false,id);
    assert.equal(typeof assignment.fallback,'string',id);
    const audit=[],attempts=[];
    const exit=await runTask(deployed,id,'brief',async plan=>{attempts.push(plan);return 124;},
      row=>audit.push(row),{inspectVisibleOutputs:async()=>[],runDate:'2026-10-05'});
    assert.equal(exit,124,id);assert.equal(attempts.length,1,id);
    assert.equal(audit.at(-1).kind,'finish',id);assert.equal(audit.at(-1).exit,124,id);
    assert.equal(audit.some(row=>row.kind==='fallback'),false,id);
  }
});
test('known cron task IDs load the live template instead of stale crontab text',()=>{
  const task=resolveTask(config,'evolve_upkeep','old prompt');
  assert.equal(task.template,'memory_upkeep');assert.ok(task.prompt.includes('模板'));
  assert.equal(task.provider,'openai-codex');
  const custom=resolveTask(config,'custom','preserved prompt');
  assert.equal(custom.prompt,'preserved prompt');assert.equal(custom.reason,'unconfigured-task');
  assert.throws(()=>resolveTask(config,'custom'),/prompt/);
  assert.throws(()=>resolveTask({...config,tasks:{custom:{route:'missing'}}},'custom','text'),/route/);
  assert.throws(()=>resolveTask({...config,tasks:{custom:{route:'chat',fallback:'missing'}}},'custom','text'),/fallback/);
  assert.throws(()=>resolveTask({...config,routes:{flash:{...config.routes.flash,transport:'invalid'}}},'custom','text'),/route/);
});
test('primary failure is recorded and side-effecting tasks are not retried',async()=>{
  const seen=[],audit=[];
  const exit=await runTask(config,'evolve_upkeep','old',async plan=>{seen.push(plan);return 1;},entry=>audit.push(entry));
  assert.equal(exit,1);assert.equal(seen.length,1);
  assert.equal(audit.some(row=>row.kind==='retry-refused'),true);assert.equal(audit.at(-1).model,'gpt-5.5');
});
test('a read-only label cannot authorize replay of an unknown exit',async()=>{
  const safe={...config,routes:{...config.routes,chat:{...config.routes.chat,transport:'sse'}},tasks:{custom:{route:'chat',fallback:'flash',retrySafe:true}}},audit=[],seen=[];
  const exit=await runTask(safe,'custom','read only',async plan=>{seen.push(plan);return seen.length===1?1:0;},entry=>audit.push(entry));
  assert.equal(exit,1);assert.equal(seen.length,1);
  assert.equal(seen[0].transport,'sse');
  assert.equal(audit.find(e=>e.kind==='retry-refused').primaryExit,1);
  assert.equal(audit.at(-1).model,'gpt-5.5');
  assert.equal(await runTask(safe,'custom','read only',async()=>{throw Error('spawn failed');},()=>{}),1);
});
test('migration preview only replaces registered built-in invocations',()=>{
  const input='0 3 * * * /root/intel/bin/intel-task.sh evolve_upkeep "old"\n0 4 * * * /root/intel/bin/intel-task.sh custom "stay"\n# keep\n';
  const result=migrateCrontab(input,'/root/intel/dsh-fork/intel/task-runner/run.sh');
  assert.equal(result.changed,1);assert.ok(result.text.includes('run.sh evolve_upkeep'));
  assert.ok(result.text.includes('custom "stay"'));assert.ok(result.text.includes('# keep'));
  assert.throws(()=>migrateCrontab(input,'/bad path'),/path/);
  assert.equal(migrateCrontab('0 3 * * * /root/intel/bin/intel-task.sh evolve_upkeep "old" && echo chained\n','/safe/run.sh').changed,0);
});
