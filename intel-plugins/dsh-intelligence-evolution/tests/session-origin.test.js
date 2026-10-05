import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {mkdtempSync,rmSync,existsSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {apply} from '../index.js';

const envKeys=['DSH_HOME','INTEL_TASK_ID','INTEL_RUN_ID','INTEL_RUN_DATE'];
const runner={INTEL_TASK_ID:'evolve_dreaming',INTEL_RUN_ID:'run-origin-test',INTEL_RUN_DATE:'2026-10-05'};
const identity={taskId:'evolve_dreaming',runId:'run-origin-test',runDate:'2026-10-05'};

function fixture(t,environment=runner){
  const previous=new Map(envKeys.map(key=>[key,process.env[key]]));
  const home=mkdtempSync(join(tmpdir(),'intel-session-origin-'));
  for(const key of envKeys)delete process.env[key];
  Object.assign(process.env,{DSH_HOME:home},environment);
  const listeners=new Map(),registrations=[],disposers=[],effects=[];
  const tools=new Map();
  const ctx={
    provide(){},
    tools:{register(tool){tools.set(tool.name,tool);const dispose=()=>tools.delete(tool.name);disposers.push(dispose);return dispose;}},
    on(name,listener,options){
      const handlers=listeners.get(name)??new Set();listeners.set(name,handlers);handlers.add(listener);
      registrations.push({name,options});return ()=>handlers.delete(listener);
    },
    effect(factory){
      const result=factory();
      if(typeof result==='function'){disposers.push(result);return result;}
      effects.push((async()=>{let value;for(;;){const step=result.next(value);if(step.done)return;value=await step.value;}})());
    },
  };
  function dispose(){for(const disposer of disposers.splice(0).reverse())disposer();}
  t.after(()=>{
    dispose();
    for(const [key,value] of previous){if(value===undefined)delete process.env[key];else process.env[key]=value;}
    rmSync(home,{recursive:true,force:true});
  });
  return {home,ctx,registrations,dispose,
    mount:async()=>{await apply(ctx,{});await Promise.all(effects);},
    emit:session=>{for(const listener of listeners.get('session/created')??[])listener(session);}};
}

function session(id,origin){
  return Object.freeze({id,header:Object.freeze({id,version:4,createdAt:1791129600000,cwd:'/root',isSeeded:false,
    ...(origin?{origin,parentSession:'session-root'}:{})})});
}

function rows(home){
  const file=join(home,'intel-joblog','automation-sessions.sqlite');
  if(!existsSync(file))return [];
  const db=new DatabaseSync(file,{readOnly:true});
  try{return db.prepare('SELECT session_id AS sessionId, task_id AS taskId, run_id AS runId, run_date AS runDate FROM automation_sessions ORDER BY session_id').all()
    .map(row=>({...row}));}finally{db.close();}
}

test('runner root creation persists authoritative identity through the actual plugin listener',async t=>{
  const f=fixture(t);await f.mount();
  const root=session('session-root');f.emit(root);
  assert.deepEqual(rows(f.home),[{sessionId:root.id,...identity}]);
  assert.deepEqual(f.registrations.find(item=>item.name==='session/created')?.options,{global:true});
  assert.equal(root.header.origin,undefined,'classification must not mutate released header');
});

test('ordinary sessions with shared root cwd create no automatic index',async t=>{
  const f=fixture(t,{});await f.mount();f.emit(session('ordinary'));
  assert.deepEqual(rows(f.home),[]);
  assert.equal(existsSync(join(f.home,'intel-joblog','automation-sessions.sqlite')),false);
});

test('runner child subagent does not acquire its own root identity',async t=>{
  const f=fixture(t);await f.mount();f.emit(session('session-child','subagent'));f.emit(session('session-root'));
  assert.deepEqual(rows(f.home),[{sessionId:'session-root',...identity}]);
});

test('runner identity is captured at plugin mounting rather than read from later environment changes',async t=>{
  const f=fixture(t);await f.mount();process.env.INTEL_RUN_ID='later-unrelated-run';
  f.emit(session('session-root'));
  assert.deepEqual(rows(f.home),[{sessionId:'session-root',...identity}]);
});

test('partial or malformed runner environment rejects mounting before indexing sessions',async t=>{
  const f=fixture(t,{});
  const invalid=[{INTEL_TASK_ID:'evolve_dreaming'},{...runner,INTEL_TASK_ID:'../../unsafe'},
    {...runner,INTEL_RUN_ID:''},{...runner,INTEL_RUN_DATE:'2026-02-30'},
    {...runner,INTEL_RUN_DATE:'2026-13-01'},{...runner,INTEL_RUN_DATE:'2026-1-5'}];
  for(const environment of invalid){
    for(const key of envKeys.slice(1))delete process.env[key];Object.assign(process.env,environment);
    await assert.rejects(f.mount(),/invalid|incomplete/i);
    assert.equal(existsSync(join(f.home,'intel-joblog','automation-sessions.sqlite')),false);
  }
});

test('repeated event and reopened writer preserve one row while conflicting identity is rejected',async t=>{
  const f=fixture(t);await f.mount();f.emit(session('session-root'));f.emit(session('session-root'));f.dispose();
  const {AutomationSessionStore}=await import('../src/session-origin.js');
  const store=new AutomationSessionStore(join(f.home,'intel-joblog'));
  try{
    store.record('session-root',identity);
    assert.throws(()=>store.record('session-root',{...identity,runId:'conflicting-run'}),/conflict/i);
    store.record('session-next',{...identity,runId:'next-run'});
  }finally{store.close();}
  assert.deepEqual(rows(f.home),[{sessionId:'session-next',...identity,runId:'next-run'},
    {sessionId:'session-root',...identity}]);
});

test('disposed plugin listener cannot index late events',async t=>{
  const f=fixture(t);await f.mount();f.emit(session('session-root'));f.dispose();f.emit(session('late-session'));
  assert.deepEqual(rows(f.home),[{sessionId:'session-root',...identity}]);
});

test('read-only query reopens committed index and never creates a missing index',async t=>{
  const f=fixture(t);await f.mount();f.emit(session('session-root'));f.dispose();
  const {readAutomationSessions}=await import('../src/session-origin.js');
  assert.deepEqual(readAutomationSessions(join(f.home,'intel-joblog')),[{sessionId:'session-root',...identity}]);
  const missing=join(f.home,'missing-index');
  assert.deepEqual(readAutomationSessions(missing),[]);
  assert.equal(existsSync(missing),false);
});
