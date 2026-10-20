import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,stat,writeFile,readFile,rm,symlink,chmod} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createConnection} from 'node:net';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
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

test('reader rejects changed socket or parent permissions and a new Host reclaims only an owned stale socket',async t=>{
  const f=await fixture(t);const server=await startUpkeepServer({...f,timeoutMs:1000,inspect:async()=>({})});
  t.after(()=>server.close());
  await chmod(f.socketPath,0o644);await assert.rejects(()=>requestUpkeep(f.socketPath,capture,1000),{code:'UPKEEP_SOCKET_UNAVAILABLE'});
  await chmod(f.socketPath,0o600);await chmod(join(f.directory,'source'),0o755);
  await assert.rejects(()=>requestUpkeep(f.socketPath,capture,1000),{code:'UPKEEP_SOCKET_UNAVAILABLE'});
  await chmod(join(f.directory,'source'),0o700);await server.close();
  const script=`import {createServer} from 'node:net';import {chmodSync} from 'node:fs';const server=createServer();server.listen(process.argv[1],()=>{chmodSync(process.argv[1],0o600);process.stdout.write('ready');});`;
  const child=spawn(process.execPath,['--input-type=module','-e',script,f.socketPath],{stdio:['ignore','pipe','pipe']});
  t.after(()=>{if(child.exitCode===null)child.kill('SIGKILL');});
  await once(child.stdout,'data');const stopped=once(child,'close');child.kill('SIGKILL');await stopped;
  assert.equal((await stat(f.socketPath)).isSocket(),true);
  const restarted=await startUpkeepServer({...f,timeoutMs:1000,inspect:async()=>({})});t.after(()=>restarted.close());
  assert.deepEqual(await requestUpkeep(f.socketPath,capture,1000),{cursors:{anonymous:2},messages:[],counts:{newMessages:0}});
});

test('server and reader reject a parent alias even when the final directory and socket are private',async t=>{
  const f=await fixture(t);await mkdir(join(f.directory,'real'),{mode:0o700});
  await symlink(join(f.directory,'real'),join(f.directory,'alias'));
  const socketPath=join(f.directory,'alias','nested','source.sock');
  await assert.rejects(async()=>{
    const accidental=await startUpkeepServer({...f,socketPath,timeoutMs:1000,inspect:async()=>({})});
    await accidental.close();
  },{code:'UPKEEP_SOCKET_UNAVAILABLE'});
  const real=join(f.directory,'real','nested','source.sock');
  const server=await startUpkeepServer({...f,socketPath:real,timeoutMs:1000,inspect:async()=>({})});t.after(()=>server.close());
  await assert.rejects(()=>requestUpkeep(socketPath,capture,1000),{code:'UPKEEP_SOCKET_UNAVAILABLE'});
});

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
