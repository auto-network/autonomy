/* Data/operation adapter only. Design Studio uses approval-experiment.js too. */
import { mountApprovalExperiment } from './approval-experiment.js';
let activeDialog;
export function localHref(value) {
  return typeof value === 'string' && /^\/(?!\/)/.test(value)
    && !/[\\\x00-\x20]/.test(value) ? value : null;
}
export async function requestingSession(session, label) {
  const result = { name: label || session || 'Requesting session' };
  if (!session) return result;
  try {
    const response = await fetch('/api/session/' + encodeURIComponent(session));
    const info = await response.json();
    if (response.ok && info.project && info.session_id === session) {
      result.href = '/session/' + encodeURIComponent(info.project) + '/' + encodeURIComponent(info.session_id);
      result.byline = info.project;
    }
  } catch (_) {}
  return result;
}
export function openApprovalDialog({review, authorize, execute, decline, result, retained=false, onClose=()=>{}}) {
  if (activeDialog && !activeDialog.close()) throw new Error('Another approval is still finishing. Open this request when it completes.');
  const previousFocus = document.activeElement;
  const host = document.createElement('div');
  host.setAttribute('data-testid','approval-dialog');
  host.style.cssText='position:fixed;inset:0;z-index:1300;overflow:auto;background:rgba(4,6,11,.72)';
  document.body.append(host);
  const kind = review.target ? 'link' : 'central';
  const originalDuration = review.controls?.querySelector('select');
  const originalRetention = review.controls?.querySelector('input[type=checkbox]');
  const durationOptions = originalDuration ? [...originalDuration.options].map(o=>[o.value,o.textContent]) : undefined;
  const expiry = review.facts?.find(([label])=>/expir/i.test(label))?.[1];
  const input = {
    kind,title:review.title + (review.title.endsWith('?')?'':'?'),
    intro:kind==='central'?review.intro:`Create a link to this ${review.target.type.toLowerCase()} for someone outside your workspace.`,
    organization:{name:review.organization?.name||'Personal approval',image:review.organization?.image||''},
    requester:{kind:'Requesting session',...review.requester,href:localHref(review.requester?.href)},
    resourceLabel:review.target?.type,resource:review.target?.name,resourceDetail:review.target?.byline,
    facts:[...(review.facts||[]),...(originalDuration?[['Link expires','duration']]:[])],
    duration:originalDuration?.value,durationOptions,
    showRetention:!!originalRetention,allowSessionApprovals:!!originalRetention?.checked,
    consequence:kind==='link'?'Anyone who has the link can open the shared content until it expires.':'',
    retained,canDecline:!!decline,reviewUnavailable:review.unavailable,
    working:result.working,success:result.success,result:result.copy,
    resultHref:localHref(result.fact.href),
    receipt:{name:result.fact.name,byline:kind==='central'&&expiry?'Dashboard access until '+expiry:result.fact.byline,link:result.fact.linkLabel||'View item'},
  };
  let api;
  api=mountApprovalExperiment(host,input,{
    async authorize(options,snapshot) {
      if(originalDuration&&snapshot?.duration!=null){originalDuration.value=snapshot.duration;originalDuration.dispatchEvent(new window.Event('change'));}
      if(originalRetention&&snapshot){originalRetention.checked=!!snapshot.allowSessionApprovals;originalRetention.dispatchEvent(new window.Event('change'));}
      if(kind==='link'&&snapshot){
        const chosen=durationOptions?.find(([value])=>value===snapshot.duration);
        input.receipt.byline='Anyone with the link · '+(chosen?.[1]||expiry||'No expiration');
      }
      return authorize(options);
    },
    execute,
    decline:decline||(()=>Promise.reject(new Error('Declining is unavailable.'))),
    onClose(){if(activeDialog===api)activeDialog=null;if(previousFocus?.isConnected)previousFocus.focus({preventScroll:true});onClose()},
  });
  activeDialog=api;return api;
}
