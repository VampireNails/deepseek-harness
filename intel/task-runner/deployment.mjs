import {readFile,mkdir,open,link,unlink,realpath,lstat,mkdtemp,symlink,writeFile,rm,access,constants} from 'node:fs/promises';
import {join,dirname,isAbsolute,delimiter} from 'node:path';
import {tmpdir} from 'node:os';
import {createRequire} from 'node:module';
import {randomUUID} from 'node:crypto';
import {execFile} from 'node:child_process';
import {resolveTask} from './runner.mjs';
import {heartbeatConfig,captureHeartbeat} from './heartbeat-source.mjs';
import {requestUpkeep} from '../../intel-plugins/dsh-intelligence-evolution/src/upkeep-socket.js';

const fail=code=>Object.assign(Error(code),{code});
const builtins=['morning_briefing','evolve_upkeep','evolve_studying','evolve_ideas','evolve_dreaming',
  'evolve_skill_review','evolve_goal_review','evolve_goal_act','heartbeat'];
const bundles=['@deepseek-ai/dsh-base','@deepseek-ai/dsh-headless','dsh-intelligence-memory',
  'dsh-intelligence-joblog','dsh-intelligence-evolution','dsh-intelligence-feed','dsh-intelligence-profile',
  'dsh-intelligence-sysevents','dsh-intelligence-artifacts','dsh-intelligence-execguard',
  'dsh-intelligence-goals','dsh-intelligence-goalact'];

async function canonical(path){
  if(!isAbsolute(path))throw fail('SCHEDULE_UNSAFE_PATH');
  try{if(await realpath(path)!==path)throw fail('SCHEDULE_UNSAFE_PATH');}
  catch(error){if(error.code==='ENOENT'&&dirname(path)!==path)return canonical(dirname(path));throw fail('SCHEDULE_UNSAFE_PATH');}
}

/**
 * Install only the absent shipped checklist after an operator chooses initialization.
 * Existing files, including empty opt-outs, are never replaced; aliases reject before writing.
 * @param home - Existing absolute DSH home owned by the deployment operator.
 * @returns Created or preserved checklist state; no profiles, credentials or schedules are enabled.
 */
export async function initializeSources(home){
  await canonical(home);
  const owner=await lstat(home);
  if(dirname(home)===home||!owner.isDirectory()||owner.uid!==process.getuid())throw fail('SCHEDULE_UNSAFE_PATH');
  const directory=join(home,'intel-evolution');await canonical(directory);
  await mkdir(directory,{recursive:true,mode:0o700});
  const file=join(directory,'HEARTBEAT.md');
  try{const info=await lstat(file);if(!info.isFile())throw fail('SCHEDULE_UNSAFE_PATH');return {checklist:'preserved'};}
  catch(error){if(error.code!=='ENOENT')throw error;}
  const text=await readFile(new URL('../../intel-plugins/dsh-intelligence-evolution/HEARTBEAT.md',import.meta.url));
  const temporary=join(directory,'.checklist-'+randomUUID());
  const handle=await open(temporary,'wx',0o600);
  try{
    try{await handle.writeFile(text);await handle.sync();}finally{await handle.close();}
    try{await link(temporary,file);}catch(error){if(error.code!=='EEXIST')throw error;return {checklist:'preserved'};}
    const dir=await open(directory,'r');try{await dir.sync();}finally{await dir.close();}
  }finally{await unlink(temporary);}
  return {checklist:'created'};
}

/**
 * Check all nine task capabilities and compose the installed evolve profile without mounting it.
 * Dump output and provider configuration are discarded; no model or credentials probe is made.
 * @param home - DSH home used by the runner and installed CLI.
 * @param config - Parsed routes configuration.
 * @returns Task IDs whose manifest requirements and boot-free composition passed.
 */
export async function checkProfile(home,config){
  if(config.requireReadyProfile!==undefined&&typeof config.requireReadyProfile!=='boolean')throw fail('SCHEDULE_INVALID_CONFIG');
  let manifest;
  try{manifest=JSON.parse(await readFile(join(home,'profiles','evolve','package.json'),'utf8'));}
  catch(error){throw fail(error.code==='ENOENT'?'SCHEDULE_PROFILE_ABSENT':'SCHEDULE_PROFILE_INVALID');}
  const declared=manifest?.dsh?.profile?.bundles;
  if(!Array.isArray(declared)||!bundles.every(bundle=>declared.includes(bundle)))throw fail('SCHEDULE_BUNDLE_MISSING');
  try{for(const id of builtins){if(!config.tasks?.[id])throw Error('missing task');resolveTask(config,id,'deployment route check');}}
  catch(error){throw fail('SCHEDULE_ROUTES_INVALID');}
  // The installed CLI rewrites cordis.yml even for dumps. Only a private scratch profile is disposable.
  const scratch=await mkdtemp(join(tmpdir(),'intel-readiness-'));
  try{
    const directory=join(scratch,'profiles','evolve');await mkdir(directory,{recursive:true});
    await writeFile(join(directory,'package.json'),JSON.stringify(manifest));
    await symlink(join(home,'profiles','evolve','node_modules'),join(directory,'node_modules'));
    for(const [source,target] of [[join(home,'profiles','evolve','cordis.patch.yml'),join(directory,'cordis.patch.yml')],
      [join(home,'cordis.patch.yml'),join(scratch,'cordis.patch.yml')]]){
      try{await writeFile(target,await readFile(source));}catch(error){if(error.code!=='ENOENT')throw fail('SCHEDULE_PROFILE_INVALID');}
    }
    let executable;
    for(const directory of (process.env.PATH??'').split(delimiter)){
      const candidate=join(directory,'dsh');
      try{await access(candidate,constants.X_OK);executable=await realpath(candidate);break;}
      catch(error){if(!['ENOENT','EACCES'].includes(error.code))throw fail('SCHEDULE_PROFILE_COMPOSITION_FAILED');}
    }
    if(!executable)throw fail('SCHEDULE_DSH_ABSENT');
    const dump=await new Promise((resolve,reject)=>execFile(executable,['--profile','evolve','--dump-config'],
      {env:{...process.env,DSH_HOME:scratch},timeout:30000,maxBuffer:4194304},(error,stdout)=>error?reject(fail('SCHEDULE_PROFILE_COMPOSITION_FAILED')):resolve(stdout)));
    try{
      const {parseDocument}=createRequire(executable)('yaml');
      const document=parseDocument(dump,{logLevel:'silent'}),entries=document.toJS();
      if(document.errors.length||!Array.isArray(entries))throw Error('invalid composition');
      const required=bundles.filter(bundle=>!bundle.startsWith('@')).map(name=>[name,name]);
      required.push([config.runtime?.headlessId??'headless-runner','@deepseek-ai/dsh-headless'],
        ['headless-startup','@deepseek-ai/dsh-headless/startup']);
      if(required.some(([id,name])=>!entries.some(entry=>entry?.id===id&&entry.name===name&&
        (entry.disabled===undefined||entry.disabled===false))))throw Error('inactive capability');
    }catch(error){throw fail('SCHEDULE_PROFILE_CAPABILITY_INACTIVE');}
  }finally{await rm(scratch,{recursive:true,force:true});}
  return builtins;
}

/**
 * Read deployment prerequisites through the same profile, six-source capture and Host socket.
 * An empty checklist is inactive; failures remain invalid and never imply readiness to enable cron.
 * @param home - Explicit isolated or deployment DSH home.
 * @param config - Parsed deployment routes; source configuration remains operator-owned.
 * @returns A safe readiness summary. Health and provider authorization require separate acceptance.
 */
export async function checkDeployment(home,config){
  const tasks=await checkProfile(home,config);
  const heartbeat=heartbeatConfig(config.heartbeat);if(!heartbeat)throw fail('SCHEDULE_HEARTBEAT_DISABLED');
  const cut=await captureHeartbeat({home,config:heartbeat,nowMs:Date.now(),scope:process.env.INTEL_JOBLOG_SCOPE??'production'});
  if(cut.empty)return {state:'inactive',code:'SCHEDULE_CHECKLIST_OPT_OUT',tasks,modelExecutions:0};
  if(!cut.healthy)throw fail('SCHEDULE_HEALTH_ANOMALY');
  const upkeep=config.upkeep;
  if(!upkeep||typeof upkeep.socketPath!=='string')throw fail('UPKEEP_INVALID_CONFIG');
  const source=await requestUpkeep(upkeep.socketPath,{version:1,operation:'capture',request:{bootstrap:true,cursors:{},
    maxBatchMessages:upkeep.maxBatchMessages,maxBatchChars:upkeep.maxBatchChars}},upkeep.sourceTimeoutMs);
  if(!source?.cursors||typeof source.cursors!=='object'||Array.isArray(source.cursors)||
    Object.values(source.cursors).some(seq=>!Number.isSafeInteger(seq)||seq< -1)||
    !Array.isArray(source.messages)||source.messages.length||!source.counts)throw fail('UPKEEP_INCOMPLETE_SOURCE');
  return {state:'ready',tasks,profile:'composition-only',healthy:cut.healthy,modelExecutions:0,
    authorization:'unchecked',schedule:'unchanged'};
}
