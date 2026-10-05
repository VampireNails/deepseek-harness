import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,stat,writeFile,readFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createConnection} from 'node:net';
import {startUpkeepServer,requestUpkeep} from '../src/upkeep-socket.js';

async function fixture(t){
  const directory=await mkdtemp(join(tmpdir(),'upkeep-ipc-'));
  const socketPath=join(directory,'source','query.sock');
  const calls=[];
  const source={async capture(request){calls.push(request);return {cursors:{anonymous:2},messages:[],counts:{newMessages:0}};}};
  t.after(()=>rm(directory,{recursive:true,force:true}));
  return {directory,socketPath,calls,source};
}
const capture={version:1,operation:'capture',request:{bootstrap:true,cursors:{},maxBatchMessages:3,maxBatchChars:1000}};

test('Unix source socket preserves exact cuts with private permissions and releases its own path',async t=>{
  const f=await fixture(t);
  const server=await startUpkeepServer({...f,timeoutMs:1000,inspect:async()=>({complete:true,failedWrites:0,writeIds:[17]})});
  t.after(()=>server.close());
  assert.equal((await stat(join(f.directory,'source'))).mode&0o777,0o700);
  assert.equal((await stat(f.socketPath)).mode&0o777,0o600);
  assert.deepEqual(await requestUpkeep(f.socketPath,capture,1000),{cursors:{anonymous:2},messages:[],counts:{newMessages:0}});
  assert.deepEqual(f.calls,[capture.request]);
  assert.deepEqual(await requestUpkeep(f.socketPath,{version:1,operation:'inspect',identity:{taskId:'evolve_upkeep',runId:'fixture-run'}},1000),
    {complete:true,failedWrites:0,writeIds:[17]});
  await server.close();await assert.rejects(()=>stat(f.socketPath),{code:'ENOENT'});
});

test('IPC rejects unknown requests and exposes stable errors without source text or paths',async t=>{
  const f=await fixture(t);
  f.source.capture=async()=>{throw Object.assign(Error('private original source marker'),{code:'SQLITE_ERROR'});};
  const server=await startUpkeepServer({...f,timeoutMs:1000,inspect:async()=>({})});t.after(()=>server.close());
  await assert.rejects(()=>requestUpkeep(f.socketPath,{...capture,operation:'write'},1000),{code:'UPKEEP_INVALID_REQUEST'});
  await assert.rejects(()=>requestUpkeep(f.socketPath,capture,1000),error=>{
    assert.equal(error.code,'UPKEEP_SOURCE_FAILED');assert.equal(error.message,'UPKEEP_SOURCE_FAILED');
    assert.equal(JSON.stringify(error).includes('private'),false);assert.equal(error.stack.includes(f.directory),false);return true;
  });
});

test('startup never replaces a regular file or an active socket',async t=>{
  const f=await fixture(t);await mkdir(join(f.directory,'source'),{mode:0o700});
  await writeFile(f.socketPath,'private original file',{mode:0o600});
  await assert.rejects(()=>startUpkeepServer({...f,timeoutMs:1000,inspect:async()=>({})}));
  assert.equal(await readFile(f.socketPath,'utf8'),'private original file');
  await rm(f.socketPath);
  const server=await startUpkeepServer({...f,timeoutMs:1000,inspect:async()=>({})});t.after(()=>server.close());
  await assert.rejects(()=>startUpkeepServer({...f,timeoutMs:1000,inspect:async()=>({})}),{code:'UPKEEP_SOCKET_ACTIVE'});
  assert.deepEqual(await requestUpkeep(f.socketPath,capture,1000),{cursors:{anonymous:2},messages:[],counts:{newMessages:0}});
});

test('closing the server aborts an owned unfinished read before teardown resolves',async t=>{
  const f=await fixture(t);let began,aborted=false;
  const started=new Promise(resolve=>{began=resolve;});
  f.source.capture=(request,{signal})=>new Promise((resolve,reject)=>{
    began();signal.addEventListener('abort',()=>{aborted=true;reject(Error('owned read aborted'));},{once:true});
  });
  const server=await startUpkeepServer({...f,timeoutMs:1000,inspect:async()=>({})});t.after(()=>server.close());
  const reading=requestUpkeep(f.socketPath,capture,1000);const rejected=assert.rejects(reading);
  await started;await server.close();await rejected;assert.equal(aborted,true);
});

test('a stalled connection reaches a bounded transport deadline without invoking a model',async t=>{
  const f=await fixture(t);const server=await startUpkeepServer({...f,timeoutMs:30,inspect:async()=>({})});t.after(()=>server.close());
  await new Promise((resolve,reject)=>{const socket=createConnection(f.socketPath);socket.on('error',reject);socket.on('close',resolve);socket.on('connect',()=>socket.write('{'));});
  assert.equal(f.calls.length,0);
});
