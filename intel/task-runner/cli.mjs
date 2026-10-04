import {readFile,mkdtemp,writeFile,appendFile,mkdir,rm} from 'node:fs/promises';
import {dirname,join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {tmpdir,homedir} from 'node:os';
import {spawn} from 'node:child_process';
import {runTask,migrateCrontab} from './runner.mjs';
import {JobLog} from '../../intel-plugins/dsh-intelligence-joblog/src/joblog.js';

const here=dirname(fileURLToPath(import.meta.url));
async function main(){
  if(process.platform!=='linux')throw Error('task runner requires Linux');
  const [id,...rest]=process.argv.slice(2);
  if(id==='--migrate-crontab'){
    const chunks=[];for await(const part of process.stdin)chunks.push(part);
    const preview=migrateCrontab(Buffer.concat(chunks).toString('utf8'),join(here,'run.sh'));
    process.stdout.write(preview.text);process.stderr.write(`Migratable built-in jobs: ${preview.changed}\n`);return;
  }
  if(typeof id!=='string'||!/^[a-z][a-z0-9_-]{0,79}$/.test(id))throw Error('invalid task id');
  const root=join(process.env.DSH_HOME??join(homedir(),'.dsh'),'intel-joblog');
  await mkdir(root,{recursive:true});
  const log=new JobLog(root),dir=await mkdtemp(join(tmpdir(),'intel-task-'));
  try{
    const config=JSON.parse(await readFile(process.env.INTEL_ROUTER_CONFIG??join(here,'routes.json'),'utf8'));
    const timeoutMs=config.timeoutMs;
    if(!Number.isSafeInteger(timeoutMs)||timeoutMs<1000||timeoutMs>600000)throw Error('invalid timeoutMs');
    const exit=await runTask(config,id,rest.join(' '),async plan=>{
      const patch=join(dir,'model.json');
      await writeFile(patch,JSON.stringify([{id:'agent-default-model',config:{provider:plan.provider,model:plan.model}}]));
      return await new Promise((resolve,reject)=>{
        const child=spawn('dsh',['--profile','evolve','--patch',patch],{stdio:['pipe','inherit','inherit'],detached:true});
        let timedOut=false,inputError;
        const timer=setTimeout(()=>{timedOut=true;try{process.kill(-child.pid,'SIGTERM');}catch(error){if(error.code!=='ESRCH')reject(error);}},timeoutMs);
        let killTimer;
        child.once('spawn',()=>{killTimer=setTimeout(()=>{if(timedOut){try{process.kill(-child.pid,'SIGKILL');}catch(error){if(error.code!=='ESRCH')reject(error);}}},timeoutMs+5000);});
        child.stdin.on('error',error=>{
          if(error.code==='EPIPE')return;
          inputError=error;
          try{process.kill(-child.pid,'SIGKILL');}catch(killError){if(killError.code!=='ESRCH')reject(killError);}
        });
        child.once('error',error=>{clearTimeout(timer);clearTimeout(killTimer);reject(error);});
        child.once('close',(code,signal)=>{
          clearTimeout(timer);clearTimeout(killTimer);
          if(timedOut){try{process.kill(-child.pid,'SIGKILL');}catch(error){if(error.code!=='ESRCH'){reject(error);return;}}}
          if(inputError){reject(inputError);return;}
          resolve(timedOut?124:code??(signal?1:0));
        });
        child.stdin.end(plan.prompt+'\n');
      });
    },async entry=>{
      const record={time:new Date().toISOString(),...entry};
      await appendFile(join(root,'route-runs.jsonl'),JSON.stringify(record)+'\n');
      // provider/model identify the requested patch; actual request identity comes from tokenlog.
      process.stderr.write(JSON.stringify(record)+'\n');
    });
    log._recordRun(id,exit===0?'ok':'fail',`exit ${exit}; route-runs.jsonl`);
    if(exit!==0)log._alert(id,`task exit ${exit}; inspect route-runs.jsonl`);
    process.exitCode=exit;
  }catch(error){
    const code=typeof error.code==='string'&&/^[A-Z0-9_]+$/.test(error.code)?error.code:'TASK_RUNNER_FAILED';
    await appendFile(join(root,'route-runs.jsonl'),JSON.stringify({time:new Date().toISOString(),kind:'error',taskId:id,code})+'\n');
    log._recordRun(id,'fail',code);log._alert(id,code);
    throw error;
  }finally{await rm(dir,{recursive:true,force:true});}
}
main().catch(error=>{process.stderr.write('Task runner failed: '+error.message+'\n');process.exitCode=1;});
