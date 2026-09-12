// Governing specification: WORKFLOW-REQUIREMENTS.md. UI-only product operations.
import {spawnSync} from 'node:child_process';
import {randomBytes} from 'node:crypto';
import {mkdirSync,writeFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';

const directory=fileURLToPath(new URL('.',import.meta.url));
const run='workflow-'+Date.now();
const alicePort=process.env.SIM_ALICE_PORT || '25880';
const bobPort=process.env.SIM_BOB_PORT || '25881';
const relayPort=process.env.SIM_RELAY_PORT || '25477';
const onboardingOnly=process.argv.includes('--onboarding-only');
const output=process.argv[2] || '/workspace/output/'+run;
mkdirSync(output,{recursive:true});
const evidence={run,scope:onboardingOnly?'identity-and-organization-onboarding':'membership',started:new Date().toISOString(),checkpoints:[],status:'running'};
function command(bin,args,input){
  const result=spawnSync(bin,args,{input,encoding:'utf8',timeout:180000});
  if(result.status!==0)throw new Error(bin+' '+args[0]+' failed (exit '+result.status+')');
  return result.stdout;
}
function browser(...args){const r=JSON.parse(command('agent-browser',['--session',run+'-alice','--json',...args]));
  if(!r.success)throw new Error(JSON.stringify(r.error));return r.data;}
function js(code){
  const r=spawnSync('agent-browser',['--session',run+'-alice','--json','eval','--stdin'],{input:code,encoding:'utf8',timeout:40000});
  let body;try{body=JSON.parse(r.stdout);}catch{throw new Error('Browser evaluation failed without a response');}
  if(!body.success)throw new Error(JSON.stringify(body.error));return body.data.result;
}
function action(selector,values,success,errorSelector='#onboarding-error'){
  return js(`(async()=>{
    const visible=e=>!!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
    function wait(check,label){return new Promise((resolve,reject)=>{
      const observer=new MutationObserver(probe);
      const timer=setTimeout(()=>finish(new Error('UI timeout: '+label)),20000);
      function finish(error,value){observer.disconnect();clearTimeout(timer);error?reject(error):resolve(value);}
      function probe(){try{const value=check();if(value)finish(null,value);}catch(error){finish(error);}}
      observer.observe(document.documentElement,{subtree:true,childList:true,attributes:true,characterData:true});probe();
    });}
    function one(selector){const found=Array.from(document.querySelectorAll(selector)).filter(visible);
      if(found.length>1)throw new Error('Ambiguous visible control: '+selector);
      return found.length===1&&!found[0].disabled?found[0]:null;}
    for(const [selector,value] of Object.entries(${JSON.stringify(values)})){
      const input=await wait(()=>one(selector),selector);input.value=value;
      input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));
    }
    const control=await wait(()=>one(${JSON.stringify(selector)}),${JSON.stringify(selector)});
    const complete=wait(()=>{
      const error=document.querySelector(${JSON.stringify(errorSelector)});
      if(error&&visible(error)&&error.textContent.trim())throw new Error(error.textContent.trim());
      return one(${JSON.stringify(success)});
    },${JSON.stringify(success)});
    control.click();await complete;return true;
  })()`);
}
function save(name,details){const screenshot=output+'/'+String(evidence.checkpoints.length+1).padStart(2,'0')+'-'+name+'.png';
  browser('screenshot',screenshot);evidence.checkpoints.push({name,at:new Date().toISOString(),screenshot,details});
  writeFileSync(output+'/result.json',JSON.stringify(evidence,null,2));console.log(name+': observed');}
try{
  const env={...process.env,SIM_RELAY_PORT:relayPort,SIM_ALICE_PORT:alicePort,SIM_BOB_PORT:bobPort};
  const startup=spawnSync('docker',['compose','-p',run,'-f',directory+'onboarding.compose.yaml','up','-d','--wait','--wait-timeout','150'],
    {env,encoding:'utf8',timeout:180000});
  writeFileSync(output+'/startup.log',startup.stdout+startup.stderr);
  if(startup.status!==0)throw new Error('Container startup failed');
  evidence.services=command('docker',['compose','-p',run,'-f',directory+'onboarding.compose.yaml','ps','--format','json'])
    .trim().split('\n').map(line=>{const r=JSON.parse(line);return {service:r.Service,health:r.Health,state:r.State};});
  browser('open','http://localhost:'+alicePort+'/');
  save('fresh-start',browser('snapshot','-i'));
  action('[data-testid="welcome-begin"]',{},'#onboarding-name');
  const password=randomBytes(24).toString('base64url');
  action('#onboarding-primary',{'#onboarding-name':'Alice','#onboarding-password':password,'#onboarding-password2':password},
    '[data-testid="onboarding-step-device"]');
  save('alice-identity-created',browser('snapshot','-i'));
  // The product offers Not now for optional device-passkey setup.
  action('#onboarding-notnow',{},'[data-testid="welcome-create"]');
  action('[data-testid="welcome-create"]',{},'#create-org-name');
  action('#create-org-submit',{'#create-org-name':'Simulation Organization'},'[data-testid="create-org-success"]','#create-org-error');
  save('organization-created-ui',browser('snapshot','-i'));
  if(onboardingOnly){
    action('#create-org-finish',{},'[data-testid="welcome-open-workspace"]','#create-org-error');
    save('onboarding-complete',browser('snapshot','-i'));
    evidence.status='passed';
  }else{
    action('#create-org-invite',{},'[data-testid="membership-members"]','#create-org-error');
    save('organization-invite-entry',browser('snapshot','-i'));
    evidence.status='incomplete';
  }
}catch(error){evidence.status='failed';evidence.error=error.message;process.exitCode=1;
  try{browser('screenshot',output+'/failure.png');evidence.failureUI=browser('snapshot','-i');}catch{}
}finally{evidence.finished=new Date().toISOString();writeFileSync(output+'/result.json',JSON.stringify(evidence,null,2));
  console.log(JSON.stringify(evidence,null,2));}
