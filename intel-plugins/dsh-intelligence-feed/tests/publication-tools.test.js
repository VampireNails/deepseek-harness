import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,readdirSync,readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {apply as applyFeed} from '../index.js';
import {apply as applyArtifacts} from '../../dsh-intelligence-artifacts/index.js';
import {openStore} from '../../dsh-intelligence-memory/src/store.js';

const environmentKeys=['DSH_HOME','INTEL_TASK_ID','INTEL_RUN_ID','INTEL_RUN_DATE'];
const runner={INTEL_TASK_ID:'evolve_dreaming',INTEL_RUN_ID:'run-publication-test',INTEL_RUN_DATE:'2026-10-05'};

async function fixture(t,automatic=false){
  const previous=new Map(environmentKeys.map(key=>[key,process.env[key]]));
  const home=mkdtempSync(join(tmpdir(),'intel-publication-tools-'));
  process.env.DSH_HOME=home;
  for(const key of environmentKeys.slice(1))delete process.env[key];
  if(automatic)Object.assign(process.env,runner);
  const memory=openStore(join(home,'intel-memory'));
  const disposers=[];
  t.after(()=>{
    for(const dispose of disposers.reverse())dispose();
    memory.close();
    for(const [key,value] of previous){if(value===undefined)delete process.env[key];else process.env[key]=value;}
    rmSync(home,{recursive:true,force:true});
  });
  const insert=memory.prepare('INSERT INTO memories(text,kind,created_at,session_id) VALUES (?,?,?,?)');
  const ids=['用户希望报告保留来源','用户偏好明确日期'].map(text=>
    Number(insert.run(text,'fact',1791129600000,'session-publication-test').lastInsertRowid));
  const tools=new Map(),services=new Map(),effects=[];
  const ctx={
    provide:(name,value)=>services.set(name,value),
    get:name=>services.get(name),
    tools:{register:tool=>{assert.equal(typeof tool.execute,'function');tools.set(tool.name,tool);
      const dispose=()=>tools.delete(tool.name);disposers.push(dispose);return dispose;}},
    effect:factory=>{
      const effect=(async()=>{
        const iterator=factory();
        let value;
        for(;;){const step=iterator.next(value);if(step.done)return;value=await step.value;}
      })();
      effects.push(effect);
      return ()=>{};
    },
  };
  applyFeed(ctx,{});applyArtifacts(ctx,{});
  await Promise.all(effects);
  assert.ok(tools.has('feed_post')&&tools.has('artifact_save'),'both real publishing tools registered');
  const execute=(name,args)=>tools.get(name).execute(args,{agent:{id:'session-publication-test',
    session:{id:'session-publication-test',header:{id:'session-publication-test'}}}});
  function published(name){
    if(name==='feed_post')return services.get('feed').listPosts(100);
    const store=services.get('artifacts');
    return readdirSync(store.dir,{withFileTypes:true}).filter(entry=>entry.isDirectory()).map(entry=>{
      const meta=JSON.parse(readFileSync(join(store.dir,entry.name,'meta.json'),'utf8'));
      return store.get(meta.id);
    });
  }
  return {home,memory,ids,execute,published,tools};
}

function args(name,references){
  return name==='feed_post'?{title:'隔离复盘',body:'核对来源后继续推进。',source:'dreaming',
    ...(references===undefined?{}:{references})}:
    {title:'隔离复盘报告',content:'# 结论\n核对来源后继续推进。',kind:'markdown',
      ...(references===undefined?{}:{references})};
}

for(const name of ['feed_post','artifact_save']){
  test(name+' manually publishes real content without inventing a runner identity',async t=>{
    const f=await fixture(t);
    await f.execute(name,args(name));
    const rows=f.published(name);assert.equal(rows.length,1);
    assert.equal(name==='feed_post'?rows[0].body:rows[0].content,args(name)[name==='feed_post'?'body':'content']);
    for(const field of ['taskId','runId','runDate','idempotencyKey'])assert.equal(rows[0][field],undefined);
  });

  test(name+' manually validates and persists existing memory references with duplicates removed',async t=>{
    const f=await fixture(t);
    await f.execute(name,args(name,[f.ids[0],f.ids[0],f.ids[1]]));
    const [stored]=f.published(name);
    assert.deepEqual(stored.references,f.ids);
    for(const id of stored.references)assert.ok(f.memory.prepare('SELECT id FROM memories WHERE id=?').get(id));
    assert.equal(stored.runId,undefined);
  });

  test(name+' automatically attaches trusted runner identity and valid references to disk',async t=>{
    const f=await fixture(t,true);
    await f.execute(name,args(name,[f.ids[0],f.ids[0],f.ids[1]]));
    const [stored]=f.published(name);
    assert.equal(stored.taskId,runner.INTEL_TASK_ID);
    assert.equal(stored.runId,runner.INTEL_RUN_ID);
    assert.equal(stored.runDate,runner.INTEL_RUN_DATE);
    assert.ok(typeof stored.idempotencyKey==='string'&&stored.idempotencyKey.length>0);
    assert.deepEqual(stored.references,f.ids);
  });

  test(name+' never replaces trusted runner identity with model-submitted metadata',async t=>{
    const f=await fixture(t,true);
    await f.execute(name,{...args(name,[f.ids[0]]),taskId:'forged-task',runId:'forged-run',
      runDate:'1999-01-01',idempotencyKey:'forged-key'});
    const [stored]=f.published(name);
    assert.equal(stored.taskId,runner.INTEL_TASK_ID);
    assert.equal(stored.runId,runner.INTEL_RUN_ID);
    assert.equal(stored.runDate,runner.INTEL_RUN_DATE);
    assert.notEqual(stored.idempotencyKey,'forged-key');
  });

  test(name+' repeats the same automatic tool call without adding a post or version',async t=>{
    const f=await fixture(t,true),request=args(name,[f.ids[0]]);
    await f.execute(name,request);
    const [first]=f.published(name);
    await f.execute(name,request);
    const rows=f.published(name);
    assert.equal(rows.length,1);
    assert.equal(rows[0].id,first.id);
    if(name==='artifact_save')assert.equal(rows[0].version,1);
  });

  for(const automatic of [false,true])test(name+' rejects nonexistent memory references before publishing '+(automatic?'automatically':'manually'),async t=>{
    const f=await fixture(t,automatic),before=f.published(name);
    await assert.rejects(f.execute(name,args(name,[999999])));
    assert.deepEqual(f.published(name),before,'rejected reference cannot leave a published record');
  });

  test(name+' rejects invalid reference values without numeric coercion or partial writes',async t=>{
    const f=await fixture(t,true);
    const malformed=[[String(f.ids[0])],['01'],[true],[null],[{}],[[]],[0],[-1],[1.5],[NaN],[Infinity],
      [Number.MAX_SAFE_INTEGER+1],null,{},'1'];
    for(const references of malformed){
      const before=f.published(name);
      await assert.rejects(f.execute(name,args(name,references)),
        'reject '+String(references));
      assert.deepEqual(f.published(name),before,'invalid references cannot partially publish');
    }
  });
}
