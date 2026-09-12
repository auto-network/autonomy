import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {openVaultApproval} from '../static/js/components/vault-approval.js';
const {JSDOM}=createRequire(import.meta.url)('jsdom');
let dom,self,calls,view,seed,reads,execution;
const q=s=>document.querySelector('[data-testid=approval-dialog]')?.shadowRoot.querySelector(s);
const tick=()=>new Promise(r=>setTimeout(r,10));
async function until(fn){for(let i=0;i<100&&!fn();i++)await tick();assert.ok(fn());}
const fixture=()=>({id:'vault-1',kind:'vault_open',request:{setting:{set_id:'vault.secured',key:'deployment.env'},
  requester:{session:'requester',label:'Release session',organization:'autonomy',workspace:'workspace'},
  operation:'read',release_mode:'delivered',ttl_seconds:900},
  ceremony:{v:1,policy:'password',factors:[{factor_id:'pw',type:'password'}]},
  bundle:{generation:{},sealed_cek:{},class_id:'class',genesis_id:'genesis',setting_name:'setting'}});
test.beforeEach(()=>{
  dom=new JSDOM('<body></body>',{url:'https://example.test'});global.window=dom.window;global.document=dom.window.document;
  window.matchMedia=()=>({matches:false});window.Element.prototype.getAnimations=()=>[];window.Element.prototype.animate=()=>({cancel(){}});
  calls=[];reads=0;execution={ok:true};view=null;seed=new Uint8Array(32).fill(7);
  self={_markApprovalDecided(id){calls.push(['decided',id]);this._sharedApprovalDialog.close();}};
  global.fetch=async(url,options)=>{
    calls.push([url,options]);let body;
    if(url==='/api/session/requester') body={session_id:'requester',project:'workspace',org:{name:'Autonomy',favicon:'/static/icon-192.png'}};
    else if(options?.method==='POST')body={ok:true};
    else {assert.equal(url,'/api/approvals/vault-1');body={result:++reads===1?null:{approved:true,execution}};}
    return {ok:true,json:async()=>body};
  };
});
test.afterEach(()=>{self._sharedApprovalDialog?.dispose();dom.window.close();});
async function open(){await openVaultApproval(self,fixture(),{
  collect:(_ceremony,{view:render,signal})=>new Promise((resolve,reject)=>{
    view={policy:{op:'factor',factor_id:'password'},done:[],busy:false,password(){resolve({openers:{pw:'07'.repeat(32)},seeds:[seed]})},passkey(){}};
    signal.addEventListener('abort',()=>reject(Error('Approval cancelled.')),{once:true});render(view);
  }),openKey:async(_bundle,openers)=>{assert.equal(openers.pw,'07'.repeat(32));return 'ab'.repeat(32);},
});}
test('review has actual saved value, session link and lifetime; close never decides',async()=>{
  await open();assert.equal(view,null);assert.equal(q('#auth').hidden,true);
  assert.equal(q('#resource').textContent,'deployment.env');
  assert.equal(q('#requester-link').getAttribute('href'),'/session/workspace/requester');
  assert.match(q('#facts').textContent,/15 minutes/);assert.doesNotMatch(q('#review').textContent,/class_id|Release mode|vault.secured/);
  q('#close').click();assert.equal(calls.length,1);
});
for(const ok of [true,false])test('POST acknowledgment stays working until actual delivery '+(ok?'success':'failure'),async()=>{
  execution=ok?{ok:true}:{ok:false,error:'Delivery destination is unavailable'};
  await open();q('#primary').click();q('#password').value='pw';q('#password-form').dispatchEvent(new window.Event('submit',{cancelable:true}));
  await until(()=>reads===1);assert.equal(q('#result-title').textContent,'Sharing saved value…');
  await until(()=>q('#result-title').textContent!=='Sharing saved value…');
  assert.equal(q('#result-title').textContent,ok?'Value shared':'Could not complete the request');
  if(!ok)assert.match(q('#result-copy').textContent,/Delivery destination is unavailable/);
  assert.ok(seed.every(v=>v===0));
  const writes=calls.filter(([,o])=>o?.method==='POST');assert.equal(writes.length,1);
  assert.deepEqual(JSON.parse(writes[0][1].body),{approved:true,content_key:'ab'.repeat(32)});
  assert.equal(calls.some(([url])=>url.includes('?wait=')),false);
});
