import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {TASKS} from '../src/tasks.js';
import {resolveTask,runTask} from '../../../intel/task-runner/runner.mjs';

const expected=readFileSync(new URL('./expected/dreaming-task-input.txt',import.meta.url),'utf8').trimEnd();
const expectedDispatch=readFileSync(new URL('./expected/dreaming-dispatch-input.txt',import.meta.url),'utf8').trimEnd();
const config=JSON.parse(readFileSync(new URL('../../../intel/task-runner/routes.json',import.meta.url),'utf8'));

test('dreaming resolves current failure sources and reserves publication in its ten-call prompt',()=>{
  assert.equal(TASKS.dreaming.prompt,expected);
  assert.equal(resolveTask(config,'evolve_dreaming','old JobLog CLI prompt').prompt,expected);
});

test('dreaming dispatch retains dated Feed delivery and reports a missing publication as failure',async()=>{
  const records=[];
  const exit=await runTask(config,'evolve_dreaming','stale',async plan=>{
    assert.equal(plan.prompt,expectedDispatch);
    assert.ok(plan.prompt.includes('本轮任务 evolve_dreaming，日期 2026-10-07（Asia/Shanghai）'));
    assert.ok(plan.prompt.includes('仅回复聊天或写记忆不算完成'));
    assert.equal(plan.retrySafe,false);
    return 0;
  },record=>records.push(record),{runDate:'2026-10-07',runId:'owned-dreaming-prompt-fixture',inspectVisibleOutputs:async()=>[]});
  assert.notEqual(exit,0);
  assert.equal(records.some(row=>row.kind==='fallback'),false);
  assert.equal(records.at(-1).kind,'finish');
  assert.equal(records.at(-1).exit,exit);
});
