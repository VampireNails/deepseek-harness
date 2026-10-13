import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRuntimeObserver} from '../runtime-observer.mjs';
import {classifyFailure,validateReceipt} from '../failure.mjs';

const plan={runId:'run',attempt:1,provider:'p',model:'m'};
const receipt={version:1,...plan,sessions:1,steps:1,requests:1,tools:0,outputs:0,retries:0,
  complete:true,streamOutput:false,end:'error',failure:{code:'QUOTA',status:402}};
const line=value=>Buffer.from(JSON.stringify(value)+'\n');

test('private receipt keeps only safe fields, including when input contains content or controls',()=>{
  const actual=validateReceipt({...receipt,prompt:'PRIVATE\u001b[31m',token:'sk-SECRET',extra:'x'.repeat(100000)},plan);
  assert.equal(actual.failure.code,'QUOTA');
  for(const key of ['prompt','token','extra'])assert.equal(Object.hasOwn(actual,key),false,key);
  assert.equal(validateReceipt({...receipt,end:'PRIVATE\u0000'},plan),undefined);
});

test('missing, malformed, stale, incomplete and unfinished receipts have distinct safe diagnostics',()=>{
  for(const [input,status,code] of [[null,'missing','TASK_RECEIPT_MISSING'],[Buffer.from('{bad\n'),'invalid','TASK_RECEIPT_INVALID'],
    [line({...receipt,runId:'old'}),'invalid','TASK_RECEIPT_INVALID'],[line({...receipt,complete:false}),'incomplete','TASK_RECEIPT_INCOMPLETE'],
    [Buffer.from(JSON.stringify(receipt)),'invalid','TASK_RECEIPT_INVALID']]){
    const observer=createRuntimeObserver(plan);if(input)observer.push(input);
    const observed=observer.finish();assert.equal(observed.receiptStatus,status);
    const failure=classifyFailure({exit:7,...observed});assert.equal(failure.code,code);assert.equal(failure.preExecution,false);
  }
});

test('arbitrary chunking and multibyte input preserve a valid line; long input is discarded within the line cap',()=>{
  const observer=createRuntimeObserver(plan),bytes=line({...receipt,ignored:'中文'});
  for(const byte of bytes)observer.push(Buffer.from([byte]));
  assert.equal(observer.finish().receiptStatus,'complete');
  const long=createRuntimeObserver(plan);long.push(Buffer.alloc(1024*1024,120));long.push(line(receipt));
  assert.equal(long.finish().receiptStatus,'invalid');assert.ok(long.retainedBytes<=8192);
  const many=createRuntimeObserver(plan);many.push(Buffer.concat(Array.from({length:1000},()=>line(receipt))));
  assert.equal(many.finish().receiptStatus,'complete');assert.ok(many.retainedBytes<=8192);
});

test('incomplete and later reset observations never erase an observed side effect',()=>{
  const observer=createRuntimeObserver(plan);observer.push(line({...receipt,complete:false,tools:1}));observer.push(line(receipt));
  const observed=observer.finish();assert.equal(observed.receipt.tools,1);
  assert.equal(classifyFailure({exit:1,...observed}).preExecution,false);
  const incomplete=createRuntimeObserver(plan);incomplete.push(line({...receipt,complete:false,tools:1}));
  assert.equal(classifyFailure({exit:1,...incomplete.finish()}).result,'partial');
  observer.push(Buffer.from('{bad\n'));
  const invalid=observer.finish();assert.equal(invalid.receiptStatus,'invalid');
  assert.equal(classifyFailure({exit:1,...invalid}).result,'partial');
});

test('a wrong request model cannot be erased by later matching selection',()=>{
  const observer=createRuntimeObserver(plan);observer.push(line({...receipt,model:'wrong'}));observer.push(line(receipt));
  assert.equal(classifyFailure({exit:1,...observer.finish()}).preExecution,false);
});
