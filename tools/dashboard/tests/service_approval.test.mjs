import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {openServiceApproval} from '../static/js/components/service-approval.js';
const {JSDOM}=createRequire(import.meta.url)('jsdom');
let dom,owner,calls,finish;
const q=s=>document.querySelector('[data-testid=approval-dialog]')?.shadowRoot.querySelector(s);
const tick=()=>new Promise(r=>setTimeout(r,10));
async function until(fn){for(let n=0;n<250&&!fn();n++)await tick();assert.ok(fn());}
const request=(ttl=31536000)=>({id:'capture-request',session:'Autonomy Capture',staged:{
  application:'Autonomy Capture',application_scope:'dropbox',summary:'Add screenshots to the global operator dropbox',
  requester:{label:'My iPhone',kind:'device_enrollment'},requested_ttl_seconds:ttl,
  resource_audience:'global_operator_dropbox',capabilities:[{method:'POST',path:'/api/dropbox'}]}});
test.beforeEach(()=>{
  dom=new JSDOM('<body></body>',{url:'https://example.test'});global.window=dom.window;global.document=dom.window.document;
  window.matchMedia=()=>({matches:false});window.Element.prototype.getAnimations=()=>[];window.Element.prototype.animate=()=>({cancel(){}});
  calls=[];finish=null;owner={_markApprovalDecided(id){calls.push({decided:id});this._sharedApprovalDialog.close();}};
  global.fetch=async(url,options)=>{
    calls.push({url,options});
    if(options?.method==='POST')return {ok:true,json:async()=>({ok:true})};
    assert.equal(url,'/api/approvals/capture-request?wait=20');
    return new Promise(resolve=>{finish=execution=>resolve({ok:true,json:async()=>({result:{approved:true,execution}})});});
  };
});
test.afterEach(()=>{owner._sharedApprovalDialog?.dispose();dom.window.close();});
test('review and cancel use no authenticator or decision; green grace can be reverted',async()=>{
  openServiceApproval(owner,request());
  assert.equal(q('#auth').hidden,true);assert.ok(q('#primary').classList.contains('cached'));
  assert.equal(q('#requester-name').textContent,'My iPhone');assert.equal(q('#requester-link').hasAttribute('href'),false);
  assert.doesNotMatch(q('#review').textContent,/global_operator_dropbox|\/api\/dropbox/);
  q('#primary').click();assert.ok(q('#primary').classList.contains('pending'));
  q('#primary').click();assert.equal(q('#primary').textContent,'Authorize');
  q('#primary').click();q('#secondary').click();assert.equal(q('#primary').textContent,'Authorize');
  q('#close').click();await new Promise(r=>setTimeout(r,1550));assert.equal(calls.length,0);
});
for(const value of ['86400','604800','2592000','31536000','315360000',''])test('existing duration '+(value||'Never')+' reaches decision and specific receipt',async()=>{
  openServiceApproval(owner,request());q('#duration').value=value;q('#primary').click();
  assert.equal(calls.length,0);await until(()=>finish);
  assert.equal(q('#result-title').textContent,'Allowing access…');
  const writes=calls.filter(c=>c.options?.method==='POST');assert.equal(writes.length,1);
  assert.deepEqual(JSON.parse(writes[0].options.body),{approved:true,ttl_seconds:value===''?null:Number(value)});
  finish({ok:true,token:'must-not-render'});await until(()=>q('#result-title').textContent==='Access allowed');
  assert.match(q('#receipt').textContent,/My iPhone/);assert.match(q('#receipt').textContent,/Autonomy Capture/);
  assert.match(q('#receipt').textContent,new RegExp({'86400':'1 day','604800':'7 days','2592000':'30 days','31536000':'1 year','315360000':'10 years','':'Never'}[value]));
  assert.doesNotMatch(q('#result').textContent,/must-not-render/);
});
test('delivery error is not success and does not resubmit',async()=>{
  openServiceApproval(owner,request(null));assert.equal(q('#duration').value,'');q('#primary').click();await until(()=>finish);
  finish({ok:false,error:'Credential creation failed'});await until(()=>q('#result-title').textContent==='Could not complete the request');
  assert.match(q('#result-copy').textContent,/Credential creation failed/);
  assert.equal(calls.filter(c=>c.options?.method==='POST').length,1);
});
test('explicit decline retains the existing decision payload',async()=>{
  openServiceApproval(owner,request());q('#secondary').click();q('#primary').click();
  await until(()=>q('#result-title').textContent==='Request declined');
  assert.deepEqual(JSON.parse(calls[0].options.body),{approved:false});
});
