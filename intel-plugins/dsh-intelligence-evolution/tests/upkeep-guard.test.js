import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {apply} from '../index.js';

async function mount(t,taskId='evolve_upkeep'){
  const keys=['DSH_HOME','INTEL_TASK_ID','INTEL_RUN_ID','INTEL_RUN_DATE'];
  const previous=new Map(keys.map(key=>[key,process.env[key]]));
  const home=mkdtempSync(join(tmpdir(),'upkeep-native-guard-'));
  Object.assign(process.env,{DSH_HOME:home,INTEL_TASK_ID:taskId,INTEL_RUN_ID:'fixture-native-run',INTEL_RUN_DATE:'2026-10-05'});
  const listeners=new Map(),cleanup=[];
  const ctx={provide(){},tools:{register(){return ()=>{};}},
    on(name,handler){listeners.set(name,handler);return ()=>listeners.delete(name);},
    effect(factory){const result=factory();if(typeof result==='function')cleanup.push(result);else if(result?.next){for(const step of result)cleanup.push(step);}},
  };
  t.after(()=>{for(const action of cleanup.reverse())action();for(const [key,value] of previous){if(value===undefined)delete process.env[key];else process.env[key]=value;}rmSync(home,{recursive:true,force:true});});
  await apply(ctx,{upkeepToolBudget:6});return listeners;
}

test('upkeep actually denies non-native and nested tools before their bodies execute',async t=>{
  const listeners=await mount(t);const guard=listeners.get('tools/pre-execute');
  assert.equal(typeof guard,'function','Prompt restrictions must be backed by actual tool admission');
  let bodyCalls=0;const next=()=>{bodyCalls++;return {kind:'allow'};};const agent={};
  for(const name of ['bash','run_code','subagent','memory_read','feed_post']){
    const result=await guard({name,agent,arguments:{}},next);assert.equal(result.kind,'deny');
  }
  assert.equal((await guard({name:'memory_write',agent,parent:{},arguments:{}},next)).kind,'deny');
  assert.equal(bodyCalls,0);
  assert.equal((await guard({name:'memory_search',agent,arguments:{}},next)).kind,'allow');
  assert.equal(bodyCalls,1);
});

test('upkeep enforces the configured tool budget per agent without blocking the next agent',async t=>{
  const guard=(await mount(t)).get('tools/pre-execute');assert.equal(typeof guard,'function');
  const agent={},next=()=>({kind:'allow'});
  for(let i=0;i<6;i++)assert.equal((await guard({name:'memory_search',agent},next)).kind,'allow');
  assert.equal((await guard({name:'memory_write',agent},next)).kind,'deny');
  assert.equal((await guard({name:'memory_search',agent:{}},next)).kind,'allow');
});

test('other evolution tasks retain their existing tool choices',async t=>{
  assert.equal((await mount(t,'evolve_dreaming')).has('tools/pre-execute'),false);
});
