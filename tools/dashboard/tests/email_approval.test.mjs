import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {openEmailApproval} from '../static/js/components/email-approval.js';
const {JSDOM}=createRequire(import.meta.url)('jsdom');
let dom,calls,result;
const q=s=>document.querySelector('[data-testid=approval-dialog]')?.shadowRoot.querySelector(s);
const tick=()=>new Promise(r=>setTimeout(r,10));
async function until(fn){for(let n=0;n<300&&!fn();n++)await tick();assert.ok(fn());}
const item=(actions=['granted','declined'])=>({id:'attention-email',actions,requester:{href:'/session/ws1/auto-1'},
  safeReview:{requester_label:'auto-1 · Writing a reply',from_addr:'agent@auto.network',to:'a@example.com',cc:'b@example.com',
    subject:'Your code',body_lines:['Hello,','','The code is 482913.']}});
test.beforeEach(()=>{
  dom=new JSDOM('<body></body>',{url:'https://example.test'});global.window=dom.window;global.document=dom.window.document;
  window.matchMedia=()=>({matches:false});window.Element.prototype.getAnimations=()=>[];window.Element.prototype.animate=()=>({cancel(){}});
  calls=[];result={approved:true,execution:{ok:true,message_id:'<m@auto.network>'}};
  global.fetch=async(url,options)=>{
    calls.push({url,options});
    const ok=body=>({ok:true,json:async()=>body});
    if(String(url).startsWith('/api/session/'))return ok({project:'ws1',session_id:'auto-1',org:{name:'Autonomy Network',favicon:'/static/icon-192.png'}});
    if(options?.method==='POST'){const b=JSON.parse(options.body);return ok({resolution:{outcome:b.outcome}});}
    return ok({review:{application_result:result}});
  };
});
test.afterEach(()=>{document.querySelector('[data-testid=approval-dialog]')?.remove();dom.window.close();});
const posts=()=>calls.filter(c=>c.options?.method==='POST').map(c=>JSON.parse(c.options.body));

test('review shows exactly what will be sent, with no password step',async()=>{
  await openEmailApproval(item());
  assert.equal(q('#title').textContent,'Send this email?');
  assert.equal(q('#resource').textContent,'Your code');
  assert.match(q('#facts').textContent,/Fromagent@auto\.network/);
  assert.match(q('#facts').textContent,/Toa@example\.com/);
  assert.match(q('#facts').textContent,/Ccb@example\.com/);
  assert.equal(q('#request-detail pre').textContent,'Hello,\n\nThe code is 482913.');
  assert.equal(q('#consequence').textContent,'A sent email cannot be recalled.');
  assert.equal(q('#org-name').textContent,'Autonomy Network');
  assert.equal(q('#primary').textContent,'Send');
  assert.equal(q('#auth').hidden,true);
});

test('Send grants with an empty decision once and reports the sent email',async()=>{
  await openEmailApproval(item());
  q('#primary').click();
  assert.equal(q('#primary').textContent,'Sending…');
  await until(()=>q('#result-title').textContent==='Email sent');
  assert.deepEqual(posts(),[{outcome:'granted',decision:{}}]);
  assert.match(q('#receipt').textContent,/Your code/);
  assert.match(q('#receipt').textContent,/To a@example\.com · Cc b@example\.com/);
});

test('a failed send is reported as a failure and not resubmitted',async()=>{
  result={approved:true,execution:{ok:false,error:'SMTP mail.example.org: 550 relay denied'}};
  await openEmailApproval(item());
  q('#primary').click();
  await until(()=>q('#result-title').textContent==='Could not complete the request');
  assert.match(q('#result-copy').textContent,/550 relay denied/);
  assert.equal(posts().length,1);
});

test('decline records a decline and sends nothing',async()=>{
  await openEmailApproval(item());
  q('#secondary').click();q('#primary').click();
  await until(()=>q('#result-title').textContent==='Request declined');
  assert.deepEqual(posts(),[{outcome:'declined',decision:{}}]);
});

test('a request that can no longer be granted cannot be sent',async()=>{
  await openEmailApproval(item(['declined']));
  assert.equal(q('#primary').disabled,true);
});
