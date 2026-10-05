import {test} from 'node:test';import assert from 'node:assert/strict';
import {mkdtemp,mkdir,readFile,rm} from 'node:fs/promises';import {tmpdir} from 'node:os';import {join} from 'node:path';
import {apply,Config} from '../index.js';

function mount(configuration){
  const services=new Map(),tools=new Map(),disposers=[];
  apply({provide:(name,value)=>services.set(name,value),tools:{register:tool=>{tools.set(tool.name,tool);return ()=>tools.delete(tool.name)}},effect:factory=>{for(const dispose of factory())disposers.push(dispose)}},configuration);
  return {services,tools,close(){for(const dispose of disposers.reverse())dispose()}};
}

test('configured retained-log root is shared by the service and recorded tool rendering',async()=>{
  const root=await mkdtemp(join(tmpdir(),'tokenlog-configured-'));let mounted;
  try{
    mounted=mount({sessionsRoot:root});
    assert.equal(Config({}).sessionsRoot,undefined);
    assert.equal(Config({sessionsRoot:root}).sessionsRoot,root);
    assert.throws(()=>Config({sessionsRoot:''}));
    assert.throws(()=>Config({sessionsRoot:5}));
    const service=mounted.services.get('tokenlog');assert.deepEqual(await service.aggregate(undefined,1),{days:1,sessions:0,skipped:0,byTask:{},byDay:{}});
    const tool=mounted.tools.get('token_usage_summary'),value=await tool.execute({days:1},{signal:new AbortController().signal});
    assert.deepEqual(tool.output.render({days:1},value),[{type:'text',text:(await readFile(new URL('./expected/empty-retained-usage.txt',import.meta.url),'utf8')).trimEnd()}]);
    await assert.rejects(service.aggregate(join(root,'absent'),1),/TOKENLOG_SOURCE_UNAVAILABLE/);
  }finally{mounted?.close();await rm(root,{recursive:true,force:true});}
});

test('an unspecified source uses DSH_HOME without opening another home',async()=>{
  const root=await mkdtemp(join(tmpdir(),'tokenlog-configured-home-')),previous=process.env.DSH_HOME;
  let mounted;
  try{
    process.env.DSH_HOME=root;await mkdir(join(root,'sessions','--root--'),{recursive:true});mounted=mount({});
    assert.deepEqual(await mounted.services.get('tokenlog').aggregate(undefined,1),{days:1,sessions:0,skipped:0,byTask:{},byDay:{}});
  }finally{mounted?.close();if(previous===undefined)delete process.env.DSH_HOME;else process.env.DSH_HOME=previous;await rm(root,{recursive:true,force:true});}
});
