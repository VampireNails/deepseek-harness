import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, symlink, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { once } from 'node:events';

test('real Loader memory tools preserve sources, expose soft capacity and persist first-step facts into later model requests', async t => {
  const root = await mkdtemp(join(tmpdir(), 'memory-growth-profile-')), home = join(root, 'home');
  const profile = join(home, 'profiles', 't15'), receipt = join(root, 'receipt');
  let child, done;
  t.after(async () => {
    let settled;
    if (child) { if (child.exitCode === null) child.kill(); settled=await done; }
    await rm(root, {recursive:true,force:true});
    const cleanup={pid:child?.pid,root,exitCode:settled?.[0],signal:settled?.[1],removed:true};
    if(process.env.T15_EVIDENCE)await writeFile(process.env.T15_EVIDENCE+'.cleanup.json',JSON.stringify(cleanup)+'\n');
  });
  await mkdir(profile, {recursive:true});
  await mkdir(join(home,'intel-profile'),{recursive:true});await mkdir(join(home,'intel-soul'),{recursive:true});
  await writeFile(join(home,'intel-profile','user_profile.md'),'Synthetic profile fact: prefers annotated calculations.\n');
  await writeFile(join(home,'intel-soul','soul.md'),'Synthetic soul rule: retain observed evidence.\n');
  await symlink('/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules', join(profile, 'node_modules'));
  await writeFile(join(profile, 'package.json'), JSON.stringify({name:'memory-growth-fixture',private:true,dsh:{profile:{bundles:['@deepseek-ai/dsh-base']}}}));
  await writeFile(join(profile, 'cordis.yml'), '[]\n');
  await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
    {id:'tools',config:{mode:'native'}},{id:'hmr',disabled:true},{id:'session-title-llm',disabled:true},
    {insert:[{id:'memory',name:new URL('../index.js',import.meta.url).pathname,config:{warnMemoryRows:2,warnMemoryBytes:128}},
      {id:'profile',name:new URL('../../dsh-intelligence-profile/index.js',import.meta.url).pathname},
      {id:'soul',name:new URL('../../dsh-intelligence-soul/index.js',import.meta.url).pathname},
      {id:'probe',name:new URL('./fixtures/growth-probe.js',import.meta.url).pathname}]}
  ]));
  const env = Object.fromEntries(Object.entries(process.env).filter(([k]) => !/KEY|SECRET|TOKEN|PASSWORD/i.test(k) && !k.startsWith('DSH_')));
  child = spawn('/usr/local/bin/dsh', ['--profile','t15'], {env:{...env,DSH_HOME:home,T15_RECEIPT:receipt},stdio:['ignore','pipe','pipe']});
  let log=''; child.stdout.on('data',b=>log+=b); child.stderr.on('data',b=>log+=b); done=once(child,'close');
  let result; const deadline=Date.now()+30000;
  while(Date.now()<deadline) {
    try {result=JSON.parse(await readFile(receipt,'utf8'));break;} catch(error) {if(error.code!=='ENOENT')throw error;}
    if(child.exitCode!==null)throw Error(log); await new Promise(resolve=>setTimeout(resolve,25));
  }
  assert.ok(result,log);
  if(process.env.T15_EVIDENCE)await writeFile(process.env.T15_EVIDENCE,JSON.stringify(result)+'\n');
  console.log('ACTUAL_MEMORY_PROOF '+JSON.stringify({ok:result.ok,message:result.message,loop:result.loop&&{requestCount:result.loop.requestCount,firstInjectionCount:result.loop.firstInjectionCount}}));
  assert.equal(result.ok,true,result.message+log);
});
