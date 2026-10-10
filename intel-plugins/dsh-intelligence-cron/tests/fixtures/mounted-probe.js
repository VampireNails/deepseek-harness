import assert from 'node:assert/strict';
import {writeFileSync,readFileSync,mkdirSync,rmSync} from 'node:fs';
import {join} from 'node:path';
export const name='persistence-mounted-probe';
export const inject=['tools','intelGoals','sessions'];
export async function apply(ctx){
 try{
  const session=ctx.sessions.create(),agent={session};
  const call=(name,args)=>ctx.tools.execute({callId:name,name,arguments:args,agent,signal:new AbortController().signal});
  const texts={};let result=await call('cron_list',{});texts.empty=result.content[0].text;assert.match(texts.empty,/已用 0\/20/);
  result=await call('cron_create',{name:'reminder',prompt:'p',schedule:'每5分钟'});texts.created=result.content[0].text;assert.match(texts.created,/已用 1\/20/);
  assert.equal(session.ownEvents().filter(e=>e.type==='schedule/change').length,1);assert.equal(await ctx.sessions.flush(session),true);
  await call('cron_delete',{ref:'c1'});result=await call('cron_create',{name:'new',prompt:'p',schedule:'每5分钟'});assert.match(result.content[0].text,/c2/);
  const cronFile=join(process.env.DSH_HOME,'intel-cron','cron.json');writeFileSync(cronFile,'[broken');result=await call('cron_create',{name:'denied',prompt:'p',schedule:'每5分钟'});texts.corrupt=result.content[0].text;assert.match(texts.corrupt,/CRON_INVALID_DATA/);assert.equal(readFileSync(cronFile,'utf8'),'[broken');
  const md=ctx.intelGoals.mdFile;mkdirSync(md);
  result=await call('goal_create',{title:'persisted despite md'});texts.partial=result.content[0].text;assert.match(texts.partial,/GOALS_PROJECTION_FAILED/);assert.match(texts.partial,/JSON 已提交/);assert.equal(ctx.intelGoals.load().goals.length,1);
  result=await call('goal_list',{});texts.confirmed=result.content[0].text;assert.match(texts.confirmed,/persisted despite md/);assert.match(texts.confirmed,/Markdown 未同步/);assert.equal(ctx.intelGoals.load().seq,1);
  result=await call('goal_progress',{ref:'g1',text:'exactly once'});assert.match(result.content[0].text,/JSON 已提交/);assert.equal(ctx.intelGoals.load().goals[0].progress.length,1);
  rmSync(md,{recursive:true});result=await call('goal_list',{});texts.repaired=result.content[0].text;assert.match(texts.repaired,/exactly once/);assert.doesNotMatch(texts.repaired,/Markdown 未同步/);assert.match(readFileSync(md,'utf8'),/exactly once/);
  ctx.intelGoals.logProgress('g1','automatic service path');assert.equal(ctx.intelGoals.load().goals[0].progress.length,2);
  result=await call('goal_close',{ref:'g1'});assert.equal(ctx.intelGoals.load().goals[0].status,'closed');
  const summary={capacity:texts.empty,created:texts.created.includes('已用 1/20'),deletedIdReused:false,corruptCode:'CRON_INVALID_DATA',partialCode:'GOALS_PROJECTION_FAILED',committedJSONReadable:true,markdownRepair:true,serviceProgressCount:2};
  assert.deepEqual(summary,JSON.parse(readFileSync(new URL('../expected/persistence.json',import.meta.url),'utf8')));
  writeFileSync(process.env.T08_RECEIPT,JSON.stringify({ok:true,summary,texts})+'\n');
 }catch(error){writeFileSync(process.env.T08_RECEIPT,JSON.stringify({ok:false,message:error.message,stack:error.stack})+'\n');}
}
