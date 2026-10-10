import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,writeFileSync,readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {CronStore} from '../src/store.js';
import {CronScheduler,foldScheduleEvents} from '../src/scheduler.js';
import {GoalsStore} from '../../dsh-intelligence-goals/src/goals.js';

function fixture(t){const dir=mkdtempSync(join(tmpdir(),'cron-persistence-'));t.after(()=>rmSync(dir,{recursive:true,force:true}));return dir;}
function session(id){const events=[];return {id,ownEvents:()=>events.slice(),append:(type,data)=>events.push({type,data})};}
function scheduler(dir,flush=async()=>{}){return new CronScheduler({store:new CronStore(dir),timeZone:'Asia/Shanghai',ctx:{sessions:{flush}}});}

test('cron corrupt JSON rejects create without replacing original bytes',async t=>{
 const dir=fixture(t),store=new CronStore(dir);writeFileSync(store.file,'[{"id":');
 await assert.rejects(scheduler(dir).create({session:session('a')},'a','p','每5分钟'),{code:'CRON_INVALID_DATA'});
 assert.equal(readFileSync(store.file,'utf8'),'[{"id":');
});
test('goals corrupt JSON rejects progress and create without replacing original bytes',t=>{
 const store=new GoalsStore(fixture(t));writeFileSync(store.file,'{"goals":');
 assert.throws(()=>store.create('a'),{code:'GOALS_INVALID_DATA'});assert.throws(()=>store.logProgress('g1','p'),{code:'GOALS_INVALID_DATA'});
 assert.equal(readFileSync(store.file,'utf8'),'{"goals":');
});
test('delete highest and all jobs never reuse seen schedule identity across store reload',async t=>{
 const dir=fixture(t),agent={session:session('a')},sch=scheduler(dir);
 await sch.create(agent,'a','p','每5分钟');await sch.remove(agent,'c1');
 await scheduler(dir).create(agent,'b','p','每5分钟');
 assert.equal(new CronStore(dir).load()[0].id,'c2');assert.equal(foldScheduleEvents(agent.session.ownEvents()).active.size,1);
});
test('cross-agent reconcile awaiting flush cannot overwrite a concurrent create',async t=>{
 const dir=fixture(t),a={session:session('a')},b={session:session('b')};let entered,release;
 const reached=new Promise(r=>entered=r),barrier=new Promise(r=>release=r);let block=false;
 const sch=scheduler(dir,async()=>{if(block){entered();await barrier;}});
 await sch.create(a,'a','p','每天9点');a.session.ownEvents=()=>[];block=true;
 const reconcile=sch.reconcile(a);await reached;const create=sch.create(b,'b','p','每5分钟');release();await Promise.all([reconcile,create]);
 assert.equal(new CronStore(dir).load().length,2);
});
test('cross-agent create during rearm flush retains both committed jobs',async t=>{
 const dir=fixture(t),a={session:session('a')},b={session:session('b')};let entered,release;
 const reached=new Promise(r=>entered=r),barrier=new Promise(r=>release=r);
 const sch=scheduler(dir,async s=>{if(s.id==='a'){entered();await barrier;}});sch.ctx.agents={get:()=>a};sch.warn=()=>{};
 const store=new CronStore(dir);store.save([{id:'c1',name:'a',prompt:'p',sessionId:'a',schedule:{kind:'daily',hour:9,minute:0},scheduleId:'cron-c1-20000101',nextFire:'2000-01-01T01:00:00.000Z'}]);
 const rearm=sch.handleSessionEvent(a.session,{type:'schedule/change',data:{version:1,operation:'dispatch',id:'cron-c1-20000101'}});
 await reached;const create=sch.create(b,'b','p','每5分钟');await new Promise(r=>setImmediate(r));release();await Promise.all([rearm,create]);
 assert.equal(store.load().length,2);
});
