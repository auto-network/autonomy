/* vault_open review in the shared approval control (Central, operator session).
 *
 * Releasing a saved credential is two server steps behind one operator
 * gesture: the Grant is recorded with an empty decision (resolutions replicate
 * to every personal machine, so the content key can never ride them), then the
 * content key opened by the factor ceremony goes to this Dashboard's delivery
 * endpoint, which writes the value to the requesting session and returns a
 * value-free receipt. The key lives only in this function's memory and its
 * reference is cleared once the delivery settles. It is a JS string, which
 * cannot be overwritten in place: clearing drops the reference, it does not
 * wipe the bytes.
 *
 * Only the machine that accepted the request can deliver. Elsewhere the review
 * says where to go instead of starting a ceremony that could not complete. A
 * Grant whose delivery did not finish offers Deliver, which repeats the
 * ceremony; nothing about the key is ever stored to resume from. */
import {localHref, openApprovalDialog, requestingSession} from './approval-dialog.js';
import {collectVaultOpeners, clearVaultOpeners} from '../ceremony/open-vault.js';
import {openContentKey} from '../ceremony/policy-class-open.js';

// The endpoint's fixed error codes, in the words the operator reads.
const ERRORS = {
  window_closed: 'The delivery window has closed. The session must ask again.',
  elsewhere: 'This credential can only be delivered from the machine that received the request.',
  binding_drift: 'The session or the saved credential changed since the request. The session must ask again.',
  open_failed: 'The credential could not be opened with that unlock. Try again.',
  not_actionable: 'This release is no longer waiting for a decision.',
  delivery_failed: 'The credential could not be delivered. The session must ask again.',
  not_found: 'This request is no longer available.',
  invalid_request: 'This release request is incomplete.',
  unavailable: 'The Dashboard could not complete this release. Try again.',
};

function lifetime(seconds) {
  if (!seconds) return 'Until this session ends';
  if (seconds % 3600 === 0) return `${seconds / 3600} hour${seconds === 3600 ? '' : 's'}`;
  if (seconds % 60 === 0) return `${seconds / 60} minute${seconds === 60 ? '' : 's'}`;
  return `${seconds} seconds`;
}

function clock(epochSeconds) {
  return new Date(epochSeconds * 1000).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
}

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

/* What the review says for each delivery state; '' means the action is open. */
export function vaultReviewState(result, actions) {
  const state = result?.state || 'pending';
  const machine = result?.machine_label || 'the machine that received the request';
  if (state === 'elsewhere') {
    return {state, deliver: false,
      unavailable: `Deliverable only from ${machine}. Open that machine's Dashboard to release it.`};
  }
  if (state === 'awaiting_delivery') return {state, deliver: true, unavailable: ''};
  if (state === 'expired_undelivered') {
    return {state, deliver: false, unavailable: 'Approved, but the delivery window closed. The session must ask again.'};
  }
  if (state === 'delivered') return {state, deliver: false, unavailable: 'This credential was delivered.'};
  if (state === 'delivery_failed') {
    return {state, deliver: false, unavailable: 'The credential could not be delivered. The session must ask again.'};
  }
  return {state, deliver: false,
    unavailable: (actions || []).includes('granted') ? '' : 'This request is no longer available.'};
}

export async function openVaultCentralApproval(item, {
  onResolved = () => {}, onClose = () => {},
  collect = collectVaultOpeners, openKey = openContentKey,
} = {}) {
  const read = readJson;
  const review = item.safeReview || {};
  const itemUrl = '/api/attention/items/' + encodeURIComponent(item.id);
  const detail = await read(itemUrl);
  const result = detail.review?.application_result || null;
  const view = vaultReviewState(result, item.actions);
  const label = review.requester_label || 'A session';
  const session = await requestingSession(label.split(' · ')[0], label);
  const organization = session.organization || null;
  const credential = review.target || 'Credential';
  const duration = lifetime(review.ttl_seconds);
  const machine = result?.machine_label || review.machine_label || '';
  const facts = [['Available', duration]];
  if (machine) facts.push(['Delivered from', machine]);
  if (view.deliver && result?.deliverable_until) facts.push(['Deliver by', clock(result.deliverable_until)]);

  async function decide(outcome) {
    const response = await read(itemUrl + '/approval-decision', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({outcome, decision: {}}),
    });
    if (response.resolution?.outcome !== outcome) throw new Error('This request was not completed.');
  }

  return openApprovalDialog({
    retained: true,
    review: {
      kind: 'operation',
      title: view.deliver ? 'Deliver credential' : 'Release credential',
      intro: view.deliver ? 'You approved this release, but the credential was not delivered.' : '',
      organization: organization ? {name: organization.name, image: organization.favicon || organization.icon_data_uri || ''} : undefined,
      requester: {kind: 'Requesting session', name: session.name || label, byline: session.byline || '', href: session.href || localHref(item.requester?.href)},
      target: {type: 'Credential', name: credential},
      facts,
      consequence: 'Copies made by the session are not revoked when access ends.',
      unavailable: view.unavailable,
    },
    authorize: async (options) => {
      // The ceremony material is served only here, only to the operator, and
      // only while this machine can still deliver.
      const {ceremony, bundle} = await read(itemUrl + '/vault-open-bootstrap');
      let gathered;
      try {
        gathered = await collect(ceremony, options);
        if (options.signal?.aborted) throw new Error('Approval cancelled.');
        options.onAuthenticated();
        return {content_key: await openKey(bundle, gathered.openers)};
      } finally {
        clearVaultOpeners(gathered);
      }
    },
    execute: async (decision) => {
      try {
        if (!view.deliver) await decide('granted');
        const delivered = await read(itemUrl + '/vault-open-delivery', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({content_key: decision.content_key}),
        });
        onResolved();
        // The shared control confirms only a result whose execution is ok.
        return {approved: true, execution: {ok: true, ...(delivered.receipt || {})}};
      } finally {
        decision.content_key = '';
      }
    },
    decline: !view.deliver && !view.unavailable && (item.actions || []).includes('declined')
      ? async () => { await decide('declined'); onResolved(); } : null,
    result: {
      working: 'Releasing credential…',
      success: 'Credential released',
      copy: '',
      fact: {name: credential, byline: `${session.name || label} · ${duration}`, href: session.href, linkLabel: 'View requesting session'},
    },
    onClose,
  });
}
