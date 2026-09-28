/* link_publish / link_revoke review in the shared approval control (Central).
 *
 * The same sheet and the same authority as before (components/link-signing.js),
 * with one change of order the Central contract requires: the Grant is
 * recorded FIRST, and only then is the registry request signed, so the signed
 * envelope is always newer than the Grant (a pre-signed or replayed envelope
 * is refused as stale). Unlocking happens only when no retained session
 * already carries this organization. If the unlock is cancelled after the
 * Grant, nothing is published: the item waits as "Approved, not yet
 * published" and offers Publish / Revoke until its window closes.
 *
 * Only the machine that accepted the request can run the operation; elsewhere
 * the review says so instead of asking for an unlock. */
import {localHref, openApprovalDialog, requestingSession} from './approval-dialog.js';
import {_linkTtlText, _LINK_DURATION_VALUES, _matchingApprovalAuthority, _signLinkDecision} from './link-signing.js';

const ERRORS = {
  authority_refused: 'The signature was refused, so nothing was published. Try again.',
  not_actionable: 'This request is no longer waiting for approval.',
  window_closed: 'The time to publish has passed. The session must ask again.',
  elsewhere: 'Only the machine that received this request can carry it out.',
  stale_envelope: 'The approval was signed before it was granted. Try again.',
  running: 'This operation is already running.',
  invalid_request: 'This request is incomplete.',
  not_found: 'This request is no longer available.',
  unavailable: 'The Dashboard could not complete this request. Try again.',
};

async function readJson(url, init) {
  const response = await fetch(url, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(ERRORS[data.error] || data.error || 'This request is no longer available.');
    error.status = response.status;
    error.code = data.error || '';
    error.detail = typeof data.detail === 'string' ? data.detail : '';
    throw error;
  }
  return data;
}

function clock(epochSeconds) {
  return new Date(epochSeconds * 1000).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
}

/* What the review says for each operation state; '' means the action is open. */
export function linkReviewState(result, actions, op) {
  const state = result?.state || 'pending';
  const verb = op === 'revoke' ? 'revoked' : 'published';
  const machine = result?.machine_label || 'the machine that received the request';
  if (state === 'elsewhere') return {state, operate: false, unavailable: `Only ${machine} can carry this out. Open that machine's Dashboard.`};
  if (state === 'awaiting_operation') return {state, operate: true, unavailable: ''};
  if (state === 'expired_unexecuted') return {state, operate: false, unavailable: `Approved but not ${verb} in time.`};
  if (state === 'running') return {state, operate: false, unavailable: 'This operation is running.'};
  if (state === 'done') return {state, operate: false, unavailable: `This link was ${verb}.`};
  if (state === 'failed') return {state, operate: false, unavailable: result?.execution?.error || `The link could not be ${verb}.`};
  return {state, operate: false, unavailable: (actions || []).includes('granted') ? '' : 'This request is no longer available.'};
}

/* The sheet both link flows show: the signing request, the expiry choice
 * (fixed for invitations), who it is for, and anything that blocks it. */
function linkSheet(review, signing) {
  const op = review.op === 'revoke' ? 'revoke' : 'publish';
  const acting = review.acting_identity || {};
  const ttl = review.ttl == null ? null : Number(review.ttl);
  const current = ttl == null ? '604800' : String(ttl);
  const custom = ttl != null && !_LINK_DURATION_VALUES.has(current);
  const approval = {
    op, orgSlug: review.org || '', orgUuid: signing?.org_uuid || null,
    actingIdentity: acting, fixedExpiry: !!review.fixed_expiry,
    duration: custom ? 'custom' : current, customDurationSeconds: custom ? ttl : null,
    allowSessionApprovals: false,
    registryRequest: signing?.registry_request || null,
  };
  approval.allowSessionApprovals = _matchingApprovalAuthority(approval);
  const controls = document.createElement('div');
  if (op === 'publish' && !approval.fixedExpiry) {
    const row = document.createElement('label'); row.className = 'approval-fact';
    const name = document.createElement('span'); name.textContent = 'Expires';
    const select = document.createElement('select'); select.setAttribute('aria-label', 'Link expiration');
    const options = [['604800', 'In 1 week'], ['2592000', 'In 1 month'], ['31536000', 'In 1 year'], ['none', 'No expiration']];
    if (custom) options.unshift(['custom', _linkTtlText(ttl)]);
    for (const [value, text] of options) { const option = document.createElement('option'); option.value = value; option.textContent = text; select.append(option); }
    select.value = approval.duration;
    select.onchange = () => { approval.duration = select.value; };
    row.append(name, select); controls.append(row);
  }
  if (!approval.allowSessionApprovals) {
    const choice = document.createElement('label'); choice.className = 'approval-choice';
    const checkbox = document.createElement('input'); checkbox.type = 'checkbox';
    checkbox.onchange = () => { approval.allowSessionApprovals = checkbox.checked; };
    choice.append(checkbox, document.createTextNode('Allow approvals this session without unlocking again.'));
    controls.append(choice);
  }
  const facts = [];
  if (review.recipient?.display_name) facts.push(['Prepared for', review.recipient.display_name]);
  if (review.fixed_expiry && Number.isSafeInteger(review.absolute_expiry)) facts.push(['Link expires', new Date(review.absolute_expiry).toLocaleString()]);
  // A standing follow link never expires unless revoked (auto-eky23).
  else if (op === 'publish' && review.target_type === 'org:follow') facts.push(['Link expires', 'No expiration']);
  if (review.label) facts.push(['Label', review.label]);
  const blocking = signing?.blocking_error
    || (signing?.binding_drift ? 'This organization changed after the request was prepared. Close it and ask again.' : '')
    || (signing && !signing.registry_request ? 'This request is missing its auto.network details. Close it and try again.' : '');
  const target = {name: review.target_title || 'Share link', type: review.type_label || review.target_type || 'Item', byline: 'On auto.network'};
  const verb = op === 'revoke' ? 'Revoke' : 'Publish';
  return {op, approval, controls, facts, blocking, target, verb,
    organization: {name: acting.name || approval.orgSlug, image: acting.favicon || ''},
    working: op === 'revoke' ? 'Revoking link' : 'Publishing link',
    success: op === 'revoke' ? 'Link revoked' : 'Link published',
    failed: `The link could not be ${op === 'revoke' ? 'revoked' : 'published'}.`};
}

function operationBody(sheet, signed) {
  return sheet.op === 'publish' && !sheet.approval.fixedExpiry && 'ttl' in signed
    ? {envelope: signed.envelope, ttl: signed.ttl} : {envelope: signed.envelope};
}

export async function openLinkCentralApproval(item, {
  onResolved = () => {}, onClose = () => {}, sign = _signLinkDecision,
} = {}) {
  const review = item.safeReview || {};
  const itemUrl = '/api/attention/items/' + encodeURIComponent(item.id);
  const detail = await readJson(itemUrl);
  const result = detail.review?.application_result || null;
  const view = linkReviewState(result, item.actions, review.op === 'revoke' ? 'revoke' : 'publish');
  // The frozen registry request does not change after review; fetch it now
  // so drift and blocking errors show before anything is decided.
  let signing = null;
  if (!view.unavailable || view.operate) {
    try { signing = await readJson(itemUrl + '/link-operation-bootstrap'); } catch (error) { signing = {blocking_error: error.message}; }
  }
  const sheet = linkSheet(review, signing);
  const label = review.requester_label || 'A session';
  const session = await requestingSession(label.split(' · ')[0], label);
  // The link kind's intro is fixed by the dialog, so the state is a fact.
  if (view.operate) sheet.facts.push(['Status', `Approved, not yet ${sheet.op === 'revoke' ? 'revoked' : 'published'}`]);
  if (view.operate && result?.operable_until) sheet.facts.push([sheet.op === 'revoke' ? 'Revoke by' : 'Publish by', clock(result.operable_until)]);

  async function decide(outcome) {
    const response = await readJson(itemUrl + '/approval-decision', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({outcome, decision: {}}),
    });
    if (response.resolution?.outcome !== outcome) throw new Error('This request was not completed.');
  }

  return openApprovalDialog({
    retained: _matchingApprovalAuthority(sheet.approval),
    review: {
      kind: sheet.op === 'publish' ? 'link' : 'operation',
      title: `${sheet.verb} this share link`, intro: '',
      organization: sheet.organization,
      requester: {kind: 'Requesting session', name: session.name || label, byline: session.byline || '', href: session.href || localHref(item.requester?.href)},
      target: sheet.target, controls: sheet.controls, facts: sheet.facts,
      unavailable: view.unavailable || sheet.blocking,
    },
    // Grant first, then sign: the envelope must be newer than the Grant.
    authorize: async (options) => {
      if (!view.operate) { await decide('granted'); view.operate = true; }
      return sign(null, sheet.approval, options);
    },
    execute: async (signed) => {
      try {
        const response = await readJson(itemUrl + '/link-operation', {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(operationBody(sheet, signed)),
        });
        const execution = response.execution || {};
        if (execution.ok !== true) throw new Error(execution.error || sheet.failed);
        onResolved();
        return {approved: true, execution};
      } finally {
        signed.envelope = null;
      }
    },
    decline: !view.operate && !view.unavailable && (item.actions || []).includes('declined')
      ? async () => { await decide('declined'); onResolved(); } : null,
    result: {working: sheet.working, success: sheet.success, copy: '',
      fact: {...sheet.target, href: session.href, linkLabel: 'View requesting session'}},
    onClose,
  });
}

/* A link the operator asks for from this Dashboard (an org invitation, an
 * asset share, a fleet invitation). There is no approval to wait for: the
 * operator is the one acting, so the Dashboard prepares the signing request,
 * the same review is shown, and the link is signed and published only when
 * the operator confirms it, even when a retained session could sign without
 * an unlock. Resolves to the executor's output, or null when the operator
 * closes the review without confirming. */
export async function operateLinkDirectly({op, request, requester = 'This Dashboard'}, {sign = _signLinkDecision} = {}) {
  const prepared = await readJson('/api/links/operations', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({op, request}),
  }).catch((error) => {
    // A planner refusal says exactly why; there is nothing to sign.
    if (error.code === 'invalid_request') throw new Error(error.detail || error.message);
    throw error;
  });
  const sheet = linkSheet({...(prepared.review || {}), op}, prepared.signing || {});
  const operationUrl = '/api/links/operations/' + encodeURIComponent(prepared.operation_id);
  return await new Promise((resolve, reject) => {
    let execution = null;
    openApprovalDialog({
      retained: _matchingApprovalAuthority(sheet.approval),
      review: {
        kind: sheet.op === 'publish' ? 'link' : 'operation',
        title: `${sheet.verb} this share link`, intro: '',
        organization: sheet.organization,
        requester: {kind: 'Requested from', name: requester, byline: '', href: ''},
        target: sheet.target, controls: sheet.controls, facts: sheet.facts,
        unavailable: sheet.blocking,
      },
      authorize: (options) => sign(null, sheet.approval, options),
      execute: async (signed) => {
        try {
          const response = await readJson(operationUrl, {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(operationBody(sheet, signed)),
          });
          execution = response.execution || {};
          if (execution.ok !== true) throw new Error(execution.error || sheet.failed);
          return {approved: true, execution};
        } finally {
          signed.envelope = null;
        }
      },
      decline: null,
      result: {working: sheet.working, success: sheet.success, copy: '', fact: {...sheet.target}},
      onClose: () => resolve(execution && execution.ok === true ? execution : null),
    });
  });
}
