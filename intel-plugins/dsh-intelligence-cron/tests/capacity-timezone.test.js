import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {CronStore} from '../src/store.js';
import {CronScheduler} from '../src/scheduler.js';
import {nextOccurrence,formatLocal} from '../src/timecalc.js';
import {Config,apply} from '../index.js';

function fixture(t){const dir=mkdtempSync(join(tmpdir(),'cron-capacity-'));t.after(()=>rmSync(dir,{recursive:true,force:true}));const session={id:'s',events:[],ownEvents(){return this.events.slice()},append(type,data){this.events.push({type,data})}};return {store:new CronStore(dir),agent:{session},ctx:{sessions:{flush:async()=>{}}}};}
test('empty create list and delete show used capacity and the actual fixed limit',async t=>{
 const {store,agent,ctx}=fixture(t),sch=new CronScheduler({store,ctx});assert.match(await sch.list(agent),/已用 0\/20/);
 assert.match(await sch.create(agent,'a','p','每5分钟'),/已用 1\/20/);assert.match(await sch.list(agent),/已用 1\/20/);assert.match(await sch.remove(agent,'c1'),/已用 0\/20/);
});
test('at and above capacity deny only additions while delete and rearm remain available',async t=>{
 const {store,agent,ctx}=fixture(t),sch=new CronScheduler({store,ctx});ctx.agents={get:()=>agent};
 for(let n=0;n<20;n++)await sch.create(agent,'job'+n,'p',n===0?'每天9点':'每5分钟');
 await assert.rejects(sch.create(agent,'denied','p','每5分钟'),/已用 20\/20/);
 const jobs=store.load(),original=jobs[0].scheduleId;jobs[0].scheduleId='cron-c1-20000101';store.save(jobs);
 await sch.handleSessionEvent(agent.session,{type:'schedule/change',data:{version:1,operation:'dispatch',id:'cron-c1-20000101'}});assert.equal(store.load().length,20);assert.equal(store.load()[0].scheduleId,original);
 const last={...store.load()[19],id:'c21',scheduleId:'cron-c21'};store.save([...store.load(),last]);assert.match(await sch.list(agent),/已用 21\/20/);
 await assert.rejects(sch.create(agent,'denied','p','每5分钟'),/已用 21\/20/);assert.match(await sch.remove(agent,'c21'),/已用 20\/20/);assert.match(await sch.remove(agent,'c20'),/已用 19\/20/);assert.match(await sch.create(agent,'allowed','p','每5分钟'),/已用 20\/20/);
});
test('configuration has dataDir and timeZone only so a limit property cannot override 20',()=>{
 assert.deepEqual(Object.keys(Config.dict).sort(),['dataDir','timeZone']);
});
test('omitted overridden and invalid unknown limit properties keep existing schema and effective capacity',async t=>{
 const {store,agent,ctx}=fixture(t);const tools=new Map();ctx.on=()=>()=>{};ctx.tools={register:tool=>{tools.set(tool.name,tool);return ()=>{};}};ctx.effect=fn=>{const value=fn();if(value?.next)for(const disposer of value){}};
 for(const input of [{},{limit:1},{limit:-1},{limit:'invalid'}]){const cfg=Config({...input,dataDir:join(store.file,'..')});apply(ctx,cfg);const result=await tools.get('cron_create').execute({name:'n',prompt:'p',schedule:'每5分钟'},{agent});assert.match(result.text,/已用 \d+\/20/);assert.equal(result.ok,true);}
 assert.equal(store.load().length,4);
});
test('different configured zones schedule daily reminders across UTC dates',async t=>{
 const {store,agent,ctx}=fixture(t);const old=Date.now;Date.now=()=>Date.parse('2026-10-10T02:00:00Z');t.after(()=>Date.now=old);
 for(const [zone,expected] of [['Asia/Shanghai','2026-10-11T01:00:00.000Z'],['UTC','2026-10-10T09:00:00.000Z'],['America/New_York','2026-10-10T13:00:00.000Z']]){
  await new CronScheduler({store,ctx,timeZone:zone}).create(agent,zone,'p','每天9点');assert.equal(store.load().at(-1).nextFire,expected);
 }
});
test('daily and weekly valid wall times follow spring and autumn DST offsets',()=>{
 const zone='America/New_York';
 const before=nextOccurrence({hour:9,minute:0},zone,Date.parse('2026-03-07T00:00:00Z'));
 const after=nextOccurrence({hour:9,minute:0},zone,before);assert.equal(after-before,23*3600000);assert.equal(formatLocal(zone,after),'2026-03-08 09:00');
 const autumn=nextOccurrence({hour:9,minute:0},zone,Date.parse('2026-10-31T00:00:00Z'));
 assert.equal(nextOccurrence({hour:9,minute:0},zone,autumn)-autumn,25*3600000);
 assert.equal(new Date(nextOccurrence({hour:9,minute:0,dayOfWeek:7},zone,Date.parse('2026-03-07T00:00:00Z'))).toISOString(),'2026-03-08T13:00:00.000Z');
});
test('invalid nonempty zone fails at scheduler construction; omitted and empty retain Shanghai',t=>{
 const {store,ctx}=fixture(t);assert.throws(()=>new CronScheduler({store,ctx,timeZone:'Invalid/Zone'}),RangeError);
 assert.equal(new CronScheduler({store,ctx}).timeZone,'Asia/Shanghai');assert.equal(new CronScheduler({store,ctx,timeZone:''}).timeZone,'Asia/Shanghai');
});
