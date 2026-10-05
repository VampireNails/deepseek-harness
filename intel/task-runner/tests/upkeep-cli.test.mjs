import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,stat,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,delimiter} from 'node:path';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {startUpkeepServer} from '../../../intel-plugins/dsh-intelligence-evolution/src/upkeep-socket.js';

function invoke(cli,env,id='evolve_upkeep'){
  return new Promise((resolve,reject)=>{
    const child=spawn(process.execPath,[cli,id],{env,stdio:['ignore','pipe','pipe']});let stdout='',stderr='';
    child.stdout.on('data',chunk=>{stdout+=chunk;});child.stderr.on('data',chunk=>{stderr+=chunk;});
    child.once('error',reject);child.once('close',code=>resolve({code,stdout,stderr}));
  });
}
test('real Linux CLI skips empty work, sends only a new batch and verifies the same run before checkpointing',async t=>{
  const directory=await mkdtemp(join(tmpdir(),'upkeep-cli-'));t.after(()=>rm(directory,{recursive:true,force:true}));
  const bin=join(directory,'bin'),home=join(directory,'home'),socketPath=join(directory,'source','query.sock'),capture=join(directory,'dispatch.json');
  await mkdir(bin);await mkdir(home);
  await writeFile(join(bin,'dsh'),`#!/usr/bin/env node
import {readFileSync,writeFileSync} from 'node:fs';
writeFileSync(process.env.TEST_CAPTURE,JSON.stringify({runId:process.env.INTEL_RUN_ID,taskId:process.env.INTEL_TASK_ID,
  patch:JSON.parse(readFileSync(process.argv[process.argv.indexOf('--patch')+1],'utf8')),prompt:readFileSync(0,'utf8')}),{mode:0o600});
`,{mode:0o700});
  let fresh=false,captures=0,inspections=0;
  const message={sessionId:'private-session-marker',seq:1,messageId:'private-message-marker',text:'private new human input marker'};
  const server=await startUpkeepServer({socketPath,timeoutMs:1000,
    source:{async capture(request){captures++;return {cursors:{[message.sessionId]:fresh?1:0},messages:fresh?[message]:[],counts:{newMessages:fresh?1:0}};}},
    async inspect(identity){inspections++;const sent=JSON.parse(await readFile(capture,'utf8'));assert.equal(identity.runId,sent.runId);assert.equal(identity.taskId,sent.taskId);return {complete:true,failedWrites:0,writeIds:[17]};},
  });t.after(()=>server.close());
  const router=join(directory,'routes.json');
  await writeFile(router,JSON.stringify({timeoutMs:1000,defaultRoute:'fixture',routes:{fixture:{provider:'fixture',model:'fixture-model'}},
    upkeep:{socketPath,sourceTimeoutMs:1000,initialBackoffMs:100,maxBackoffMs:400,maxBatchMessages:3,maxBatchChars:1024}}));
  const env={...process.env,DSH_HOME:home,INTEL_ROUTER_CONFIG:router,TEST_CAPTURE:capture,DSH_TOOLS_MODE:'ptc',PATH:bin+delimiter+process.env.PATH};
  const cli=fileURLToPath(new URL('../cli.mjs',import.meta.url));
  const bootstrap=await invoke(cli,env);assert.equal(bootstrap.code,0,bootstrap.stderr);
  await assert.rejects(()=>stat(capture),{code:'ENOENT'},'Bootstrap must not launch the model');
  const stateFile=join(home,'intel-joblog','upkeep','state.json');
  const before=JSON.parse(await readFile(stateFile,'utf8'));assert.equal(before.cursors[message.sessionId],0);
  await writeFile(stateFile,JSON.stringify({...before,nextDueAtMs:0}),{mode:0o600});fresh=true;
  const processed=await invoke(cli,env,'memory_upkeep');assert.equal(processed.code,0,processed.stderr);
  const sent=JSON.parse(await readFile(capture,'utf8'));
  assert.equal(sent.taskId,'memory_upkeep');assert.ok(sent.prompt.includes(message.text));assert.ok(sent.prompt.includes(message.messageId));
  assert.deepEqual(sent.patch.find(row=>row.id==='tools'),{id:'tools',config:{mode:'native'}},
    'Upkeep must offer the native tools allowed by its guard even when the process opts into PTC');
  const after=JSON.parse(await readFile(stateFile,'utf8'));assert.equal(after.cursors[message.sessionId],1);assert.equal(after.pending??null,null);
  assert.equal(captures,2);assert.equal(inspections,1);
  const audit=await readFile(join(home,'intel-joblog','route-runs.jsonl'),'utf8');
  for(const privateMarker of [message.text,message.messageId,message.sessionId])assert.equal(audit.includes(privateMarker),false);
  assert.equal((await stat(stateFile)).mode&0o777,0o600);
});

test('upkeep without a reachable configured source fails rather than falling back to the old search loop',async t=>{
  const directory=await mkdtemp(join(tmpdir(),'upkeep-cli-unavailable-'));t.after(()=>rm(directory,{recursive:true,force:true}));
  const bin=join(directory,'bin');await mkdir(bin);
  await writeFile(join(bin,'dsh'),'#!/bin/sh\nexit 0\n',{mode:0o700});
  const config=join(directory,'routes.json');await writeFile(config,JSON.stringify({timeoutMs:1000,defaultRoute:'fixture',routes:{fixture:{provider:'fixture',model:'fixture'}}}));
  const result=await invoke(fileURLToPath(new URL('../cli.mjs',import.meta.url)),{...process.env,DSH_HOME:join(directory,'home'),INTEL_ROUTER_CONFIG:config,PATH:bin+delimiter+process.env.PATH});
  assert.equal(result.code,1);assert.match(result.stderr,/UPKEEP_INVALID_CONFIG/);
});
