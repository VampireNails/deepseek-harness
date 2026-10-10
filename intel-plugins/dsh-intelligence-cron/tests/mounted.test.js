import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,symlink,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
test('supported isolated DSH profile mounts cron and goals and confirms partial commits through real tools',async t=>{
 const root=await mkdtemp(join(tmpdir(),'cron-goals-profile-'));t.after(()=>rm(root,{recursive:true,force:true}));const home=join(root,'home'),profile=join(home,'profiles','t08'),receipt=join(root,'receipt');await mkdir(profile,{recursive:true});
 await symlink('/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules',join(profile,'node_modules'));
 await writeFile(join(profile,'package.json'),JSON.stringify({name:'persistence-mounted-test',private:true,dsh:{profile:{bundles:['@deepseek-ai/dsh-base']}}}));await writeFile(join(profile,'cordis.yml'),'[]\n');
 await writeFile(join(profile,'cordis.patch.yml'),JSON.stringify([{id:'tools',config:{mode:'native'}},{id:'hmr',disabled:true},{insert:[{id:'cron',name:new URL('../index.js',import.meta.url).pathname},{id:'goals',name:new URL('../../dsh-intelligence-goals/index.js',import.meta.url).pathname},{id:'probe',name:new URL('./fixtures/mounted-probe.js',import.meta.url).pathname}]}]));
 const env=Object.fromEntries(Object.entries(process.env).filter(([k])=>!/KEY|SECRET|TOKEN|PASSWORD/i.test(k)&&!k.startsWith('DSH_')));
 const child=spawn('/usr/local/bin/dsh',['--profile','t08'],{env:{...env,DSH_HOME:home,T08_RECEIPT:receipt},stdio:['ignore','pipe','pipe']});let log='';child.stdout.on('data',b=>log+=b);child.stderr.on('data',b=>log+=b);const done=once(child,'close');t.after(async()=>{if(child.exitCode===null)child.kill();await done;});
 let result;const deadline=Date.now()+20000;while(Date.now()<deadline){try{result=JSON.parse(await readFile(receipt,'utf8'));break;}catch(error){if(error.code!=='ENOENT')throw error;}if(child.exitCode!==null)throw Error(log);await new Promise(r=>setTimeout(r,25));}
 assert.ok(result,log);assert.equal(result.ok,true,result.message+log);console.log('REAL_PROFILE '+JSON.stringify(result.summary));console.log('MODEL_OUTPUT '+JSON.stringify(result.texts));
});
