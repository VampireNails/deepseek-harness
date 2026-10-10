import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {once} from 'node:events';
import {spawn} from 'node:child_process';
import {mkdtemp,mkdir,writeFile,readFile,rm,symlink} from 'node:fs/promises';
import {join,dirname} from 'node:path';
import {tmpdir} from 'node:os';
import {fileURLToPath} from 'node:url';

const cli=fileURLToPath(new URL('../cli.mjs',import.meta.url));
const linux={skip:process.platform!=='linux'};
const cleanEnv=()=>Object.fromEntries(Object.entries(process.env).filter(([key])=>
  !/KEY|SECRET|TOKEN|PASSWORD|BASE_URL/i.test(key)&&!key.startsWith('INTEL_')&&!key.startsWith('DSH_')));

async function fixture(t){
  const root=await mkdtemp(join(tmpdir(),'routing-actual-'));t.after(()=>rm(root,{recursive:true,force:true}));
  const home=join(root,'home'),profile=join(home,'profiles','evolve'),bin=join(root,'bin');
  await mkdir(profile,{recursive:true});await mkdir(bin);
  const installed=process.env.INTEL_TEST_DSH_MODULES;
  assert.ok(installed,'set INTEL_TEST_DSH_MODULES to the existing installed CLI node_modules');
  await symlink(installed,join(profile,'node_modules'),'dir');
  await writeFile(join(profile,'package.json'),JSON.stringify({name:'isolated-t06-evolve',private:true,
    dsh:{profile:{bundles:['@deepseek-ai/dsh-base','@deepseek-ai/dsh-headless']}}}));
  await writeFile(join(profile,'cordis.yml'),'[]\n');
  let mode,requests=[],sockets=new Set();
  const marker=join(root,'effect');
  const server=createServer(async(req,res)=>{
    const parts=[];for await(const part of req)parts.push(part);
    const data=JSON.parse(Buffer.concat(parts).toString('utf8'));requests.push({model:data.model,path:req.url});
    const count=requests.length;
    if(mode==='network'){req.socket.destroy();return;}
    if(mode==='timeout')return;
    const status=mode==='auth'?401:mode==='service'?503:mode==='rate'?429:
      mode==='quota'||mode==='quota-success'&&count===1||['effect','tool-failure'].includes(mode)&&count===2?402:undefined;
    if(status){res.writeHead(status,{'content-type':'application/json'});res.end(JSON.stringify({error:{type:status===429?'rate_limit_error':'api_error',message:'controlled refusal'}}));return;}
    res.writeHead(200,{'content-type':'text/event-stream'});
    const event=(type,data)=>res.write(`event: ${type}\ndata: ${JSON.stringify({type,...data})}\n\n`);
    event('message_start',{message:{id:'isolated-message',type:'message',role:'assistant',model:data.model,content:[],usage:{input_tokens:1,output_tokens:0}}});
    const tool=['effect','tool-failure'].includes(mode)&&count===1;
    event('content_block_start',{index:0,content_block:tool?{type:'tool_use',id:'isolated-tool',name:'bash',input:{}}:{type:'text',text:''}});
    if(tool)event('content_block_delta',{index:0,delta:{type:'input_json_delta',partial_json:JSON.stringify({command:`printf effect > '${marker}'`,
      ...(mode==='effect'?{description:'write isolated effect'}:{}),timeoutMs:1000})}});
    else event('content_block_delta',{index:0,delta:{type:'text_delta',text:'isolated answer'}});
    if(mode==='partial'){
      event('error',{error:{type:'rate_limit_error',message:'quota'}});res.end();return;
    }
    event('content_block_stop',{index:0});
    event('message_delta',{delta:{stop_reason:tool?'tool_use':'end_turn',stop_sequence:null},usage:{output_tokens:1}});
    event('message_stop',{});res.end();
  });
  server.on('connection',socket=>{sockets.add(socket);socket.once('close',()=>sockets.delete(socket));});
  server.listen(0,'127.0.0.1');await once(server,'listening');
  t.after(async()=>{for(const socket of sockets)socket.destroy();await new Promise(resolve=>server.close(resolve));});
  const url='http://127.0.0.1:'+server.address().port;
  await writeFile(join(profile,'cordis.patch.yml'),JSON.stringify([
    {id:'llm-deepseek',config:{baseURL:url,apiKeyEnv:'T06_FIXTURE_KEY',thinking:'disabled',
      retryPolicy:{mode:'normal',maxRetries:2,retryableCodes:['RATE_LIMIT'],backoff:{initialDelayMs:1,maxDelayMs:1,jitterRatio:0}}}},
    {id:'llm-pi-ai',config:{providers:{'t06-pi':{baseURL:url,api:'anthropic-messages',apiKeyEnv:'T06_FIXTURE_KEY',models:[{id:'t06-model'}]}}}},
    {id:'session-title-llm',disabled:true},{id:'tools',config:{mode:'native'}},
  ]));
  await writeFile(join(bin,'dsh'),'#!/bin/sh\nexec /usr/local/bin/dsh "$@"\n',{mode:0o700});
  const configFile=join(root,'routes.json');
  const config={timeoutMs:12000,defaultRoute:'primary',routes:{primary:{provider:'deepseek-official',model:'deepseek-flash'},
    backup:{provider:'deepseek-official',model:'deepseek-pro'}},tasks:{custom:{route:'primary',fallback:'backup',retrySafe:false}},
    fallback:{enabled:true,maxAttempts:2,totalTimeoutMs:24000}};
  async function run(nextMode,override={}){
    mode=nextMode;requests=[];await writeFile(configFile,JSON.stringify({...config,...override}));
    const child=spawn(process.execPath,[cli,'custom','isolated task'],{cwd:root,env:{...cleanEnv(),
      PATH:bin+':'+dirname(process.execPath)+':/usr/local/bin:/usr/bin:/bin',DSH_HOME:home,INTEL_JOBLOG_SCOPE:'test',
      INTEL_ROUTER_CONFIG:configFile,T06_FIXTURE_KEY:'local-provider-key'},stdio:['ignore','pipe','pipe']});
    let stdout='',stderr='';child.stdout.on('data',data=>stdout+=data);child.stderr.on('data',data=>stderr+=data);
    const timer=setTimeout(()=>child.kill('SIGKILL'),40000);
    const [exit,signal]=await once(child,'close');clearTimeout(timer);assert.equal(signal,null,stderr);
    const audit=(await readFile(join(home,'intel-joblog','route-runs.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);
    const finish=audit.findLast(row=>row.kind==='finish');
    const current=audit.filter(row=>row.runId===finish?.runId);
    return {exit,stdout,stderr,requests:[...requests],finish,audit:current};
  }
  return {run,home,marker,config};
}

test('actual installed CLI/provider requests match primary and bounded fallback models; stable failures never replay unknown effects',linux,async t=>{
  const f=await fixture(t),summaries=[];
  for(const [mode,exit,category,count] of [['quota-success',0,undefined,2],['quota',1,'quota',2],['rate',1,'rate-limit',2],
    ['auth',1,'authentication',1],['service',1,'service',1],['network',1,'network',1],['partial',1,'rate-limit',1],['effect',1,'quota',2]]){
    const r=await f.run(mode,mode==='effect'?{tasks:{custom:{route:'primary',fallback:'backup',retrySafe:true}}}:{});
    assert.equal(r.exit,exit,r.stderr);assert.equal(r.finish?.category,category,r.stderr);
    assert.equal(r.requests.length,count,r.stderr);
    if(mode==='effect'){
      assert.equal(await readFile(f.marker,'utf8'),'effect');assert.deepEqual(r.requests.map(row=>row.model),['deepseek-flash','deepseek-flash']);
      assert.equal(r.audit.some(row=>row.kind==='fallback'),false);
    }else if(count===2)assert.deepEqual(r.requests.map(row=>row.model),['deepseek-flash','deepseek-pro']);
    assert.equal(r.finish.actualModel,r.requests.at(-1)?.model);
    const jobs=JSON.parse(await readFile(join(f.home,'intel-joblog','job_test_runs.json'),'utf8'));
    assert.equal(jobs.custom.runId,r.finish.runId);assert.equal(jobs.custom.scope,'test');
    if(exit!==0){
      const events=(await readFile(join(f.home,'intel-system-events','events.test.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);
      const current=events.filter(row=>row.runId===r.finish.runId);
      assert.equal(current.length,1);assert.equal(current[0].code,r.finish.code);assert.equal(current[0].exitCode,exit);
    }
    summaries.push({mode,exit,category:category??'success',models:r.requests.map(row=>row.model),attempts:r.finish.attempts});
    console.log('ACTUAL_REQUEST '+JSON.stringify(summaries.at(-1)));
  }
  const timed=await f.run('timeout',{timeoutMs:5000,fallback:{enabled:true,maxAttempts:2,totalTimeoutMs:6000}});
  assert.equal(timed.exit,124,timed.stderr);assert.equal(timed.requests.length,1);assert.equal(timed.finish.result,'unknown');
  for(const fallback of [undefined,{enabled:false}]){
    const r=await f.run('auth',{fallback});assert.equal(r.exit,1);assert.equal(r.requests.length,1);
  }
  const unknown=await f.run('success',{tasks:{}});assert.equal(unknown.exit,0,unknown.stderr);
  const jobs=JSON.parse(await readFile(join(f.home,'intel-joblog','job_test_runs.json'),'utf8'));
  assert.match(jobs.custom.detail,/reason=unconfigured-task; requested=deepseek-official\/deepseek-flash; actual=deepseek-official\/deepseek-flash; attempts=1/);
  assert.match(jobs.custom.summary,/actual=deepseek-official\/deepseek-flash; attempts=1/);
  const events=(await readFile(join(f.home,'intel-system-events','events.test.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);
  assert.ok(events.some(row=>row.code==='MODEL_QUOTA'&&row.runId));
  console.log('ACTUAL_REQUESTS '+JSON.stringify(summaries));
  assert.deepEqual(summaries,JSON.parse(await readFile(new URL('./expected/routing-requests.json',import.meta.url),'utf8')));
});
test('actual cross-provider fallback reaches pi-ai HTTP client and stale model plugin ID makes zero requests',linux,async t=>{
  const f=await fixture(t);
  const routes={...f.config.routes,backup:{provider:'t06-pi',model:'t06-model'}};
  const success=await f.run('quota-success',{routes});
  assert.equal(success.exit,0,success.stderr);assert.equal(success.finish.actualProvider,'t06-pi');
  assert.deepEqual(success.requests.map(row=>row.model),['deepseek-flash','t06-model']);
  console.log('CROSS_PROVIDER '+JSON.stringify({provider:success.finish.actualProvider,models:success.requests.map(row=>row.model)}));
  const invalid=await f.run('success',{routes:{...f.config.routes,primary:{provider:'deepseek-official',model:'deepseek-pro'}},runtime:{defaultModelId:'missing-model-entry'}});
  assert.equal(invalid.exit,1);assert.equal(invalid.requests.length,0);
  assert.equal(invalid.finish.category,'model-config');
  const retry=await f.run('rate',{runtime:{retryId:'missing-retry-entry'}});
  assert.equal(retry.exit,1,retry.stderr);assert.equal(retry.requests.length,1,retry.stderr);assert.equal(retry.finish.attempts,1);
});
test('actual registered tool failure stays tool failure despite subsequent provider quota',linux,async t=>{
  const f=await fixture(t),result=await f.run('tool-failure',{tasks:{custom:{route:'primary',fallback:'backup',retrySafe:true}}});
  assert.equal(result.exit,1,result.stderr);assert.equal(result.finish.category,'tool');assert.equal(result.finish.code,'TASK_TOOL_FAILED');
  assert.equal(result.finish.attempts,1);assert.deepEqual(result.requests.map(row=>row.model),['deepseek-flash','deepseek-flash']);
  assert.equal(result.audit.some(row=>row.kind==='fallback'),false);
});
