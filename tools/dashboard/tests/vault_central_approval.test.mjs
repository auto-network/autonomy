import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {openVaultCentralApproval, vaultReviewState} from '../static/js/components/vault-central-approval.js';
import {setCachedConfirmDelayForTest} from '../static/js/components/approval-experiment.js';
// The 1.5s green-Authorize cancel window is exercised in approval_dialog.test.mjs.
setCachedConfirmDelayForTest(1);
const {JSDOM}=createRequire(import.meta.url)('jsdom');
let dom,calls,result,delivery;
const CEK='ab'.repeat(32);
const q=s=>document.querySelector('[data-testid=approval-dialog]')?.shadowRoot.querySelector(s);
const tick=()=>new Promise(r=>setTimeout(r,10));
async function until(fn){for(let n=0;n<300&&!fn();n++)await tick();assert.ok(fn());}
const item=(actions=['granted','declined'])=>({id:'attention-vault',actions,requester:{href:'/session/ws1/auto-1'},
  safeReview:{requester_label:'auto-1 · Signing a release',target:'autonomy:cosign-release-key',ttl_seconds:0,machine_label:'Home'}});
const opts={collect:async()=>({openers:[]}),openKey:async()=>CEK};
test.beforeEach(()=>{
  dom=new JSDOM('<body></body>',{url:'https://example.test'});global.window=dom.window;global.document=dom.window.document;
  window.matchMedia=()=>({matches:false});window.Element.prototype.getAnimations=()=>[];window.Element.prototype.animate=()=>({cancel(){}});
  calls=[];result={state:'pending',machine_label:'Home'};
  delivery={status:200,body:{receipt:{release_id:'r1',delivery:'session-ramfs',path:'/run/secrets/cosign-release-key',ttl_seconds:0}}};
  global.fetch=async(url,options)=>{
    calls.push({url:String(url),options});
    const reply=(status,body)=>({ok:status<400,status,json:async()=>body});
    if(String(url).startsWith('/api/session/'))return reply(200,{project:'ws1',session_id:'auto-1',org:{name:'Autonomy Network',favicon:'/static/icon-192.png'}});
    if(String(url).endsWith('/vault-open-bootstrap'))return reply(200,{ceremony:{v:2},bundle:{generation:1}});
    if(String(url).endsWith('/vault-open-delivery'))return reply(delivery.status,delivery.body);
    if(options?.method==='POST'){const b=JSON.parse(options.body);return reply(200,{resolution:{outcome:b.outcome}});}
    return reply(200,{review:{application_result:result}});
  };
});
test.afterEach(()=>{document.querySelector('[data-testid=approval-dialog]')?.remove();dom.window.close();});
const posts=()=>calls.filter(c=>c.options?.method==='POST').map(c=>({url:c.url.replace('/api/attention/items/attention-vault',''),body:JSON.parse(c.options.body)}));

test('review names the credential and the machine it is delivered from, before any ceremony',async()=>{
  await openVaultCentralApproval(item(),opts);
  assert.equal(q('#title').textContent,'Release credential?');
  assert.match(q('#facts').textContent,/AvailableUntil this session ends/);
  assert.match(q('#facts').textContent,/Delivered fromHome/);
  assert.equal(q('#primary').textContent,'Authorize');
  assert.ok(!calls.some(c=>c.url.endsWith('/vault-open-bootstrap')),'no ceremony material before Authorize');
});

test('Authorize records an empty Grant, then delivers the key once, and never puts the key in the decision',async()=>{
  await openVaultCentralApproval(item(),opts);
  q('#primary').click();
  await until(()=>q('#result-title').textContent==='Credential released');
  assert.deepEqual(posts(),[
    {url:'/approval-decision',body:{outcome:'granted',decision:{}}},
    {url:'/vault-open-delivery',body:{content_key:CEK}},
  ]);
  const decisionBody=calls.find(c=>c.url.endsWith('/approval-decision')).options.body;
  assert.ok(!decisionBody.includes(CEK));
});

test('elsewhere: says where to release it and offers no ceremony',async()=>{
  result={state:'elsewhere',machine_label:'Office NUC'};
  await openVaultCentralApproval(item(),opts);
  assert.equal(q('#primary').disabled,true);
  assert.match(q('[id=review-unavailable]')?.textContent||document.querySelector('[data-testid=approval-dialog]').shadowRoot.textContent,/Deliverable only from Office NUC/);
});

test('awaiting delivery: Deliver repeats the ceremony and posts only the delivery',async()=>{
  result={state:'awaiting_delivery',machine_label:'Home'};
  await openVaultCentralApproval(item([]),opts);
  assert.equal(q('#title').textContent,'Deliver credential?');
  assert.doesNotMatch(q('#facts').textContent,/Deliver by/);
  q('#primary').click();
  await until(()=>q('#result-title').textContent==='Credential released');
  assert.deepEqual(posts().map(p=>p.url),['/vault-open-delivery']);
});

test('a wrong key is reported in words and the Grant is not re-sent',async()=>{
  delivery={status:409,body:{error:'open_failed'}};
  await openVaultCentralApproval(item(),opts);
  q('#primary').click();
  await until(()=>q('#result-title').textContent==='Could not complete the request');
  assert.match(q('#result-copy').textContent,/could not be opened with that unlock/);
  assert.equal(posts().filter(p=>p.url==='/approval-decision').length,1);
});

test('decline records a decline and opens nothing',async()=>{
  await openVaultCentralApproval(item(),opts);
  q('#secondary').click();q('#primary').click();
  await until(()=>q('#result-title').textContent==='Request declined');
  assert.deepEqual(posts(),[{url:'/approval-decision',body:{outcome:'declined',decision:{}}}]);
  assert.ok(!calls.some(c=>c.url.endsWith('/vault-open-bootstrap')));
});

test('state wording for every delivery state',()=>{
  assert.equal(vaultReviewState({state:'session_gone'},[]).unavailable,'Approved, but the session ended before delivery. It must ask again.');
  assert.equal(vaultReviewState({state:'delivered'},[]).unavailable,'This credential was delivered.');
  assert.equal(vaultReviewState({state:'delivery_failed'},[]).deliver,false);
  assert.equal(vaultReviewState(null,['granted']).unavailable,'');
  assert.equal(vaultReviewState(null,['declined']).unavailable,'This request is no longer available.');
});
