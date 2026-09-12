/* Vault data/operation adapter. Presentation is the shared approved control. */
import {openApprovalDialog, requestingSession} from './approval-dialog.js';
import {collectVaultOpeners, clearVaultOpeners} from '../ceremony/open-vault.js';
import {openContentKey} from '../ceremony/policy-class-open.js';

function lifetime(seconds) {
  if(seconds === 0) return 'Until this session ends';
  if(seconds % 3600 === 0) return `${seconds / 3600} hour${seconds === 3600 ? '' : 's'}`;
  if(seconds % 60 === 0) return `${seconds / 60} minute${seconds === 60 ? '' : 's'}`;
  return `${seconds} seconds`;
}
async function json(url, options) {
  const response = await fetch(url, options);
  const body = await response.json();
  if(!response.ok) throw new Error(body.error || 'Could not complete this release.');
  return body;
}

export async function openVaultApproval(self, r, {collect=collectVaultOpeners, openKey=openContentKey} = {}) {
  const req=r.request||{}, ceremony=r.ceremony, bundle=r.bundle, setting=req.setting||{}, requester=req.requester||{};
  const legacy=ceremony?.v===1 && ['password','prf','both'].includes(ceremony.policy) && ceremony.factors?.length;
  const rooted=ceremony?.v===2 && ceremony.governance?.form==='root-reachable'
    && ceremony.anchor && ceremony.root && ceremony.governance.anchor_id===ceremony.anchor.anchor_id
    && ceremony.root.methods?.length && ceremony.root.methods.every(m=>['password','passkey','both'].includes(m));
  if((!legacy&&!rooted)||!setting.set_id||!setting.key||!requester.session||!requester.organization
    ||!requester.workspace||req.operation!=='read'||req.release_mode!=='delivered') {
    throw new Error('This release request is incomplete.');
  }
  if(!bundle?.generation||!bundle.sealed_cek||!bundle.class_id||!bundle.genesis_id||!bundle.setting_name) {
    throw new Error('This release request has no frozen content bundle.');
  }
  const session=await requestingSession(requester.session,requester.label||r.session_label);
  const org=session.organization;
  const duration=lifetime(req.ttl_seconds);
  const post=async body=>{
    const result=await json('/api/approvals/'+encodeURIComponent(r.id)+'/decision',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),
    });
    if(!result.ok) throw new Error(result.error||'This release is no longer available.');
  };
  self._sharedApprovalDialog=openApprovalDialog({
    review:{kind:'vault',title:'Release credential',
      intro:'',
      organization:org?{name:org.name,image:org.favicon}:null,
      target:{type:'Credential',name:setting.key},requester:session,
      facts:[['Available',duration]],
      consequence:'Copies made by the session are not revoked when access ends.',
    },
    authorize:async options=>{
      let gathered;
      try {
        gathered=await collect(ceremony,options);
        if(options.signal.aborted) throw new Error('Approval cancelled.');
        options.onAuthenticated();
        return {content_key:await openKey(bundle,gathered.openers)};
      } finally {clearVaultOpeners(gathered);}
    },
    execute:async decision=>{
      try {await post({approved:true,content_key:decision.content_key});}
      finally {decision.content_key='';}
      // The operator can read the value-free result, but the waiting form is
      // requester-only. Observe completion without resubmitting the decision.
      const deadline=Date.now()+20000;
      do {
        const row=await json('/api/approvals/'+encodeURIComponent(r.id));
        if(row.result) {
          if(row.result.execution?.ok!==true) throw new Error(row.result.execution?.error||'The saved value could not be delivered.');
          self._markApprovalDecided(r.id);return row.result;
        }
        await new Promise(resolve=>setTimeout(resolve,250));
      } while(Date.now()<deadline);
      throw new Error('Delivery has not been confirmed. Check the requesting session.');
    },
    decline:async()=>{await post({approved:false});self._markApprovalDecided(r.id);},
    result:{working:'Releasing credential…',success:'Credential released',copy:'',
      fact:{name:setting.key,href:session.href,byline:`${session.name} · ${duration}`,linkLabel:'View requesting session'}},
    onClose:()=>{self._sharedApprovalDialog=null;self._sharedApprovalId=null;},
  });
  self._sharedApprovalId=r.id;
}
