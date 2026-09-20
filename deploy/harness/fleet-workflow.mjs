// Personal Fleet scenario. Same harness, separate test from organization invites.
import {randomBytes} from 'node:crypto';
import {createWorkflow} from './ui-harness.mjs';

createWorkflow({scope:'personal-fleet',initialServices:['alice'],output:process.argv[2]})
.runScenario(({browser,js,action,waitFor,waitValue,textOf,save,withStep,evidence,alicePort,bobPort,launchFleetJoiner,readCopiedText,compose})=>{
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

  // ---- A note published on the first machine is read through the relay,
  // reaches the second machine by Fleet sync (its rows, its grant and its
  // channel key), and is served by the second machine once the first is
  // gone (operator order 2026-09-20). Every step is a product control or a
  // product API called from the operator's own signed-on browser session.
  const sharedBody='Shared over the relay from the first dashboard: '+randomBytes(8).toString('hex');
  const noteId=withStep('alice','create a graph note to share',()=>js('alice',`(async()=>{
    const form=new FormData();form.append('content',${JSON.stringify(sharedBody)});
    const r=await fetch('/api/graph/note',{method:'POST',headers:{'X-Graph-Org':'personal'},body:form});
    const body=await r.json();if(!r.ok)throw new Error('note create refused: '+JSON.stringify(body));
    return body.source_id||body.id;})()`));
  if(typeof noteId!=='string'||!noteId)throw new Error('graph note was not created');
  withStep('alice','open the note page',()=>{
    browser('alice','open','https://localhost:'+alicePort+'/graph/'+noteId+'?org=personal');
    waitFor('alice','[data-testid="asset-share-request"]','.design-presence-note.is-error');
  });
  // The share request opens the publish approval in place; it is authorized
  // and signed exactly like the Fleet invitation above.
  withStep('alice','request share by link',()=>action('alice','[data-testid="asset-share-request"]',{},'[data-testid="approval-dialog"]','.design-presence-note.is-error',15000));
  withStep('alice','authorize note publication',()=>action('alice','#primary',{},'#password'));
  withStep('alice','sign note publication',()=>action('alice','.verify',{'#password':password},'#result[aria-busy="false"] #result-title','#error:not([hidden])',30000));
  if(resultTitle()!=='Link published')throw new Error('Note publication did not succeed: '+resultTitle());
  save('alice','note-link-published',browser('alice','snapshot','-i'));
  withStep('alice','close note publication receipt',()=>action('alice','#primary',{},'[data-testid="asset-share-row"]','.design-presence-note.is-error',30000));
  // The link itself, from the product's own published-links record: the
  // canonical registry URL with the channel key in its fragment, which is
  // exactly what the share control copies and opens.
  const shareUrl=withStep('alice','read the published note link',()=>js('alice',`(async()=>{
    const r=await fetch('/api/network/published-links',{headers:{'X-Graph-Org':'personal'}});
    const j=await r.json();if(!r.ok)throw new Error('published links refused: '+JSON.stringify(j));
    const share=(j.shares||[]).find(s=>s.target_uuid===${JSON.stringify(noteId)});
    if(!share)throw new Error('the note is not among the published links: '+JSON.stringify(j.shares||[]).slice(0,300));
    return share.url;})()`));
  if(!/^https:\/\/.+\/l\/[0-9a-f]{32}#./.test(shareUrl))throw new Error('the published link is not a fragment-keyed share URL: '+String(shareUrl).slice(0,80));
  evidence.sharedNote={noteId,tokenPrefix:shareUrl.split('/l/')[1].slice(0,8)};
  // The viewer: a third browser, no session, only the link. The note body
  // arrives through the relay from whichever member the relay routed to and
  // renders inside the viewer page's frame.
  // The viewer page renders the note inside a sandboxed frame the page's own
  // script cannot read, so the note is observed the way a person sees it: in
  // the browser's accessibility snapshot of the whole page, frame included.
  const viewerSees=(person,timeoutMs)=>{
    const started=Date.now();let last='';
    while(Date.now()-started<timeoutMs){
      const snapshot=JSON.stringify(browser(person,'snapshot','-i'));
      if(snapshot.includes(sharedBody))return true;
      last=js(person,`Array.from(document.querySelectorAll('.error-view:not([hidden])')).map(e=>e.id).join(',')`);
      js(person,'new Promise(r=>setTimeout(r,500))');
    }
    throw new Error('viewer did not render the note'+(last?' ('+last+')':''));
  };
  withStep('viewer','open the note link through the relay',()=>{
    browser('viewer','open',shareUrl);
    browser('viewer','set','viewport','1000','800');
    viewerSees('viewer',30000);
  });
  save('viewer','note-served-through-relay',browser('viewer','snapshot','-i'));
  // Propagation: the second machine holds the note itself, not a copy the
  // viewer fetched: its own dashboard renders it from its own store.
  withStep('bob','observe the shared note synchronized',()=>{
    let lastError=null;
    for(let attempt=0;attempt<12;attempt++){
      try{
        const body=js('bob',`(async()=>{const r=await fetch('/api/graph/source/${noteId}',{headers:{'X-Graph-Org':'personal'}});if(!r.ok)throw new Error('HTTP '+r.status);const j=await r.json();return JSON.stringify(j);})()`);
        if(!String(body).includes(sharedBody))throw new Error('note not yet on the second machine');
        evidence.sharedNoteSyncAttempts=attempt+1;
        return true;
      }catch(error){lastError=error;}
      // One product action per look: the second dashboard's own pull cadence
      // decides when the row lands; nothing here nudges it.
      js('bob','new Promise(r=>setTimeout(r,2500))');
    }
    throw lastError;
  });
  withStep('bob','open the synchronized note page',()=>{
    browser('bob','open','https://localhost:'+bobPort+'/graph/'+noteId+'?org=personal');
    waitFor('bob','[data-testid="note-presence"]','.mem-error');
  });
  save('bob','shared-note-on-second-machine',browser('bob','snapshot','-i'));
  // Serving from both: with the publisher gone the relay must route the same
  // link to the second machine, which resolves the grant and the channel
  // key from its own synchronized store.
  withStep('alice','stop the publishing dashboard',()=>compose('stop','alice'));
  // The viewer page is already at the link's address; opening the same
  // address again is a same-document navigation (only the fragment could
  // change) and never reaches the relay. A reload loads the page again and
  // dials the relay again.
  withStep('viewer','reopen the note link with the publisher gone',()=>{
    let lastError=null;
    for(let attempt=0;attempt<8;attempt++){
      try{
        browser('viewer','reload');
        viewerSees('viewer',20000);
        evidence.servedByBobAttempts=attempt+1;
        return true;
      }catch(error){lastError=error;}
    }
    throw lastError;
  });
  save('viewer','note-served-by-second-machine',browser('viewer','snapshot','-i'));
  // The same health wait the run began with: the step ends when the
  // dashboard answers its health check, not when the container is started.
  withStep('alice','start the publishing dashboard again',()=>{
    compose('up','-d','--wait','--wait-timeout','150','alice');
    browser('alice','open','https://localhost:'+alicePort+'/api/ping');
  });
  evidence.sharedNote.servedByBothMachines=true;
  evidence.status='passed';
});
