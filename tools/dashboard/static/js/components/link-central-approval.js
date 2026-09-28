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

export async function openLinkCentralApproval(item, {
  onResolved = () => {}, onClose = () => {}, sign = _signLinkDecision,
} = {}) {
  const review = item.safeReview || {};
  const op = review.op === 'revoke' ? 'revoke' : 'publish';
  const itemUrl = '/api/attention/items/' + encodeURIComponent(item.id);
  const detail = await readJson(itemUrl);
  const result = detail.review?.application_result || null;
  const view = linkReviewState(result, item.actions, op);
  // The frozen registry request does not change after review; fetch it now
  // so drift and blocking errors show before anything is decided.
  let bootstrap = null;
  if (!view.unavailable || view.operate) {
    try { bootstrap = await readJson(itemUrl + '/link-operation-bootstrap'); } catch (error) { bootstrap = {blocking_error: error.message}; }
  }
  const label = review.requester_label || 'A session';
  const session = await requestingSession(label.split(' · ')[0], label);
  const acting = review.acting_identity || {};
  const ttl = review.ttl == null ? null : Number(review.ttl);
  const current = ttl == null ? '604800' : String(ttl);
  const custom = ttl != null && !_LINK_DURATION_VALUES.has(current);
  const approval = {
    id: item.id, op, orgSlug: review.org || '', orgUuid: bootstrap?.org_uuid || null,
    actingIdentity: acting, fixedExpiry: !!review.fixed_expiry,
    duration: custom ? 'custom' : current, customDurationSeconds: custom ? ttl : null,
    allowSessionApprovals: false,
    registryRequest: bootstrap?.registry_request || null,
    registrationRequired: !bootstrap?.registry_request,
    refreshRegistryRequest: async () => (await readJson(itemUrl + '/link-operation-bootstrap')).registry_request || null,
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
  if (review.label) facts.push(['Label', review.label]);
  // The link kind's intro is fixed by the dialog, so the state is a fact.
  if (view.operate) facts.push(['Status', `Approved, not yet ${op === 'revoke' ? 'revoked' : 'published'}`]);
  if (view.operate && result?.operable_until) facts.push([op === 'revoke' ? 'Revoke by' : 'Publish by', clock(result.operable_until)]);
  const blocking = view.unavailable || bootstrap?.blocking_error
    || (bootstrap?.binding_drift ? 'This organization changed after the request was prepared. Close it and ask again.' : '');
  const target = {name: review.target_title || 'Share link', type: review.type_label || review.target_type || 'Item', byline: 'On auto.network'};

  async function decide(outcome) {
    const response = await readJson(itemUrl + '/approval-decision', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({outcome, decision: {}}),
    });
    if (response.resolution?.outcome !== outcome) throw new Error('This request was not completed.');
  }

  const verb = op === 'revoke' ? 'Revoke' : 'Publish';
  return openApprovalDialog({
    retained: !approval.registrationRequired && _matchingApprovalAuthority(approval),
    review: {
      kind: op === 'publish' ? 'link' : 'operation',
      title: `${verb} this share link`,
      intro: '',
      organization: {name: acting.name || approval.orgSlug, image: acting.favicon || ''},
      requester: {kind: 'Requesting session', name: session.name || label, byline: session.byline || '', href: session.href || localHref(item.requester?.href)},
      target, controls, facts,
      unavailable: blocking,
    },
    // Grant first, then sign: the envelope must be newer than the Grant.
    authorize: async (options) => {
      if (!view.operate) { await decide('granted'); view.operate = true; }
      if (!approval.registryRequest && !approval.registrationRequired) {
        approval.registryRequest = await approval.refreshRegistryRequest();
      }
      return sign(null, approval, options);
    },
    execute: async (signed) => {
      const body = op === 'publish' && !approval.fixedExpiry && 'ttl' in signed
        ? {envelope: signed.envelope, ttl: signed.ttl} : {envelope: signed.envelope};
      try {
        const response = await readJson(itemUrl + '/link-operation', {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
        });
        const execution = response.execution || {};
        if (execution.ok !== true) throw new Error(execution.error || `The link could not be ${op === 'revoke' ? 'revoked' : 'published'}.`);
        onResolved();
        return {approved: true, execution};
      } finally {
        signed.envelope = null;
      }
    },
    decline: !view.operate && !view.unavailable && (item.actions || []).includes('declined')
      ? async () => { await decide('declined'); onResolved(); } : null,
    result: {
      working: op === 'revoke' ? 'Revoking link' : 'Publishing link',
      success: op === 'revoke' ? 'Link revoked' : 'Link published',
      copy: '',
      fact: {...target, href: session.href, linkLabel: 'View requesting session'},
    },
    onClose,
  });
}
