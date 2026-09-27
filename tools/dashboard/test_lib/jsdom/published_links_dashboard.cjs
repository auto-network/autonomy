// The node's own dashboard as a published Service (auto-e21gu): the card names
// it, its access is the personal passkey with no editor, and a session card
// keeps its editor. Exercises the production renderer, not a second UI.
const {JSDOM}=require('jsdom');
const fs=require('node:fs');
const assert=require('node:assert/strict');
const script=fs.readFileSync(require('node:path').resolve(__dirname,'../../static/js/published-links.js'),'utf8');
const settle=()=>new Promise(r=>setTimeout(r,0));
async function main(){
  const dom=new JSDOM('<body></body>',{runScripts:'dangerously',url:'https://dashboard.test'}),w=dom.window;
  const data={zones:[{zone:'anchore.serve.auto.network',state:'active'}],shares:[],authentication:{configured:false,default_access:'public'},services:[
    {reservation_id:'dash',app_label:'dash',zone:'anchore.serve.auto.network',origin:'https://dash.anchore.serve.auto.network',state:'active',
     session_title:'This dashboard',session_local:true,remote:false,target:{kind:'dashboard',session_id:null,port:8081,access_mode:'personal'}},
    {reservation_id:'hello',app_label:'hello',zone:'anchore.serve.auto.network',origin:'https://hello.anchore.serve.auto.network',state:'active',
     session_title:'Hello',session_local:true,remote:false,target:{kind:'session',session_id:'session-a',port:8123,access_mode:'public'}},
  ]};
  let registration;
  w.AutonomyOrgSettings={register(r){registration=r}};
  w.fetch=async(url)=>({ok:true,status:200,json:async()=>url.endsWith('/status')?{status:{state:'Live'}}:data});
  w.eval(script);
  const root=await registration.render('anchore',{});w.document.body.appendChild(root);await settle();
  const dash=root.querySelector('[data-service="dash"]');
  assert(dash,'the dashboard service renders a card');
  assert.match(dash.textContent,/This dashboard/);
  assert.match(dash.textContent,/dashboard · port 8081/);
  assert.match(dash.textContent,/Access control/);
  assert.match(dash.textContent,/Personal/);
  assert.match(dash.textContent,/Your personal passkey is required for access\./);
  assert.equal(dash.querySelector('[data-action="access-edit"]'),null,'no access editor on the dashboard card');
  assert.equal(dash.querySelector('.pl-access-editor'),null);
  const hello=root.querySelector('[data-service="hello"]');
  assert.match(hello.textContent,/session-a/);
  assert(hello.querySelector('[data-action="access-edit"]'),'a session card keeps its access editor');
  console.log('PASS: published links: dashboard card names this dashboard, Personal access, no editor');
}
main().catch(e=>{console.error(e);process.exit(1);});
