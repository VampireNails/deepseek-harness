import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,readFile,writeFile,rm,readdir,symlink} from 'node:fs/promises';
import {join,delimiter} from 'node:path';
import {tmpdir} from 'node:os';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const linux={skip:process.platform!=='linux'};
const entry=fileURLToPath(new URL('../run.sh',import.meta.url));
async function fixture(t){
  const directory=await mkdtemp(join(tmpdir(),'deployment-test-'));t.after(()=>rm(directory,{recursive:true,force:true}));
  const home=join(directory,'home'),bin=join(directory,'bin'),file=join(directory,'routes.json');await mkdir(home);await mkdir(bin);
  const config=JSON.parse(await readFile(new URL('../routes.json',import.meta.url),'utf8'));
  config.upkeep.socketPath=join(directory,'source.sock');config.upkeep.sourceTimeoutMs=100;
  const save=()=>writeFile(file,JSON.stringify(config));await save();
  await writeFile(join(bin,'dsh'),'#!/bin/sh\necho unexpected-model >&2\nexit 99\n',{mode:0o700});
  const env={...process.env,DSH_HOME:home,INTEL_ROUTER_CONFIG:file,INTEL_RUNNER_ENV_FILE:'/dev/null',PATH:bin+delimiter+process.env.PATH};
  for(const key of ['DEEPSEEK_API_KEY','INTEL_RUNNER_LOCK','INTEL_TASK_ID','INTEL_RUN_ID','INTEL_RUN_DATE'])delete env[key];
  const run=(...args)=>spawnSync('bash',[entry,...args],{env,encoding:'utf8',timeout:15000});
  return {directory,home,bin,config,save,run};
}

test('explicit source initialization publishes the shipped checklist and preserves empty and unfamiliar existing files',linux,async t=>{
  const f=await fixture(t),file=join(f.home,'intel-evolution','HEARTBEAT.md');
  assert.equal(f.run('--initialize-sources').status,0);
  assert.equal(await readFile(file,'utf8'),await readFile(new URL('../../../intel-plugins/dsh-intelligence-evolution/HEARTBEAT.md',import.meta.url),'utf8'));
  assert.deepEqual(await readdir(f.home),['intel-evolution']);
  for(const text of ['', 'damaged operator list']){
    await writeFile(file,text);const result=f.run('--initialize-sources');assert.equal(result.status,0,result.stderr);
    assert.equal(JSON.parse(result.stdout).checklist,'preserved');assert.equal(await readFile(file,'utf8'),text);
  }
});

test('source initialization refuses a parent alias without creating files in its target',linux,async t=>{
  const f=await fixture(t),target=join(f.directory,'outside');await mkdir(target);
  await symlink(target,join(f.home,'intel-evolution'));const result=f.run('--initialize-sources');
  assert.equal(result.status,1);assert.match(result.stderr,/SCHEDULE_UNSAFE_PATH/);assert.deepEqual(await readdir(target),[]);
});

test('readiness of a reset home is absent and does not create a profile, JobLog or source',linux,async t=>{
  const f=await fixture(t);const result=f.run('--check-ready');assert.equal(result.status,1);
  assert.match(result.stderr,/SCHEDULE_PROFILE_ABSENT/);assert.deepEqual(await readdir(f.home),[]);
});

test('ordinary daily and weekly tasks reject an absent profile before launching the model',linux,async t=>{
  const f=await fixture(t);
  for(const id of Object.keys(f.config.tasks).filter(id=>!['heartbeat','evolve_upkeep'].includes(id))){
    const result=f.run(id,'owned preflight prompt');assert.equal(result.status,1,result.stderr);assert.match(result.stderr,/SCHEDULE_PROFILE_ABSENT/);
    assert.doesNotMatch(result.stderr,/unexpected-model/);
  }
});

test('missing capability bundles and a malformed profile are distinguished before composition',linux,async t=>{
  const f=await fixture(t),directory=join(f.home,'profiles','evolve');await mkdir(directory,{recursive:true});
  const manifest=join(directory,'package.json');await writeFile(manifest,'{}');
  assert.match(f.run('--check-ready').stderr,/SCHEDULE_BUNDLE_MISSING/);
  await writeFile(manifest,'{');assert.match(f.run('--check-ready').stderr,/SCHEDULE_PROFILE_INVALID/);
});

test('healthy explicit opt-out runs through run.sh without credentials, profile or model completion',linux,async t=>{
  const f=await fixture(t);assert.equal(f.run('--initialize-sources').status,0);
  await writeFile(join(f.home,'intel-evolution','HEARTBEAT.md'),'');
  const result=f.run('heartbeat');assert.equal(result.status,0,result.stderr);assert.match(result.stderr,/heartbeat-empty-checklist/);
  assert.doesNotMatch(result.stderr,/unexpected-model/);
  const files=await readdir(join(f.home,'intel-joblog'));assert.equal(files.includes('job_runs.json'),false);
});

test('an invalid readiness flag rejects configuration instead of bypassing admission',linux,async t=>{
  const f=await fixture(t);f.config.requireReadyProfile='true';await f.save();
  const result=f.run('evolve_goal_act');assert.equal(result.status,1);assert.match(result.stderr,/SCHEDULE_INVALID_CONFIG/);
});

test('bootstrap refuses an existing installation before replacing personal files',linux,async t=>{
  const f=await fixture(t),profile=join(f.home,'profiles/evolve');await mkdir(profile,{recursive:true});
  const file=join(profile,'cordis.patch.yml');await writeFile(file,'owned-personal-settings');
  const bootstrap=fileURLToPath(new URL('../../bootstrap.sh',import.meta.url));
  const result=spawnSync('bash',[bootstrap],{env:{...process.env,DSH_HOME:f.home},encoding:'utf8',timeout:10000});
  assert.equal(result.status,1,result.stderr);assert.match(result.stderr,/Refusing existing profile/);
  assert.equal(await readFile(file,'utf8'),'owned-personal-settings');
});
