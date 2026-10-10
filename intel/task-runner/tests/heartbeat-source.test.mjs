import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,rm,chmod,symlink} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {captureHeartbeat,heartbeatConfig} from '../heartbeat-source.mjs';
import {createHeartbeatStateStore} from '../heartbeat.mjs';
import {JobLog} from '../../../intel-plugins/dsh-intelligence-joblog/src/joblog.js';

async function fixture(t){
  const home=await mkdtemp(join(tmpdir(),'heartbeat-source-'));t.after(()=>rm(home,{recursive:true,force:true}));await mkdir(join(home,'intel-evolution'));await mkdir(join(home,'intel-joblog'));
  const file=join(home,'intel-evolution','HEARTBEAT.md'),routes=join(home,'intel-joblog','route-runs.jsonl'),latest=join(home,'intel-joblog','job_runs.json');
  await writeFile(file,await readFile(new URL('../../../intel-plugins/dsh-intelligence-evolution/HEARTBEAT.md',import.meta.url)));await writeFile(routes,'');await writeFile(latest,'{}');
  const config=heartbeatConfig(JSON.parse(await readFile(new URL('../routes.json',import.meta.url),'utf8')).heartbeat);
  const outputs={systemctl:'ActiveState=active\nSubState=running\nNRestarts=0\nInvocationID=aa\n',ss:'LISTEN 0 1 100.73.148.102:43197 0.0.0.0:*\n',df:'Filesystem 1024-blocks Used Available Capacity Mounted on\nx 9000000 1000000 8000000 12% /\n',free:'Mem: 900000000 100000000 400000000 0 400000000 800000000\n',journalctl:''};
  return {home,file,routes,latest,outputs,config,read:(nowMs=Date.now(),scope='production')=>captureHeartbeat({home,config,nowMs,scope,probe:async key=>outputs[key]})};
}
test('health identity is stable across safe resource fluctuations; each real health threshold changes it',async t=>{
  const f=await fixture(t),first=await f.read();f.outputs.free='Mem: 900000000 100000000 400000000 0 400000000 700000000\n';assert.equal((await f.read()).signalId,first.signalId);
  for(const [key,value,source]of [['free','Mem: 900000000 100000000 400000000 0 400000000 10\n','memory'],['df','Filesystem 1024-blocks Used Available Capacity Mounted on\nx 9000000 8999999 1 99% /\n','disk'],['ss','','listener']]){
    const previous=f.outputs[key];f.outputs[key]=value;const cut=await f.read();assert.equal(cut.healthy,false);assert.equal(cut.signals.find(x=>x.source===source).status,'anomaly');assert.notEqual(cut.signalId,first.signalId);f.outputs[key]=previous;
  }
});
test('overridden failures and overdue unfinished runs retain exact identity/time; explicit test data stays excluded',async t=>{
  const f=await fixture(t),nowMs=Date.now(),time=new Date(nowMs-1000).toISOString();
  const rows=[{time,kind:'start',taskId:'custom',runId:'run-a',scope:'production'},{time,kind:'finish',taskId:'custom',runId:'run-a',scope:'production',exit:7},{time,kind:'finish',taskId:'custom',runId:'test-a',scope:'test',exit:8},{time:new Date(nowMs-f.config.pendingAfterMs-1).toISOString(),kind:'start',taskId:'waiting',runId:'run-b',scope:'production'}];
  await writeFile(f.routes,rows.map(JSON.stringify).join('\n')+'\n');await writeFile(f.latest,JSON.stringify({custom:{status:'ok',last:time,scope:'production'}}));
  const cut=await f.read(nowMs),events=cut.signals.find(x=>x.source==='cron').events;assert.equal(events.length,2);assert.equal(events[0].scope,'production');assert.ok(events.some(x=>x.runId==='run-a'&&x.timeMs===nowMs-1000));assert.ok(events.some(x=>x.runId==='run-b'&&x.status==='unfinished'));
});
test('T03 read-only history exposes a previous failure overwritten by success without source writes',async t=>{
  const f=await fixture(t),log=new JobLog(join(f.home,'intel-joblog')),time=new Date().toISOString();log._recordRun('study','fail','private prompt token=SECRET',{runId:'history-failure',finishedAt:time});log._recordRun('study','ok','done',{runId:'history-success'});
  const before=await readFile(log.historyFile);const cut=await f.read();assert.ok(cut.signals.find(x=>x.source==='cron').events.some(x=>x.runId==='history-failure'));assert.equal(Buffer.compare(before,await readFile(log.historyFile)),0);assert.doesNotMatch(JSON.stringify(cut),/private|SECRET/);
});
test('missing, corrupt, partial, symlinked, oversized and unknown legacy source never claim healthy',async t=>{
  const f=await fixture(t);
  await writeFile(f.routes,'{}');await assert.rejects(f.read(),{code:'HEARTBEAT_SOURCE_INCOMPLETE'});
  await writeFile(f.routes,'{broken\n');await assert.rejects(f.read(),{code:'HEARTBEAT_INVALID_SOURCE'});
  await writeFile(f.routes,JSON.stringify({time:new Date().toISOString(),taskId:'legacy',kind:'start'})+'\n');await assert.rejects(f.read(),{code:'HEARTBEAT_LEGACY_RUN_UNVERIFIED'});
  await writeFile(f.routes,'');await writeFile(f.latest,'{');await assert.rejects(f.read(),{code:'HEARTBEAT_INVALID_SOURCE'});
  await writeFile(f.latest,'{}');await rm(f.file);await symlink(f.latest,f.file);await assert.rejects(f.read(),{code:'HEARTBEAT_SOURCE_READ_FAILED'});
  await rm(f.file);await writeFile(f.file,'x'.repeat(f.config.maxSourceBytes+1));await assert.rejects(f.read(),{code:'HEARTBEAT_SOURCE_TOO_LARGE'});
  await rm(f.file);await assert.rejects(f.read(),{code:'HEARTBEAT_SOURCE_READ_FAILED'});
});
test('real unprivileged source read refusal is explicit and private checkpoint corruption is rejected',{skip:process.platform!=='linux'||process.getuid()!==0},async t=>{
  const f=await fixture(t);await chmod(f.home,0o755);await chmod(f.file,0o000);
  const source=fileURLToPath(new URL('../heartbeat-source.mjs',import.meta.url));
  const script=`import {captureHeartbeat} from ${JSON.stringify('file://'+source)};try{await captureHeartbeat({home:${JSON.stringify(f.home)},config:${JSON.stringify(f.config)},nowMs:Date.now()});process.exitCode=2;}catch(e){console.log(e.code);process.exitCode=0;}`;
  const child=spawnSync(process.execPath,['--input-type=module','-e',script],{uid:65534,gid:65534,encoding:'utf8',timeout:10000});assert.equal(child.signal,null);assert.equal(child.status,0,child.stderr);assert.match(child.stdout,/HEARTBEAT_SOURCE_READ_FAILED/);
  const stateFile=join(f.home,'private','state.json'),store=createHeartbeatStateStore(stateFile);assert.equal(await store.read(),null);await store.write({version:1});await writeFile(stateFile,'{');await assert.rejects(store.read(),{code:'HEARTBEAT_STATE_INVALID'});await chmod(stateFile,0o644);await assert.rejects(store.read(),{code:'HEARTBEAT_STATE_INVALID'});
});
test('config is opt-in compatible and refuses incomplete or invalid enabled limits',()=>{
  assert.equal(heartbeatConfig(undefined),null);assert.equal(heartbeatConfig({enabled:false}),null);assert.throws(()=>heartbeatConfig({enabled:true}),{code:'HEARTBEAT_INVALID_CONFIG'});assert.throws(()=>heartbeatConfig({enabled:'true'}),{code:'HEARTBEAT_INVALID_CONFIG'});
});
test('bounded historical alert identity preserves dates, excludes test bodies and rejects damaged source',async t=>{
  const f=await fixture(t),file=join(f.home,'intel-joblog','job_alerts.md'),old='2020-01-01 00:00:00';
  await writeFile(file,`\n## ${old} [study] 运行失败\nold PRIVATE token=SECRET\n`);const cut=await f.read();assert.equal(cut.healthy,true);assert.doesNotMatch(JSON.stringify(cut),/PRIVATE|SECRET/);
  await writeFile(file,`\n## ${old} [study] 运行失败\nold PRIVATE token=SECRET\n\n## ${old} [test] 运行失败 {scope:test}\nprivate test\n`);assert.equal((await f.read()).signalId,cut.signalId);
  await writeFile(file,'corrupt alert source\n');await assert.rejects(f.read(),{code:'HEARTBEAT_INVALID_SOURCE'});
});
test('future route start/finish, JobLog latest/history and alert times cannot prove current health',async t=>{
  const f=await fixture(t),now=Date.now(),future=new Date(now+60000).toISOString();
  for(const kind of ['start','finish']){await writeFile(f.routes,JSON.stringify({time:future,taskId:'custom',runId:'future-run',scope:'production',kind,...kind==='finish'?{exit:0}:{}})+'\n');await assert.rejects(f.read(now),{code:'HEARTBEAT_SOURCE_TIME_FUTURE'});}
  await writeFile(f.routes,'');await writeFile(f.latest,JSON.stringify({custom:{last:future,status:'ok',scope:'production'}}));await assert.rejects(f.read(now),{code:'HEARTBEAT_SOURCE_TIME_FUTURE'});
  await writeFile(f.latest,'{}');const log=new JobLog(join(f.home,'intel-joblog'));log._recordRun('custom','ok','done',{runId:'future-history',finishedAt:future});await writeFile(f.latest,'{}');await assert.rejects(f.read(now),{code:'HEARTBEAT_SOURCE_TIME_FUTURE'});await rm(log.historyFile);
  const date=new Date(now+60000),stamp=date.getFullYear()+'-'+String(date.getMonth()+1).padStart(2,'0')+'-'+String(date.getDate()).padStart(2,'0')+' '+String(date.getHours()).padStart(2,'0')+':'+String(date.getMinutes()).padStart(2,'0')+':'+String(date.getSeconds()).padStart(2,'0');
  await writeFile(join(f.home,'intel-joblog','job_alerts.md'),`\n## ${stamp} [custom] 运行失败\nunknown future\n`);await assert.rejects(f.read(now),{code:'HEARTBEAT_SOURCE_TIME_FUTURE'});
});
