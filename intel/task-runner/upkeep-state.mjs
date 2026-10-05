import {open,lstat,mkdir,readFile,rename,unlink} from 'node:fs/promises';
import {dirname,join,isAbsolute} from 'node:path';
import {randomUUID} from 'node:crypto';

const MAX_STATE_BYTES=2*1024*1024;
const fail=code=>Object.assign(new Error(code),{code});
async function inspect(file){
  let info;try{info=await lstat(file);}catch(error){if(error.code==='ENOENT')return null;throw error;}
  if(!info.isFile()||info.uid!==process.getuid()||(info.mode&0o077)!==0||info.size>MAX_STATE_BYTES)throw fail('UPKEEP_STATE_INVALID');
  return info;
}

/**
 * Own a private JSON checkpoint; fsync file and parent before acknowledging pending model work.
 * The caller holds the shared upkeep flock. Missing reads create nothing; corrupt or linked state is refused.
 * @param file - Absolute state path in the runner's private directory.
 * @returns Asynchronous read and atomic durable write operations.
 */
export function createUpkeepStateStore(file){
  if(process.platform!=='linux'||typeof file!=='string'||!isAbsolute(file))throw fail('UPKEEP_INVALID_CONFIG');
  return {
    async read(){
      if(!await inspect(file))return null;
      let value;try{value=JSON.parse(await readFile(file,'utf8'));}catch{throw fail('UPKEEP_STATE_INVALID');}
      return value;
    },
    async write(value){
      const original=await inspect(file),directory=dirname(file);
      await mkdir(directory,{recursive:true,mode:0o700});
      const parent=await lstat(directory);
      if(!parent.isDirectory()||parent.uid!==process.getuid()||(parent.mode&0o077)!==0)throw fail('UPKEEP_STATE_INVALID');
      const serialized=JSON.stringify(value)+'\n';
      if(Buffer.byteLength(serialized)>MAX_STATE_BYTES)throw fail('UPKEEP_STATE_INVALID');
      const temporary=join(directory,'.'+randomUUID()+'.tmp');
      let handle;
      try{
        handle=await open(temporary,'wx',0o600);await handle.writeFile(serialized);await handle.sync();await handle.close();handle=null;
        const current=await inspect(file);
        if(original?(current?.ino!==original.ino||current?.dev!==original.dev):current!==null)throw fail('UPKEEP_STATE_CHANGED');
        await rename(temporary,file);
        const parentHandle=await open(directory,'r');try{await parentHandle.sync();}finally{await parentHandle.close();}
      }finally{
        if(handle)await handle.close();
        try{await unlink(temporary);}catch(error){if(error.code!=='ENOENT')throw error;}
      }
    },
  };
}
