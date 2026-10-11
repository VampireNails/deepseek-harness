import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,symlink,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {spawn} from 'node:child_process';
import {once} from 'node:events';

test('mounted artifact reference reaches the paired bridge with pinned content and notification semantics',
  {skip:process.platform!=='linux'||!process.env.T17_HMC_ROOT?'requires Linux and the joint HMC service source':false},async t=>{
  const root=await mkdtemp(join(tmpdir(),'artifact-reference-profile-'));
  const home=join(root,'home'),profile=join(home,'profiles','t17'),receipt=join(root,'receipt');
  let child,done;
  t.after(async()=>{
    let settled;
    if(child){if(child.exitCode===null)child.kill();settled=await done;}
    await rm(root,{recursive:true,force:true});
    if(process.env.T17_EVIDENCE)await writeFile(process.env.T17_EVIDENCE+'.cleanup.json',JSON.stringify({pid:child?.pid,root,exitCode:settled?.[0],signal:settled?.[1],removed:true})+'\n');
  });
  await mkdir(profile,{recursive:true});
  await symlink('/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules',join(profile,'node_modules'));
  await writeFile(join(profile,'package.json'),JSON.stringify({name:'artifact-reference-fixture',private:true,dsh:{profile:{bundles:['@deepseek-ai/dsh-base']}}}));
  await writeFile(join(profile,'cordis.yml'),'[]\n');
  await writeFile(join(profile,'cordis.patch.yml'),JSON.stringify([
    {id:'tools',config:{mode:'native'}},{id:'hmr',disabled:true},{id:'session-title-llm',disabled:true},
    {insert:[{id:'artifacts',name:new URL('../index.js',import.meta.url).pathname},
      {id:'events',name:new URL('../../dsh-intelligence-sysevents/index.js',import.meta.url).pathname},
      {id:'probe',name:new URL('./fixtures/reference-probe.js',import.meta.url).pathname}]}
  ]));
  const env=Object.fromEntries(Object.entries(process.env).filter(([k])=>!/KEY|SECRET|TOKEN|PASSWORD/i.test(k)&&!k.startsWith('DSH_')));
  child=spawn('/usr/local/bin/dsh',['--profile','t17'],{env:{...env,DSH_HOME:home,T17_RECEIPT:receipt},stdio:['ignore','pipe','pipe']});
  done=once(child,'close');let log='';child.stdout.on('data',b=>log+=b);child.stderr.on('data',b=>log+=b);
  let proof;const deadline=Date.now()+30000;
  while(Date.now()<deadline){
    try{proof=JSON.parse(await readFile(receipt,'utf8'));break;}catch(e){if(e.code!=='ENOENT')throw e;}
    if(child.exitCode!==null)throw Error(log);await new Promise(resolve=>setTimeout(resolve,25));
  }
  assert.ok(proof,log);
  if(process.env.T17_EVIDENCE)await writeFile(process.env.T17_EVIDENCE,JSON.stringify(proof,null,2)+'\n');
  assert.equal(proof.ok,true,proof.message+log);
  child.kill();const [code,signal]=await done;
  assert.equal(code,0);assert.equal(signal,null,'graceful dsh shutdown');
});
