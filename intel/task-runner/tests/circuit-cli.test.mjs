import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {fileURLToPath} from 'node:url';
import {JobLog} from '../../../intel-plugins/dsh-intelligence-joblog/src/joblog.js';

const linux={skip:process.platform!=='linux'};
async function fixture(t){
  const dir=await mkdtemp(join(tmpdir(),'circuit-cli-'));t.after(()=>rm(dir,{recursive:true,force:true}));
  const home=join(dir,'home'),bin=join(dir,'bin'),config=join(dir,'routes.json'),calls=join(dir,'calls');await mkdir(bin);await mkdir(home);
  await writeFile(join(bin,'dsh'),`#!/bin/sh\necho call >> '${calls}'\ncat >/dev/null\nexit "\${TEST_EXIT:-1}"\n`,{mode:0o700});
  const settings={timeoutMs:1000,lockTimeoutSeconds:2,defaultRoute:'one',routes:{one:{provider:'isolated',model:'stub'}},circuit:{enabled:true,threshold:3}};
  await writeFile(config,JSON.stringify(settings));
  const state=join(home,'intel-joblog','circuits','test','custom.json');
  async function run(extra={},args=['custom','isolated']){
    const child=spawn(process.execPath,[fileURLToPath(new URL('../cli.mjs',import.meta.url)),...args],
      {env:{...process.env,PATH:bin+':'+process.env.PATH,DSH_HOME:home,INTEL_ROUTER_CONFIG:config,INTEL_JOBLOG_SCOPE:'test',...extra},stdio:['ignore','ignore','pipe']});
    let stderr='';child.stderr.on('data',x=>stderr+=x);const [code,signal]=await once(child,'close');assert.equal(signal,null,stderr);return {code,stderr};
  }
  return {home,state,run,settings,config,calls};
}
test('concurrent processes serialize a three-run streak; open invocations never create a fake completion',linux,async t=>{
  const f=await fixture(t),results=await Promise.all([f.run(),f.run(),f.run(),f.run()]);
  assert.deepEqual(results.map(r=>r.code).sort(),[1,1,1,78]);
  assert.equal((await readFile(f.calls,'utf8')).trim().split('\n').length,3);
  assert.equal(new JobLog(join(f.home,'intel-joblog'),{scope:'test'}).readHistory({includeTests:true}).length,3);
  const state=JSON.parse(await readFile(f.state,'utf8'));assert.equal(state.failures,3);assert.equal(state.pending,null);
  const rows=(await readFile(join(f.home,'intel-system-events','events.test.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);
  assert.equal(rows.filter(x=>x.type==='task.circuit_open').length,1);
});
test('failed event append preserves the opening exit and outbox; next process repairs delivery without another model',linux,async t=>{
  const f=await fixture(t);f.settings.circuit.threshold=1;await writeFile(f.config,JSON.stringify(f.settings));
  const events=join(f.home,'intel-system-events');await mkdir(events);await writeFile(join(events,'events.test.jsonl'),'{broken\n');
  const failed=await f.run();assert.equal(failed.code,1,failed.stderr);assert.match(failed.stderr,/circuit-event-pending/);
  assert.notEqual(JSON.parse(await readFile(f.state,'utf8')).outbox,null);
  await writeFile(join(events,'events.test.jsonl'),'');
  assert.equal((await f.run()).code,78);assert.equal((await readFile(f.calls,'utf8')).trim().split('\n').length,1);
  assert.equal(JSON.parse(await readFile(f.state,'utf8')).outbox,null);
});
test('unfinished or corrupt durable admission never executes or releases an old run',linux,async t=>{
  const f=await fixture(t);await f.run({TEST_EXIT:'0'});
  const state=JSON.parse(await readFile(f.state,'utf8'));state.pending='old-uncertain';await writeFile(f.state,JSON.stringify(state),{mode:0o600});
  const blocked=await f.run({TEST_EXIT:'0'});assert.equal(blocked.code,1);assert.match(blocked.stderr,/CIRCUIT_RUN_UNRESOLVED/);
  assert.equal((await f.run({},['--release-circuit','custom','old-uncertain'])).code,1);
  assert.equal((await readFile(f.calls,'utf8')).trim().split('\n').length,1);
  await writeFile(f.state,'{broken');assert.match((await f.run()).stderr,/CIRCUIT_STATE_INVALID/);
});
