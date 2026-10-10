import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,symlink,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
test('real Loader executor distinguishes intel tool failures and partial JSON from success',async t=>{
 const root=await mkdtemp(join(tmpdir(),'intel-errors-profile-')),home=join(root,'home'),profile=join(home,'profiles','t09'),receipt=join(root,'receipt');
 let child,done;
 t.after(async()=>{if(child){if(child.exitCode===null)child.kill();await done;}await rm(root,{recursive:true,force:true});});await mkdir(profile,{recursive:true});
 await symlink('/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules',join(profile,'node_modules'));
 await writeFile(join(profile,'package.json'),JSON.stringify({name:'intel-errors-mounted-test',private:true,dsh:{profile:{bundles:['@deepseek-ai/dsh-base']}}}));await writeFile(join(profile,'cordis.yml'),'[]\n');
 const plugin=name=>new URL('../../'+name+'/index.js',import.meta.url).pathname;
 await writeFile(join(profile,'cordis.patch.yml'),JSON.stringify([{id:'tools',config:{mode:'native'}},{id:'hmr',disabled:true},{id:'session-title-llm',disabled:true},{insert:[{id:'cron',name:plugin('dsh-intelligence-cron')},{id:'goals',name:plugin('dsh-intelligence-goals')},{id:'memory',name:plugin('dsh-intelligence-memory')},{id:'events',name:plugin('dsh-intelligence-sysevents')},{id:'probe',name:new URL('./fixtures/tool-errors-probe.js',import.meta.url).pathname}]}]));
 const env=Object.fromEntries(Object.entries(process.env).filter(([k])=>!/KEY|SECRET|TOKEN|PASSWORD/i.test(k)&&!k.startsWith('DSH_')));
 child=spawn('/usr/local/bin/dsh',['--profile','t09'],{env:{...env,DSH_HOME:home,T09_RECEIPT:receipt},stdio:['ignore','pipe','pipe']});let log='';child.stdout.on('data',b=>log+=b);child.stderr.on('data',b=>log+=b);done=once(child,'close');
 let result;const deadline=Date.now()+20000;while(Date.now()<deadline){try{result=JSON.parse(await readFile(receipt,'utf8'));break;}catch(error){if(error.code!=='ENOENT')throw error;}if(child.exitCode!==null)throw Error(log);await new Promise(r=>setTimeout(r,25));}
 assert.ok(result,log);console.log('ACTUAL_TOOL_RESULTS '+JSON.stringify(result));assert.equal(result.ok,true,JSON.stringify(result.failures)+result.message+log);
});
