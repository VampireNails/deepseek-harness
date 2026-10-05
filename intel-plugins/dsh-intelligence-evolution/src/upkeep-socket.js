import {createServer,createConnection} from 'node:net';
import {mkdir,lstat,chmod,unlink,link} from 'node:fs/promises';
import {dirname,isAbsolute,join} from 'node:path';
import {randomBytes} from 'node:crypto';

const MAX_WIRE_BYTES=2*1024*1024;
const fail=code=>Object.assign(new Error(code),{code});
const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
const safeCode=error=>typeof error?.code==='string'&&/^UPKEEP_[A-Z0-9_]{1,64}$/.test(error.code)?error.code:'UPKEEP_SOURCE_FAILED';
function pathAndTimeout(path,timeoutMs){
  if(process.platform!=='linux'||typeof path!=='string'||!isAbsolute(path)||Buffer.byteLength(path)>100||
    !Number.isSafeInteger(timeoutMs)||timeoutMs<1||timeoutMs>300000)throw fail('UPKEEP_INVALID_CONFIG');
}
function validateRequest(value){
  if(!object(value)||value.version!==1)throw fail('UPKEEP_INVALID_REQUEST');
  if(value.operation==='capture'){
    if(Object.keys(value).some(key=>!['version','operation','request'].includes(key)))throw fail('UPKEEP_INVALID_REQUEST');
    const request=value.request;
    if(!object(request)||Object.keys(request).some(key=>!['bootstrap','cursors','maxBatchMessages','maxBatchChars'].includes(key))||
      typeof request.bootstrap!=='boolean'||!object(request.cursors)||Object.keys(request.cursors).length>10000||
      Object.entries(request.cursors).some(([id,seq])=>!id||id.length>1024||!Number.isSafeInteger(seq)||seq< -1)||
      !Number.isSafeInteger(request.maxBatchMessages)||request.maxBatchMessages<1||request.maxBatchMessages>100||
      !Number.isSafeInteger(request.maxBatchChars)||request.maxBatchChars<1||request.maxBatchChars>262144)throw fail('UPKEEP_INVALID_REQUEST');
  }else if(value.operation==='inspect'){
    if(Object.keys(value).some(key=>!['version','operation','identity'].includes(key))||!object(value.identity)||
      Object.keys(value.identity).some(key=>!['taskId','runId'].includes(key))||
      !['evolve_upkeep','memory_upkeep'].includes(value.identity.taskId)||typeof value.identity.runId!=='string'||
      !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(value.identity.runId))throw fail('UPKEEP_INVALID_REQUEST');
  }else throw fail('UPKEEP_INVALID_REQUEST');
  return value;
}
function privateOwner(info){
  return info.uid===process.getuid()&&(info.mode&0o077)===0;
}
async function removeStale(path,timeoutMs){
  let original;
  try{original=await lstat(path);}catch(error){if(error.code==='ENOENT')return;throw fail('UPKEEP_SOCKET_UNAVAILABLE');}
  if(!original.isSocket()||!privateOwner(original))throw fail('UPKEEP_SOCKET_UNAVAILABLE');
  const active=await new Promise((resolve,reject)=>{
    const socket=createConnection(path),timer=setTimeout(()=>{socket.destroy();reject(fail('UPKEEP_SOCKET_UNAVAILABLE'));},timeoutMs);
    socket.once('connect',()=>{clearTimeout(timer);socket.destroy();resolve(true);});
    socket.once('error',error=>{clearTimeout(timer);socket.destroy();if(error.code==='ECONNREFUSED')resolve(false);else reject(fail('UPKEEP_SOCKET_UNAVAILABLE'));});
  });
  if(active)throw fail('UPKEEP_SOCKET_ACTIVE');
  const current=await lstat(path);
  if(current.ino!==original.ino||current.dev!==original.dev||!current.isSocket()||!privateOwner(current))throw fail('UPKEEP_SOCKET_UNAVAILABLE');
  await unlink(path);
}

/**
 * Serve capture and run inspection on an owner-only Unix socket in a private directory.
 * No model or mutation method exists; close aborts and awaits owned reads before releasing its path.
 * The published hard link is separate from Node's bind path, which Node unlinks on close.
 * @param options - Absolute socket path, bounded deadline and Host-owned read handlers.
 * @returns The asynchronous idempotent close operation.
 */
export async function startUpkeepServer({socketPath,timeoutMs,source,inspect}){
  pathAndTimeout(socketPath,timeoutMs);
  const directory=dirname(socketPath);
  await mkdir(directory,{recursive:true,mode:0o700});
  const parent=await lstat(directory);
  if(!parent.isDirectory()||!privateOwner(parent))throw fail('UPKEEP_SOCKET_UNAVAILABLE');
  const bindPath=join(directory,'.u-'+randomBytes(6).toString('hex'));
  pathAndTimeout(bindPath,timeoutMs);
  await removeStale(socketPath,timeoutMs);
  const connections=new Set(),running=new Set();let closed,owned;
  const server=createServer(socket=>{
    if(connections.size>=4){socket.destroy();return;}
    const controller=new AbortController(),connection={socket,controller};connections.add(connection);
    let buffer=Buffer.alloc(0),dispatched=false;
    const timer=setTimeout(()=>{controller.abort();socket.destroy();},timeoutMs);
    socket.on('error',()=>{controller.abort();});
    socket.once('close',()=>{clearTimeout(timer);controller.abort();connections.delete(connection);});
    socket.on('data',chunk=>{
      if(dispatched)return;
      buffer=Buffer.concat([buffer,chunk]);
      if(buffer.length>MAX_WIRE_BYTES){socket.destroy();return;}
      const newline=buffer.indexOf(10);if(newline<0)return;
      dispatched=true;
      const operation=(async()=>{
        try{
          if(newline!==buffer.length-1)throw fail('UPKEEP_INVALID_REQUEST');
          let parsed;try{parsed=JSON.parse(buffer.subarray(0,newline).toString('utf8'));}catch{throw fail('UPKEEP_INVALID_REQUEST');}
          const request=validateRequest(parsed);
          const value=request.operation==='capture'?await source.capture(request.request,{signal:controller.signal}):
            await inspect(request.identity,{signal:controller.signal});
          controller.signal.throwIfAborted();
          const response=JSON.stringify({version:1,ok:true,value})+'\n';
          if(Buffer.byteLength(response)>MAX_WIRE_BYTES)throw fail('UPKEEP_SOURCE_TOO_LARGE');
          if(!socket.destroyed)socket.end(response);
        }catch(error){if(!socket.destroyed)socket.end(JSON.stringify({version:1,ok:false,code:safeCode(error)})+'\n');}
      })();
      running.add(operation);operation.then(()=>running.delete(operation),()=>running.delete(operation));
    });
  });
  server.on('error',()=>{for(const item of connections){item.controller.abort();item.socket.destroy();}});
  const close=()=>{
    if(closed)return closed;
    closed=(async()=>{
      for(const item of connections){item.controller.abort();item.socket.destroy();}
      if(server.listening)await new Promise((resolve,reject)=>server.close(error=>error?reject(error):resolve()));
      await Promise.allSettled([...running]);
      if(!owned)return;
      let current;try{current=await lstat(socketPath);}catch(error){if(error.code==='ENOENT')return;throw error;}
      if(current.ino===owned.ino&&current.dev===owned.dev&&current.isSocket())await unlink(socketPath);
    })();return closed;
  };
  try{
    await new Promise((resolve,reject)=>{
      const onError=error=>{server.removeListener('listening',onListening);reject(error);};
      const onListening=()=>{server.removeListener('error',onError);resolve();};
      server.once('error',onError);server.once('listening',onListening);server.listen(bindPath);
    });
    owned=await lstat(bindPath);
    await chmod(bindPath,0o600);
    await link(bindPath,socketPath);
    await chmod(socketPath,0o600);
    const published=await lstat(socketPath);
    if(published.ino!==owned.ino||published.dev!==owned.dev||!published.isSocket()||!privateOwner(published)){
      throw fail('UPKEEP_SOCKET_UNAVAILABLE');
    }
  }catch(error){
    await close();
    throw fail(safeCode(error));
  }
  return {close};
}

/**
 * Read one versioned response from the configured owner-only Host socket.
 * @param socketPath - Existing absolute socket path; never logged.
 * @param request - Capture or exact run-inspection request.
 * @param timeoutMs - Whole connection deadline.
 * @returns The private response value; failures contain only a stable code.
 */
export async function requestUpkeep(socketPath,request,timeoutMs){
  pathAndTimeout(socketPath,timeoutMs);validateRequest(request);
  let info;try{info=await lstat(socketPath);}catch{throw fail('UPKEEP_SOCKET_UNAVAILABLE');}
  if(!info.isSocket()||!privateOwner(info))throw fail('UPKEEP_SOCKET_UNAVAILABLE');
  return await new Promise((resolve,reject)=>{
    const socket=createConnection(socketPath);let buffer=Buffer.alloc(0),settled=false;
    const finish=(error,value)=>{if(settled)return;settled=true;clearTimeout(timer);socket.destroy();if(error)reject(error);else resolve(value);};
    const timer=setTimeout(()=>finish(fail('UPKEEP_SOURCE_TIMEOUT')),timeoutMs);
    socket.once('connect',()=>socket.write(JSON.stringify(request)+'\n'));
    socket.on('error',()=>finish(fail('UPKEEP_SOCKET_UNAVAILABLE')));
    socket.once('close',()=>{if(!settled)finish(fail('UPKEEP_SOURCE_INCOMPLETE'));});
    socket.on('data',chunk=>{
      buffer=Buffer.concat([buffer,chunk]);if(buffer.length>MAX_WIRE_BYTES){finish(fail('UPKEEP_SOURCE_TOO_LARGE'));return;}
      const newline=buffer.indexOf(10);if(newline<0)return;
      let response;try{response=JSON.parse(buffer.subarray(0,newline).toString('utf8'));}catch{finish(fail('UPKEEP_SOURCE_INCOMPLETE'));return;}
      if(newline!==buffer.length-1||!object(response)||response.version!==1||typeof response.ok!=='boolean'){
        finish(fail('UPKEEP_SOURCE_INCOMPLETE'));return;
      }
      if(response.ok)finish(null,response.value);else finish(fail(safeCode(response)));
    });
  });
}
