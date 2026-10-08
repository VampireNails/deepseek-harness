import test from 'node:test';
import assert from 'node:assert/strict';
import {createUpkeepInputOrigin} from '../src/upkeep-input-origin.js';
import {captureUpkeepSource} from '../src/upkeep-source.js';

const event=(seq,text,id='message-'+seq)=>({seq,type:'user/message',data:{id,source:{kind:'user'},content:text}});
const entry={sessionId:'mixed',seq:1,messageId:'message-1',kind:'human'};
test('exact authorship cannot transfer to a different message, sequence, session or inherited child',()=>{
  const classify=createUpkeepInputOrigin({messageOrigins:[entry]});
  assert.equal(classify('mixed',event(1,'untrusted text')).kind,'human');
  for(const [id,message] of [['mixed',event(2,'same body')],['mixed',event(1,'same body','different-id')],
    ['other',event(1,'same body')],['child',event(1,'same body')]])assert.equal(classify(id,message).kind,'unknown');
  entry.kind='machine';
  assert.equal(classify('mixed',event(1,'same body')).kind,'human','Validated assignments own their immutable copy');
  entry.kind='human';
});
test('machine session scope filters a controlled root and acknowledges only its raw cut without model input',async()=>{
  const classify=createUpkeepInputOrigin({machineSessionIds:['controlled-machine']});
  const query={listSessions:async()=>[{header:{id:'controlled-machine'}}],observeSession:async()=>({
    header:{id:'controlled-machine'},cursor:1,inheritedEventCount:0,events:[event(0,'test'),event(1,'more test')],
    [Symbol.dispose](){} })};
  const result=await captureUpkeepSource(query,[],{cursors:{},bootstrap:false,maxBatchMessages:1,maxBatchChars:4},classify);
  assert.deepEqual(result.messages,[]);assert.equal(result.cursors['controlled-machine'],1);
  assert.equal(result.counts.filteredSessions,1);
});
test('machine message exclusion preserves unknown data, inherited filtering and checkpoints without replay',async()=>{
  const classify=createUpkeepInputOrigin({messageOrigins:[entry,{...entry,seq:2,messageId:'message-2',kind:'machine'}]});
  const events=[event(0,'inherited'),event(1,'human'),event(2,'machine'),event(3,'unknown')];
  const query={listSessions:async()=>[{header:{id:'mixed'}}],observeSession:async()=>({
    header:{id:'mixed'},cursor:3,inheritedEventCount:1,events,[Symbol.dispose](){} })};
  const request={cursors:{},bootstrap:false,maxBatchMessages:5,maxBatchChars:100};
  const result=await captureUpkeepSource(query,[],request,classify);
  assert.deepEqual(result.messages.map(message=>[message.text,message.inputOrigin.kind]),[['human','human'],['unknown','unknown']]);
  assert.equal(result.cursors.mixed,3);
  const replay=await captureUpkeepSource(query,[],{...request,cursors:result.cursors},classify);
  assert.deepEqual(replay.messages,[]);assert.equal(replay.counts.newMessages,0);
});
test('invalid or conflicting owner assignments fail before querying user data',()=>{
  for(const options of [{machineSessionIds:['a','a']},{machineSessionIds:['']},
    {messageOrigins:[entry,entry]},{messageOrigins:[{...entry,kind:'user'}]},
    {messageOrigins:[{...entry,seq:-1}]},{messageOrigins:[{...entry,messageId:''}]},
    {machineSessionIds:['mixed'],messageOrigins:[entry]}])
    assert.throws(()=>createUpkeepInputOrigin(options),{code:'UPKEEP_INVALID_INPUT_ORIGINS'});
});
