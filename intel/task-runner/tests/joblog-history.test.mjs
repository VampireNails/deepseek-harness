import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,rm} from 'node:fs/promises';
import {join,delimiter} from 'node:path';
import {tmpdir} from 'node:os';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {JobLog} from '../../../intel-plugins/dsh-intelligence-joblog/src/joblog.js';

async function fixture(t){
  const dir=await mkdtemp(join(tmpdir(),'runner-job-history-'));t.after(()=>rm(dir,{recursive:true,force:true}));
  const home=join(dir,'home'),bin=join(dir,'bin'),config=join(dir,'routes.json');
  await mkdir(home);await mkdir(bin);
  await writeFile(join(bin,'dsh'),'#!/bin/sh\ncat >/dev/null\nexit "${TEST_EXIT:-0}"\n',{mode:0o700});
  await writeFile(config,JSON.stringify({timeoutMs:1000,defaultRoute:'test',routes:{test:{provider:'isolated',model:'fixture'}}}));
  const env={...process.env,DSH_HOME:home,INTEL_ROUTER_CONFIG:config,PATH:bin+delimiter+process.env.PATH};
  const cli=fileURLToPath(new URL('../cli.mjs',import.meta.url));
  return {home,run:extra=>spawnSync(process.execPath,[cli,'custom','PRIVATE_PROMPT'],{env:{...env,...extra},encoding:'utf8',timeout:15000})};
}

test('Linux runner histories correlate with route runId and explicit tests cannot overwrite production',{skip:process.platform!=='linux'},async t=>{
  const {home,run}=await fixture(t);
  for(const code of [1,1,0]){const result=run({TEST_EXIT:String(code)});assert.equal(result.signal,null);assert.equal(result.status,code,result.stderr);}
  const log=new JobLog(join(home,'intel-joblog'));
  const rows=log.readHistory();assert.deepEqual(rows.map(r=>r.status),['ok','fail','fail']);
  const audit=(await readFile(join(log.dir,'route-runs.jsonl'),'utf8')).trim().split('\n').map(line=>JSON.parse(line));
  for(const row of rows){assert.ok(audit.some(entry=>entry.kind==='finish'&&entry.runId===row.runId));assert.ok(row.startedAt);assert.match(row.detail,/route-runs.jsonl/);assert.doesNotMatch(row.detail,/PRIVATE_PROMPT/);}
  const raw=await readFile(log.runsFile,'utf8');const result=run({INTEL_JOBLOG_SCOPE:'test'});assert.equal(result.status,0,result.stderr);
  assert.equal(await readFile(log.runsFile,'utf8'),raw);assert.equal(log.readHistory().length,3);assert.equal(log.readHistory({includeTests:true}).length,4);
});

test('Linux CLI reports a completion storage fault once without inventing another failed run',{skip:process.platform!=='linux'},async t=>{
  const {home,run}=await fixture(t);const log=new JobLog(join(home,'intel-joblog'));
  await writeFile(log.runsFile,'{');const result=run({});assert.equal(result.signal,null);assert.equal(result.status,1);
  assert.match(result.stderr,/Task runner failed: JOBLOG_INVALID_DATA/);assert.doesNotMatch(result.stderr,/PRIVATE_PROMPT/);
  assert.equal(await readFile(log.runsFile,'utf8'),'{');
  const audit=(await readFile(join(log.dir,'route-runs.jsonl'),'utf8')).trim().split('\n').map(line=>JSON.parse(line));
  assert.equal(audit.filter(entry=>entry.kind==='finish').length,1);assert.equal(audit.find(entry=>entry.kind==='finish').exit,0);
  assert.equal(audit.filter(entry=>entry.kind==='error').length,1);assert.equal(audit.at(-1).code,'JOBLOG_INVALID_DATA');
});
