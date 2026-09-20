// Governing specification: WORKFLOW-REQUIREMENTS.md. UI-only product operations.
import {spawnSync} from 'node:child_process';
import {randomBytes,createHash} from 'node:crypto';
import {mkdirSync,writeFileSync,mkdtempSync,existsSync,readFileSync} from 'node:fs';
import {homedir,tmpdir} from 'node:os';
import {fileURLToPath} from 'node:url';

export function createWorkflow({scope,output,initialServices=[]}={}){

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
output ||= '/workspace/output/'+run;
// Environment-only TLS: one directly trusted server certificate, no CA or
// certificate-error bypass. The private key stays in local temporary storage.
const tlsDirectory=mkdtempSync(tmpdir()+'/invitation-tls-');
const nssDirectory=homedir()+(existsSync(homedir()+'/.pki/nssdb')?'/.pki/nssdb':'/.local/share/pki/nssdb');
process.env.AGENT_BROWSER_IGNORE_HTTPS_ERRORS='false';
mkdirSync(output,{recursive:true});
const evidence={run,scope,started:new Date().toISOString(),checkpoints:[],status:'running',steps:[]};
const openedBrowsers=new Set();
function command(bin,args,input,options={}){
  const result=spawnSync(bin,args,{input,encoding:'utf8',timeout:180000,...options});
  if(result.error&&result.error.code==='ENOENT')throw new Error(bin+' is not installed (required by the harness; certutil comes from libnss3-tools)');
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
function launchFleetJoiner(installCode){
  const prefix='AUTONOMY_FLEET_INVITE=';
  if(typeof installCode!=='string'||!installCode.startsWith(prefix))throw new Error('Missing copied Fleet install code');
  command('docker',[...composeArgs(run),'up','-d','--wait','--wait-timeout','150','bob'],undefined,{
    env:{...process.env,SIM_SOURCE_DIR:repository,SIM_TLS_DIR:tlsDirectory,
      SIM_RELAY_PORT:relayPort,SIM_ALICE_PORT:alicePort,SIM_BOB_PORT:bobPort,
      SIM_FLEET_INVITE:installCode.slice(prefix.length)},
  });
}
// Browser environment only: read the clipboard populated by the real Copy
// control. Keep permission and read on one CDP connection: agent-browser
// reapplies its default denied permissions on each new command.
function readCopiedText(person){
  const {cdpUrl}=browser(person,'get','cdp-url');
  const origin='https://localhost:'+(person==='alice'?alicePort:bobPort);
  return command('node',['--input-type=module','-e',`
    const ws=new WebSocket(process.argv[1]);
    await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
    let seq=0;
    function call(method,params={},sessionId){return new Promise((resolve,reject)=>{
      const id=++seq;
      const timer=setTimeout(()=>reject(new Error('Clipboard browser operation timed out')),5000);
      const receive=e=>{const result=JSON.parse(e.data);if(result.id!==id)return;
        clearTimeout(timer);ws.removeEventListener('message',receive);
        result.error?reject(new Error(result.error.message)):resolve(result.result);
      };
      ws.addEventListener('message',receive);ws.send(JSON.stringify({id,method,params,sessionId}));
    });}
    try{
      const {targetInfos}=await call('Target.getTargets');
      const target=targetInfos.find(t=>t.type==='page'&&new URL(t.url).origin===process.argv[2]);
      if(!target)throw new Error('Clipboard browser page is missing');
      const {sessionId}=await call('Target.attachToTarget',{targetId:target.targetId,flatten:true});
      await call('Browser.grantPermissions',{permissions:['clipboardReadWrite','clipboardSanitizedWrite'],origin:process.argv[2],browserContextId:target.browserContextId});
      const result=await call('Runtime.evaluate',{expression:'navigator.clipboard.readText()',awaitPromise:true,returnByValue:true},sessionId);
      if(result.exceptionDetails||typeof result.result?.value!=='string')throw new Error('Could not read copied text');
      process.stdout.write(result.result.value);
    }finally{ws.close();}
  `,cdpUrl,origin]);
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
  const invocation=spawnSync('agent-browser',['--session',run+'-'+person,'--json',...args],{encoding:'utf8',timeout:180000});
  let r;
  try { r=JSON.parse(invocation.stdout); }
  catch { throw new Error('Browser '+args[0]+' failed without a JSON response (exit '+invocation.status+')'); }
  if(!r.success)throw new Error(JSON.stringify(r.error));
  openedBrowsers.add(person);
  return r.data;
}
function js(person,code){
  const r=spawnSync('agent-browser',['--session',run+'-'+person,'--json','eval','--stdin'],{input:code,encoding:'utf8',timeout:40000});
  let body;try{body=JSON.parse(r.stdout);}catch{throw new Error('Browser evaluation failed without a response');}
  if(!body.success)throw new Error(JSON.stringify(body.error));return body.data.result;
}
function action(person,selector,values,success,errorSelector='#onboarding-error',timeoutMs=5000){
  return js(person,`(async()=>{
    const visible=e=>!!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
    function wait(check,label){return new Promise((resolve,reject)=>{
      const observers=new Map();
      const timer=setTimeout(()=>finish(new Error('UI timeout: '+label)),${timeoutMs});
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
function waitFor(person,selector,errorSelector='.mem-error',timeoutMs=8000){
  return js(person,`(async()=>{
    const visible=e=>!!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
    function all(selector,root=document,out=[]){out.push(...root.querySelectorAll(selector));for(const node of root.querySelectorAll('*'))if(node.shadowRoot)all(selector,node.shadowRoot,out);return out;}
    function one(selector){return all(selector).find(visible)||null;}
    return new Promise((resolve,reject)=>{const observers=new Map();const timer=setTimeout(()=>finish(new Error('UI timeout: '+${JSON.stringify(selector)})),${timeoutMs});
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
  browser(person,'open','https://localhost:'+port+'/account/profile');
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
  browser(person,'open','https://localhost:'+port+'/');
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

function runScenario(scenario){
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
  // The relay note viewer is built once here, like the CSS: inside the
  // simulation the source tree is read-only, so a member must find it built.
  command('python3',['-m','tools.dashboard.scripts.build_relay_note_viewer'],undefined,{cwd:repository});
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
  // Disposable test TLS key: the production dashboard runs as uid 1000.
  // Mount only this file, not the private temporary directory around it.
  command('chmod',['644',tlsDirectory+'/server.key']);
  mkdirSync(nssDirectory,{recursive:true});
  if(!existsSync(nssDirectory+'/cert9.db'))command('certutil',['-N','-d','sql:'+nssDirectory,'--empty-password']);
  command('certutil',['-A','-d','sql:'+nssDirectory,'-t','P,,','-n',run,'-i',tlsDirectory+'/server.crt']);
  const composeFile=directory+'onboarding.compose.yaml';
  const composeEnv=()=>({...process.env,SIM_SOURCE_DIR:repository,SIM_TLS_DIR:tlsDirectory,SIM_RELAY_HOST:process.env.SIM_RELAY_HOST,SIM_RELAY_PORT:relayPort,SIM_ALICE_PORT:alicePort,SIM_BOB_PORT:bobPort});
  // A scenario may stop and start a dashboard mid-run (the note-serving proof
  // stops the publisher to make the relay fall over to the other machine).
  const compose=(...args)=>command('docker',[...composeArgs(run),...args],undefined,{env:composeEnv(),maxBuffer:64*1024*1024});
  const startupLogLines=[];
  let startupAttempts=0;
  while(true){
    startupAttempts+=1;
    const startup=spawnSync('docker',[...composeArgs(run),'up','-d','--wait','--wait-timeout','150',...initialServices],{env:composeEnv(),encoding:'utf8',timeout:180000});
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

scenario({browser,js,action,waitFor,waitValue,lockAndUnlock,memberRows,click,textOf,setPersonalProfile,openOrganizationSettings,save,withStep,evidence,directory,run,output,alicePort,bobPort,relayOrigin,launchFleetJoiner,readCopiedText,compose});

}catch(error){
  evidence.status='failed';
  evidence.error=error.message;
  process.exitCode=1;
  evidence.failureContext={};
  for(const person of openedBrowsers)try{
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
      const container=command('docker',['ps','-aq','--filter',`label=com.docker.compose.project=${run}`,'--filter',`label=com.docker.compose.service=${person}`]).trim();
      if(!container)continue;
      try{
        const text=command('docker',['exec',container,'sh','-c','for f in /app/data/logs/fleet.log /app/data/logs/dashboard.log /app/data/logs/http.log; do if [ -f "$f" ]; then echo "--- $f"; cat "$f"; fi; done; for f in /app/data/network/*.log; do [ -f "$f" ] || continue; echo "--- $f"; cat "$f"; done'],undefined,{maxBuffer:64*1024*1024});
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
    const completed=scope==='personal-fleet'
      ? /fleet sync pull [0-9a-f]+ scope 'personal': got [1-9][0-9]* transaction/g
      : /fleet sync pull [0-9a-f]+ scope 'simulation-organization': got [1-9][0-9]* transaction/g;
    evidence.transport={
      directPullsCompleted:count(sections.dashboard,completed),
      relayPullsCompleted:count(sections.connector,completed),
      directCandidateFailures:count(sections.dashboard,/no candidate connected/g),
      relayPullFailures:count(sections.dashboard,/relay fallback retry after direct failed/g),
    };
    // The relay's own routing decisions (its registry runs at info here, not
    // in production): which member each viewer dial was routed to, in order.
    evidence.relayRouting=(serviceLog.match(/relay dial routed: token=[0-9a-f]+ org=[0-9a-f]+ tunnel machine=[0-9a-f]+[^\n]*/g)||[]).map(line=>line.replace(/^.*relay dial routed: /,''));
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

}
return {runScenario};
}
