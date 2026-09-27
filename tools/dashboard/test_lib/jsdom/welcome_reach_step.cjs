// The welcome page's "Reach this dashboard from anywhere" step (auto-1zjk8):
// the state machine of welcomeApp() against the remote-access API, with the
// production script and stubbed fetch/location. Every transition is an API
// result; no inference, no free text but the label.
const fs=require('node:fs');
const assert=require('node:assert/strict');
const script=fs.readFileSync(require('node:path').resolve(__dirname,'../../static/js/pages/welcome.js'),'utf8');
const settle=()=>new Promise(r=>setTimeout(r,0));
const wait=(ms)=>new Promise(r=>setTimeout(r,ms));

async function main(){
  const calls=[];let navigated=null;
  const state={status:{mode:null,origin:null,recorded:false},labelChecks:{},publish:null};
  const sandbox={
    window:{addEventListener(){},dispatchEvent(){}},
    document:{addEventListener(){},visibilityState:'visible'},
    location:{search:'',hash:'',assign(url){navigated=url;},replace(){}},
    URLSearchParams,
    Event:class{constructor(n){this.type=n;}},
    setInterval,clearInterval,setTimeout,clearTimeout,
    fetch:async(url,options={})=>{
      calls.push({url,method:options.method||'GET',body:options.body?JSON.parse(options.body):null});
      const json=async(v)=>v;
      if(url==='/api/identity/status')return{ok:true,json:()=>json({personal_identity:{display_name:'Alice'}})};
      if(url==='/api/orgs')return{ok:true,json:()=>json({orgs:[]})};
      if(url==='/api/network/remote-access/status')return{ok:true,json:()=>json({ok:true,status:state.status})};
      if(url.startsWith('/api/network/remote-access/label/check?label=')){
        const label=decodeURIComponent(url.split('=')[1]);
        return{ok:true,json:()=>json({ok:true,label:state.labelChecks[label]||{ok:true,label,code:'',against:'',reason:'',bound:false}})};
      }
      if(url==='/api/network/remote-access/publish'){
        const body=JSON.parse(options.body);
        if(state.publish)return state.publish(body);
        const row={mode:body.mode,origin:body.mode==='autonomy'?'https://dashboard.alice-0123456789abcdef0123.serve.auto.network':(body.origin||'http://localhost'),published_at:'2026-09-27T18:00:00.000Z'};
        if(body.mode==='autonomy'){row.reservation_id='11111111-1111-4111-8111-111111111111';row.publisher='personal';row.app_label='dashboard';}
        state.status={...row,recorded:true,route_state:'Unavailable',certificate:'pending',advertised:false,gate:'pending',enrollment:'closed',failed_stage:'certificate'};
        return{ok:true,json:()=>json({ok:true,remote_access:row})};
      }
      throw new Error('unexpected fetch '+url);
    },
  };
  const factory=new Function(...Object.keys(sandbox),script+'\nreturn welcomeApp;')(...Object.values(sandbox));
  const app=factory();
  app.$root={dataset:{}};
  await app.init();
  // 1. identity done, nothing recorded: the reach step is current with the default choice
  assert.equal(app.step,2);
  assert.equal(app.reachChoice,'autonomy');
  assert.equal(app.reachCanSubmit(),true);                     // no label is fine (random name)
  assert.equal(app.reachSubmitText(),'Publish on the Autonomy Network');
  // 2. the label question: a look-alike is refused with its reason, a good one is available
  state.labelChecks['aut0n0my']={ok:false,label:'aut0n0my',code:'platform_name',against:'autonomy',reason:'That reads as "autonomy", which belongs to the platform.',bound:false};
  app.reachLabel='aut0n0my';app.checkReachLabel();await wait(300);
  assert.equal(app.reachCanSubmit(),false);
  assert.match(app.reachLabelText(),/belongs to the platform/);
  app.reachLabel='boat-lore';app.checkReachLabel();await wait(300);
  assert.equal(app.reachCanSubmit(),true);
  assert.equal(app.reachLabelText(),'Available: boat-lore');
  // 3. publish: one POST with the mode and the label; the step is done; polling starts
  await app.submitReach();
  const publish=calls.find(c=>c.url==='/api/network/remote-access/publish');
  assert.deepEqual(publish.body,{mode:'autonomy',label:'boat-lore'});
  assert.equal(app.reach.recorded,true);
  assert.equal(app.step,3);
  assert.equal(app.reachTitle(),'Reachable on the Autonomy Network');
  assert.match(app.reachSummary(),/setting up \(certificate\)/);
  await settle();
  // 4. status drives the summary: certificate ok + advertised but gate pending
  state.status={...state.status,certificate:'ok',advertised:true,failed_stage:null};
  await app.pollReach();
  assert.match(app.reachSummary(),/waiting for the passkey gate/);
  assert.equal(app.reachProgress().map(r=>r.tone).join(','),'ok,ok,ok,');
  // 5. gate up with an enrollment URL: the page lands on it, polling stops
  state.status={...state.status,gate:'up',enrollment_url:'https://dashboard.alice-0123456789abcdef0123.serve.auto.network/oauth2/enroll?token=abc'};
  await app.pollReach();
  assert.equal(navigated,state.status.enrollment_url);
  assert.equal(app.reachTimer,null);
  // 6. errors are exact and the step stays current
  const app2=factory();app2.$root={dataset:{}};state.status={mode:null,origin:null,recorded:false};
  await app2.init();
  state.publish=async(body)=>({ok:false,status:409,json:async()=>({ok:false,error:'dashboard_container_unavailable'})});
  await app2.submitReach();
  assert.match(app2.reachError,/not running as a node container/);
  assert.equal(app2.step,2);
  // 7. Tailscale needs an origin; Local only records at once
  state.publish=null;
  app2.reachChoice='tailscale';assert.equal(app2.reachCanSubmit(),false);
  app2.reachOrigin='https://desktop.tail1234.ts.net:8080';assert.equal(app2.reachCanSubmit(),true);
  await app2.submitReach();
  assert.deepEqual(calls[calls.length-1].body,{mode:'tailscale',origin:'https://desktop.tail1234.ts.net:8080'});
  assert.equal(app2.step,3);assert.equal(app2.reachTitle(),'Reachable on your Tailnet');
  assert.equal(app2.reachSummary(),'https://desktop.tail1234.ts.net:8080');
  const app3=factory();app3.$root={dataset:{}};state.status={mode:null,origin:null,recorded:false};
  await app3.init();app3.reachChoice='local';await app3.submitReach();
  assert.deepEqual(calls[calls.length-1].body,{mode:'local'});
  assert.equal(app3.reachTitle(),'This machine only');
  console.log('PASS: welcome reach step: choices, label check, publish, status-driven landing, errors');
}
main().then(()=>process.exit(0)).catch(e=>{console.error(e);process.exit(1);});
