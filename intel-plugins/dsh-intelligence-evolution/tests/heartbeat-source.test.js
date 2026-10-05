import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,readFile,writeFile,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {Context} from '@deepseek-ai/cordis';
import SessionStore,{SessionId} from '@deepseek-ai/dsh-session';
import Tools from '@deepseek-ai/dsh-tools';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import Agents from '@deepseek-ai/dsh-agent';
import * as evolution from '../index.js';

test('heartbeat serves the current cron checklist through the native tool runtime',async()=>{
  const keys=['DSH_HOME','INTEL_TASK_ID','INTEL_RUN_ID','INTEL_RUN_DATE'];
  const previous=new Map(keys.map(key=>[key,process.env[key]]));
  const home=await mkdtemp(join(tmpdir(),'heartbeat-source-')),ctx=new Context();
  try{
    for(const key of keys)delete process.env[key];process.env.DSH_HOME=home;
    const checklist=await readFile(new URL('../HEARTBEAT.md',import.meta.url),'utf8');
    await mkdir(join(home,'intel-evolution'));
    await writeFile(join(home,'intel-evolution','HEARTBEAT.md'),checklist);
    await ctx.plugin(SessionStore);await ctx.plugin(SystemPrompt);await ctx.plugin(Tools,{mode:'native'});await ctx.plugin(Agents);
    await ctx.plugin(evolution);
    const session=ctx.sessions.create(SessionId('owned-heartbeat-check'));
    const agent={id:session.id,session};
    const execute=async name=>{
      const result=await ctx.tools.execute({callId:'owned-'+name,name,arguments:{},agent,signal:new AbortController().signal});
      assert.equal(result.isError,false);return result.content.filter(block=>block.type==='text').map(block=>block.text).join('\n');
    };
    const shown=await execute('heartbeat_check');
    const cronLine=shown.split('\n').find(line=>line.includes('cron 任务健康：'));
    assert.equal(cronLine?.replace(/^\d+\. /,''),(await readFile(new URL('./expected/heartbeat-cron-check.txt',import.meta.url),'utf8')).trimEnd());
  }finally{
    try{await ctx.fiber.dispose();}finally{for(const [key,value] of previous){if(value===undefined)delete process.env[key];else process.env[key]=value;}await rm(home,{recursive:true,force:true});}
  }
});
