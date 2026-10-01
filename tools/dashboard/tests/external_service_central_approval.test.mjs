import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {openExternalServiceCentralApproval, serviceReviewState} from '../static/js/components/external-service-central-approval.js';
const {JSDOM}=createRequire(import.meta.url)('jsdom');
// Ported from the retired service_approval.test.mjs (auto-fkhq0.26).
let dom,calls,result;
const q=s=>document.querySelector('[data-testid=approval-dialog]')?.shadowRoot.querySelector(s);
const tick=()=>new Promise(r=>setTimeout(r,10));
async function until(fn){for(let n=0;n<250&&!fn();n++)await tick();assert.ok(fn(),q('#result-title')?.textContent);}
const review=(ttl=31536000)=>({title:'Allow service access',detail:'',requester_label:'My iPhone',application:'Autonomy Capture',
  summary:'Add screenshots to the global operator dropbox',capabilities:[{method:'POST',path:'/api/dropbox'}],
  resource_audience:'global_operator_dropbox',requested_ttl_seconds:ttl,machine_label:'Home'});
const item=(ttl,actions=['granted','declined'])=>({id:'X',actions,safeReview:review(ttl)});
test.beforeEach(()=>{
  dom=new JSDOM('<body></body>',{url:'https://example.test'});global.window=dom.window;global.document=dom.window.document;
  window.matchMedia=()=>({matches:false});window.Element.prototype.getAnimations=()=>[];window.Element.prototype.animate=()=>({cancel(){}});
  calls=[];result={state:'pending',machine_label:'Home'};
  global.fetch=async(url,options)=>{
    calls.push({url:String(url),options});
    const reply=body=>({ok:true,status:200,json:async()=>body});
    if(options?.method==='POST'){const b=JSON.parse(options.body);return reply({resolution:{outcome:b.outcome}});}
    return reply({review:{application_result:result}});
  };
});
test.afterEach(()=>{document.querySelector('[data-testid=approval-dialog]')?.remove();dom.window.close();});
const posts=()=>calls.filter(c=>c.options?.method==='POST').map(c=>JSON.parse(c.options.body));

test('review and cancel use no authenticator or decision; the green grace can be reverted',async()=>{
  await openExternalServiceCentralApproval(item());
  assert.equal(q('#auth').hidden,true);assert.ok(q('#primary').classList.contains('cached'));
  assert.equal(q('#requester-name').textContent,'My iPhone');assert.equal(q('#requester-link').hasAttribute('href'),false);
  assert.doesNotMatch(q('#review').textContent,/global_operator_dropbox|\/api\/dropbox/);
  q('#primary').click();assert.ok(q('#primary').classList.contains('pending'));
  q('#primary').click();assert.equal(q('#primary').textContent,'Authorize');
  q('#close').click();await new Promise(r=>setTimeout(r,1550));assert.equal(posts().length,0);
});

for(const value of ['86400','604800','2592000','31536000','315360000',''])test('duration '+(value||'Never')+' reaches the Grant and the receipt',async()=>{
  await openExternalServiceCentralApproval(item());q('#duration').value=value;q('#primary').click();
  await until(()=>q('#result-title').textContent==='Access allowed');
  assert.deepEqual(posts(),[{outcome:'granted',decision:{ttl_seconds:value===''?null:Number(value)}}]);
  assert.match(q('#receipt').textContent,/My iPhone/);assert.match(q('#receipt').textContent,/Autonomy Capture/);
  assert.match(q('#receipt').textContent,new RegExp({'86400':'1 day','604800':'7 days','2592000':'30 days','31536000':'1 year','315360000':'10 years','':'Never'}[value]));
});

test('the requested lifetime is the default when it is offered, else 1 year',async()=>{
  await openExternalServiceCentralApproval(item(null));assert.equal(q('#duration').value,'');
  document.querySelector('[data-testid=approval-dialog]').remove();
  await openExternalServiceCentralApproval(item(12345));assert.equal(q('#duration').value,'31536000');
});

test('decline records a decline with an empty decision',async()=>{
  await openExternalServiceCentralApproval(item());q('#secondary').click();q('#primary').click();
  await until(()=>q('#result-title').textContent==='Request declined');
  assert.deepEqual(posts(),[{outcome:'declined',decision:{}}]);
});

test('elsewhere and collection states say so and offer no decision',async()=>{
  result={state:'elsewhere',machine_label:'Office NUC'};
  await openExternalServiceCentralApproval(item());
  assert.equal(q('#primary').disabled,true);
  assert.match(q('#review-unavailable').textContent,/Approved; the device collects its access from Office NUC\./);
  assert.equal(serviceReviewState({state:'awaiting_collection'},[]).unavailable,'Allowed. The device picks up its access when it next checks in.');
  assert.equal(serviceReviewState({state:'delivered'},[]).unavailable,'The device has its access.');
});
