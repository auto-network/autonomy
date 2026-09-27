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
  const access=root.querySelector('[data-access]');
  assert.deepEqual([...access.options].map(o=>o.textContent),['Public','Personal (passkey)','Org (OIDC)']);
  assert.equal(access.querySelector('[value="personal"]').disabled,true);
  access.value='oidc';access.dispatchEvent(new w.Event('change'));await settle();await settle();
  assert.equal(writes[1].body.access_mode,'oidc');
  assert.equal(root.querySelector('[data-access]').value,'oidc');
  w.close();console.log('PASS: OIDC prerequisite, exact callback list, setup, default and service selection');
}
main().catch(e=>{console.error(e);process.exitCode=1});
