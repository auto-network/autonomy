/* Production and Design Studio use this one control. Extracted from revision
 * 42774731-9210-4e67-a0f6-e755bbeaf92f; only data/operation boundaries are wired. */
export const approvedMarkup = "\n<main class=\"surface\" x-data=\"window.FIXTURE\"><div class=\"sheet\" role=\"dialog\" aria-modal=\"true\" aria-labelledby=\"title\">\n<div class=\"brand\"><img id=\"org-image\" src=\"/static/icon-192.png\" alt=\"\"><span id=\"org-fallback\" class=\"brand-fallback\" hidden>A</span><span id=\"org-name\">Autonomy</span><button id=\"close\" class=\"close\" aria-label=\"Close approval\">×</button></div>\n<div id=\"review\"><h1 id=\"title\"></h1><p class=\"intro\" id=\"intro\"></p>\n<div id=\"membership-review\" class=\"identity-review\" hidden></div><p id=\"review-unavailable\" class=\"review-unavailable\" role=\"status\" hidden></p>\n<div class=\"resource\"><p class=\"eyebrow\" id=\"resource-label\"></p><p class=\"resource-name\" id=\"resource\"></p><p class=\"resource-detail\" id=\"resource-detail\"></p><button class=\"resource-link\" id=\"preview-button\" hidden>Read the note</button><div class=\"preview\" id=\"preview\" hidden></div><div id=\"fleet-check\" hidden><p class=\"code\" id=\"code\"></p><label class=\"review-choice\"><input type=\"checkbox\" id=\"code-matches\"><span>The code matches the one on the new machine.</span></label><label class=\"machine-label\">Machine name<input id=\"machine-name\" maxlength=\"120\" autocomplete=\"off\"></label></div></div>\n<div class=\"facts\" id=\"facts\"></div>\n<a class=\"from\" id=\"requester-link\" target=\"_top\"><span class=\"avatar\" id=\"requester-avatar\"><svg viewBox=\"0 0 24 24\"><rect x=\"3\" y=\"4\" width=\"18\" height=\"13\" rx=\"3\"/><path d=\"M8 21h8m-4-4v4m-6-10 3 3-3 3m6 0h5\"/></svg></span><div><p class=\"eyebrow\" id=\"requester-kind\">Requesting session</p><p class=\"name\" id=\"requester-name\"></p><p class=\"byline\" id=\"requester-byline\"></p></div></a>\n<p class=\"consequence\" id=\"consequence\"></p><label class=\"review-choice\" id=\"retention\" hidden><input type=\"checkbox\" id=\"retain-authority\"><span>Allow approvals this session without unlocking again.</span></label>\n</div>\n<div class=\"auth\" id=\"auth\" role=\"group\" aria-label=\"Authorization\" tabindex=\"-1\" hidden><div class=\"auth-content\">\n<form id=\"password-form\"><div class=\"password-label\"><label for=\"password\">Password</label><span id=\"password-tag\" class=\"verified\"></span></div><div class=\"password-entry\" id=\"password-entry\"><input id=\"password\" type=\"password\" placeholder=\"Enter your password\" autocomplete=\"current-password\" required><button class=\"verify\" type=\"submit\">Verify</button></div><p class=\"verified-line\" id=\"password-done\" hidden>✓ Password verified</p></form>\n<div class=\"separator\" id=\"separator\">OR</div><button class=\"passkey\" id=\"passkey\"><svg viewBox=\"0 0 24 24\"><circle cx=\"9\" cy=\"8\" r=\"3\"/><path d=\"M3 20v-2a6 6 0 0 1 12 0v2m3-12v6m-3-3h6\"/></svg><span id=\"passkey-copy\">Use a passkey</span></button></div></div>\n<div class=\"status\" role=\"status\" aria-live=\"polite\" id=\"status\" hidden></div><p class=\"error\" role=\"alert\" id=\"error\" hidden></p>\n<div class=\"result\" id=\"result\" hidden><div class=\"result-mark\"><svg viewBox=\"0 0 24 24\"><path d=\"m5 12 4 4L19 6\"/></svg></div><h2 id=\"result-title\"></h2><p id=\"result-copy\"></p></div>\n<div id=\"decline-confirm\" hidden><h1>Decline this request?</h1><p class=\"decline-text\">The requester will be told that you declined. Closing this window leaves the request unanswered.</p></div>\n<div class=\"buttons\"><button class=\"secondary\" id=\"secondary\">Decline</button><button class=\"primary\" id=\"primary\">Approve</button></div>\n</div></main>\n";
export const approvedStyles = "\n*{box-sizing:border-box}[hidden]{display:none!important}:root{color-scheme:dark}body{margin:0;background:#080b12;color:#e9eaf0;font:14px/1.5 ui-sans-serif,-apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif}button,input,select{font:inherit}button{cursor:pointer}button:disabled{cursor:default;opacity:.5}button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid #b9aaff;outline-offset:3px}\n.surface{min-height:100dvh;display:flex;align-items:flex-end;justify-content:center;padding-top:24px}.sheet{width:100%;max-width:460px;max-height:calc(100dvh - 24px);overflow:auto;background:#11141c;border:1px solid #2b2e3c;border-radius:22px 22px 0 0;padding:24px 24px max(20px,env(safe-area-inset-bottom))}.brand{display:flex;align-items:center;gap:9px;color:#c3c4d0;font-size:12px}.brand img{width:24px;height:24px;border-radius:7px;object-fit:cover}.brand-fallback{width:24px;height:24px;display:grid;place-items:center;border-radius:7px;background:#6c63ff;color:white;font-weight:700}.brand .close{margin-left:auto;font-size:23px;padding:0 4px;background:none;border:0;color:#9398ab;line-height:1}\nh1{font-size:24px;line-height:1.22;letter-spacing:-.6px;font-weight:650;margin:25px 0 11px}p{margin:0}.intro{color:#a8adbd;font-size:14px;line-height:1.65}.resource{margin-top:24px;padding-bottom:22px;border-bottom:1px solid #2b2e3c}.eyebrow{color:#939aad;font-size:11px;font-weight:500;margin:0 0 6px}.resource-name{font-size:17px;font-weight:600;line-height:1.45;color:#e9eaf0;overflow-wrap:anywhere}.resource-detail{color:#a2a9ba;font-size:12px;margin-top:5px;line-height:1.65}.facts{padding:16px 0;border-bottom:1px solid #2b2e3c;display:grid;gap:12px}.fact{display:flex;align-items:baseline;justify-content:space-between;gap:20px;font-size:12px}.fact span:first-child{color:#939aad}.fact span:last-child{color:#d8dbe5;text-align:right}.fact select{max-width:65%;background:#11141c;border:1px solid #404456;border-radius:7px;color:#e4e6ee;padding:5px 8px;font-size:12px}.from{display:flex;gap:11px;align-items:center;padding:19px 0}.avatar{width:34px;height:34px;flex:none;border-radius:10px;display:grid;place-items:center;color:#beb5ff;background:#27233b}.avatar svg{width:17px;height:17px;stroke:currentColor;fill:none;stroke-width:1.7}.from .name{font-size:13px;font-weight:600}.from .byline{font-size:11px;color:#939aad;margin-top:2px}.from .eyebrow{margin:0 0 2px;font-size:10px}.consequence{font-size:12px;color:#a6adbf;line-height:1.7;padding:0 0 6px}.resource-link{border:0;background:none;color:#bfb3ff;font-size:12px;padding:8px 0 0}.preview{margin:8px 0 0;padding:12px 0;border-top:1px solid #303443;font-size:12px;color:#bec4d2;line-height:1.7}\n.review-choice{display:flex;align-items:flex-start;gap:9px;color:#a6adbf;font-size:12px;margin:14px 0}.review-choice input{width:15px;height:15px;margin:3px 0 0;flex:none;accent-color:#9b89f4}.code{font-size:19px;letter-spacing:1px;color:#d5ccff;font-weight:600;margin-top:10px}.machine-label{display:block;font-size:12px;color:#b6bdcc;margin-top:14px}.machine-label input{display:block;width:100%;margin-top:6px;height:42px;background:#10131b;border:1px solid #414657;border-radius:8px;color:#f0f1f5;padding:0 11px}\n.auth{padding:17px 0 0;border-top:1px solid #2b2e3c;margin-top:12px}.auth-title{font-size:12px;color:#c8c6d7;margin-bottom:14px}.password-label{display:flex;align-items:center;justify-content:space-between;font-size:12px;color:#b4bccd;margin-bottom:7px}.verified{color:#8cdbb7;font-size:11px}.password-entry{display:flex;gap:8px}.password-entry input{min-width:0;width:100%;height:43px;border:1px solid #414657;background:#0e121b;border-radius:8px;padding:0 11px;color:#f0f1f5;font-size:16px}.password-entry input::placeholder{font-size:13px;color:#858da2}.verify{border:1px solid #6f609f;border-radius:8px;background:#29223f;color:#e5dfff;padding:0 13px;font-size:12px;font-weight:600}.separator{display:flex;align-items:center;gap:12px;color:#8c94a7;font-size:10px;letter-spacing:.8px;margin:16px 0}.separator:before,.separator:after{content:'';height:1px;flex:1;background:#2b2e3c}.passkey{width:100%;display:flex;align-items:center;gap:9px;justify-content:center;min-height:42px;background:none;border:0;color:#d4cafa;font-size:13px;font-weight:500}.passkey svg{height:18px;width:18px;fill:none;stroke:currentColor;stroke-width:1.7}.passkey.done{color:#8cdbb7;opacity:1}.verified-line{color:#8cdbb7;font-size:12px;padding:9px 0}.buttons{display:flex;gap:10px;margin-top:22px}.buttons button{min-height:44px;border-radius:9px;font-size:13px;font-weight:600}.primary{flex:1.6;background:#7663da;border:1px solid #9380ee;color:#fff}.secondary{flex:1;background:none;border:1px solid #363c4b;color:#abb3c3}.status{font-size:12px;color:#bdb4e3;display:flex;align-items:center;justify-content:center;gap:8px;margin-top:16px;min-height:20px}.spinner{width:13px;height:13px;border:2px solid #514b6c;border-top-color:#d2c3ff;border-radius:50%;animation:spin .8s linear infinite}.error{font-size:12px;line-height:1.6;color:#f2b1af;margin-top:13px}.result{text-align:center;padding:30px 0 12px}.result-mark{margin:0 auto 20px;width:46px;height:46px;border-radius:50%;background:#15372c;display:grid;place-items:center;color:#8cdbb7}.result-mark svg{width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:1.8}.result h2{font-size:21px;margin:0 0 8px}.result p{font-size:13px;line-height:1.7;color:#a8b2c3}.retained{font-size:12px;color:#9dceb7;margin-top:12px}.decline-text{font-size:13px;color:#a9b1c2;margin:18px 0;line-height:1.7}@keyframes spin{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){.spinner{animation:none}}@media(min-width:768px){.surface{padding:28px;align-items:center}.sheet{border-radius:20px;max-height:calc(100dvh - 56px);padding:28px}}\n.auth{padding:0;margin:18px -24px 0;border-top:1px solid #374564;border-bottom:1px solid #2c3b57;background:#182236;box-shadow:inset 0 1px 0 #ffffff05;overflow:hidden}.auth-content{padding:20px 24px}.passkey{min-height:44px;border:1px solid #52668a;border-radius:9px;background:#253957;color:#e0ebff;box-shadow:0 1px 2px #0003}.passkey:hover:not(:disabled){background:#30496f;border-color:#7490bd}.primary{background:#326add;border-color:#5988eb}.primary:hover:not(:disabled){background:#3975ef}.auth .password-entry input{background:#101a2b;border-color:#425573}.auth .verify{background:#293c60;border-color:#506b98;color:#e1ebff}.auth .separator{color:#a0b0cc}.auth .separator:before,.auth .separator:after{background:#344665}@media(min-width:768px){.auth{margin-left:-28px;margin-right:-28px}.auth-content{padding-left:28px;padding-right:28px}}\n.auth:focus{outline:none}\n.identity-review{display:grid;gap:16px;padding:22px 0;border-bottom:1px solid #2b2e3c}.identity-row{display:flex;align-items:center;gap:12px}.identity-row .avatar{border-radius:50%;overflow:hidden}.identity-row img{width:100%;height:100%;object-fit:cover}.identity-row strong{font-size:14px}.identity-row p{font-size:12px;color:#a8adbd}.identity-row .eyebrow{font-size:11px;margin-bottom:3px}.result-mark.neutral{background:#1b2d4b;color:#99baff}.result-mark.failed{background:#39232c;color:#e7a0ac}.review-unavailable{margin-top:16px;padding:12px 14px;background:#282333;border-radius:9px;color:#dec8ad;font-size:12px}\n.primary.cached{background:#23744e;border-color:#45986e}.primary.cached:hover:not(:disabled){background:#2b8259}\n.primary.cached.pending{opacity:1;background-image:linear-gradient(#399e6d,#399e6d);background-repeat:no-repeat;background-size:0% 100%;animation:cached-fill 1.5s linear forwards}@keyframes cached-fill{to{background-size:100% 100%}}@media(prefers-reduced-motion:reduce){.primary.cached.pending{animation:none;background-size:0% 100%}}\n.resource{position:relative}.resource:has(.resource-link:not([hidden])){padding-right:44px}.resource-link{position:absolute;right:0;top:12px;width:44px;height:44px;display:grid;place-items:center;padding:0;border-radius:8px}.resource-link:hover{background:#ffffff08}.resource-link svg{width:18px;height:18px}\n.result-mark.declined{background:#39232c;color:#e7a0ac}\n.from{text-decoration:none;color:inherit;border-radius:8px}.from[href]:hover{background:#ffffff06}.from[href]:focus-visible{outline:2px solid #99baff;outline-offset:4px}.from[href]::after{content:'↗';margin-left:auto;color:#a6c4ff}\n.receipt{margin-top:22px;padding-top:18px;border-top:1px solid #2b2e3c}.receipt-name{font-size:15px;font-weight:600;color:#e9eaf0;overflow-wrap:anywhere}.result .receipt-byline{font-size:12px;margin-top:5px;min-height:3.4em}.receipt-link{display:inline-flex;align-items:center;justify-content:center;min-height:44px;color:#a6c4ff;background:none;border:0;font-size:12px;padding:8px 4px}.receipt-link:disabled{opacity:0;pointer-events:none}.receipt-preview{margin-top:14px;border-top:1px solid #2b2e3c;padding-top:16px;text-align:left}.receipt-preview h3{font-size:15px;margin:0 0 8px}.result .receipt-preview p{min-height:0;white-space:pre-line;font-size:12px}.receipt-preview button{display:block;margin:12px 0 0;color:#a6c4ff;background:none;border:0;min-height:44px;padding:0;font-size:12px}\n.result-mark.working{background:#1b2d4b;color:#99baff}.result-mark.working svg{animation:spin 1s linear infinite}.result h2:focus{outline:none}.result p{min-height:3.4em}@media(prefers-reduced-motion:reduce){.result-mark.working svg{animation:none}}\n.auth-overlay{position:fixed;inset:0;z-index:2000;background:#04091480;display:flex;align-items:flex-end;justify-content:center;padding:16px;overscroll-behavior:contain}.auth-panel{width:100%;max-width:460px;max-height:calc(100dvh - 32px);overflow:auto;background:#182236;border:1px solid #425574;border-radius:18px;box-shadow:0 22px 80px #0009;padding:18px 20px max(16px,env(safe-area-inset-bottom))}.auth-panel-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;color:#e0e9f8;font-size:15px;font-weight:600}.auth-panel-close{border:0;background:none;color:#b4c3dc;font-size:23px;line-height:1;padding:2px 5px}.auth-panel .auth{margin:0;border:0;box-shadow:none;background:none;overflow:visible}.auth-panel .auth-content{padding:0}.auth-back{margin-top:18px;min-height:42px;width:100%;border:1px solid #455b7b;border-radius:9px;background:transparent;color:#b9c9e2;font-size:12px}.auth-panel .status{margin-top:14px}\n.sheet.desktop-auth .buttons{background:#182236;margin:0 -28px -28px;padding:0 28px 24px}.sheet.desktop-auth .auth{border-bottom:0}.sheet.desktop-auth .secondary{border-color:#455b7b;color:#b9c9e2}\n.request-detail{margin-top:18px;border-top:1px solid #2b2e3c;padding-top:16px}.request-detail pre{white-space:pre-wrap;overflow-wrap:anywhere;font-family:inherit;font-size:13px;line-height:1.65;color:#c6d1e4;margin:0;padding:12px;background:#151d2b;border-radius:9px}\n";
export function mountApprovalExperiment(host,input,services=null){
 const realDocument=host.ownerDocument, root=host.attachShadow({mode:'open'});
 root.innerHTML='<style>'+approvedStyles.replace(/body\{/g,':host{').replace(/:root\{/g,':host{')+'</style>'+approvedMarkup;
 const document={getElementById:id=>root.getElementById(id),querySelector:s=>root.querySelector(s),querySelectorAll:s=>root.querySelectorAll(s),createElement:t=>realDocument.createElement(t),createTextNode:t=>realDocument.createTextNode(t),createComment:t=>realDocument.createComment(t),addEventListener:(...args)=>root.addEventListener(...args),body:root,get activeElement(){return root.activeElement}};
 const window=realDocument.defaultView;
 let controller=null,factorState=null,disposed=false;
 function dispose(){if(disposed)return;disposed=true;controller?.abort();token++;host.remove();services?.onClose?.()}
 function expandAuthorization(){if(services){void authorizeRequest();return;}displayAuthorization()}
 async function authorizeRequest(){
   $('retain-authority').disabled=true;
   const attempt=controller=new AbortController();let shown=false;
   try{
    const decision=await services.authorize({signal:attempt.signal,view(state){
      if(attempt.signal.aborted||disposed)return;factorState=state;model.policy=state.policy;done=new Set(state.done);phase=state.busy?'verifying':'collecting';
      if(!shown){shown=true;displayAuthorization()}renderAuth();status(state.busy?'Confirming your identity…':'',state.busy);$('error').hidden=!state.error;$('error').textContent=state.error||'';
    },onAuthenticated(){if(attempt.signal.aborted||disposed)return;phase='executing';showResult(model.working,'',true);$('result-title').tabIndex=-1;$('result-title').focus({preventScroll:true})}},reviewSnapshot);
    if(attempt.signal.aborted||disposed)return;
    phase='executing';showResult(model.working,'',true);
    const response=await services.execute(decision);if(!disposed)finishResponse(response);
   }catch(error){if(attempt.signal.aborted||disposed)return;if(phase==='executing'){operationFailed(error.message)}else{if(mobileLayer)dismissMobileLayer();else collapseDesktopAuth();if(error.message!=='Approval cancelled.'){$('error').hidden=false;$('error').textContent=error.message}}}
 }
 function operationFailed(message){complete('Could not complete the request',message);resultTone('failed');$('result-copy').hidden=false;}
 async function decideDecline(){phase='executing';showResult('Declining request…','',true);try{await services.decline();phase='decline';complete('Request declined','The requester has been told that you declined.')}catch(error){operationFailed(error.message)}}

const F=id=>({op:'factor',factor_id:id}),either={op:'or',children:[F('password'),F('passkey')]};
const org={name:'Autonomy',image:'/static/icon-192.png'};
const session={kind:'Requesting session',name:'Approval library — design contracts',byline:'Developer workspace',href:'/session/autonomy-codex/auto-0911-112429'};
const recipes={
operation:{kind:'operation',title:'Authorize this change?',intro:'Review the change before continuing.',facts:[],requester:{kind:'Requested by',name:'You',byline:'Autonomy'},working:'Applying change…',success:'Change saved',result:'The change has been saved.'},
vault:{kind:'vault',title:'Share this saved value?',intro:'Allow this session to receive the saved value shown below.',resourceLabel:'Saved value',resource:'staging-deployment',facts:[['Delivered file available for','15 minutes'],['Request expires','In 5 minutes']],requester:session,consequence:'The session receives the saved value. Removing the delivered file later does not revoke copies it may have made.',action:'Share credential',working:'Sharing credential…',success:'Value shared',result:'The saved value was delivered to the requesting session.'},
service:{kind:'service',title:'Allow service access?',intro:'Let this device send screenshots to your dropbox.',resourceLabel:'Application',resource:'Autonomy Capture',resourceDetail:'Add screenshots to your dropbox',facts:[['Access lasts','duration']],requester:{kind:'Requesting device',name:'My iPhone',byline:'Autonomy Capture'},consequence:'This grants screenshot-upload access, not access to browse your dashboard.',action:'Allow access',working:'Allowing access…',success:'Access allowed',result:'My iPhone can send screenshots to your dropbox for one year.'},
fleet:{kind:'fleet',title:'Add this machine?',intro:'Compare the code with the one on the new machine before adding it to your fleet.',resourceLabel:'New machine',resource:'Studio Mac',resourceDetail:'Only approve a machine you recognize.',facts:[],requester:{kind:'Joining machine',name:'Studio Mac',byline:'Your personal fleet'},code:'RIVER · MAPLE · SEVEN',consequence:'The machine will become a member of your personal fleet.',action:'Add machine',working:'Adding machine…',success:'Machine added',result:'The machine is now part of your fleet.'},
member:{kind:'member',title:'Approve this membership?',intro:'Review who is joining and the invitation they received.',facts:[],requester:{kind:'Person joining',name:'Jordan',byline:'Product designer'},claim:{introduction:{display_name:'Jordan',byline:'Product designer'},granted_role:'Member',have:0,need:2},sponsor:{display_name:'Avery',byline:'Engineering lead'},consequence:'Your approval counts toward this request. Membership starts only after all required approvals and the joining steps are complete.',action:'Approve membership',working:'Recording approval…',success:'Approval recorded',result:'Your approval has been recorded.'},
link:{kind:'link',title:'Publish a share link?',intro:'Create a link to this note for someone outside your workspace.',resourceLabel:'Note',resource:'Authentication control specification',resourceDetail:'Published through auto.network',facts:[['Prepared for','Jordan'],['Link expires','duration']],requester:{kind:'Requesting session',name:'Approval library — design contracts',byline:'Developer workspace'},consequence:'Anyone who has the link can open the shared content until it expires.',action:'Publish link',working:'Publishing link…',success:'Link published',result:'The published link is ready for the requesting session.',preview:'Authentication control specification\n\nA consistent review and authentication experience for approvals across the platform.'},
central:{kind:'central',title:'Allow dashboard access?',intro:'This lets the session use your signed-in dashboard through a browser.',facts:[['Access expires','Today at 6:00 PM']],requester:{kind:'Requesting session',name:'Refine approval experience',byline:'Developer workspace'},action:'Allow access',working:'Allowing dashboard access…',success:'Access allowed',result:'The requesting session can now access your dashboard.'}
};

// Sample requesting-session destinations; production request data supplies these URLs.
recipes.central.requester.href=session.href;recipes.link.requester.href=session.href;
recipes.link.resourceDetail='On auto.network';
const $=id=>document.getElementById(id),wait=ms=>new Promise(r=>setTimeout(r,ms));let model,phase,done,token=0,reviewSnapshot;
function contains(id,n=model.policy){return n.op==='factor'?n.factor_id===id:n.children.some(c=>contains(id,c))}
function satisfied(n=model.policy){return n.op==='factor'?done.has(n.factor_id):n.children[n.op==='and'?'every':'some'](c=>satisfied(c))}
function status(text,spin=false){$('status').replaceChildren();$('status').hidden=!text;if(spin){const s=document.createElement('span');s.className='spinner';s.setAttribute('aria-hidden','true');$('status').append(s)}$('status').append(document.createTextNode(text))}
function renderAuth(){const busy=['verifying','executing'].includes(phase);$('password-form').hidden=!contains('password');$('passkey').hidden=!contains('passkey');$('separator').hidden=!(contains('password')&&contains('passkey'));$('separator').textContent=model.policy.op==='and'?'AND':'OR';$('password-entry').hidden=done.has('password');$('password-done').hidden=!done.has('password');$('password').disabled=busy||done.has('password');document.querySelector('.verify').disabled=busy||done.has('password');$('passkey').disabled=busy||done.has('passkey');$('passkey').classList.toggle('done',done.has('passkey'));$('passkey-copy').textContent=done.has('passkey')?'✓ Passkey verified':'Use a passkey'}
function init(data){resetDesktopAnchor();restoreMobileLayer();token++;$('auth').getAnimations().forEach(a=>a.cancel());$('auth').style.height='';model={...recipes[data.kind||'vault'],...data,policy:data.policy||either,organization:data.organization||org};$('retain-authority').checked=!!model.allowSessionApprovals;phase='review';done=new Set();reviewSnapshot=null;document.querySelector('.sheet').hidden=false;for(const id of ['auth','result','error','status','decline-confirm','preview'])$(id).hidden=true;$('review').hidden=false;$('primary').hidden=false;$('primary').disabled=false;$('secondary').disabled=false;$('close').disabled=false;$('primary').textContent='Authorize';$('secondary').textContent='Decline';$('password').value='';$('org-name').textContent=model.organization.name;$('org-image').src=model.organization.image;$('org-image').hidden=false;$('org-fallback').hidden=true;$('org-fallback').textContent=model.organization.name[0];$('org-image').onerror=()=>{$('org-image').hidden=true;$('org-fallback').hidden=false};
for(const [id,key]of [['title','title'],['intro','intro'],['resource-label','resourceLabel'],['resource','resource'],['resource-detail','resourceDetail'],['consequence','consequence']])$(id).textContent=model[key]||'';
document.querySelector('.resource').hidden=!model.resource;
$('request-detail')?.remove();if(model.reviewText){const detail=document.createElement('div');detail.id='request-detail';detail.className='request-detail';const heading=document.createElement('p');heading.className='eyebrow';heading.textContent=model.reviewLabel||'Change';const text=document.createElement('pre');text.textContent=model.reviewText;detail.append(heading,text);$('review').append(detail)}
$('primary').classList.remove('pending');
$('primary').style.visibility='';$('secondary').hidden=false;document.querySelector('.sheet').setAttribute('aria-labelledby','title');$('result-title').setAttribute('aria-live','polite');
document.getElementById('receipt')?.remove();
$('requester-kind').textContent=model.requester.kind;$('requester-name').textContent=model.requester.name;$('requester-byline').textContent=model.requester.byline;
if(model.kind==='member'&&(!model.claim?.introduction?.display_name||!model.claim?.granted_role||!model.sponsor?.display_name))model.reviewUnavailable='The invitation or joining profile is incomplete. Approval is unavailable until the verified details can be shown.';
$('requester-link').hidden=model.kind==='member';renderMembership();
$('requester-link').removeAttribute('href');
if(model.requester.href&&/^\/(?!\/)/.test(model.requester.href))$('requester-link').href=model.requester.href;
$('requester-link').onclick=()=>{if($('requester-link').hasAttribute('href'))close()};
const identityIcons={member:'<circle cx="12" cy="8" r="3"/><path d="M5 21v-2a7 7 0 0 1 14 0v2"/>',service:'<path d="m12 3 8 5v8l-8 5-8-5V8Zm-8 5 8 5 8-5m-8 5v8"/>',fleet:'<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/>'};$('requester-avatar').innerHTML='<svg viewBox="0 0 24 24">'+(identityIcons[model.kind]||'<rect x="3" y="4" width="18" height="13" rx="3"/><path d="M8 21h8m-4-4v4m-6-10 3 3-3 3m6 0h5"/>')+'</svg>';
$('facts').replaceChildren();$('facts').hidden=!model.facts.length;for(const [label,value]of model.facts){const row=document.createElement('div');row.className='fact';const a=document.createElement('span');a.textContent=label;row.append(a);if(value==='duration'){const s=document.createElement('select');s.id='duration';s.setAttribute('aria-label',model.kind==='service'?'Access duration':'Link expiration');for(const [v,t]of (model.durationOptions|| (model.kind==='service'?[['86400','1 day'],['604800','1 week'],['2592000','1 month'],['31536000','1 year'],['315360000','10 years'],['','No expiry']]:[['604800','In 1 week'],['2592000','In 1 month'],['31536000','In 1 year']]))){const o=document.createElement('option');o.value=v;o.textContent=t;s.append(o)}if(model.kind==='service')s.value='31536000';if(model.duration!=null)s.value=model.duration;row.append(s)}else{const b=document.createElement('span');b.textContent=value;row.append(b)}$('facts').append(row)}
$('fleet-check').hidden=model.kind!=='fleet';$('code').textContent=model.code||'';$('code-matches').checked=false;$('code-matches').disabled=false;$('machine-name').value=model.kind==='fleet'?model.resource:'';$('machine-name').disabled=false;renderTargetLink();checkFleet();renderAuth();window.ready=true;}
function checkFleet(){ $('retention').hidden=!model.showRetention;$('primary').classList.toggle('cached',phase==='review'&&!!model.retained);if(phase==='review'){$('retain-authority').disabled=false;$('secondary').hidden=model.canDecline===false;$('primary').disabled=!!model.reviewUnavailable||model.kind==='fleet'&&(!$('code-matches').checked||!$('machine-name').value.trim())}}
function renderMembership(){
 const host=$('membership-review');host.replaceChildren();host.hidden=model.kind!=='member';
 $('review-unavailable').hidden=!model.reviewUnavailable;$('review-unavailable').textContent=model.reviewUnavailable||'';
 if(host.hidden)return;
 const identity=(label,profile)=>{const row=document.createElement('div');row.className='identity-row';const avatar=document.createElement('span');avatar.className='avatar';avatar.textContent=profile?.display_name?.split(/\s+/).map(x=>x[0]).slice(0,2).join('')||'?';
 if(profile?.avatar?.startsWith('/api/attachment/')){const initials=avatar.textContent,img=document.createElement('img');img.src=profile.avatar;img.alt='Profile photo of '+profile.display_name;img.onerror=()=>{avatar.textContent=initials};avatar.replaceChildren(img)}
 const text=document.createElement('div'),eyebrow=document.createElement('p'),name=document.createElement('strong'),byline=document.createElement('p');eyebrow.className='eyebrow';eyebrow.textContent=label;name.textContent=profile?.display_name||'Profile unavailable';byline.textContent=profile?.byline||'';text.append(eyebrow,name,byline);row.append(avatar,text);host.append(row)};
 identity('Invited by',model.sponsor);identity('Person joining',model.claim?.introduction);
 const role=document.createElement('div');role.className='fact';const label=document.createElement('span'),value=document.createElement('span');label.textContent='Joining as';value.textContent=model.claim?.granted_role||'Role unavailable';role.append(label,value);host.append(role);
}
function renderTargetLink(){
 const button=$('preview-button');button.hidden=!(model.kind==='link'&&model.resultHref);
 button.setAttribute('aria-label','Open note');button.title='Open note';
 // Existing published-links.js open icon.
 button.innerHTML='<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 5h5v5M19 5l-9 9"/><path d="M19 13v6H5V5h6"/></svg>';
}
function close(){if(phase==='executing')return;controller?.abort();if(mobileLayer){dismissMobileLayer();return;}token++;$('password').value='';done.clear();document.querySelector('.sheet').hidden=true;if(services)dispose()}
function showResult(title,copy,working,declined=false){
 restoreMobileLayer();document.querySelector('.sheet').classList.remove('desktop-auth');$('password').blur();$('password').value='';
 $('review').hidden=true;$('auth').hidden=true;$('decline-confirm').hidden=true;$('error').hidden=true;status('');
 $('result').hidden=false;$('result-title').textContent=title;$('result-copy').hidden=false;$('result-copy').textContent=copy;
 renderReceipt(working,!declined);
 const mark=document.querySelector('.result-mark');mark.classList.toggle('working',working);mark.setAttribute('aria-hidden','true');
 mark.classList.remove('neutral','failed');
 mark.classList.toggle('declined',declined);
 mark.innerHTML=working?'<svg viewBox="0 0 24 24"><path d="M20 7v5h-5M4 17v-5h5M6.1 6.1A8 8 0 0 1 20 12M4 12a8 8 0 0 0 13.9 5.9"/></svg>':'<svg viewBox="0 0 24 24"><path d="m5 12 4 4L19 6"/></svg>';
 if(declined)mark.innerHTML='<svg viewBox="0 0 24 24"><path d="m7 7 10 10M17 7 7 17" stroke-linecap="round"/></svg>';
 $('result').setAttribute('aria-busy',String(working));
 $('primary').hidden=false;$('primary').disabled=working;$('primary').textContent='Done';$('primary').style.visibility=working?'hidden':'';
 $('primary').classList.remove('cached','pending');
 $('secondary').hidden=true;$('close').disabled=working;
 document.querySelector('.sheet').setAttribute('aria-labelledby','result-title');
}
function complete(title,copy){const declined=phase==='decline';phase='complete';done.clear();showResult(title,copy,false,declined)}
function renderReceipt(working,approved){
 $('receipt')?.remove();if(!approved)return;
 const request=reviewSnapshot||model;
 const duration={'86400':'1 day','604800':'1 week','2592000':'1 month','31536000':'1 year','315360000':'10 years','':'No expiry'}[request.duration]||'1 week';
 const orgName=model.organization.name;
 const entries={
 vault:{name:model.resource,byline:`Shared with ${model.requester.name}`,pending:`Sharing with ${model.requester.name}`,link:'View requesting session'},
 central:{name:model.requester.name,byline:'Dashboard access until today at 6:00 PM',link:'View requesting session',title:model.requester.name,detail:`${model.requester.byline}\n\nDashboard access approved until today at 6:00 PM.`},
 link:{name:model.resource,byline:`Anyone with the link · Expires in ${duration}`,link:'View '+(model.resourceLabel||'item').toLowerCase(),title:model.resource,detail:model.preview},
 member:{name:model.claim?.introduction?.display_name||'Joining person',byline:`${model.claim?.granted_role||'Role unavailable'} · ${orgName}`,pending:`${model.claim?.granted_role||'Role unavailable'} · ${orgName}`,link:'View membership'},
 fleet:{name:request.machineName||model.resource,byline:'Added to your personal fleet',pending:'Adding to your personal fleet',link:'View machine',title:request.machineName||model.resource,detail:'Member of your personal fleet'},
 service:{name:model.requester.name,byline:`Autonomy Capture · Upload screenshots · ${duration}`,link:'View service access'}
 };
 const entry=model.receipt||entries[model.kind]||{name:model.resource||model.title,byline:model.result,link:'View item'};
 if(model.kind==='member')$('result-copy').hidden=false;
 // The same compact receipt stays in place through progress and completion.
 $('result-copy').hidden=model.kind!=='member';
 const receipt=document.createElement('div');receipt.id='receipt';receipt.className='receipt';
 const name=document.createElement('div');name.className='receipt-name';name.textContent=entry.name;
 const byline=document.createElement('p');byline.className='receipt-byline';byline.textContent=working?(entry.pending||entry.byline):entry.byline;
 receipt.append(name,byline);
 // Navigation belongs to the PWA, never to a second view inside this dialog.
 if(typeof model.resultHref==='string'&&/^\/(?!\/)/.test(model.resultHref)){
  const link=document.createElement('a');link.className='receipt-link';link.textContent=(model.kind==='fleet'?'View fleet':entry.link)+' →';
  link.target='_top';link.style.textDecoration='none';
  if(working){link.style.visibility='hidden';link.setAttribute('aria-hidden','true')}else link.href=model.resultHref;
  receipt.append(link);
 }
 $('result').append(receipt);
}
async function execute(t){
 if(services){await authorizeRequest();return;}
 phase='executing';renderAuth();
 showResult(model.kind==='central'?'Granting access…':model.working,'Please keep this window open.',true);
 $('result-title').tabIndex=-1;$('result-title').focus({preventScroll:true});
 await wait(800);if(t!==token)return;
 const response=Object.hasOwn(model,'operationResponse')?model.operationResponse:(model.kind==='member'?{status:'pending',have:1,need:2}:{approved:true,execution:{ok:true}});
 finishResponse(response,false);
}
function resultTone(tone){const mark=document.querySelector('.result-mark');mark.classList.remove('working','declined');mark.classList.add(tone);mark.innerHTML=tone==='failed'?'<svg viewBox="0 0 24 24"><path d="m7 7 10 10M17 7 7 17"/></svg>':'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 7v5l3 2"/></svg>'}
function finishResponse(response,fromStatus=false){
 if(services&&response?.execution?.ok!==true){operationFailed(response?.execution?.error||'The operation did not return a confirmed result.');return}
 if(!response){complete('Status not confirmed','We could not confirm the outcome. Check the request status before trying again.');resultTone('neutral');$('result-copy').hidden=false;document.querySelector('.receipt-byline')?.remove();$('secondary').hidden=false;$('secondary').disabled=false;$('secondary').textContent='Check status';return}
 if(response.execution?.ok===false){complete('Action could not finish','The operation reported a failure. Review its status before submitting another request.');resultTone('failed');$('result-copy').hidden=false;document.querySelector('.receipt-byline')?.remove();return}
 if(model.kind==='member'){
  if(response.status==='admitted'&&fromStatus){complete('Member joined',`${model.claim.introduction.display_name} has joined ${model.organization.name}.`);return}
  if(response.status==='absent'){complete('Request no longer available','This joining request is no longer available. Ask the person joining to check their invitation.');resultTone('neutral');return}
  if(response.status==='rejected'){complete('Approval not recorded',response.reason==='invite-expired'?'This invitation has expired. A new invitation is needed.':'This approval could not be accepted. Check the invitation and your permission to approve it.');resultTone('failed');return}
  if(response.status==='ready'){complete('Approval recorded','All required approvals are recorded. Their machine still needs to finish joining.');resultTone('neutral');$('secondary').hidden=false;$('secondary').disabled=false;$('secondary').textContent='Check status';return}
  if(response.status==='pending'&&Number.isInteger(response.have)&&Number.isInteger(response.need)){complete('Approval recorded',`${response.have} of ${response.need} approvals recorded. Membership is still pending.`);resultTone('neutral');return}
  finishResponse(null);return;
 }
 if(response.execution?.ok!==true){finishResponse(null);return}
 const chosen=reviewSnapshot?.duration;const accessDuration={'86400':'one day','604800':'one week','2592000':'one month','31536000':'one year','315360000':'ten years','':'no expiry'}[chosen]||'the approved duration';complete(model.success,model.kind==='service'?`${model.requester.name} can upload screenshots ${chosen===''?'with no expiry':'for '+accessDuration}.`:model.result);
}
async function cachedAuthorize(t){
 phase='confirming';$('primary').hidden=false;$('primary').disabled=false;
 $('primary').textContent='Authorizing…';$('primary').classList.add('cached','pending');$('secondary').textContent='Cancel';
 await wait(1500);if(t!==token||phase!=='confirming')return;
 await execute(t);
}
function revertCachedAuthorization(){
 if(phase!=='confirming')return;
 token++;phase='review';reviewSnapshot=null;
 $('primary').classList.remove('pending');$('primary').textContent='Authorize';$('secondary').textContent='Decline';
 if($('duration'))$('duration').disabled=false;
 $('machine-name').disabled=false;$('code-matches').disabled=false;checkFleet();
}

let mobileLayer=null,layerReturnFocus=null,layerScroll=0;
const authAnchor=document.createComment('authorization position');
$('auth').before(authAnchor);
const statusAnchor=document.createComment('status position');
$('status').before(statusAnchor);
function restoreMobileLayer(){
 if(!mobileLayer)return;
 const sheet=document.querySelector('.sheet');
 $('auth').hidden=true;
 authAnchor.after($('auth'));
 statusAnchor.after($('status'));$('status').after($('error'));
 mobileLayer.remove();mobileLayer=null;
 sheet.inert=false;sheet.setAttribute('aria-modal','true');sheet.scrollTop=layerScroll;
}
function dismissMobileLayer(){
 if(!mobileLayer||phase==='executing')return;controller?.abort();
 token++;$('password').value='';done.clear();status('');$('error').hidden=true;
 const focus=layerReturnFocus;
 restoreMobileLayer();phase='review';reviewSnapshot=null;
 if($('duration'))$('duration').disabled=false;
 $('machine-name').disabled=false;$('code-matches').disabled=false;
 $('primary').hidden=false;$('primary').textContent='Authorize';$('secondary').textContent='Decline';
 checkFleet();focus?.focus({preventScroll:true});
}
function openMobileLayer(){
 const sheet=document.querySelector('.sheet');
 layerScroll=sheet.scrollTop;layerReturnFocus=$('primary');
 mobileLayer=document.createElement('div');mobileLayer.className='auth-overlay';
 const panel=document.createElement('div');panel.className='auth-panel';
 panel.setAttribute('role','dialog');panel.setAttribute('aria-modal','true');panel.setAttribute('aria-label','Authorize request');
 const header=document.createElement('div');header.className='auth-panel-header';
 const title=document.createElement('span');title.textContent='Authorize';
 const closeButton=document.createElement('button');closeButton.className='auth-panel-close';closeButton.textContent='×';closeButton.setAttribute('aria-label','Back to request');closeButton.onclick=dismissMobileLayer;
 header.append(title,closeButton);
 const back=document.createElement('button');back.className='auth-back';back.textContent='Back to request';back.onclick=dismissMobileLayer;
 panel.append(header,$('auth'),$('status'),$('error'),back);mobileLayer.append(panel);
 mobileLayer.addEventListener('click',event=>{if(event.target===mobileLayer)dismissMobileLayer()});
 document.body.append(mobileLayer);
 sheet.inert=true;sheet.removeAttribute('aria-modal');
 $('auth').hidden=false;renderAuth();
 closeButton.focus({preventScroll:true});
 const reduced=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
 window.expansion=panel.animate([{transform:'translateY(24px)',opacity:0},{transform:'translateY(0)',opacity:1}],{duration:reduced?0:240,easing:'cubic-bezier(.22,1,.36,1)'});
}

function resetDesktopAnchor(){const surface=document.querySelector('.surface'),sheet=document.querySelector('.sheet');surface.style.alignItems='';surface.style.paddingTop='';sheet.style.maxHeight='';sheet.classList.remove('desktop-auth')}
function anchorDesktopReview(){const sheet=document.querySelector('.sheet'),surface=document.querySelector('.surface'),top=sheet.getBoundingClientRect().top;surface.style.alignItems='flex-start';surface.style.paddingTop=top+'px';sheet.style.maxHeight='calc(100dvh - '+(top+24)+'px)'}
function collapseDesktopAuth(){controller?.abort();token++;$('auth').getAnimations().forEach(a=>a.cancel());$('auth').hidden=true;$('password').value='';done.clear();status('');$('error').hidden=true;phase='review';reviewSnapshot=null;document.querySelector('.sheet').classList.remove('desktop-auth');$('primary').hidden=false;$('primary').textContent='Authorize';$('secondary').textContent='Decline';if($('duration'))$('duration').disabled=false;$('machine-name').disabled=false;$('code-matches').disabled=false;checkFleet();$('primary').focus({preventScroll:true})}
function displayAuthorization(){if(window.matchMedia('(max-width: 767px)').matches){openMobileLayer();return}const node=$('auth');document.querySelector('.sheet').classList.add('desktop-auth');$('primary').hidden=true;$('secondary').textContent='Back to request';node.hidden=false;renderAuth();const height=node.getBoundingClientRect().height,reduced=window.matchMedia('(prefers-reduced-motion: reduce)').matches;node.focus({preventScroll:true});window.expansion=node.animate([{height:'0px',opacity:0},{height:height+'px',opacity:1}],{duration:reduced?0:300,easing:'cubic-bezier(.22,1,.36,1)'});}

$('primary').onclick=()=>{if(phase==='complete'){close();return}if(phase==='decline'){if(services){void decideDecline();return;}complete('Request declined','The requester has been told that you declined.');return}if(phase!=='review'||$('primary').disabled)return;reviewSnapshot=structuredClone({...model,duration:$('duration')?.value,machineName:$('machine-name').value});if($('duration'))$('duration').disabled=true;$('machine-name').disabled=true;$('code-matches').disabled=true;phase='collecting';if(model.retained){void cachedAuthorize(token);return}if(!window.matchMedia('(max-width: 767px)').matches)anchorDesktopReview();expandAuthorization()};
$('secondary').onclick=async()=>{if(document.querySelector('.sheet').classList.contains('desktop-auth')&&['collecting','verifying'].includes(phase)){collapseDesktopAuth();return}if(phase==='complete'&&$('secondary').textContent==='Check status'){const t=token;$('secondary').disabled=true;$('secondary').textContent='Checking…';const response=services?await services.checkStatus().catch(()=>null):(await wait(500),model.statusResponse||null);if(t===token)finishResponse(response,true);return}if(phase==='review'){phase='decline';$('review').hidden=true;$('decline-confirm').hidden=false;$('primary').textContent='Decline request';$('primary').disabled=false;$('secondary').textContent='Go back'}else if(phase==='decline'){phase='review';$('review').hidden=false;$('decline-confirm').hidden=true;$('primary').textContent='Authorize';$('secondary').textContent='Decline';checkFleet()}else close()};
$('retain-authority').onchange=()=>{model.allowSessionApprovals=$('retain-authority').checked};$('close').onclick=close;$('code-matches').onchange=checkFleet;$('machine-name').oninput=checkFleet;
$('secondary').addEventListener('click',checkFleet);
// A repeat tap or Cancel during the grace interval returns to review only.
for(const id of ['primary','secondary'])$(id).addEventListener('click',event=>{
 if(phase==='confirming'){event.stopImmediatePropagation();revertCachedAuthorization()}
},true);
$('preview-button').onclick=()=>{if(model.kind==='link'&&model.resultHref){close();window.top.location.href=model.resultHref}};
async function verify(id){if(services){const value=$('password').value;$('password').blur();$('password').value='';if(id==='password')factorState?.password(value);else factorState?.passkey();return;}if(phase!=='collecting'||done.has(id))return;const t=token;$('error').hidden=true;phase='verifying';$('password').blur();$('password').value='';renderAuth();status('Confirming your identity…',true);await wait(650);if(t!==token)return;if(model.factorError?.[id]){phase='collecting';renderAuth();status('');$('error').textContent=model.factorError[id];$('error').hidden=false;return}done.add(id);if(satisfied()){await execute(t)}else{phase='collecting';renderAuth();status(id==='password'?'Password verified. Use your passkey to continue.':'Passkey verified. Enter your password to continue.')}}
$('password-form').onsubmit=e=>{e.preventDefault();void verify('password')};$('passkey').onclick=()=>void verify('passkey');
document.addEventListener('keydown',e=>{if(e.key==='Escape')close();if(e.key==='Tab'){const els=[...document.querySelectorAll('button,input,select,a[href]')].filter(n=>!n.disabled&&!n.closest('[hidden]')&&!n.closest('[inert]')&&n.getClientRects().length);const i=els.indexOf(document.activeElement);if(els.length&&((e.shiftKey&&i<=0)||(!e.shiftKey&&(i<0||i===els.length-1)))){e.preventDefault();(e.shiftKey?els.at(-1):els[0]).focus()}}});
init(input);

 return {root,close(){if(phase==='executing')return false;dispose();return true},dispose,update(data){init(data)}};
}
