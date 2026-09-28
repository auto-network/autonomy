/* external_service_access review in the shared approval control (Central).
 *
 * A device (the iPhone capture action) asks for upload access. The operator
 * chooses how long it lasts; the Grant carries only that lifetime. The device
 * collects its credential itself when it next checks in, so the browser never
 * sees it. Ported from components/service-approval.js (auto-fkhq0.26). */
import {openApprovalDialog} from './approval-dialog.js';

// When the access expires; "Never" reads as an expiry, not a duration.
const durations = [['86400', 'In 1 day'], ['604800', 'In 7 days'], ['2592000', 'In 30 days'],
  ['31536000', 'In 1 year'], ['315360000', 'In 10 years'], ['', 'Never']];

async function readJson(url, init) {
  const response = await fetch(url, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error === 'not_found' ? 'This request is no longer available.' : (data.error || 'This request is no longer available.'));
    error.status = response.status;
    throw error;
  }
  return data;
}

/* What the review says for each state; '' means the decision is open. */
export function serviceReviewState(result, actions, review = {}) {
  const state = result?.state || 'pending';
  const machine = result?.machine_label || review.machine_label || 'another machine';
  if (state === 'elsewhere') return {state, unavailable: `This device enrolled with ${machine}; decide it there.`};
  if (state === 'awaiting_collection') return {state, unavailable: 'Allowed. The device picks up its access when it next checks in.'};
  if (state === 'delivered') return {state, unavailable: 'The device has its access.'};
  if (state === 'expired_undelivered') return {state, unavailable: 'Allowed, but the device did not check in within 30 minutes; enroll it again.'};
  return {state, unavailable: (actions || []).includes('granted') ? '' : 'This request is no longer available.'};
}

export async function openExternalServiceCentralApproval(item, {onResolved = () => {}, onClose = () => {}} = {}) {
  const review = item.safeReview || {};
  const itemUrl = '/api/attention/items/' + encodeURIComponent(item.id);
  const detail = await readJson(itemUrl);
  const view = serviceReviewState(detail.review?.application_result || null, item.actions, review);
  const requested = review.requested_ttl_seconds == null ? '' : String(review.requested_ttl_seconds);
  const duration = document.createElement('select');
  for (const [value, label] of durations) { const option = document.createElement('option'); option.value = value; option.textContent = label; duration.append(option); }
  duration.value = durations.some(([value]) => value === requested) ? requested : '31536000';
  const controls = document.createElement('div'); controls.append(duration);
  const device = review.requester_label || 'A device';
  const application = review.application || 'External service';

  async function decide(outcome, decision) {
    const response = await readJson(itemUrl + '/approval-decision', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({outcome, decision}),
    });
    if (response.resolution?.outcome !== outcome) throw new Error('This request was not completed.');
  }

  return openApprovalDialog({
    retained: true,
    // Design of record: bc4d034a revision 42774731, state "Allow service access".
    review: {
      kind: 'service', title: 'Allow service access', intro: 'Let this device send screenshots to your dropbox.',
      target: {type: 'Application', name: application, byline: 'Add screenshots to your dropbox'},
      requester: {kind: 'Requesting device', name: device, byline: application},
      facts: [], controls, durationLabel: 'Access expires',
      consequence: 'This grants screenshot-upload access, not access to browse your dashboard.',
      unavailable: view.unavailable,
    },
    authorize: async (options) => { options.onAuthenticated(); return {}; },
    execute: async () => {
      // The dialog writes the chosen lifetime back into this select.
      await decide('granted', {ttl_seconds: duration.value === '' ? null : Number(duration.value)});
      onResolved();
      return {approved: true, execution: {ok: true}};
    },
    decline: !view.unavailable && (item.actions || []).includes('declined')
      ? async () => { await decide('declined', {}); onResolved(); } : null,
    result: {working: 'Allowing access…', success: 'Access allowed', copy: 'The device picks up its access when it next checks in.',
      fact: {name: device, byline: application}},
    onClose,
  });
}
