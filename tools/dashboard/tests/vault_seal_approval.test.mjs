import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {openVaultSealApproval, releaseLabel} from '../static/js/components/vault-seal-approval.js';
const {JSDOM}=createRequire(import.meta.url)('jsdom');
let dom,calls,resolved,closed,dialog,depositStatus,depositBody;
const q=s=>document.querySelector('[data-testid=approval-dialog]')?.shadowRoot.querySelector(s);
const tick=()=>new Promise(r=>setTimeout(r,10));
async function until(fn){for(let i=0;i<200&&!fn();i++)await tick();assert.ok(fn(),'expected state; error='+q('#error')?.textContent+' result='+q('#result-title')?.textContent);}
const item=()=>({id:'central-vault-seal-1',actions:['granted','declined'],
  safeReview:{approval_id:'central-vault-seal-1',name:'github.token',key:'autonomy:github.token',tier:'audited',
    tier_label:'Released unattended to authorized sessions',detail:'Personal access token for pushing release tags from CI.',
    requester_label:'auto-real · Release pipeline',replace:false},
  requester:{href:'/session/autonomy-developer/auto-real',byline:'autonomy-developer'}});
test.beforeEach(()=>{
  dom=new JSDOM('<body></body>',{url:'https://example.test'});global.window=dom.window;global.document=dom.window.document;
  window.matchMedia=()=>({matches:false});window.Element.prototype.getAnimations=()=>[];window.Element.prototype.animate=()=>({cancel(){}});
  calls=[];resolved=0;closed=0;depositStatus=201;depositBody={setting_id:'row-abc123',set_id:'autonomy.vault.audited',key:'autonomy:github.token',tier:'audited',name:'github.token'};
  global.fetch=async(url,options)=>{
    const body=options?.body?JSON.parse(options.body):undefined;calls.push([url,options?.method||'GET',body]);
    if(url.startsWith('/api/vault/deposit/'))return {ok:depositStatus<400,status:depositStatus,json:async()=>depositStatus<400?depositBody:{error:'The vault is locked and must be warmed by the operator.'}};
    if(url.endsWith('/approval-decision'))return {ok:true,status:200,json:async()=>({resolution:{outcome:body.outcome}})};
    if(url==='/api/attention/items/central-vault-seal-1')return {ok:true,status:200,json:async()=>({review:{application_result:{approved:true,execution:{ok:true,setting_id:'row-abc123'}}}})};
    throw new Error('unexpected '+url);
  };
});
test.afterEach(()=>{dialog?.dispose();dom.window.close();});
function open(overrides={}){dialog=openVaultSealApproval({...item(),...overrides},{onResolved(){resolved++},onClose(){closed++}});}
test('release labels name the tier in operator words',()=>{
  assert.equal(releaseLabel('audited'),'Unattended, to authorized sessions');
  assert.match(releaseLabel('secured'),/your approval/);
});
test('review shows the ask, the destination, and a masked secret field that gates approval',async()=>{
  open();
  assert.equal(q('#title').textContent,'Vault a secret?');
  assert.equal(q('#intro').textContent,'Personal access token for pushing release tags from CI.');
  assert.equal(q('#resource').textContent,'github.token');
  assert.match(q('#resource-detail').textContent,/Stored as autonomy:github.token/);
  assert.match(q('#facts').textContent,/Unattended, to authorized sessions/);
  assert.equal(q('#requester-link').getAttribute('href'),'/session/autonomy-developer/auto-real');
  assert.equal(q('#secret-entry').hidden,false);
  assert.equal(q('#secret-label-text').textContent,'Secret value');
  assert.ok(q('#secret-value').classList.contains('masked'));
  assert.equal(q('#primary').disabled,true);
  q('#secret-value').value='ghp_secret';q('#secret-value').dispatchEvent(new window.Event('input'));
  assert.equal(q('#primary').disabled,false);
  q('#secret-show').checked=true;q('#secret-show').dispatchEvent(new window.Event('change'));
  assert.equal(q('#secret-value').classList.contains('masked'),false);
  // Nothing was posted by rendering alone.
  assert.equal(calls.length,0);
});
test('approving deposits the value first, then commits a decision that names only the row',async()=>{
  open();
  q('#secret-value').value='ghp_secret';q('#secret-value').dispatchEvent(new window.Event('input'));
  q('#primary').click();
  await until(()=>q('#result-title')?.textContent==='Secret vaulted');
  const posts=calls.filter(([,m])=>m==='POST');
  assert.equal(posts.length,2);
  assert.deepEqual(posts[0],['/api/vault/deposit/central-vault-seal-1','POST',{value:'ghp_secret'}]);
  assert.deepEqual(posts[1],['/api/attention/items/central-vault-seal-1/approval-decision','POST',{outcome:'granted',decision:{setting_id:'row-abc123'}}]);
  assert.equal(JSON.stringify(posts[1][2]).includes('ghp_secret'),false);
  assert.equal(resolved,1);
  assert.equal(q('#secret-value').value,'');
  assert.match(q('#receipt').textContent,/github.token/);
});
test('a refused deposit surfaces the vault message, keeps the typed value for retry, and commits nothing',async()=>{
  depositStatus=423;
  open();
  q('#secret-value').value='ghp_secret';q('#secret-value').dispatchEvent(new window.Event('input'));
  q('#primary').click();
  await until(()=>q('#error')?.hidden===false);
  assert.match(q('#error').textContent,/vault is locked/);
  assert.equal(calls.filter(([u])=>u.endsWith('/approval-decision')).length,0);
  assert.equal(q('#secret-value').value,'ghp_secret');
  assert.equal(q('#secret-value').disabled,false);
  assert.equal(resolved,0);
});
test('declining posts an empty decline and never touches the deposit route',async()=>{
  open();
  q('#secondary').click();
  q('#primary').click();
  await until(()=>q('#result-title')?.textContent==='Request declined');
  const posts=calls.filter(([,m])=>m==='POST');
  assert.deepEqual(posts,[['/api/attention/items/central-vault-seal-1/approval-decision','POST',{outcome:'declined',decision:{}}]]);
  assert.equal(resolved,1);
});
test('a request with no grant action or no approval id renders unavailable',()=>{
  open({actions:['declined']});
  assert.equal(q('#review-unavailable').hidden,false);
  assert.equal(q('#primary').disabled,true);
});
