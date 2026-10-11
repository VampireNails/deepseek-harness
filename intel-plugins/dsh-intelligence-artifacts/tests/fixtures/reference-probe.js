import assert from 'node:assert/strict';
import {writeFileSync,readFileSync,mkdirSync,existsSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {spawnSync} from 'node:child_process';
import https from 'node:https';
import {randomBytes} from 'node:crypto';
export const name='artifact-reference-probe';
export const inject=['tools','sessions','artifacts','sysevents'];
export async function apply(ctx){
  const proof={runtime:{pid:process.pid,node:process.version},checks:[],records:[]};let server;
  try{
    const {startServer,dshAdapter}=await import(pathToFileURL(join(process.env.T17_HMC_ROOT,'service/server.mjs')));
    const deadline=Date.now()+10000;
    while(!ctx.tools.get('artifact_save')||!ctx.tools.get('sysevents_emit')){
      if(Date.now()>deadline)throw Error('tools not ready');await new Promise(r=>setTimeout(r,10));
    }
    const session=ctx.sessions.create(),agent={session};
    const call=async(name,args)=>{
      const result=await ctx.tools.execute({callId:name+proof.records.length,name,arguments:args,agent,signal:new AbortController().signal});
      assert.equal(result.isError,false);proof.records.push({name,result});return result;
    };
    const root=join(process.env.DSH_HOME,'tls');mkdirSync(root);
    const certPath=join(root,'cert.pem'),keyPath=join(root,'key.pem');
    const issued=spawnSync('openssl',['req','-x509','-newkey','rsa:2048','-sha256','-nodes','-keyout',keyPath,'-out',certPath,'-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1'],{encoding:'utf8'});
    assert.equal(issued.status,0);const cert=readFileSync(certPath),key=readFileSync(keyPath),token=randomBytes(32).toString('base64url');
    const host={get:name=>ctx.get(name),connection:{createSharedFetchHandler:()=>({fetch:()=>{throw Error('unexpected generic Remote call');}})},typertGateway:{wireStream:{failure:()=>({code:'UPSTREAM_FAILURE'})}}};
    const bridge=dshAdapter(host);
    server=await startServer({port:0,token,tls:{cert,key},adapter:bridge,workspaceRoot:root});
    const request=(path,{body,secret=token,headers={}}={})=>new Promise((resolve,reject)=>{
      const req=https.request({hostname:'127.0.0.1',port:server.port,path,method:body?'POST':'GET',ca:cert,
        headers:{authorization:'Bearer '+secret,'content-type':'application/json',...headers}},res=>{
        let text='';res.on('data',b=>text+=b);res.on('end',()=>resolve({status:res.statusCode,value:JSON.parse(text)}));res.on('error',reject);
      });req.on('error',reject);req.end(body?JSON.stringify(body):undefined);
    });
    const caps=(await request('/v1/capabilities')).value;
    assert.equal(caps.protocol,1);assert.equal(caps.intel.version,1);assert.equal(caps.intel.artifacts.available,true);
    assert.equal(caps.intel.systemEvents.delivery,'bounded-tail-poll');assert(!caps.unary.includes('intel/status'));
    proof.capabilities=caps.intel;proof.checks.push('mounted-capability-not-generic-remote');
    const first=await call('artifact_save',{title:'Owned report',kind:'html',content:'<html><body>owned first</body></html>'});
    const ref=first.value.reference;
    assert.deepEqual(ref,{version:1,endpoint:'muse/artifacts/get',args:{id:ref.args.id,version:1}});
    assert.match(first.content[0].text,new RegExp(`id=${ref.args.id}`));assert.match(first.content[0].text,/version=1/);assert.doesNotMatch(first.content[0].text,/展示链接|(?:^|\s)\/artifacts\//);
    await call('artifact_save',{title:'Owned report',kind:'text',content:'owned second'});
    const consume=async(reference)=>{
      assert.equal(reference.version,1);assert.equal(reference.endpoint,'muse/artifacts/get');
      return request('/v1/rpc',{body:{endpoint:reference.endpoint,args:reference.args}});
    };
    const pinned=await consume(ref);assert.equal(pinned.status,200);assert.equal(pinned.value.value.content,'<html><body>owned first</body></html>');assert.equal(pinned.value.value.kind,'html');assert.equal(pinned.value.value.version,1);
    const latest=await request('/v1/rpc',{body:{endpoint:ref.endpoint,args:{id:ref.args.id}}});assert.equal(latest.value.value.version,2);assert.equal(latest.value.value.kind,'text');
    proof.reference=ref;proof.pinned=pinned.value;proof.latest=latest.value;proof.checks.push('actual-tool-reference-through-authenticated-https','pinned-html-after-newer-text','legacy-id-selects-latest');
    const got=await call('artifact_get',ref.args);assert.deepEqual(got.value.reference,ref);assert.equal(got.content[0].text.endsWith(pinned.value.value.content),true);
    const read=await call('artifact_read',{id:ref.args.id});assert.equal(read.value.reference.args.version,2);
    const list=await request('/v1/rpc',{body:{endpoint:'muse/artifacts/list',args:{}}});assert.equal(list.value.value.items[0].id,ref.args.id);
    for(const [args,status,code] of [[{id:'absent'},404,'MUSE_ARTIFACT_NOT_FOUND'],[{...ref.args,version:99},404,'MUSE_ARTIFACT_NOT_FOUND'],[{id:'../private'},400,'INVALID_CALL'],[{...ref.args,version:0},400,'INVALID_CALL']]){
      const r=await request('/v1/rpc',{body:{endpoint:ref.endpoint,args}});assert.equal(r.status,status);assert.equal(r.value.error.code,code);
    }
    const body={endpoint:ref.endpoint,args:ref.args};
    assert.equal((await request('/v1/rpc',{body,secret:'invalid'})).status,401);
    assert.equal((await request('/v1/rpc',{body,headers:{origin:'https://untrusted.invalid'}})).status,401);
    assert.equal((await request('/artifacts/'+ref.args.id)).status,404);
    proof.checks.push('missing-version-path-auth-origin-no-http-route');
    // Artifacts intentionally have Host scope; a Session field never turns a reference into a Session permission.
    const scoped=await request('/v1/rpc',{body:{...body,sessionId:'unrelated-session'}});assert.equal(scoped.status,200);
    proof.checks.push('existing-host-wide-artifact-scope');
    const absent=dshAdapter({...host,get:()=>undefined});assert.equal(absent.museArtifactsAvailable,false);
    await assert.rejects(absent.call(ref.endpoint,ref.args),e=>e.code==='MUSE_ARTIFACTS_UNAVAILABLE');
    const file=join(ctx.artifacts.dir,ref.args.id,'meta.json'),saved=readFileSync(file);
    const legacyMeta=JSON.parse(saved);delete legacyMeta.versions[0].kind;writeFileSync(file,JSON.stringify(legacyMeta));
    const oldTool=await call('artifact_get',ref.args),oldBridge=await consume(oldTool.value.reference);
    assert.match(oldTool.content[0].text,/kind=html/);assert.equal(oldBridge.value.value.kind,'html');
    legacyMeta.versions[0].kind='PRIVATE_BAD_KIND';writeFileSync(file,JSON.stringify(legacyMeta));
    const failed=await ctx.tools.execute({callId:'bad-kind',name:'artifact_get',arguments:ref.args,agent,signal:new AbortController().signal});
    assert.equal(failed.isError,true);assert.equal(failed.error.info.code,'ARTIFACT_INVALID_DATA');assert.doesNotMatch(JSON.stringify(failed),/PRIVATE_BAD_KIND/);
    assert.equal((await consume(ref)).value.error.code,'INVALID_MUSE_RESULT');writeFileSync(file,saved);
    proof.records.push({name:'artifact_get-legacy-kind',result:oldTool},{name:'artifact_get-invalid-kind',result:failed});
    proof.checks.push('legacy-mixed-kind-producer-bridge-agree','invalid-explicit-kind-native-safe-error');
    writeFileSync(file,'PRIVATE_BROKEN_JSON');const bad=await consume(ref);assert.equal(bad.status,502);assert.equal(bad.value.error.code,'MUSE_ARTIFACT_READ_FAILED');assert.doesNotMatch(JSON.stringify(bad),/PRIVATE_BROKEN/);writeFileSync(file,saved);
    proof.checks.push('missing-mounted-service-and-bad-data-safe');
    const eventsBody={endpoint:'muse/system-events/list',args:{}};
    const empty=await request('/v1/rpc',{body:eventsBody});assert.deepEqual(empty.value.value.items,[]);assert.equal(empty.value.value.continuity,'bounded-tail');
    await call('sysevents_emit',{type:'owned.status',title:'Owned status',severity:'high'});
    const notifications=await request('/v1/rpc',{body:eventsBody}),item=notifications.value.value.items[0];assert.equal(item.type,'owned.status');
    const cursor=await request('/v1/rpc',{body:{...eventsBody,args:{since:item.ts}}});assert.equal(cursor.value.value.items[0].id,item.id);
    const eventFile=join(process.env.DSH_HOME,'intel-system-events/events.jsonl');const eventBytes=readFileSync(eventFile);
    writeFileSync(eventFile,'{"PRIVATE_BAD":"data"}\n');const invalid=await request('/v1/rpc',{body:eventsBody});assert.equal(invalid.value.error.code,'SYSTEM_EVENTS_INVALID_DATA');assert.doesNotMatch(JSON.stringify(invalid),/PRIVATE_BAD/);writeFileSync(eventFile,eventBytes);
    proof.notification={empty:empty.value,items:notifications.value,cursor:cursor.value};proof.checks.push('mounted-notification-empty-event-equal-cursor-corrupt');
    proof.ok=true;
  }catch(error){proof.ok=false;proof.message=error.message;proof.stack=error.stack;}
  finally{if(server)await server.close();proof.serverClosed=true;writeFileSync(process.env.T17_RECEIPT,JSON.stringify(proof)+'\n');}
}
