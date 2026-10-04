import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,readdirSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {FeedStore} from '../../../intel-plugins/dsh-intelligence-feed/src/feed.js';
import {ArtifactStore} from '../../../intel-plugins/dsh-intelligence-artifacts/src/artifacts.js';
import {openStore} from '../../../intel-plugins/dsh-intelligence-memory/src/store.js';
import {runTask} from '../runner.mjs';

function fixture(t){
  const home=mkdtempSync(join(tmpdir(),'intel-visible-output-'));
  const memory=openStore(join(home,'intel-memory'));
  t.after(()=>{memory.close();rmSync(home,{recursive:true,force:true});});
  const reference=memory.prepare('INSERT INTO memories(text,kind,created_at,session_id) VALUES (?,?,?,?)')
    .run('用户希望复盘结论能追溯来源','fact',1791129600000,'isolated-source-session');
  const referenceId=Number(reference.lastInsertRowid);
  const metadata={taskId:'evolve_dreaming',runId:'run-isolated-dreaming',runDate:'2026-10-05',
    idempotencyKey:'evolve_dreaming:2026-10-05:run-isolated-dreaming',references:[referenceId]};
  return {home,memory,metadata,feed:new FeedStore(join(home,'intel-feed')),
    artifacts:new ArtifactStore(join(home,'intel-artifacts'))};
}

test('Feed publication persists the runner date, run identity and existing memory references',t=>{
  const f=fixture(t);
  const id=f.feed.addPost('复盘结论','下一次先核对运行身份，再评估结果。','dreaming',f.metadata);
  const stored=new FeedStore(f.feed.dir).listPosts(10).find(post=>post.id===id);
  assert.equal(stored.body,'下一次先核对运行身份，再评估结果。');
  for(const [key,value] of Object.entries(f.metadata))assert.deepEqual(stored[key],value,key+' survives disk read');
  assert.ok(f.memory.prepare('SELECT id FROM memories WHERE id=?').get(stored.references[0]));
});

test('replaying the same Feed publication after reopening the store returns the existing post',t=>{
  const f=fixture(t);
  const first=f.feed.addPost('复盘结论','已经验证的结论','dreaming',f.metadata);
  const reopened=new FeedStore(f.feed.dir);
  const second=reopened.addPost('复盘结论','已经验证的结论','dreaming',f.metadata);
  assert.equal(second,first);
  assert.equal(reopened.listPosts(100).length,1);
});

test('Artifact publication is idempotent and different runs retain their own version references',t=>{
  const f=fixture(t);
  const first=f.artifacts.save('复盘报告','# 已核对的来源','markdown',f.metadata);
  const reopened=new ArtifactStore(f.artifacts.dir);
  const repeated=reopened.save('复盘报告','# 已核对的来源','markdown',f.metadata);
  assert.equal(repeated.id,first.id);
  assert.equal(repeated.version,1,'retry must not create an extra version');
  const next={...f.metadata,runId:'run-isolated-next',runDate:'2026-10-06',
    idempotencyKey:'evolve_dreaming:2026-10-06:run-isolated-next'};
  const updated=reopened.save('复盘报告','# 第二次核对的来源','markdown',next);
  assert.equal(updated.id,first.id);
  assert.equal(updated.version,2);
  for(const [key,value] of Object.entries(f.metadata))assert.deepEqual(reopened.get(first.id,1)[key],value);
  for(const [key,value] of Object.entries(next))assert.deepEqual(reopened.get(first.id,2)[key],value);
});

const taskIds=['evolve_dreaming','evolve_skill_review','evolve_studying','evolve_ideas','morning_briefing'];
function routes(taskId,visibleOutput='required'){
  return {defaultRoute:'primary',routes:{primary:{provider:'isolated-provider',model:'isolated-model'},
    fallback:{provider:'isolated-fallback',model:'isolated-fallback-model'}},
  tasks:{[taskId]:{route:'primary',fallback:'fallback',retrySafe:true,visibleOutput}}};
}

for(const taskId of taskIds)test(taskId+' cannot report success with only a process exit and memory writes',async t=>{
  const f=fixture(t),audit=[],executions=[];
  const exit=await runTask(routes(taskId),taskId,'controlled task',async plan=>{
    executions.push(plan);
    f.memory.prepare('INSERT INTO memories(text,kind,created_at) VALUES (?,?,?)').run('隔离复盘结果','fact',1791216000000);
    return 0;
  },entry=>audit.push(entry),{...f.metadata,taskId,timeZone:'Asia/Shanghai',
    inspectVisibleOutputs:async()=>[...new FeedStore(f.feed.dir).listPosts(100)]});
  assert.notEqual(exit,0,'memory-only success must fail the visible-output requirement');
  assert.equal(executions.length,1,'missing output must not replay completed side effects');
  assert.ok(audit.some(entry=>entry.kind==='no-visible-output'&&entry.taskId===taskId));
});

test('required task succeeds only with a persisted output from its own run',async t=>{
  const f=fixture(t),audit=[];
  const exit=await runTask(routes('evolve_dreaming'),'evolve_dreaming','controlled task',async plan=>{
    assert.equal(plan.runId,f.metadata.runId);
    assert.equal(plan.runDate,'2026-10-05');
    f.feed.addPost('来源已核对','可以追溯到保存的记忆。','dreaming',f.metadata);
    return 0;
  },entry=>audit.push(entry),{...f.metadata,timeZone:'Asia/Shanghai',inspectVisibleOutputs:async()=>
    new FeedStore(f.feed.dir).listPosts(100).filter(post=>post.runId===f.metadata.runId&&
      post.body.trim()&&post.references.every(id=>f.memory.prepare('SELECT id FROM memories WHERE id=?').get(id)))});
  assert.equal(exit,0);
  assert.equal(new FeedStore(f.feed.dir).listPosts(100).length,1);
  assert.equal(audit.some(entry=>entry.kind==='no-visible-output'),false);
});

test('upkeep with no new signal stays successful and creates no Feed or Artifact',async t=>{
  const f=fixture(t),audit=[];
  const exit=await runTask(routes('evolve_upkeep','optional'),'evolve_upkeep','controlled task',async()=>0,
    entry=>audit.push(entry),{...f.metadata,taskId:'evolve_upkeep',timeZone:'Asia/Shanghai',inspectVisibleOutputs:async()=>[]});
  assert.equal(exit,0);
  assert.deepEqual(new FeedStore(f.feed.dir).listPosts(100),[]);
  assert.deepEqual(readdirSync(f.artifacts.dir),[]);
  assert.equal(audit.some(entry=>entry.kind==='no-visible-output'),false);
});
