import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {JobLog} from '../src/joblog.js';

const diagnostic={version:1,category:'quota',code:'MODEL_QUOTA',result:'rejected',provider:'openai-codex',model:'gpt-5.5',
  actualProvider:'openai-codex',actualModel:'gpt-5.5',providerCode:'QUOTA',httpStatus:402,attempts:1,retryReason:'attempt-limit'};
test('completed diagnostic survives history and latest, excludes private fields, and conflicts on repeated identity',t=>{
  const dir=mkdtempSync(join(tmpdir(),'job-diagnostic-'));t.after(()=>rmSync(dir,{recursive:true,force:true}));
  const log=new JobLog(dir),identity={runId:'diagnostic-run',diagnostic:{...diagnostic,prompt:'PRIVATE',token:'SECRET'}};
  log._recordRun('task','fail','safe code',identity);
  assert.deepEqual(log.readHistory()[0].diagnostic,diagnostic);
  assert.deepEqual(JSON.parse(readFileSync(log.runsFile,'utf8')).task.diagnostic,diagnostic);
  assert.throws(()=>log._recordRun('task','fail','safe code',{...identity,diagnostic:{...diagnostic,category:'unknown'}}),{code:'JOBLOG_IDENTITY_COLLISION'});
});
test('legacy failures stay unclassified and malformed diagnostics refuse completion',t=>{
  const dir=mkdtempSync(join(tmpdir(),'job-diagnostic-'));t.after(()=>rmSync(dir,{recursive:true,force:true}));
  const log=new JobLog(dir);log._recordRun('legacy','fail','exit 1',{runId:'legacy'});
  assert.equal(log.readHistory()[0].diagnostic,undefined);
  for(const change of [{category:'PRIVATE'},{provider:'/private'},{attempts:3},{providerCode:'SECRET'},{httpStatus:99},{retryReason:'retry-now'}])
    assert.throws(()=>log._recordRun('task','fail','safe',{diagnostic:{...diagnostic,...change}}),{code:'JOBLOG_INVALID_DATA'});
});
