/* Existing service approval protocol, rendered by the shared approval control. */
import {openApprovalDialog} from './approval-dialog.js';

const durations=[['86400','1 day'],['604800','7 days'],['2592000','30 days'],
  ['31536000','1 year'],['315360000','10 years'],['','Never']];

export function openServiceApproval(self,r){
  const access=r.staged||r.request||{}, requester=access.requester||{};
  const requested=access.requested_ttl_seconds==null?'':String(access.requested_ttl_seconds);
  const duration=document.createElement('select');
  for(const [value,label] of durations){const option=document.createElement('option');option.value=value;option.textContent=label;duration.append(option)}
  duration.value=durations.some(([value])=>value===requested)?requested:'31536000';
  const controls=document.createElement('div');controls.append(duration);
  const device=requester.label||r.session;
  const result={working:'Allowing access…',success:'Access allowed',copy:'',
    fact:{name:device,byline:`${access.application||'External service'} · ${duration.selectedOptions[0].textContent}`}};
  async function post(body){
    const response=await fetch('/api/approvals/'+encodeURIComponent(r.id)+'/decision',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data=await response.json();
    if(!response.ok||!data.ok)throw new Error(data.error||'This approval is no longer available.');
  }
  self._sharedApprovalDialog=openApprovalDialog({
    retained:true,
    review:{kind:'service',title:'Allow service access',intro:'',
      target:{type:'Application',name:access.application||'External service',
        byline:access.application_scope==='dropbox'?'Upload screenshots to your dropbox':access.summary},
      requester:{kind:'Requesting device',name:device},facts:[],controls,durationLabel:'Access lasts',
      consequence:'This allows screenshot uploads, not access to browse your dashboard.'},
    authorize:async options=>{options.onAuthenticated();return {};},
    execute:async()=>{
      await post({approved:true,ttl_seconds:duration.value===''?null:Number(duration.value)});
      const response=await fetch('/api/approvals/'+encodeURIComponent(r.id)+'?wait=20');
      const row=await response.json();
      if(!response.ok||row.result?.execution?.ok!==true){
        throw new Error(row.result?.execution?.error||row.error||'The approval did not finish executing.');
      }
      self._markApprovalDecided(r.id);
      // Do not pass the issued bearer into the presentation state.
      return {execution:{ok:true}};
    },
    decline:async()=>{await post({approved:false});self._markApprovalDecided(r.id);},
    result,
    onClose:()=>{self._sharedApprovalDialog=null;self._sharedApprovalId=null;},
  });
  self._sharedApprovalId=r.id;
}
