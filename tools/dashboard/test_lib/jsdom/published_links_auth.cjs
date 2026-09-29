// Exercise the production renderer and real form payloads, not a second UI.
const {JSDOM}=require('jsdom');
const fs=require('node:fs');
const assert=require('node:assert/strict');
const script=fs.readFileSync(require('node:path').resolve(__dirname,'../../static/js/published-links.js'),'utf8');
const settle=()=>new Promise(r=>setTimeout(r,0));
async function main(){
  const dom=new JSDOM('<body></body>',{runScripts:'dangerously',url:'https://dashboard.test'}),w=dom.window;
  const data={zones:[],services:[],shares:[],authentication:{configured:false,default_access:'public'}};
  const writes=[];let registration;
  w.AutonomyOrgSettings={register(r){registration=r}};
  w.fetch=async(url,options={})=>{
    if(options.method==='PUT'){
      const body=JSON.parse(options.body);writes.push({url,body});
      if(url==='/api/network/service-auth'){data.authentication={...body,configured:true};delete data.authentication.client_secret;}
      else data.services[0].target={...data.services[0].target,...body};
    }
    return {ok:true,status:200,json:async()=>url.endsWith('/status')?{status:{state:'Live'}}:data};
  };
  w.eval(script);
  let root=await registration.render('anchore',{});w.document.body.appendChild(root);await settle();
  root.querySelector('[data-oidc="open"]').click();
  assert.match(root.textContent,/Add a custom domain before setting up OIDC/);
  assert.equal(root.querySelector('[data-oidc-form]'),null);
  root.remove();
  data.zones=[{zone:'anchore.serve.auto.network',state:'active'},{zone:'services.example.com',state:'active'}];
  data.services=[{reservation_id:'hello',app_label:'hello',zone:'anchore.serve.auto.network',origin:'https://hello.anchore.serve.auto.network',state:'active',target:{session_id:'session-a',port:8123,access_mode:'public'}}];
  root=await registration.render('anchore',{});w.document.body.appendChild(root);await settle();
  root.querySelector('[data-oidc="open"]').click();
  const uris=[...root.querySelectorAll('[data-copy]')].map(e=>e.dataset.copy);
  assert(uris.includes('https://*.anchore.serve.auto.network/oauth2/callback'));
  assert(uris.includes('https://*.services.example.com/oauth2/callback'));
  for(const [field,value] of Object.entries({issuer:'https://example.okta.com',client_id:'client',secret:'private'})){
    const input=root.querySelector('[data-oidc-field="'+field+'"]');input.value=value;input.dispatchEvent(new w.Event('input'));
  }
  root.querySelector('[data-oidc-form]').dispatchEvent(new w.Event('submit',{cancelable:true}));await settle();await settle();
  assert.equal(writes[0].body.default_access,'oidc');
  assert.equal(writes[0].body.client_secret,'private');
  assert.match(root.textContent,/OIDC configured/);
  assert.equal(root.querySelector('[data-oidc-field="secret"]').value,'');
  const card=()=>root.querySelector('[data-service]');
  assert.equal(card().querySelector('[data-access]'),null);
  assert.equal(card().querySelector('[data-action="rename"]').closest('.pl-section-heading').textContent,'Hosted at');
  assert.equal(card().querySelector('.pl-footer [data-action="rename"]'),null);
  card().querySelector('[data-action="rename"]').click();
  assert(card().querySelector('.pl-hosted .pl-address-editor [data-field="app"]'));
  card().querySelector('[data-action="cancel"]').click();
  card().querySelector('[data-action="access-edit"]').click();
  const access=card().querySelector('.pl-access-editor');
  assert.deepEqual([...access.querySelectorAll('b')].map(o=>o.textContent),['Public','Personal','Organization (OIDC)']);
  assert.equal(access.querySelector('[value="personal"]').disabled,false);   // auto-z98nc: one gate passkey covers Personal services too
  assert.match(access.textContent,/One gate passkey covers the dashboard and every Personal service/);
  assert.equal(card().querySelector('.pl-access-summary .pl-eyebrow').textContent,'Access control');
  access.querySelector('[value="oidc"]').checked=true;
  card().querySelector('[data-action="access-cancel"]').click();
  assert.equal(writes.length,1);
  assert.match(card().querySelector('.pl-access-heading').textContent,/Public/);
  card().querySelector('[data-action="access-edit"]').click();
  card().querySelector('[value="oidc"]').checked=true;
  card().querySelector('[data-action="access-save"]').click();await settle();await settle();
  assert.equal(writes[1].body.access_mode,'oidc');
  assert.equal(card().querySelector('.pl-access-heading').textContent,'Organization (OIDC)');
  assert.equal(card().querySelector('.pl-access-editor'),null);
  w.close();console.log('PASS: OIDC prerequisite, exact callback list, setup, default and service selection');
}
main().catch(e=>{console.error(e);process.exitCode=1});
