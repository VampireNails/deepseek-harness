import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,writeFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {aggregateSessions} from '../src/aggregator.js';
import {defaultSessionsRoot} from '../src/aggregator.js';

test('missing retained-log source is unavailable rather than zero usage',t=>{
  const root=mkdtempSync(join(tmpdir(),'tokenlog-source-'));
  t.after(()=>rmSync(root,{recursive:true,force:true}));
  assert.throws(()=>aggregateSessions(join(root,'missing'),7,()=>{throw Error('Unexpected decompression')}),/TOKENLOG_SOURCE_UNAVAILABLE/);
});

test('a file at the configured source cannot report zero usage',t=>{
  const root=mkdtempSync(join(tmpdir(),'tokenlog-source-file-'));
  t.after(()=>rmSync(root,{recursive:true,force:true}));
  const file=join(root,'not-a-directory');writeFileSync(file,'owned source placeholder');
  assert.throws(()=>aggregateSessions(file,7,()=>{throw Error('Unexpected decompression')}),/TOKENLOG_SOURCE_UNAVAILABLE/);
});

test('the default retained-log directory follows the current DSH home',t=>{
  const root=mkdtempSync(join(tmpdir(),'tokenlog-home-')),previous=process.env.DSH_HOME;
  t.after(()=>{if(previous===undefined)delete process.env.DSH_HOME;else process.env.DSH_HOME=previous;rmSync(root,{recursive:true,force:true});});
  process.env.DSH_HOME=root;
  assert.equal(defaultSessionsRoot(),join(root,'sessions','--root--'));
});
