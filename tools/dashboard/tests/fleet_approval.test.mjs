import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {openFleetApproval} from '../static/js/components/fleet-approval.js';
import {mintPasswordArmor} from '../static/js/ceremony/root-factor-policy.js';
import {bytesToHex} from '../static/js/ceremony/primitives.js';
const {JSDOM}=createRequire(import.meta.url)('jsdom');
let dom,dialog,armor,rootPub,calls;
const q=s=>document.querySelector('[data-testid=approval-dialog]')?.shadowRoot.querySelector(s);
async function until(fn){for(let i=0;i<300&&!fn();i++)await new Promise(r=>setTimeout(r,10));assert.ok(fn(),q('#error')?.textContent+' / '+q('#result')?.textContent);}
test.before(async()=>{
  const root=await crypto.subtle.generateKey('Ed25519',true,['sign','verify']);
  const seed=new Uint8Array(await crypto.subtle.exportKey('pkcs8',root.privateKey)).slice(-32);
  rootPub=bytesToHex(new Uint8Array(await crypto.subtle.exportKey('raw',root.publicKey)));
  armor=await mintPasswordArmor({rootSeed:seed,rootPub,password:'pw',factorId:'pw',iterations:10000});seed.fill(0);
});
test.beforeEach(()=>{
  dom=new JSDOM('<body></body>',{url:'https://example.test'});global.window=dom.window;global.document=dom.window.document;
  window.matchMedia=()=>({matches:false});window.Element.prototype.getAnimations=()=>[];window.Element.prototype.animate=()=>({cancel(){}});
  calls=[];
  global.fetch=async(url,options)=>{
    calls.push({url,body:options?.body?JSON.parse(options.body):null});
    let value;
    if(url==='/api/identity/status')value={passkeys:[]};
    else if(url==='/api/identity/personal')value={armored_private_key:armor,root_pub:rootPub};
    else if(url==='/api/identity/factor-policy')value={armor_version:3,factors:[{factor_id:'pw',label:'Password'}]};
    else if(url.endsWith('/approval-decision'))value={resolution:{outcome:JSON.parse(options.body).outcome}};
    else if(url==='/api/fleet/runtime')value={ok:true};
    else value={review:{application_result:{execution:{ok:true}}}};
    return {ok:true,json:async()=>value};
  };
});
test.afterEach(()=>{dialog?.dispose();dom.window.close();});
function open(bootstrap=false){
  dialog=openFleetApproval({id:'central-test',actions:['granted','declined'],safeReview:{verification_code:'123 456',fleet:{
    request:{machine_id:'ab'.repeat(32),personal_root_pub:rootPub,invite_id:'cd'.repeat(32)},
    channel_binding:'ef'.repeat(32),personal_root_pub:rootPub,issued_at:Date.now(),
    local_bootstrap_machine_id:bootstrap?'12'.repeat(32):null,
  }}},{onResolved(){},onClose(){}});
}
test('review starts with a blank name; closing does not decide',()=>{
  open();assert.equal(q('#machine-name').value,'');assert.equal(q('#machine-name').maxLength,80);
  assert.equal(q('#auth').hidden,true);assert.equal(q('#primary').disabled,true);
  q('#close').click();assert.equal(calls.length,0);
});
for(const bootstrap of [false,true])test('public decision and local runtime separation: '+bootstrap,async()=>{
  open(bootstrap);q('#machine-name').value='Studio Mac';q('#machine-name').dispatchEvent(new window.Event('input'));
  q('#code-matches').checked=true;q('#code-matches').dispatchEvent(new window.Event('change'));
  q('#primary').click();await until(()=>q('#auth').hidden===false&&!q('input[type=password]').disabled);
  const input=q('input[type=password]');input.value='pw';input.dispatchEvent(new window.Event('input'));q('.verify').click();
  await until(()=>q('#result-title').textContent==='Machine added');
  const decision=calls.find(c=>c.url.endsWith('/approval-decision'));
  assert.equal(decision.body.decision.machine_name,'Studio Mac');
  assert.equal(JSON.stringify(decision.body).includes('process_private_seed'),false);
  assert.equal('local_runtime' in decision.body.decision,false);
  const runtime=calls.find(c=>c.url==='/api/fleet/runtime');
  assert.equal(!!runtime,bootstrap);
  if(bootstrap){assert.equal(runtime.body.process_private_seed.length,64);assert.ok(calls.indexOf(runtime)>calls.indexOf(decision));}
  assert.match(q('#result').textContent,/Studio Mac/);
  assert.equal(q('.receipt-link').getAttribute('href'),'/fleet');
});

test('runtime activation failure does not show a successful completion',async()=>{
  const originalFetch=global.fetch;
  global.fetch=async(url,options)=>url==='/api/fleet/runtime'
    ? {ok:false,json:async()=>({error:'activation failed'})}
    : originalFetch(url,options);
  open(true);q('#machine-name').value='Studio Mac';q('#machine-name').dispatchEvent(new window.Event('input'));
  q('#code-matches').checked=true;q('#code-matches').dispatchEvent(new window.Event('change'));
  q('#primary').click();await until(()=>q('#auth').hidden===false&&!q('input[type=password]').disabled);
  const input=q('input[type=password]');input.value='pw';input.dispatchEvent(new window.Event('input'));q('.verify').click();
  await until(()=>q('#result').textContent.includes('needs unlocking'));
  assert.notEqual(q('#result-title').textContent,'Machine added');
});
