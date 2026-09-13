// Governing specification: WORKFLOW-REQUIREMENTS.md. UI-only product operations.
import {spawnSync} from 'node:child_process';
import {randomBytes,createHash} from 'node:crypto';
import {mkdirSync,writeFileSync,mkdtempSync,existsSync,readFileSync} from 'node:fs';
import {homedir,tmpdir} from 'node:os';
import {fileURLToPath} from 'node:url';

const directory=fileURLToPath(new URL('.',import.meta.url));
const repository=fileURLToPath(new URL('../../',import.meta.url));
const run='workflow-'+Date.now();
function parsePort(value,name){
  const parsed=Number(value);
  if(!Number.isInteger(parsed)||parsed<1||parsed>65535||String(parsed)!==String(parseInt(value,10))){
    throw new Error(`Invalid ${name}: ${value}`);
  }
  return String(parsed);
}
function findFreePort(){
  const port=command('node',['-e',`const server=require('node:net').createServer();server.listen(0,'0.0.0.0',()=>{const {port}=server.address();server.close(()=>process.stdout.write(String(port)));});`]).trim();
  return parsePort(port,'auto-selected port');
}
function resolvePort(name){
  const explicit=process.env[name];
  return {locked:explicit!==undefined,value:parsePort(explicit ?? findFreePort(),name)};
}
const relay=resolvePort('SIM_RELAY_PORT');
const alice=resolvePort('SIM_ALICE_PORT');
const bob=resolvePort('SIM_BOB_PORT');
let relayPort=relay.value;
let alicePort=alice.value;
let bobPort=bob.value;
const onboardingOnly=process.argv.includes('--onboarding-only');
const output=process.argv[2] || '/workspace/output/'+run;
// Environment-only TLS: one directly trusted server certificate, no CA or
// certificate-error bypass. The private key stays in local temporary storage.
const tlsDirectory=mkdtempSync(tmpdir()+'/invitation-tls-');
const nssDirectory=homedir()+(existsSync(homedir()+'/.pki/nssdb')?'/.pki/nssdb':'/.local/share/pki/nssdb');
process.env.AGENT_BROWSER_IGNORE_HTTPS_ERRORS='false';
mkdirSync(output,{recursive:true});
const evidence={run,scope:onboardingOnly?'identity-and-organization-onboarding':'membership',started:new Date().toISOString(),checkpoints:[],status:'running',steps:[]};
const openedBrowsers=new Set();
function command(bin,args,input,options={}){
  const result=spawnSync(bin,args,{input,encoding:'utf8',timeout:180000,...options});
  if(result.status!==0)throw new Error(bin+' '+args[0]+' failed (exit '+result.status+')');
  return result.stdout;
}
function composeDown(composeFile,project,extraEnv){
  command('docker',['compose','-p',project,'-f',composeFile,'down','--volumes','--remove-orphans','--timeout','10'],undefined,{env:extraEnv});
}
function pruneComposeContainers(project){
  const ids=command('docker',['ps','-aq','--filter',`label=com.docker.compose.project=${project}`]).trim().split('\n').filter(Boolean);
  if(ids.length)command('docker',['rm','-f',...ids]);
}
function withStep(person,step,fn){
  const previous=evidence.lastStep;
  const stepStarted=Date.now();
  evidence.lastStep={person,step,started:new Date(stepStarted).toISOString()};
  try{
    const result=fn();
    evidence.steps.push({person,step,durationMs:Date.now()-stepStarted,status:'ok'});
    return result;
  }catch(error){
    evidence.lastStep={...evidence.lastStep,failedAt:new Date().toISOString(),error:error.message};
    evidence.failureStep=evidence.lastStep;
    evidence.steps.push({person,step,durationMs:Date.now()-stepStarted,status:'failed',error:error.message});
    throw error;
  }finally{
    evidence.lastStep=previous;
  }
}
function browser(person,...args){
  const r=JSON.parse(command('agent-browser',['--session',run+'-'+person,'--json',...args]));
  if(!r.success)throw new Error(JSON.stringify(r.error));
  openedBrowsers.add(person);
  return r.data;
}
function js(person,code){
  const r=spawnSync('agent-browser',['--session',run+'-'+person,'--json','eval','--stdin'],{input:code,encoding:'utf8',timeout:40000});
  let body;try{body=JSON.parse(r.stdout);}catch{throw new Error('Browser evaluation failed without a response');}
  if(!body.success)throw new Error(JSON.stringify(body.error));return body.data.result;
}
function action(person,selector,values,success,errorSelector='#onboarding-error'){
  return js(person,`(async()=>{
    const visible=e=>!!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
    function wait(check,label){return new Promise((resolve,reject)=>{
      const observers=new Map();
      const timer=setTimeout(()=>finish(new Error('UI timeout: '+label)),5000);
      function roots(root=document,out=[]){out.push(root);for(const node of root.querySelectorAll('*'))if(node.shadowRoot)roots(node.shadowRoot,out);return out;}
      function observe(){for(const root of roots())if(!observers.has(root)){const observer=new MutationObserver(probe);observer.observe(root,{subtree:true,childList:true,attributes:true,characterData:true});observers.set(root,observer);}}
      function finish(error,value){for(const observer of observers.values())observer.disconnect();clearTimeout(timer);error?reject(error):resolve(value);}
      function probe(){try{observe();const value=check();if(value)finish(null,value);}catch(error){finish(error);}}
      probe();
    });}
    function all(selector,root=document,out=[]){out.push(...root.querySelectorAll(selector));for(const node of root.querySelectorAll('*'))if(node.shadowRoot)all(selector,node.shadowRoot,out);return out;}
    function one(selector){const found=all(selector).filter(visible);
      if(found.length>1)throw new Error('Ambiguous visible control: '+selector);
      return found.length===1&&!found[0].disabled?found[0]:null;}
    for(const [selector,value] of Object.entries(${JSON.stringify(values)})){
      const input=await wait(()=>one(selector),selector);input.value=value;
      input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));
    }
    const control=await wait(()=>one(${JSON.stringify(selector)}),${JSON.stringify(selector)});
    const complete=wait(()=>{
      const error=all(${JSON.stringify(errorSelector)}).find(visible);
      if(error&&visible(error)&&error.textContent.trim())throw new Error(error.textContent.trim());
      return one(${JSON.stringify(success)});
    },${JSON.stringify(success)});
    control.click();await complete;return true;
  })()`);
}
function waitFor(person,selector,errorSelector='.mem-error'){
  return js(person,`(async()=>{
    const visible=e=>!!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
    function all(selector,root=document,out=[]){out.push(...root.querySelectorAll(selector));for(const node of root.querySelectorAll('*'))if(node.shadowRoot)all(selector,node.shadowRoot,out);return out;}
    function one(selector){return all(selector).find(visible)||null;}
    return new Promise((resolve,reject)=>{const observers=new Map();const timer=setTimeout(()=>finish(new Error('UI timeout: '+${JSON.stringify(selector)})),8000);
      function roots(root=document,out=[]){out.push(root);for(const node of root.querySelectorAll('*'))if(node.shadowRoot)roots(node.shadowRoot,out);return out;}
      function finish(error){for(const observer of observers.values())observer.disconnect();clearTimeout(timer);error?reject(error):resolve(true);}
      function probe(){try{for(const root of roots())if(!observers.has(root)){const observer=new MutationObserver(probe);observer.observe(root,{subtree:true,childList:true,attributes:true,characterData:true});observers.set(root,observer);}const error=one(${JSON.stringify(errorSelector)});if(error&&error.textContent.trim())throw new Error(error.textContent.trim());if(one(${JSON.stringify(selector)}))finish();}catch(error){finish(error);}}probe();});
  })()`);
}
function click(person,selector){return js(person,`(()=>{const e=Array.from(document.querySelectorAll(${JSON.stringify(selector)})).filter(e=>e.getClientRects().length);if(e.length!==1||e[0].disabled)throw new Error('Expected one enabled visible control: '+${JSON.stringify(selector)});e[0].click();return true})()`);}
function textOf(person,selector){return js(person,`(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e||!e.getClientRects().length)throw new Error('Missing visible value: '+${JSON.stringify(selector)});return e.textContent.trim()})()`);}
function setPersonalProfile(person,name,biography){
  const port=person==='alice'?alicePort:bobPort;
  browser(person,'open','http://localhost:'+port+'/account/profile');
  waitFor(person,'[data-testid="profile-name"]','#onboarding-error');
  action(person,'[data-testid="profile-save"]',{
    '[data-testid="profile-name"]':name,
    '[data-testid="profile-bio"]':biography,
  },'[data-testid="profile-saved"]','[data-testid="profile-error"]');
  save(person,person+'-personal-profile-saved',browser(person,'snapshot','-i'));
  browser(person,'open','http://localhost:'+port+'/');
}
function failureContext(person){
  return js(person,`(()=>{try{
    const visible=e=>!!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
    return {
      url:window.location.href,
      title:document.title,
      orgSettings:!!document.querySelector('[data-testid=\"org-settings\"]'),
      membershipRail:!!document.querySelector('[data-testid=\"orgset-rail-membership\"]'),
      identityRows:Array.from(document.querySelectorAll('[data-testid^=\"identity-org-\"]')).map(el=>el.getAttribute('data-testid')),
      errors:Array.from(document.querySelectorAll('.mem-error,#error')).filter(e=>visible(e)&&e.textContent.trim()).map(e=>e.textContent.trim()),
      visibleText:(document.body?(document.body.innerText||'').replace(/\\s+/g,' ').slice(0,1200):'')
    };
  }catch(error){return {error:error.message};}})()`);
}
function openOrganizationSettings(person,organization='simulation-organization'){
  // Follow the visible organization-selection workflow. Do not inject the
  // organization slug through the URL or call the settings controller.
  waitFor(person,'[data-testid=\"identity-trigger\"]','#onboarding-error, .mem-error');
  browser(person,'click','[data-testid=\"identity-trigger\"]');
  waitFor(person,`[data-testid=\"identity-org-${organization}\"]`,'#onboarding-error, .mem-error');
  browser(person,'click',`[data-testid=\"identity-org-${organization}\"]`);
  waitFor(person,'[data-testid=\"org-settings\"]','.mem-error, #error:not([hidden])');
}
function save(person,name,details){const screenshot=output+'/'+String(evidence.checkpoints.length+1).padStart(2,'0')+'-'+name+'.png';
  browser(person,'screenshot',screenshot);evidence.checkpoints.push({name,person,at:new Date().toISOString(),screenshot,details});
  writeFileSync(output+'/result.json',JSON.stringify(evidence,null,2));console.log(name+': observed');}
try{
  // One relay address reachable from both browser host and dashboard containers.
  const relayHost=command('docker',['network','inspect','bridge','--format','{{(index .IPAM.Config 0).Gateway}}']).trim();
  process.env.SIM_RELAY_HOST=relayHost;
  const relayOrigin='https://'+relayHost+':'+relayPort;
  const preparationStarted=Date.now();
  // Source is mounted directly. Rebuild only when the installed runtime's
  // dependency declarations differ (or the runtime image is missing).
  const runtimeFiles=['deploy/requirements.txt','deploy/Dockerfile'];
  const expected=runtimeFiles.map(path=>createHash('sha256').update(readFileSync(repository+path)).digest('hex'));
  const installed=spawnSync('docker',['run','--rm','--entrypoint','sha256sum','autonomy-node:onboarding',
    ...runtimeFiles.map(path=>'/app/'+path)],{encoding:'utf8',timeout:30000});
  const actual=(installed.stdout||'').trim().split('\n').map(line=>line.split(/\s+/)[0]);
  if(installed.status!==0 || JSON.stringify(actual)!==JSON.stringify(expected)){
  // The production Dockerfile needs a real .git directory, not a worktree
  // pointer. Maintain this build-only checkout automatically; copy current
  // files (including uncommitted edits), never product data. Docker caches
  // unchanged layers. Callers need only this script, not manual build steps.
  console.log('Preparing current worktree and building simulation image...');
  const buildDirectory=tmpdir()+'/autonomy-workflow-build-'+createHash('sha256').update(repository).digest('hex').slice(0,16);
  if(!existsSync(buildDirectory+'/.git'))command('git',['clone','--local','--no-hardlinks','--no-checkout','--quiet',repository,buildDirectory]);
  const revision=command('git',['-C',repository,'rev-parse','HEAD']).trim();
  if(command('git',['-C',buildDirectory,'rev-parse','HEAD']).trim()!==revision){
    command('git',['-C',buildDirectory,'fetch','--quiet',repository,'HEAD']);
    command('git',['-C',buildDirectory,'update-ref','HEAD',revision]);
  }
  command('rsync',['-a','--delete','--exclude=.git','--exclude=.venv','--exclude=node_modules',
    '--exclude=data','--exclude=.browser_profile','--exclude=__pycache__','--exclude=.pytest_cache',repository,buildDirectory+'/']);
  const build=spawnSync('docker',['build','-f',buildDirectory+'/deploy/Dockerfile','-t','autonomy-node:onboarding',buildDirectory],
    {encoding:'utf8',timeout:1200000,maxBuffer:16*1024*1024});
  writeFileSync(output+'/build.log',(build.stdout||'')+(build.stderr||''));
  if(build.status!==0)throw new Error('Image build failed; see '+output+'/build.log');
  }
  // Compile the generated CSS once, not in two container watchers writing
  // the checkout. All product source remains read-only inside the simulation.
  command('tailwindcss',['--cwd',repository+'tools/dashboard','-i','tailwind.input.css',
    '-o','static/tailwind.css','--minify']);
  mkdirSync(repository+'orgs',{recursive:true});
  process.env.SIM_SOURCE_DIR=repository;
  evidence.build={source:repository,mode:'bind-mounted-current-worktree',includesUncommittedChanges:true,
    image:command('docker',['image','inspect','autonomy-node:onboarding','--format','{{.Id}}']).trim()};
  evidence.preparationMs=Date.now()-preparationStarted;
  console.log('Current source ready in '+evidence.preparationMs+'ms; starting services.');
  command('openssl',['req','-x509','-newkey','rsa:2048','-sha256','-noenc','-days','7',
    '-keyout',tlsDirectory+'/server.key','-out',tlsDirectory+'/server.crt','-subj','/CN=localhost',
    '-addext','subjectAltName=DNS:localhost,DNS:relay,IP:127.0.0.1,IP:'+relayHost,
    '-addext','basicConstraints=critical,CA:FALSE',
    '-addext','keyUsage=critical,digitalSignature,keyEncipherment','-addext','extendedKeyUsage=serverAuth']);
  mkdirSync(nssDirectory,{recursive:true});
  if(!existsSync(nssDirectory+'/cert9.db'))command('certutil',['-N','-d','sql:'+nssDirectory,'--empty-password']);
  command('certutil',['-A','-d','sql:'+nssDirectory,'-t','P,,','-n',run,'-i',tlsDirectory+'/server.crt']);
  const composeFile=directory+'onboarding.compose.yaml';
  const composeEnv=()=>({...process.env,SIM_SOURCE_DIR:repository,SIM_TLS_DIR:tlsDirectory,SIM_RELAY_HOST:process.env.SIM_RELAY_HOST,SIM_RELAY_PORT:relayPort,SIM_ALICE_PORT:alicePort,SIM_BOB_PORT:bobPort});
  const startupLogLines=[];
  let startupAttempts=0;
  while(true){
    startupAttempts+=1;
    const startup=spawnSync('docker',['compose','-p',run,'-f',composeFile,'up','-d','--wait','--wait-timeout','150'],{env:composeEnv(),encoding:'utf8',timeout:180000});
    startupLogLines.push(`--- compose up attempt ${startupAttempts} ---\n${startup.stdout+startup.stderr}`);
    writeFileSync(output+'/startup.log',startupLogLines.join('\n\n'));
    if(startup.status===0)break;
    const startupLog=startup.stdout+startup.stderr;
    const canRetryPortConflict=!relay.locked&&!alice.locked&&!bob.locked&&startupLog.includes('address already in use')&&startupAttempts<4;
    if(!canRetryPortConflict)throw new Error('Container startup failed');
    relayPort=findFreePort();
    alicePort=findFreePort();
    bobPort=findFreePort();
    composeDown(composeFile,run,composeEnv());
  }
  evidence.startupAttempts=startupAttempts;
  evidence.ports={relay:relayPort,alice:alicePort,bob:bobPort,locked:{relay:relay.locked,alice:alice.locked,bob:bob.locked}};
  process.env.SIM_RELAY_PORT=relayPort;
  process.env.SIM_ALICE_PORT=alicePort;
  process.env.SIM_BOB_PORT=bobPort;
  evidence.servicesReadyMs=Date.now()-preparationStarted;
  console.log('Fresh services healthy in '+evidence.servicesReadyMs+'ms total.');
  process.env.SIM_TLS_DIR=tlsDirectory;
  evidence.services=command('docker',['compose','-p',run,'-f',composeFile,'ps','--format','json'])
    .trim().split('\n').map(line=>{const r=JSON.parse(line);return {service:r.Service,health:r.Health,state:r.State};});
  command('curl',['--fail','--silent','--show-error','--cacert',tlsDirectory+'/server.crt',relayOrigin+'/healthz']);
  browser('alice','open',relayOrigin+'/healthz');
  const tlsPage=browser('alice','snapshot');
  if(JSON.stringify(tlsPage).includes('Your connection is not private'))throw new Error('Browser did not trust the relay certificate');
  evidence.tls={selfSigned:true,ca:false,verification:'curl and browser',publicCertificate:tlsDirectory+'/server.crt'};
  withStep('alice','open alice home',()=>browser('alice','open','http://localhost:'+alicePort+'/'));
  withStep('alice','save fresh start',()=>save('alice','fresh-start',browser('alice','snapshot','-i')));
  withStep('alice','begin alice onboarding',()=>action('alice','[data-testid="welcome-begin"]',{},'#onboarding-name'));
  const alicePassword=withStep('alice','create alice password',()=>randomBytes(24).toString('base64url'));
  withStep('alice','submit alice identity',()=>action('alice','#onboarding-primary',{'#onboarding-name':'Alice','#onboarding-password':alicePassword,'#onboarding-password2':alicePassword},
    '[data-testid="onboarding-step-device"]'));
  withStep('alice','save alice-identity-created',()=>save('alice','alice-identity-created',browser('alice','snapshot','-i')));
  // The product offers Not now for optional device-passkey setup.
  withStep('alice','skip alice device setup',()=>action('alice','#onboarding-notnow',{},'[data-testid="welcome-create"]'));
  withStep('alice','save alice personal profile',()=>setPersonalProfile('alice','Alice','Organization founder'));
  withStep('alice','start org create',()=>action('alice','[data-testid="welcome-create"]',{},'#create-org-name'));
  withStep('alice','submit org name',()=>action('alice','#create-org-submit',{'#create-org-name':'Simulation Organization'},'.or-in-bare','#create-org-error'));
  withStep('alice','confirm org creation',()=>action('alice','.or-ok',{'.or-in-bare':alicePassword},'[data-testid="create-org-success"]','#create-org-error'));
  withStep('alice','save organization-created-and-founded',()=>save('alice','organization-created-and-founded',browser('alice','snapshot','-i')));
  if(onboardingOnly){
    withStep('alice','finish onboarding',()=>action('alice','#create-org-finish',{},'[data-testid="welcome-open-workspace"]','#create-org-error'));
    withStep('alice','save onboarding-complete',()=>save('alice','onboarding-complete',browser('alice','snapshot','-i')));
    evidence.status='passed';
  }else{
    withStep('alice','open settings panel',()=>browser('alice','click','#create-org-goto-settings'));
    withStep('alice','open organization settings',()=>openOrganizationSettings('alice'));
    withStep('alice','open membership rail',()=>action('alice','[data-testid="orgset-rail-membership"]',{},'[data-testid="membership-members"]'));
    withStep('alice','open invites tab',()=>action('alice','button[data-tab="invites"]',{},'[data-action="open-mint"]'));
    withStep('alice','open mint form',()=>action('alice','[data-action="open-mint"]',{},'[data-action="mint"]'));
    withStep('alice','set invite label and continue',()=>action('alice','[data-action="mint"]',{'[data-mint-label]':'Bob invitation'},'.or-in-bare','.mem-error'));
    withStep('alice','acknowledge password prompt',()=>action('alice','.or-ok',{'.or-in-bare':alicePassword},'[data-testid="approval-dialog"]','.mem-error'));
    withStep('alice','show publish password',()=>action('alice','#primary',{},'#password','.mem-error'));
    withStep('alice','submit publish password',()=>action('alice','.verify',{'#password':alicePassword},'#result[aria-busy="false"] #result-title','.mem-error, #error:not([hidden])'));
    const publicationResult=js('alice',`document.querySelector('[data-testid="approval-dialog"]').shadowRoot.querySelector('#result-title').textContent`);
    if(publicationResult!=='Link published')throw new Error('Publish approval failed: '+js('alice',`document.querySelector('[data-testid="approval-dialog"]').shadowRoot.querySelector('#result-copy').textContent`));
    withStep('alice','show invitation code',()=>action('alice','#primary',{},'.mem-link-code','.mem-error'));
    const invitation=textOf('alice','.mem-link-code');
    withStep('alice','finish mint',()=>action('alice','[data-action="finish-mint"]',{},'[data-invite]','.mem-error'));
    withStep('alice','save invitation-published',()=>save('alice','invitation-published',browser('alice','snapshot','-i')));

    withStep('bob','open bob home',()=>browser('bob','open','http://localhost:'+bobPort+'/'));
    withStep('bob','begin bob onboarding',()=>action('bob','[data-testid="welcome-begin"]',{},'#onboarding-name'));
    const bobPassword=withStep('bob','create bob password',()=>randomBytes(24).toString('base64url'));
    withStep('bob','submit bob identity',()=>action('bob','#onboarding-primary',{'#onboarding-name':'Bob','#onboarding-password':bobPassword,'#onboarding-password2':bobPassword},
      '[data-testid="onboarding-step-device"]'));
    withStep('bob','skip bob device setup',()=>action('bob','#onboarding-notnow',{},'[data-testid="welcome-join"]'));
    withStep('bob','save bob personal profile',()=>setPersonalProfile('bob','Bob','Joining member'));
    withStep('bob','open invite input',()=>browser('bob','click','[data-testid="welcome-join"]'));
    withStep('bob','wait invite input',()=>waitFor('bob','[data-testid="invite-input"]','#paste-hint'));
    withStep('bob','submit invite code',()=>action('bob','[data-testid="invite-next"]',{'[data-testid="invite-input"]':invitation},'[data-testid="invite-accept"]','#paste-hint'));
    withStep('bob','save invitation-previewed',()=>save('bob','invitation-previewed',browser('bob','snapshot','-i')));
    withStep('bob','accept invite',()=>action('bob','[data-testid="invite-accept"]',{},'.or-in-bare','#accept-hint'));
    withStep('bob','submit bob password',()=>action('bob','.or-ok',{'.or-in-bare':bobPassword},'#waiting-block:not(.hidden)','#accept-hint'));
    withStep('bob','save join-request-sent',()=>save('bob','join-request-sent',browser('bob','snapshot','-i')));

    withStep('alice','wait for claim',()=>waitFor('alice','[data-claim]','.mem-error'));
    withStep('alice','approve invite claim',()=>action('alice','[data-claim] [data-action="approve"]',{},'.or-in-bare','.mem-error'));
    withStep('alice','confirm claim with password',()=>action('alice','.or-ok',{'.or-in-bare':alicePassword},'.mem-word.good','.mem-error'));
    withStep('alice','save join-request-approved',()=>save('alice','join-request-approved',browser('alice','snapshot','-i')));
    withStep('bob','done screen',()=>action('bob','.or-ok',{'.or-in-bare':bobPassword},'#done-block:not(.hidden)','#accept-hint'));
    withStep('bob','save invitation-complete',()=>save('bob','invitation-complete',browser('bob','snapshot','-i')));
    evidence.status='passed';
  }
}catch(error){
  evidence.status='failed';
  evidence.error=error.message;
  process.exitCode=1;
  evidence.failureContext={};
  for(const person of ['alice','bob'])try{
    const secretVisible=js(person,`Array.from(document.querySelectorAll('.mem-link-code, #invite-input')).some(e=>e.getClientRects().length&&(e.value||e.textContent).includes('#'))`);
    if(!secretVisible){
      browser(person,'screenshot',output+'/failure-'+person+'.png');
      evidence['failureUI_'+person]=browser(person,'snapshot','-i');
    }
    evidence.failureContext[person]=failureContext(person);
  }catch(errorCapture){
    evidence.failureContext[person]={captureError:errorCapture.message};
  }
}finally{
  evidence.finished=new Date().toISOString();
  evidence.durationMs=Date.parse(evidence.finished)-Date.parse(evidence.started);
  evidence.cleanup={keptRunning:process.env.SIM_KEEP_RUNNING==='1',errors:[]};
  if(!evidence.cleanup.keptRunning){
    for(const person of openedBrowsers)try{browser(person,'close');}catch(error){evidence.cleanup.errors.push(error.message);}
    try{
      const composeFile=directory+'onboarding.compose.yaml';
      const composeEnv={...process.env,SIM_SOURCE_DIR:repository,SIM_TLS_DIR:tlsDirectory,SIM_RELAY_HOST:process.env.SIM_RELAY_HOST||'127.0.0.1',SIM_RELAY_PORT:relayPort,SIM_ALICE_PORT:alicePort,SIM_BOB_PORT:bobPort};
      composeDown(composeFile,run,composeEnv);
      pruneComposeContainers(run);
    }catch(error){evidence.cleanup.errors.push(error.message);}
    if(evidence.cleanup.errors.length)evidence.status='failed';
    if(evidence.cleanup.errors.length)process.exitCode=1;
  }
  writeFileSync(output+'/result.json',JSON.stringify(evidence,null,2));
  console.log(JSON.stringify(evidence,null,2));}
