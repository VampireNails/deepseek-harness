import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,lstat,symlink,rm,readdir} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createUpkeepStateStore} from '../upkeep-state.mjs';

async function fixture(t){const directory=await mkdtemp(join(tmpdir(),'upkeep-state-'));t.after(()=>rm(directory,{recursive:true,force:true}));return {directory,file:join(directory,'private','state.json')};}
const state={version:1,cursors:{anonymous:2},backoffMs:0,nextDueAtMs:1000,lastNowMs:1000};
test('missing state reads without creating files and atomic writes retain private state permissions',async t=>{
  const f=await fixture(t),store=createUpkeepStateStore(f.file);
  assert.equal(await store.read(),null);assert.deepEqual(await readdir(f.directory),[]);
  await store.write(state);assert.deepEqual(await store.read(),state);
  assert.equal((await lstat(f.file)).mode&0o777,0o600);assert.equal((await lstat(join(f.directory,'private'))).mode&0o777,0o700);
  await store.write({...state,cursors:{anonymous:3}});assert.equal((await store.read()).cursors.anonymous,3);
  assert.deepEqual(await readdir(join(f.directory,'private')),['state.json']);
});
test('invalid private JSON does not become an empty bootstrap checkpoint',async t=>{
  const f=await fixture(t);await mkdir(join(f.directory,'private'),{mode:0o700});await writeFile(f.file,'{"private-marker":',{mode:0o600});
  await assert.rejects(()=>createUpkeepStateStore(f.file).read(),error=>{assert.equal(error.code,'UPKEEP_STATE_INVALID');assert.equal(error.message.includes('private-marker'),false);return true;});
  assert.equal(await readFile(f.file,'utf8'),'{"private-marker":');
});
test('state store refuses a link instead of reading or overwriting its private target',async t=>{
  const f=await fixture(t),target=join(f.directory,'original.json');await writeFile(target,JSON.stringify(state),{mode:0o600});
  await mkdir(join(f.directory,'private'),{mode:0o700});await symlink(target,f.file);
  const store=createUpkeepStateStore(f.file);await assert.rejects(()=>store.read());await assert.rejects(()=>store.write(state));
  assert.equal(await readFile(target,'utf8'),JSON.stringify(state));assert.equal((await lstat(f.file)).isSymbolicLink(),true);
});
