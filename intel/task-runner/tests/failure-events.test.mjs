import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,delimiter} from 'node:path';
import {spawnSync,spawn} from 'node:child_process';
import {once} from 'node:events';
import {fileURLToPath} from 'node:url';
import {JobLog} from '../../../intel-plugins/dsh-intelligence-joblog/src/joblog.js';
import {startUpkeepServer} from '../../../intel-plugins/dsh-intelligence-evolution/src/upkeep-socket.js';

async function fixture(t){
  const dir=await mkdtemp(join(tmpdir(),'runner-failure-events-'));t.after(()=>rm(dir,{recursive:true,force:true}));
  const home=join(dir,'home'),bin=join(dir,'bin'),config=join(dir,'routes.json');await mkdir(home);await mkdir(bin);
  await writeFile(join(bin,'dsh'),'#!/bin/sh\ncat >/dev/null\nif [ "$TEST_HANG" = 1 ]; then sleep 30; fi\nif [ "$TEST_SIGNAL" = 1 ]; then kill -TERM $$; fi\nexit "${TEST_EXIT:-0}"\n',{mode:0o700});
  await writeFile(config,JSON.stringify({timeoutMs:1000,lockTimeoutSeconds:1,defaultRoute:'test',routes:{test:{provider:'isolated',model:'fixture'}}}));
  const env={...process.env,DSH_HOME:home,INTEL_ROUTER_CONFIG:config,PATH:bin+delimiter+process.env.PATH};delete env.INTEL_RUNNER_LOCK;
  const cli=fileURLToPath(new URL('../cli.mjs',import.meta.url));
  return {home,env,cli,config,run:extra=>spawnSync(process.execPath,[cli,'custom','PRIVATE_PROMPT token=SECRET'],{env:{...env,...extra},encoding:'utf8',timeout:15000}),
    events:async(scope='production')=>{try{return (await readFile(join(home,'intel-system-events',scope==='test'?'events.test.jsonl':'events.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);}catch(e){if(e.code==='ENOENT')return [];throw e;}}};
}
test('real Linux CLI failure/timeout events correlate with JobLog and successful/test runs stay isolated',{skip:process.platform!=='linux'},async t=>{
  const f=await fixture(t);assert.equal(f.run({}).status,0);assert.deepEqual(await f.events(),[]);
  for(const [env,exit,category] of [[{TEST_EXIT:'7'},7,'nonzero'],[{TEST_HANG:'1'},124,'timeout']]){
    const r=f.run(env);assert.equal(r.status,exit,r.stderr);const ev=(await f.events()).at(-1);
    assert.equal(ev.type,'task.failed');assert.equal(ev.exitCode,exit);assert.equal(ev.exitCategory,category);
    assert.ok(new JobLog(join(f.home,'intel-joblog')).readHistory().some(row=>row.runId===ev.runId&&row.status==='fail'));
    assert.doesNotMatch(JSON.stringify(ev),/PRIVATE_PROMPT|SECRET|\/tmp\//);
  }
  assert.equal(f.run({TEST_EXIT:'2',INTEL_JOBLOG_SCOPE:'test'}).status,2);assert.equal((await f.events()).length,2);assert.equal((await f.events('test')).length,1);
  const reserved=f.run({TEST_EXIT:'75'});assert.equal(reserved.status,75,reserved.stderr);assert.equal((await f.events()).at(-1).type,'task.failed');assert.equal((await f.events()).length,3);
  const signal=f.run({TEST_SIGNAL:'1'});assert.equal(signal.status,1,signal.stderr);assert.equal((await f.events()).at(-1).exitCategory,'signal');assert.equal((await f.events()).at(-1).code,'TASK_SIGNAL');
});
test('real upkeep quiet admission is silent and failed execution preserves run identity and exit',{skip:process.platform!=='linux'},async t=>{
  const f=await fixture(t),socket=join(f.home,'upkeep','source.sock');
  let cut={cursors:{s:0},messages:[]},verified=true;
  const server=await startUpkeepServer({socketPath:socket,timeoutMs:2000,source:{capture:async()=>cut},inspect:async()=>({complete:verified,failedWrites:0,writeIds:[]})});t.after(()=>server.close());
  const config=JSON.parse(await readFile(f.config,'utf8'));config.upkeep={socketPath:socket,sourceTimeoutMs:1000,initialBackoffMs:10000,maxBackoffMs:20000,maxBatchMessages:3,maxBatchChars:1000};await writeFile(f.config,JSON.stringify(config));
  async function run(extra={}){const child=spawn(process.execPath,[f.cli,'evolve_upkeep'],{env:{...f.env,...extra},stdio:['ignore','pipe','pipe']});let stderr='';child.stderr.on('data',x=>stderr+=x);const [code]=await once(child,'close');return {code,stderr};}
  assert.equal((await run()).code,0);assert.equal((await run()).code,0);assert.deepEqual(await f.events(),[]);assert.equal(new JobLog(join(f.home,'intel-joblog')).readHistory().length,0);
  const stateFile=join(f.home,'intel-joblog','upkeep','state.json');let state=JSON.parse(await readFile(stateFile,'utf8'));state.nextDueAtMs=0;await writeFile(stateFile,JSON.stringify(state));
  assert.equal((await run()).code,0);assert.deepEqual(await f.events(),[]);
  state=JSON.parse(await readFile(stateFile,'utf8'));state.nextDueAtMs=0;await writeFile(stateFile,JSON.stringify(state));
  cut={cursors:{s:1},messages:[{sessionId:'s',seq:1,text:'PRIVATE_SOURCE'}]};
  const failed=await run({TEST_EXIT:'6'});assert.equal(failed.code,6,failed.stderr);
  const ev=(await f.events()).at(-1),rows=new JobLog(join(f.home,'intel-joblog')).readHistory();assert.equal(rows.length,1);assert.equal(rows[0].runId,ev.runId);assert.equal(rows[0].status,'fail');assert.equal(ev.exitCode,6);
  assert.doesNotMatch(JSON.stringify(ev)+failed.stderr,/PRIVATE_SOURCE/);
  // Process exit 0 alone does not erase a genuine final verification failure.
  state.pending=null;state.nextDueAtMs=0;await writeFile(stateFile,JSON.stringify(state));verified=false;
  const unverified=await run({});assert.equal(unverified.code,1,unverified.stderr);const invalid=(await f.events()).at(-1);
  assert.equal(invalid.code,'UPKEEP_RUN_UNVERIFIED');assert.equal(new JobLog(join(f.home,'intel-joblog')).readHistory()[0].runId,invalid.runId);
});
test('real flock timeout emits one lock event without executing the task',{skip:process.platform!=='linux'},async t=>{
  const f=await fixture(t),locks=join(f.home,'intel-joblog','runner-locks');await mkdir(locks,{recursive:true});
  const holder=spawn('flock',['-x',join(locks,'custom.lock'),'sh','-c','echo ready; exec sleep 30'],{detached:true,stdio:['ignore','pipe','ignore']});
  await once(holder.stdout,'data');t.after(()=>{try{process.kill(-holder.pid,'SIGTERM');}catch(e){if(e.code!=='ESRCH')throw e;}});
  const r=f.run({});assert.equal(r.status,75,r.stderr);const events=await f.events();assert.equal(events.length,1);
  assert.equal(events[0].type,'runner.lock_timeout');assert.equal(events[0].code,'RUNNER_LOCK_TIMEOUT');
  assert.equal(new JobLog(join(f.home,'intel-joblog')).readHistory().length,0);
});
test('event source failure is visible and never replaces a real exit or successful completion',{skip:process.platform!=='linux'},async t=>{
  const f=await fixture(t);await mkdir(join(f.home,'intel-system-events'));await writeFile(join(f.home,'intel-system-events','events.jsonl'),'{broken\n');
  const r=f.run({TEST_EXIT:'9'});assert.equal(r.status,9,r.stderr);assert.match(r.stderr,/SYSEVENTS_INVALID_DATA/);
  const rows=new JobLog(join(f.home,'intel-joblog')).readHistory();assert.equal(rows.length,1);assert.equal(rows[0].status,'fail');
  assert.equal(f.run({}).status,0);assert.equal(new JobLog(join(f.home,'intel-joblog')).readHistory()[0].status,'ok');
});
test('JobLog failure cannot suppress the original task event or manufacture failure after success',{skip:process.platform!=='linux'},async t=>{
  const f=await fixture(t);const log=new JobLog(join(f.home,'intel-joblog'));await writeFile(log.runsFile,'{');
  const fail=f.run({TEST_EXIT:'8'});assert.equal(fail.status,8,fail.stderr);assert.match(fail.stderr,/JOBLOG_INVALID_DATA/);
  assert.equal((await f.events()).length,1);assert.equal((await f.events())[0].exitCode,8);
  const success=f.run({});assert.equal(success.status,1,success.stderr);assert.equal((await f.events()).length,1);
});
