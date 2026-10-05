import {test} from 'node:test';
import assert from 'node:assert/strict';
import {memoryWriteTool} from '../src/tools.js';

test('native memory_write publishes the canonical successful ID for persisted run inspection',async()=>{
  const tool=memoryWriteTool({add(){return {id:17};}});
  const value=await tool.execute({text:'anonymous fact',kind:'fact'});
  assert.equal(typeof tool.output.presentationMeta,'function');
  assert.deepEqual(tool.output.presentationMeta({},value),{memoryWrite:{ok:true,id:17}});
});

test('failed native memory_write metadata cannot carry a fabricated ID or private error text',async()=>{
  const tool=memoryWriteTool({add(){throw Error('private database marker');}});
  const value=await tool.execute({text:'anonymous fact'});
  assert.equal(typeof tool.output.presentationMeta,'function');
  const meta=tool.output.presentationMeta({},value);
  assert.deepEqual(meta,{memoryWrite:{ok:false}});
  assert.equal(JSON.stringify(meta).includes('private'),false);
});
