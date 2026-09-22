// Organization invitation scenario; shared environment and UI driver in ui-harness.mjs.
import {randomBytes} from 'node:crypto';
import {createWorkflow} from './ui-harness.mjs';
const onboardingOnly=process.argv.includes('--onboarding-only');
createWorkflow({scope:onboardingOnly?'identity-and-organization-onboarding':'membership',output:process.argv.slice(2).find(arg=>!arg.startsWith('--'))}).runScenario(({browser,js,action,waitFor,waitValue,lockAndUnlock,memberRows,click,textOf,setPersonalProfile,openOrganizationSettings,save,withStep,evidence,directory,run,output,alicePort,bobPort})=>{
  withStep('alice','open alice home',()=>{browser('alice','open','https://localhost:'+alicePort+'/');browser('alice','set','viewport','1280','900');});
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
    // Step 3 starts the first session in Getting Started and lands on it
    // (design of record graph://5f2f5a49-00d v11 §10.6).
    withStep('alice','go to your workspace',()=>browser('alice','click','[data-testid="welcome-open-workspace"]'));
    // The click navigates, which ends any in-page wait; poll the location
    // from outside until the session page is open, then wait inside it.
    withStep('alice','first session page',()=>{const started=Date.now();for(;;){
      const path=js('alice','location.pathname');
      if(path.startsWith('/session/'))break;
      const error=js('alice',`(()=>{const e=document.querySelector('[data-testid="welcome-start-error"]');return e&&e.textContent.trim()||'';})()`);
      if(error)throw new Error(error);
      if(Date.now()-started>30000)throw new Error('UI timeout: the first session page did not open (still on '+path+')');
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0,500);}
      return waitFor('alice','[data-testid="session-header"]','.sv-error, [data-testid="session-error"]',20000);});
    withStep('alice','save getting-started-session',()=>save('alice','getting-started-session',{
      snapshot:browser('alice','snapshot','-i'),
      session:js('alice',`(async()=>{const name=location.pathname.split('/').pop();const s=await (await fetch('/api/session/'+name)).json();return {path:location.pathname,tmux_name:name,project:s.project||s.session&&s.session.project||null,startup_state:s.startup_state||s.session&&s.session.startup_state||null,raw:JSON.stringify(s).slice(0,600)};})()`)}));
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

    withStep('bob','open bob home',()=>{browser('bob','open','https://localhost:'+bobPort+'/');browser('bob','set','viewport','1280','900');});
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
    // The bootstrap seeds names only; Alice's photo must reach Bob's listing
    // through the same organization sync the charter just proved (operator
    // ruling 2026-09-13: no photos in the install material). The photo is an
    // attachment in the org store: the row replicates, the bytes follow by
    // content hash, and Bob's dashboard serves them at /api/attachment/<id>.
    // The proof is the image LOADING (naturalWidth > 0), not the tag existing.
    withStep('bob','reopen member directory after sync',()=>action('bob','[data-testid="orgset-rail-membership"]',{},'[data-testid="membership-members"]','.mem-error'));
    withStep('bob','observe alice photo synced',()=>waitFor('bob','[data-member] .mem-avatar img[src^="/api/attachment/"]','.mem-error'));
    withStep('bob','observe alice photo served',()=>{
      js('bob',`new Promise((resolve,reject)=>{
        const selector='[data-member] .mem-avatar img[src^="/api/attachment/"]';
        const observer=new MutationObserver(probe);
        const timer=setTimeout(()=>finish(new Error('synced member photo never loaded from /api/attachment')),25000);
        function finish(error){clearTimeout(timer);observer.disconnect();document.removeEventListener('load',probe,true);error?reject(error):resolve(true);}
        function probe(){if(Array.from(document.querySelectorAll(selector)).some(i=>i.getClientRects().length&&i.complete&&i.naturalWidth>0))finish();}
        document.addEventListener('load',probe,true);
        observer.observe(document,{subtree:true,childList:true,attributes:true});
        probe();
      })`);
    });
    evidence.bobMembersAfterSync=memberRows('bob');
    evidence.bobPhotosAfterSync=js('bob',`Array.from(document.querySelectorAll('[data-member] .mem-avatar img[src^="/api/attachment/"]')).filter(i=>i.complete&&i.naturalWidth>0).length`);
    withStep('bob','save bob-member-directory-synced',()=>save('bob','bob-member-directory-synced',browser('bob','snapshot','-i')));
    evidence.status='passed';
  }
});
