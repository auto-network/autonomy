import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {openCrosstalkCentralApproval, crosstalkReviewState} from '../static/js/components/crosstalk-central-approval.js';
const {JSDOM}=createRequire(import.meta.url)('jsdom');
let dom,calls,result;
const q=s=>document.querySelector('[data-testid=approval-dialog]')?.shadowRoot.querySelector(s);
const tick=()=>new Promise(r=>setTimeout(r,10));
async function until(fn){for(let n=0;n<250&&!fn();n++)await tick();assert.ok(fn(),q('#result-title')?.textContent+' / '+q('#result-copy')?.textContent);}
const MESSAGE='Line one of the request.\n\n'+'x'.repeat(5800)+'\nLast line: exactly this.';
const review={title:'Message a session',detail:'',requester_label:'chatgpt:ops',handle:'chatgpt:ops',target_session:'auto-0928-010203',
  target_label:'Release checklist',target_org:'autonomy',intent:'status check',message_lines:MESSAGE.split('\n'),machine_label:'Home'};
const item=(actions=['granted','declined'])=>({id:'C',actions,safeReview:review});
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

test('the review shows who, to whom, and the whole message, untruncated',async()=>{
  await openCrosstalkCentralApproval(item());
  assert.equal(q('#title').textContent,'Message a session?');
  assert.equal(q('#requester-kind').textContent,'From');
  assert.equal(q('#requester-name').textContent,'chatgpt:ops');
  assert.match(q('#facts').textContent,/ToRelease checklist/);
  assert.match(q('#facts').textContent,/Intentstatus check/);
  assert.equal(q('#request-detail pre').textContent,MESSAGE);
  assert.equal(q('#request-detail .eyebrow').textContent,'Message');
  assert.equal(q('#duration').value,'86400');
  assert.equal(q('#auth').hidden,true);
});

for(const value of ['3600','43200','86400','604800'])test('lifetime '+value+' reaches the Grant',async()=>{
  await openCrosstalkCentralApproval(item());q('#duration').value=value;q('#primary').click();
  await until(()=>q('#result-title').textContent==='Message approved');
  assert.deepEqual(posts(),[{outcome:'granted',decision:{ttl_seconds:Number(value)}}]);
});

test('decline records a decline with an empty decision',async()=>{
  await openCrosstalkCentralApproval(item());q('#secondary').click();q('#primary').click();
  await until(()=>q('#result-title').textContent==='Request declined');
  assert.deepEqual(posts(),[{outcome:'declined',decision:{}}]);
});

test('elsewhere and delivery states say so and offer no decision',async()=>{
  result={state:'elsewhere',machine_label:'Office NUC'};
  await openCrosstalkCentralApproval(item());
  assert.equal(q('#primary').disabled,true);
  assert.match(q('#review-unavailable').textContent,/This chat asked Office NUC; decide it there\./);
  assert.equal(crosstalkReviewState({state:'awaiting_delivery'},[],review).unavailable,'Approved; delivering.');
  assert.equal(crosstalkReviewState({state:'delivered'},[],review).unavailable,'Delivered to Release checklist.');
  assert.equal(crosstalkReviewState({state:'delivery_failed',reason:'session ended'},[],review).unavailable,'Not delivered: session ended.');
  assert.equal(crosstalkReviewState({state:'expired_undelivered'},[],review).unavailable,'Approved too late; the message was not delivered.');
  assert.equal(crosstalkReviewState({state:'superseded'},[],review).unavailable,'A newer message from this chat replaced this one.');
});
