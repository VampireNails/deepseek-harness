import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,writeFileSync,mkdirSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {apply} from '../index.js';
import {CronStore} from '../src/store.js';
import {listEvents} from '../../dsh-intelligence-sysevents/src/store.js';

test('mounted cron dispatch failures are structured, idempotent, safe and recoverable',async t=>{
  const home=mkdtempSync(join(tmpdir(),'mounted-cron-failure-')),oldHome=process.env.DSH_HOME,oldScope=process.env.INTEL_JOBLOG_SCOPE;
  process.env.DSH_HOME=home;delete process.env.INTEL_JOBLOG_SCOPE;
  t.after(()=>{if(oldHome===undefined)delete process.env.DSH_HOME;else process.env.DSH_HOME=oldHome;if(oldScope===undefined)delete process.env.INTEL_JOBLOG_SCOPE;else process.env.INTEL_JOBLOG_SCOPE=oldScope;rmSync(home,{recursive:true,force:true});});
  const handlers=new Map(),warnings=[],events=[],store=new CronStore(join(home,'intel-cron'));
  const session={id:'session-1',ownEvents:()=>events.slice(),append:(type,data)=>events.push({type,data})},agent={session};
  let broken=true;
  const ctx={on:(type,fn)=>{handlers.set(type,fn);return ()=>handlers.delete(type);},effect:fn=>{const out=fn();if(out?.next)for(const x of out){}},
    tools:{register:()=>()=>{}},agents:{get:id=>id===session.id?agent:undefined},
    sessions:{flush:async()=>{if(broken)throw Object.assign(Error('PRIVATE_PROMPT token=SECRET /private'),{code:'EACCES'});}},logger:{warn:msg=>warnings.push(msg)}};
  apply(ctx,{dataDir:join(home,'intel-cron'),timeZone:'Asia/Shanghai'});
  const original={id:'c1',name:'PRIVATE_NAME',prompt:'PRIVATE_PROMPT',schedule:{kind:'daily',hour:9,minute:0},sessionId:session.id,scheduleId:'cron-c1-20000101',nextFire:'2000-01-01T01:00:00.000Z'};
  store.save([original]);const dispatch={type:'schedule/change',data:{version:1,operation:'dispatch',id:original.scheduleId}};
  // Mounted listener may be fire-and-forget; wait until its per-agent asynchronous queue settles.
  handlers.get('session/event')(session,dispatch);await new Promise(r=>setTimeout(r,30));
  let rows=listEvents({},join(home,'intel-system-events'));assert.equal(rows.length,1);assert.equal(rows[0].type,'cron.rearm_failed');
  assert.equal(rows[0].taskId,'c1');assert.equal(rows[0].sessionId,session.id);assert.equal(rows[0].scheduleId,original.scheduleId);assert.equal(rows[0].code,'EACCES');
  assert.doesNotMatch(JSON.stringify(rows)+warnings.join(),/PRIVATE_NAME|PRIVATE_PROMPT|SECRET|\/private/);
  // A replay after a failed flush must flush the already appended schedule before saving metadata.
  handlers.get('session/event')(session,dispatch);await new Promise(r=>setTimeout(r,30));assert.equal(listEvents({},join(home,'intel-system-events')).length,1);
  assert.equal(store.load()[0].scheduleId,original.scheduleId);
  broken=false;handlers.get('session/event')(session,dispatch);await new Promise(r=>setTimeout(r,30));assert.notEqual(store.load()[0].scheduleId,original.scheduleId);
  assert.equal(events.filter(e=>e.data.operation==='create').length,1);
  // Other sessions and interval dispatches are irrelevant, and completed dispatches stay quiet.
  handlers.get('session/event')(session,{...dispatch,data:{...dispatch.data,acceptedAt:new Date().toISOString()}});
  handlers.get('session/event')(session,dispatch);await new Promise(r=>setTimeout(r,30));assert.equal(listEvents({},join(home,'intel-system-events')).length,1);
  process.env.INTEL_JOBLOG_SCOPE='test';store.save([{...original,id:'c2',scheduleId:'cron-c2-20000101'}]);broken=true;
  handlers.get('session/event')(session,{...dispatch,data:{...dispatch.data,id:'cron-c2-20000101'}});await new Promise(r=>setTimeout(r,30));
  assert.equal(listEvents({},join(home,'intel-system-events')).length,1);assert.equal(listEvents({includeTests:true},join(home,'intel-system-events')).length,2);
  // Source corruption remains an observable warning alongside the original rearm failure.
  writeFileSync(join(home,'intel-system-events','events.test.jsonl'),'{broken\n');events.length=0;
  handlers.get('session/event')(session,{...dispatch,data:{...dispatch.data,id:'cron-c2-20000101'}});await new Promise(r=>setTimeout(r,30));
  assert.ok(warnings.some(s=>s.includes('SYSEVENTS_INVALID_DATA')&&s.includes('EACCES')));
});
