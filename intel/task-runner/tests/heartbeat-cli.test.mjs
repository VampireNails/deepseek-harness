import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,delimiter} from 'node:path';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {fileURLToPath} from 'node:url';
import {JobLog} from '../../../intel-plugins/dsh-intelligence-joblog/src/joblog.js';

async function fixture(t){
  const dir=await mkdtemp(join(tmpdir(),'heartbeat-cli-'));t.after(()=>rm(dir,{recursive:true,force:true}));
  const home=join(dir,'home'),bin=join(dir,'bin'),configFile=join(dir,'routes.json'),count=join(dir,'models');
  await mkdir(bin);await mkdir(join(home,'intel-evolution'),{recursive:true});
  await mkdir(join(home,'intel-joblog'));await writeFile(join(home,'intel-joblog','route-runs.jsonl'),'');await writeFile(join(home,'intel-joblog','job_runs.json'),'{}');await writeFile(join(home,'intel-joblog','job_test_runs.json'),'{}');
  await writeFile(join(home,'intel-evolution','HEARTBEAT.md'),await readFile(new URL('../../../intel-plugins/dsh-intelligence-evolution/HEARTBEAT.md',import.meta.url)));
  const dataFile=join(dir,'probes.json');
  const data={systemctl:'ActiveState=active\nSubState=running\nNRestarts=0\nInvocationID=aaaa\n',ss:'LISTEN 0 511 100.73.148.102:43197 0.0.0.0:*\n',df:'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/root 9000000 1000000 8000000 12% /\n',free:'              total used free shared buff/cache available\nMem: 900000000 100000000 400000000 0 400000000 800000000\nSwap: 0 0 0\n',journalctl:''};
  const probe=join(dir,'probe.mjs');
  await writeFile(probe,`import fs from 'node:fs';import path from 'node:path';const data=JSON.parse(fs.readFileSync(${JSON.stringify(dataFile)},'utf8'));const key=path.basename(process.argv[2]);if(data[key]===null)process.exit(3);if(data[key]==='HANG'){setInterval(()=>{},1000);await new Promise(()=>{});}process.stdout.write(data[key]);`);
  for(const command of Object.keys(data))await writeFile(join(bin,command),`#!/bin/sh\nexec '${process.execPath}' '${probe}' '${command}'\n`,{mode:0o700});
  await writeFile(join(bin,'dsh'),`#!/bin/sh\necho run >> '${count}'\ncat > '${join(dir,'prompt')}'\nexit "\${TEST_EXIT:-0}"\n`,{mode:0o700});
  const config={timeoutMs:1000,lockTimeoutSeconds:2,defaultRoute:'test',routes:{test:{provider:'isolated',model:'stub'}},heartbeat:{enabled:true,sourceTimeoutMs:500,maxSourceBytes:262144,windowMs:3600000,pendingAfterMs:700000,initialBackoffMs:100000,maxBackoffMs:200000,serviceUnit:'dsh-prod.service',listenAddress:'100.73.148.102',listenPort:43197,minDiskBytes:2147483648,minMemoryBytes:104857600}};
  const save=async()=>{await writeFile(dataFile,JSON.stringify(data));await writeFile(configFile,JSON.stringify(config));};await save();
  const cli=fileURLToPath(new URL('../cli.mjs',import.meta.url)),env={...process.env,DSH_HOME:home,INTEL_ROUTER_CONFIG:configFile,PATH:bin+delimiter+process.env.PATH};delete env.INTEL_RUNNER_LOCK;
  async function run(extra={}){const child=spawn(process.execPath,[cli,'heartbeat'],{env:{...env,...extra},stdio:['ignore','pipe','pipe']});let stderr='';child.stderr.on('data',x=>stderr+=x);const [code,signal]=await once(child,'close');assert.equal(signal,null);return {code,stderr};}
  const rows=async()=>{try{return (await readFile(join(home,'intel-joblog','route-runs.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);}catch(e){if(e.code==='ENOENT')return [];throw e;}};
  const models=async()=>{try{return (await readFile(count,'utf8')).trim().split('\n').length;}catch(e){if(e.code==='ENOENT')return 0;throw e;}};
  const events=async()=>{try{return (await readFile(join(home,'intel-system-events','events.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);}catch(e){if(e.code==='ENOENT')return [];throw e;}};
  return {home,config,data,save,run,rows,models,events,prompt:join(dir,'prompt'),checklist:join(home,'intel-evolution','HEARTBEAT.md'),state:join(home,'intel-joblog','heartbeat','state.json')};
}
const linux={skip:process.platform!=='linux'};
test('healthy and reliably empty heartbeat skip with zero model completions or notifications',linux,async t=>{
  const f=await fixture(t);assert.equal((await f.run()).code,0);assert.equal((await f.run()).code,0);assert.equal(await f.models(),0);
  assert.equal(new JobLog(join(f.home,'intel-joblog')).readHistory().length,0);assert.deepEqual(await f.events(),[]);
  const rows=await f.rows();assert.equal(rows[0].kind,'heartbeat-healthy');assert.equal(rows[1].kind,'heartbeat-no-change');assert.match(rows[0].signalId,/^[a-f0-9]{64}$/);assert.ok(rows[0].observedAtMs>0);
  await writeFile(f.checklist,'# empty\n');assert.equal((await f.run()).code,0);assert.equal(await f.models(),0);assert.equal((await f.rows()).at(-1).kind,'heartbeat-empty-checklist');
});
test('new anomaly, identical backoff, recovery and another signal each admit the intended run',linux,async t=>{
  const f=await fixture(t);await f.run();f.data.systemctl='ActiveState=failed\nSubState=failed\nNRestarts=0\nInvocationID=aaaa\n';await f.save();
  assert.equal((await f.run()).code,0);assert.equal(await f.models(),1);assert.equal((await f.run()).code,0);assert.equal(await f.models(),1);assert.equal((await f.rows()).at(-1).kind,'heartbeat-backoff');
  f.data.systemctl='ActiveState=active\nSubState=running\nNRestarts=0\nInvocationID=aaaa\n';await f.save();assert.equal((await f.run()).code,0);assert.equal(await f.models(),2);
  f.data.journalctl=JSON.stringify({__CURSOR:'cursor-1',__REALTIME_TIMESTAMP:String(Date.now()*1000),MESSAGE:'PRIVATE token=SECRET'})+'\n';await f.save();await f.run();assert.equal(await f.models(),3);await f.run();assert.equal(await f.models(),3);
  f.data.journalctl=JSON.stringify({__CURSOR:'cursor-2',__REALTIME_TIMESTAMP:String(Date.now()*1000),MESSAGE:'PRIVATE token=SECRET'})+'\n';await f.save();await f.run();assert.equal(await f.models(),4);assert.deepEqual(await f.events(),[]);
  assert.doesNotMatch(JSON.stringify(await f.rows()),/PRIVATE|SECRET|cursor-1/);
});
test('unavailable, malformed, partial, oversized and unknown checklist fail visibly before model admission',linux,async t=>{
  for(const [key,value,code] of [['systemctl',null,'HEARTBEAT_PROBE_FAILED'],['systemctl','ActiveState=active\n','HEARTBEAT_INVALID_SOURCE'],['journalctl','{broken\n','HEARTBEAT_INVALID_SOURCE'],['journalctl','{}','HEARTBEAT_SOURCE_INCOMPLETE'],['ss','x'.repeat(262145),'HEARTBEAT_SOURCE_TOO_LARGE'],['free','HANG','HEARTBEAT_SOURCE_TIMEOUT']]){
    const f=await fixture(t);f.data[key]=value;await f.save();const result=await f.run();assert.equal(result.code,1,result.stderr);assert.match(result.stderr,new RegExp(code));assert.equal(await f.models(),0);assert.equal((await f.events()).at(-1).code,code);
  }
  const f=await fixture(t);await writeFile(f.checklist,'- custom command\n');const result=await f.run();assert.equal(result.code,1);assert.match(result.stderr,/HEARTBEAT_UNSUPPORTED_CHECKLIST/);assert.equal(await f.models(),0);
  await rm(f.checklist);assert.equal((await f.run()).code,1);assert.equal(await f.models(),0);
});
test('concurrent real CLI invocations share admission and restart preserves pending failure without replay',linux,async t=>{
  const f=await fixture(t);f.data.systemctl='ActiveState=failed\nSubState=failed\nNRestarts=0\nInvocationID=aaaa\n';await f.save();
  const results=await Promise.all([f.run({TEST_EXIT:'7'}),f.run({TEST_EXIT:'7'})]);assert.ok(results.some(x=>x.code===7));assert.equal(await f.models(),1);
  const pending=JSON.parse(await readFile(f.state,'utf8')).pending;assert.ok(pending?.runId);const again=await f.run();assert.equal(again.code,1);assert.match(again.stderr,/HEARTBEAT_PREVIOUS_RUN_UNRESOLVED/);assert.equal(await f.models(),1);
  f.data.systemctl='ActiveState=active\nSubState=running\nNRestarts=0\nInvocationID=bbbb\n';await f.save();assert.equal((await f.run()).code,0);assert.equal(await f.models(),2);
  const old=(await f.events()).find(x=>x.exitCode===7);assert.equal(old.runId,pending.runId);assert.equal(old.code,'TASK_EXIT_NONZERO');
});
test('disabled or absent heartbeat config retains legacy execution',linux,async t=>{
  const f=await fixture(t);f.config.heartbeat.enabled=false;await f.save();assert.equal((await f.run()).code,0);delete f.config.heartbeat;await f.save();assert.equal((await f.run()).code,0);assert.equal(await f.models(),2);
});
test('actual CLI clock rollback, changed signal and test scope preserve admission and logged safe model input',linux,async t=>{
  const f=await fixture(t);f.data.systemctl='ActiveState=failed\nSubState=failed\nNRestarts=0\nInvocationID=aaaa\n';await f.save();assert.equal((await f.run()).code,0);
  let state=JSON.parse(await readFile(f.state,'utf8'));state.lastNowMs+=1000000;state.nextDueAtMs+=1000000;await writeFile(f.state,JSON.stringify(state));
  const rollback=await f.run();assert.equal(rollback.code,1);assert.match(rollback.stderr,/HEARTBEAT_CLOCK_ROLLBACK/);assert.equal(await f.models(),1);
  f.data.journalctl=JSON.stringify({__CURSOR:'source-cursor',__REALTIME_TIMESTAMP:String(Date.now()*1000),MESSAGE:'PRIVATE token=SECRET'})+'\n';await f.save();assert.equal((await f.run()).code,0);assert.equal(await f.models(),2);
  const prompt=await readFile(f.prompt,'utf8');assert.match(prompt,/运行器已完成有界只读预检/);assert.doesNotMatch(prompt,/PRIVATE|SECRET|source-cursor/);const summary=JSON.parse(prompt.split('\n')[1]);assert.equal(summary.complete,true);assert.match(summary.signalId,/^[a-f0-9]{64}$/);
  assert.equal((await f.run({INTEL_JOBLOG_SCOPE:'test'})).code,0);assert.equal(await f.models(),3);const isolated=JSON.parse(await readFile(join(f.home,'intel-joblog','heartbeat','test-state.json'),'utf8'));assert.equal(isolated.pending,null);
  assert.equal(JSON.parse(await readFile(f.state,'utf8')).lastNowMs,state.lastNowMs);assert.ok((await f.rows()).some(x=>x.scope==='test'&&x.kind==='heartbeat-signals'));
});
test('heartbeat real lock conflict reports timeout without probing or model completion',linux,async t=>{
  const f=await fixture(t),locks=join(f.home,'intel-joblog','runner-locks');await mkdir(locks);
  const holder=spawn('flock',['-x',join(locks,'heartbeat.lock'),'sh','-c','echo ready; exec sleep 30'],{detached:true,stdio:['ignore','pipe','ignore']});
  t.after(async()=>{const closed=once(holder,'close');try{process.kill(-holder.pid,'SIGTERM');}catch(e){if(e.code!=='ESRCH')throw e;}await closed;});await once(holder.stdout,'data');
  const result=await f.run();assert.equal(result.code,75);assert.equal(await f.models(),0);assert.equal((await f.events()).at(-1).code,'RUNNER_LOCK_TIMEOUT');assert.equal(new JobLog(join(f.home,'intel-joblog')).readHistory().length,0);
});
test('heartbeat JobLog source corruption emits the original source code without executing a model',linux,async t=>{
  const f=await fixture(t);await writeFile(join(f.home,'intel-joblog','job_history.sqlite'),'broken sqlite');const result=await f.run();assert.equal(result.code,1);assert.equal(await f.models(),0);assert.match(result.stderr,/JOBLOG_READ_FAILED/);
  const events=await f.events();assert.equal(events.length,1);assert.equal(events[0].code,'JOBLOG_READ_FAILED');assert.match(result.stderr,/"source":"joblog"/);
});
