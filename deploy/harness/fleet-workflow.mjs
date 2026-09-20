// Personal Fleet scenario. Same harness, separate test from organization invites.
import {randomBytes} from 'node:crypto';
import {createWorkflow} from './ui-harness.mjs';

createWorkflow({scope:'personal-fleet',initialServices:['alice'],output:process.argv[2]})
.runScenario(({browser,js,action,waitFor,waitValue,textOf,save,withStep,evidence,alicePort,bobPort,launchFleetJoiner,readCopiedText})=>{
  const password=randomBytes(24).toString('base64url');
  const noteTitle='Fleet synchronization proof';
  const noteBody='Created on the first dashboard for its second machine: '+randomBytes(12).toString('hex');
  const resultTitle=()=>js('alice',"document.querySelector('[data-testid=approval-dialog]').shadowRoot.querySelector('#result-title').textContent");
  withStep('alice','open first dashboard',()=>{
    browser('alice','open','https://localhost:'+alicePort+'/');
    browser('alice','set','viewport','1280','900');
  });
  withStep('alice','begin identity',()=>action('alice','[data-testid="welcome-begin"]',{},'#onboarding-name'));
  withStep('alice','create personal identity',()=>action('alice','#onboarding-primary',{
    '#onboarding-name':'Alice','#onboarding-password':password,'#onboarding-password2':password,
  },'[data-testid="onboarding-step-device"]'));
  withStep('alice','skip optional passkey',()=>action('alice','#onboarding-notnow',{},'[data-testid="welcome-create"]'));
  withStep('alice','open personal notes',()=>{
    browser('alice','open','https://localhost:'+alicePort+'/voice-notes');
    waitFor('alice','[data-testid="voice-notes-new"]');
  });
  withStep('alice','create synchronization note',()=>action('alice','[data-testid="voice-notes-new"]',{},'[data-testid="voice-notes-editor"]'));
  withStep('alice','save synchronization note',()=>action('alice','[data-testid="voice-notes-save"]',{
    '[aria-label="Note title"]':noteTitle,'[data-testid="voice-notes-editor"]':noteBody,
  },'.vn-note-row','.vn-status-line.is-error'));
  withStep('alice','observe saved note',()=>waitValue('alice','[data-testid="voice-notes-save"]','Saved'));
  save('alice','personal-note-created',browser('alice','snapshot','-i'));
  withStep('alice','open Fleet',()=>{
    browser('alice','open','https://localhost:'+alicePort+'/fleet');
    waitFor('alice','.invite-toggle');
  });
  withStep('alice','activate Fleet invitation',()=>action('alice','.invite-toggle',{},'[data-testid="approval-dialog"]'));
  withStep('alice','authorize publication',()=>action('alice','#primary',{},'#password'));
  withStep('alice','sign publication',()=>action('alice','.verify',{'#password':password},'#result[aria-busy="false"] #result-title','#error:not([hidden])',30000));
  if(resultTitle()!=='Link published')throw new Error('Fleet invitation publication did not succeed');
  save('alice','fleet-link-published',browser('alice','snapshot','-i'));
  withStep('alice','close publication receipt',()=>action('alice','#primary',{},'.invite-toggle'));
  withStep('alice','observe invitation ready for signature',()=>waitValue('alice','.invitation-status','Ready to sign'));
  withStep('alice','sign Fleet invitation',()=>action('alice','.invite-toggle',{},'.or-in-bare'));
  withStep('alice','confirm invitation signature',()=>action('alice','.or-ok',{'.or-in-bare':password},'.invitation-status.good'));
  withStep('alice','copy install code',()=>browser('alice','find','role','button','click','--name','Copy install code'));
  const installCode=withStep('alice','read copied install code',()=>readCopiedText('alice'));
  withStep('bob','launch second dashboard with copied install code',()=>launchFleetJoiner(installCode));
  withStep('bob','observe comparison code',()=>{
    browser('bob','open','https://localhost:'+bobPort+'/');
    browser('bob','set','viewport','1280','900');
    waitFor('bob','[data-testid="fleet-comparison-code"]');
  });
  const code=textOf('bob','[data-testid="fleet-comparison-code"]');
  save('bob','fleet-request-pending',browser('bob','snapshot','-i'));
  withStep('alice','open pending Fleet approval',()=>{
    browser('alice','click','[data-row-kind="pending_admission"]');
    browser('alice','wait','--fn',"location.pathname==='/activity'");
    waitFor('alice','#code');
  });
  const approvalCode=js('alice',"document.querySelector('[data-testid=approval-dialog]').shadowRoot.querySelector('#code').textContent.trim()");
  if(code.replaceAll(' ','')!==approvalCode.replaceAll(' ',''))throw new Error('Fleet comparison codes differ');
  withStep('alice','confirm matching code',()=>js('alice',"(()=>{const box=document.querySelector('[data-testid=approval-dialog]').shadowRoot.querySelector('#code-matches');if(!box.getClientRects().length||box.disabled)throw new Error('Code confirmation unavailable');box.click();return true})()"));
  withStep('alice','name and authorize machine',()=>action('alice','#primary',{'#machine-name':'Alice second dashboard'},'#password'));
  save('alice','fleet-authorization',browser('alice','snapshot','-i'));
  withStep('alice','approve machine',()=>action('alice','.verify',{'#password':password},'#result[aria-busy="false"] #result-title','#error:not([hidden])'));
  if(resultTitle()!=='Machine added')throw new Error('Fleet admission failed: '+resultTitle());
  save('alice','fleet-machine-added',browser('alice','snapshot','-i'));
  withStep('bob','observe approval',()=>waitFor('bob','[data-testid="welcome-fleet-approved"]'));
  withStep('bob','unlock joined dashboard',()=>{
    browser('bob','find','role','button','click','--name','Unlock to continue');
    browser('bob','wait','--fn',"location.pathname.startsWith('/unlock')");
    waitFor('bob','#unlock-password');
    browser('bob','fill','#unlock-password',password);
    browser('bob','click','#unlock-primary');
    browser('bob','wait','--fn',"!location.pathname.startsWith('/unlock') || !!document.querySelector('#unlock-error:not(.hidden)')?.textContent.trim()");
    const unlockError=js('bob',"document.querySelector('#unlock-error:not(.hidden)')?.textContent.trim()||''");
    if(unlockError)throw new Error(unlockError);
    waitFor('bob','[data-testid="welcome-fleet-sync"]');
  });
  withStep('bob','observe initial synchronization',()=>waitFor('bob','.fleet-sync-row.complete','.mem-error',30000));
  save('bob','fleet-initial-sync-complete',browser('bob','snapshot','-i'));
  withStep('bob','open synchronized personal notes',()=>{
    browser('bob','open','https://localhost:'+bobPort+'/voice-notes');
    waitFor('bob','.vn-note-row','.vn-status-line.is-error');
    // The newly unlocked machine offers its optional device passkey. Use
    // the same visible Not now action as the first dashboard's setup.
    action('bob','#onboarding-notnow',{},'.vn-note-row');
    action('bob','.vn-note-row',{},'[data-testid="voice-notes-editor"]');
    waitValue('bob','[aria-label="Note title"]',noteTitle,'value');
    waitValue('bob','[data-testid="voice-notes-editor"]',noteBody,'value');
  });
  save('bob','personal-note-synchronized',browser('bob','snapshot','-i'));
  evidence.note={title:noteTitle,exactBodyMatched:true};
  evidence.status='passed';
});
