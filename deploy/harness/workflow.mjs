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
// SIM_ISOLATE_DASHBOARDS=1: the dashboards share no network with each other,
// only with the relay, so organization sync must use the relay fallback.
const isolateDashboards=process.env.SIM_ISOLATE_DASHBOARDS==='1';
function composeArgs(project){
  const files=['-f',directory+'onboarding.compose.yaml'];
  if(isolateDashboards)files.push('-f',directory+'onboarding.isolated.compose.yaml');
  return ['compose','-p',project,...files];
}
function composeDown(composeFile,project,extraEnv){
  command('docker',[...composeArgs(project),'down','--volumes','--remove-orphans','--timeout','10'],undefined,{env:extraEnv});
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
function waitValue(person,selector,expected,prop='textContent',timeoutMs=8000){
  return js(person,`(()=>{const sel=${JSON.stringify(selector)},expected=${JSON.stringify(expected)};
    const read=e=>String(${prop==='value'?'e.value':'e.textContent'}).trim();
    return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{observer.disconnect();const e=document.querySelector(sel);
        reject(new Error('UI timeout: '+sel+' is '+(e?JSON.stringify(read(e)):'missing')+', expected '+JSON.stringify(expected)));},${timeoutMs});
      const observer=new MutationObserver(probe);
      function probe(){const e=document.querySelector(sel);if(e&&e.getClientRects().length&&read(e)===expected){observer.disconnect();clearTimeout(timer);resolve(true);}}
      observer.observe(document,{subtree:true,childList:true,attributes:true,characterData:true});probe();});})()`);
}
// Lock the dashboard from the identity panel and sign on again with the
// password: the product's own root ceremony, which is where membership
// checkpoints are published or adopted and org sync credentials are minted.
function lockAndUnlock(person,password){
  // The organization settings dialog is modal; close it before reaching the
  // identity panel in the header.
  if(js(person,`!!document.querySelector('[data-testid="orgset-close"]')`)){
    browser(person,'click','[data-testid="orgset-close"]');
  }
  waitFor(person,'[data-testid="identity-trigger"]','#onboarding-error, .mem-error');
  browser(person,'click','[data-testid="identity-trigger"]');
  waitFor(person,'[data-testid="identity-action-lock"]','.mem-error');
  browser(person,'click','[data-testid="identity-action-lock"]');
  // Locking navigates to /unlock; wait for that navigation itself before
  // observing the new document.
  browser(person,'wait','--fn',"location.pathname.startsWith('/unlock')");
  waitFor(person,'#unlock-password, #unlock-use-password','[data-testid="unlock-loaderror"]');
  js(person,`(()=>{const b=document.getElementById('unlock-use-password');if(b&&b.getClientRects().length)b.click();return true})()`);
  waitFor(person,'#unlock-password','[data-testid="unlock-loaderror"]');
  js(person,`(()=>{const i=document.getElementById('unlock-password');i.value=${JSON.stringify(password)};i.dispatchEvent(new Event('input',{bubbles:true}));return true})()`);
  browser(person,'click','#unlock-primary');
  // A successful sign-on navigates back to the dashboard.
  browser(person,'wait','--fn',"!location.pathname.startsWith('/unlock')");
  waitFor(person,'[data-testid="identity-trigger"]','#unlock-error, [data-testid="unlock-error"]');
}
function memberRows(person){
  return js(person,`Array.from(document.querySelectorAll('[data-member]')).map(e=>e.textContent.trim().replace(/\s+/g,' ').slice(0,80))`);
}
function click(person,selector){return js(person,`(()=>{const e=Array.from(document.querySelectorAll(${JSON.stringify(selector)})).filter(e=>e.getClientRects().length);if(e.length!==1||e[0].disabled)throw new Error('Expected one enabled visible control: '+${JSON.stringify(selector)});e[0].click();return true})()`);}
function textOf(person,selector){return js(person,`(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e||!e.getClientRects().length)throw new Error('Missing visible value: '+${JSON.stringify(selector)});return e.textContent.trim()})()`);}
function setPersonalProfile(person,name,biography,photo){
  const port=person==='alice'?alicePort:bobPort;
  browser(person,'open','http://localhost:'+port+'/account/profile');
  waitFor(person,'[data-testid="profile-name"]','#onboarding-error');
  action(person,'[data-testid="profile-save"]',{
    '[data-testid="profile-name"]':name,
    '[data-testid="profile-bio"]':biography,
  },'[data-testid="profile-saved"]','[data-testid="profile-error"]');
  if(photo){
    // The photo path: the file chooser (driven by the browser's file-upload
    // primitive on the editor's own input), the crop editor, then the stored
    // icon rendered back in the editor.
    browser(person,'upload','input[type="file"]',photo);
    waitFor(person,'[data-testid="profile-use-photo"]','[data-testid="profile-photo-error"]');
    action(person,'[data-testid="profile-use-photo"]',{},'[data-testid="profile-photo"]','[data-testid="profile-photo-error"]');
    save(person,person+'-personal-profile-photo',browser(person,'snapshot','-i'));
  }else{
    save(person,person+'-personal-profile-saved',browser(person,'snapshot','-i'));
  }
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
  // Mountpoints for the per-person volumes; /app is bind-mounted read-only.
  for(const mountpoint of ['orgs','attachments'])mkdirSync(repository+mountpoint,{recursive:true});
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
    const startup=spawnSync('docker',[...composeArgs(run),'up','-d','--wait','--wait-timeout','150'],{env:composeEnv(),encoding:'utf8',timeout:180000});
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
  evidence.isolatedDashboards=isolateDashboards;
  evidence.services=command('docker',[...composeArgs(run),'ps','--format','json'])
    .trim().split('\n').map(line=>{const r=JSON.parse(line);return {service:r.Service,health:r.Health,state:r.State};});
  command('curl',['--fail','--silent','--show-error','--cacert',tlsDirectory+'/server.crt',relayOrigin+'/healthz']);
  browser('alice','open',relayOrigin+'/healthz');
  const tlsPage=browser('alice','snapshot');
  if(JSON.stringify(tlsPage).includes('Your connection is not private'))throw new Error('Browser did not trust the relay certificate');
  evidence.tls={selfSigned:true,ca:false,verification:'curl and browser',publicCertificate:tlsDirectory+'/server.crt'};
  // Desktop-height viewport so each evidence screenshot holds a whole screen.
  withStep('alice','open alice home',()=>{browser('alice','open','http://localhost:'+alicePort+'/');browser('alice','set','viewport','1280','900');});
  withStep('alice','save fresh start',()=>save('alice','fresh-start',browser('alice','snapshot','-i')));
  withStep('alice','begin alice onboarding',()=>action('alice','[data-testid="welcome-begin"]',{},'#onboarding-name'));
  const alicePassword=withStep('alice','create alice password',()=>randomBytes(24).toString('base64url'));
  withStep('alice','submit alice identity',()=>action('alice','#onboarding-primary',{'#onboarding-name':'Alice','#onboarding-password':alicePassword,'#onboarding-password2':alicePassword},
    '[data-testid="onboarding-step-device"]'));
  withStep('alice','save alice-identity-created',()=>save('alice','alice-identity-created',browser('alice','snapshot','-i')));
  // The product offers Not now for optional device-passkey setup.
  withStep('alice','skip alice device setup',()=>action('alice','#onboarding-notnow',{},'[data-testid="welcome-create"]'));
  withStep('alice','save alice personal profile',()=>setPersonalProfile('alice','Alice','Organization founder',directory+'fixtures/alice-photo.png'));
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

    withStep('bob','open bob home',()=>{browser('bob','open','http://localhost:'+bobPort+'/');browser('bob','set','viewport','1280','900');});
    withStep('bob','begin bob onboarding',()=>action('bob','[data-testid="welcome-begin"]',{},'#onboarding-name'));
    const bobPassword=withStep('bob','create bob password',()=>randomBytes(24).toString('base64url'));
    withStep('bob','submit bob identity',()=>action('bob','#onboarding-primary',{'#onboarding-name':'Bob','#onboarding-password':bobPassword,'#onboarding-password2':bobPassword},
      '[data-testid="onboarding-step-device"]'));
    withStep('bob','skip bob device setup',()=>action('bob','#onboarding-notnow',{},'[data-testid="welcome-join"]'));
    withStep('bob','save bob personal profile',()=>setPersonalProfile('bob','Bob','Joining member'));
    // The welcome rail renders inside the dashboard shell after its fragment
    // loads; observe the control before operating it.
    withStep('bob','wait welcome join',()=>waitFor('bob','[data-testid="welcome-join"]','#onboarding-error, .mem-error'));
    withStep('bob','open invite input',()=>browser('bob','click','[data-testid="welcome-join"]'));
    withStep('bob','wait invite input',()=>waitFor('bob','[data-testid="invite-input"]','#paste-hint'));
    withStep('bob','submit invite code',()=>action('bob','[data-testid="invite-next"]',{'[data-testid="invite-input"]':invitation},'[data-testid="invite-accept"]','#paste-hint'));
    withStep('bob','save invitation-previewed',()=>save('bob','invitation-previewed',browser('bob','snapshot','-i')));
    withStep('bob','accept invite',()=>action('bob','[data-testid="invite-accept"]',{},'.or-in-bare','#accept-hint'));
    withStep('bob','submit bob password',()=>action('bob','.or-ok',{'.or-in-bare':bobPassword},'#waiting-block:not(.hidden)','#accept-hint'));
    withStep('bob','save join-request-sent',()=>save('bob','join-request-sent',browser('bob','snapshot','-i')));

    withStep('alice','wait for claim',()=>waitFor('alice','[data-claim]','.mem-error'));
    withStep('alice','save join-request-received',()=>save('alice','join-request-received',browser('alice','snapshot','-i')));
    withStep('alice','approve invite claim',()=>action('alice','[data-claim] [data-action="approve"]',{},'.or-in-bare','.mem-error'));
    // The approval prompt itself: the root-unlock dialog, captured before any
    // factor is entered so no secret can appear in the evidence.
    withStep('alice','save approval-prompt',()=>save('alice','approval-prompt',browser('alice','snapshot','-i')));
    withStep('alice','confirm claim with password',()=>action('alice','.or-ok',{'.or-in-bare':alicePassword},'.mem-word.good','.mem-error'));
    withStep('alice','save join-request-approved',()=>save('alice','join-request-approved',browser('alice','snapshot','-i')));
    withStep('bob','done screen',()=>action('bob','.or-ok',{'.or-in-bare':bobPassword},'#done-block:not(.hidden)','#accept-hint'));
    withStep('bob','save invitation-complete',()=>save('bob','invitation-complete',browser('bob','snapshot','-i')));

    // ---- After admission (punch list 34–35): Bob's organization must be usable
    // on his own machine, both member directories must list both people, and
    // one organization change made on Alice must be observed on Bob. Every
    // step is a visible product control; a missing element is the finding.
    withStep('bob','open dashboard from admitted screen',()=>browser('bob','click','[data-testid="invite-open-dashboard"]'));
    withStep('bob','wait dashboard home',()=>waitFor('bob','[data-testid="identity-trigger"]','#accept-hint'));
    evidence.bobHomeAfterAdmission=js('bob','location.pathname');
    withStep('bob','open organization settings',()=>openOrganizationSettings('bob'));
    withStep('bob','save bob-organization-opened',()=>save('bob','bob-organization-opened',browser('bob','snapshot','-i')));
    withStep('bob','open membership rail',()=>action('bob','[data-testid="orgset-rail-membership"]',{},'[data-testid="membership-members"]'));
    withStep('bob','wait member directory',()=>waitFor('bob','[data-member]','.mem-error'));
    evidence.bobMembers=memberRows('bob');
    withStep('bob','save bob-member-directory',()=>save('bob','bob-member-directory',browser('bob','snapshot','-i')));
    withStep('alice','open members tab',()=>browser('alice','click','button[data-tab="members"]'));
    withStep('alice','wait member directory',()=>waitFor('alice','[data-member]','.mem-error'));
    evidence.aliceMembers=memberRows('alice');
    withStep('alice','save alice-member-directory',()=>save('alice','alice-member-directory',browser('alice','snapshot','-i')));
    // Both members sign on again: Alice's ceremony publishes the membership
    // checkpoint that includes Bob and mints her org sync credential; Bob's
    // adopts that checkpoint from the registry and mints his.
    withStep('alice','lock and unlock',()=>lockAndUnlock('alice',alicePassword));
    withStep('alice','save alice-signed-on-again',()=>save('alice','alice-signed-on-again',browser('alice','snapshot','-i')));
    withStep('bob','lock and unlock',()=>lockAndUnlock('bob',bobPassword));
    withStep('bob','save bob-signed-on-again',()=>save('bob','bob-signed-on-again',browser('bob','snapshot','-i')));
    // One organization change on Alice, observed on Bob through the same screen.
    withStep('alice','reopen organization settings',()=>openOrganizationSettings('alice'));
    withStep('alice','open charter',()=>action('alice','[data-testid="orgset-rail-charter"]',{},'#ch-byline','.mem-error'));
    withStep('alice','save charter byline',()=>action('alice','[data-action="save"]',{'#ch-byline':'Synced from Alice'},'#ch-byline','.mem-error'));
    withStep('alice','wait charter saved',()=>waitValue('alice','[data-action="save"]','Saved'));
    withStep('alice','save alice-charter-saved',()=>save('alice','alice-charter-saved',browser('alice','snapshot','-i')));
    const syncStarted=Date.now();
    withStep('bob','reopen organization settings',()=>openOrganizationSettings('bob'));
    // Convergence is observed by re-opening the charter screen (which reads
    // the organization's current row) a bounded number of times; each look
    // is a product action plus an observer, never a sleep.
    withStep('bob','observe synced byline',()=>{
      let lastError=null;
      for(let attempt=0;attempt<8;attempt++){
        try{
          action('bob','[data-testid="orgset-rail-membership"]',{},'[data-testid="membership-members"]','.mem-error');
          action('bob','[data-testid="orgset-rail-charter"]',{},'#ch-byline','.mem-error');
          waitValue('bob','#ch-byline','Synced from Alice','value',10000);
          evidence.syncAttempts=attempt+1;
          return true;
        }catch(error){lastError=error;}
      }
      throw lastError;
    });
    evidence.syncObservedMs=Date.now()-syncStarted;
    withStep('bob','save bob-charter-synced',()=>save('bob','bob-charter-synced',browser('bob','snapshot','-i')));
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
  // Service logs are evidence too: what each dashboard, connector and the
  // relay said while the workflow ran, captured before anything is torn down.
  try{
    const composeFile=directory+'onboarding.compose.yaml';
    const composeEnv={...process.env,SIM_SOURCE_DIR:repository,SIM_TLS_DIR:tlsDirectory,SIM_RELAY_HOST:process.env.SIM_RELAY_HOST||'127.0.0.1',SIM_RELAY_PORT:relayPort,SIM_ALICE_PORT:alicePort,SIM_BOB_PORT:bobPort};
    const serviceLog=command('docker',[...composeArgs(run),'logs','--no-color','--timestamps'],undefined,{env:composeEnv,maxBuffer:64*1024*1024});
    writeFileSync(output+'/services.log',serviceLog);
    // Each dashboard's fleet channel log and its connectors' logs (a
    // relay-delegated pull runs in the connector process, so that is where a
    // relay pull is recorded), read from the containers before teardown.
    let fleetLogs='';
    for(const person of ['alice','bob']){
      try{
        const text=command('docker',['exec',run+'-'+person+'-1','sh','-c','cat /app/data/logs/fleet.log 2>/dev/null; for f in /app/data/network/*.log; do echo "--- $f"; cat "$f"; done 2>/dev/null'],undefined,{maxBuffer:64*1024*1024});
        writeFileSync(output+'/'+person+'-fleet.log',text);fleetLogs+=text;
      }catch(error){evidence.cleanup.errors.push(person+' fleet logs: '+error.message);}
    }
    // Which transport carried the organization pulls, in the services' own
    // words. A pull that completes in a DASHBOARD process dialled the peer
    // directly; one that completes in a CONNECTOR process was delegated
    // there for the relay pair. The two are told apart by where the line
    // was written (the connector logs follow their "--- /app/data/network"
    // markers in each collected file).
    const sections={dashboard:'',connector:''};
    for(const person of ['alice','bob']){
      let text='';try{text=readFileSync(output+'/'+person+'-fleet.log','utf8');}catch{continue;}
      const marker=text.indexOf('--- /app/data/network/');
      sections.dashboard+=marker<0?text:text.slice(0,marker);
      sections.connector+=marker<0?'':text.slice(marker);
    }
    const count=(text,re)=>(text.match(re)||[]).length;
    const completed=/fleet sync pull [0-9a-f]+ scope 'simulation-organization': got [1-9][0-9]* transaction/g;
    evidence.transport={
      directPullsCompleted:count(sections.dashboard,completed),
      relayPullsCompleted:count(sections.connector,completed),
      directCandidateFailures:count(sections.dashboard,/no candidate connected/g),
      relayPullFailures:count(sections.dashboard,/relay fallback retry after direct failed/g),
    };
  }catch(error){evidence.cleanup.errors.push('service logs: '+error.message);}
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
