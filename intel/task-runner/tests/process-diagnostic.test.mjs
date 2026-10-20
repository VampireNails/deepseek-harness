import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createStderrObserver,processDiagnostic} from '../process-diagnostic.mjs';
import {createRuntimeObserver} from '../runtime-observer.mjs';
import {classifyFailure} from '../failure.mjs';

test('stderr retains only bounded counts and fixed hints, including short and empty input',()=>{
  const empty=createStderrObserver();assert.deepEqual(empty.finish(),{bytes:0,capped:false,hint:'none'});
  const short=createStderrObserver();short.push(Buffer.from('PRIVATE token=SECRET\u001b[31m'));
  assert.deepEqual(short.finish(),{bytes:25,capped:false,hint:'none'});
  const long=createStderrObserver();long.push(Buffer.alloc(1024*1024,120));
  assert.deepEqual(long.finish(),{bytes:65536,capped:true,hint:'none'});assert.equal(long.retainedBytes,0);
  const plugin=createStderrObserver();for(const byte of Buffer.from('dsh: plugin tree failed to load PRIVATE\n'))plugin.push(Buffer.from([byte]));
  assert.equal(plugin.finish().hint,'plugin-startup');assert.equal(plugin.retainedBytes,0);
  assert.doesNotMatch(JSON.stringify(short.finish())+JSON.stringify(plugin.finish()),/PRIVATE|SECRET|\u001b/);
});

test('runtime observations distinguish activation, receipt corruption and FD errors without permitting replay',()=>{
  const plan={runId:'r',attempt:1,provider:'p',model:'m'};
  const value={version:1,runId:'r',attempt:1,sessions:0,steps:0,requests:0,tools:0,outputs:0,retries:0,streamOutput:false,complete:false};
  const observer=createRuntimeObserver(plan);observer.push(Buffer.from(JSON.stringify(value)+'\n'));
  assert.equal(observer.finish().runtimeStage,'activated');
  observer.push(Buffer.from('{bad\n'));assert.equal(observer.finish().receiptIssue,'malformed-json');
  assert.equal(observer.finish().runtimeStage,'activated');
  const fd=createRuntimeObserver(plan);fd.invalidate('fd-error');assert.equal(fd.finish().receiptIssue,'fd-error');
  const result={exit:1,...observer.finish(),stderr:{bytes:12,capped:false,hint:'plugin-startup'}};
  assert.match(processDiagnostic(result),/receipt=invalid; receiptIssue=malformed-json; runtime=activated/);
  assert.equal(classifyFailure(result).preExecution,false);
  assert.equal(classifyFailure({exit:1,receiptStatus:'missing',stderr:{hint:'quota'}}).preExecution,false);
  assert.doesNotMatch(processDiagnostic({...result,stderr:{bytes:1e20,capped:'SECRET',hint:'PRIVATE'}}),/PRIVATE|SECRET|1e/);
});
