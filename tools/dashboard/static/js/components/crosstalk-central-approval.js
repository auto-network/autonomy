/* mcp_crosstalk review in the shared approval control (Central, operator).
 *
 * A chat on the relay asks to message one of the operator's sessions. The
 * operator authorizes exactly this text, so the review shows the whole
 * message, never a summary; a long message makes the sheet scroll, as an email
 * review does. The Grant carries only how long the channel stays open. */
import {openApprovalDialog} from './approval-dialog.js';

const durations = [['3600', '1 hour'], ['43200', '12 hours'], ['86400', '1 day'], ['604800', '1 week']];

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
export function crosstalkReviewState(result, actions, review = {}) {
  const state = result?.state || 'pending';
  const machine = result?.machine_label || review.machine_label || 'another machine';
  const target = review.target_label || review.target_session || 'the session';
  if (state === 'elsewhere') return {state, unavailable: `This chat asked ${machine}; decide it there.`};
  if (state === 'awaiting_delivery') return {state, unavailable: 'Approved; delivering.'};
  if (state === 'delivered') return {state, unavailable: `Delivered to ${target}.`};
  if (state === 'delivery_failed') return {state, unavailable: `Not delivered: ${result.reason || 'unknown reason'}.`};
  if (state === 'expired_undelivered') return {state, unavailable: 'Approved too late; the message was not delivered.'};
  if (state === 'superseded') return {state, unavailable: 'A newer message from this chat replaced this one.'};
  return {state, unavailable: (actions || []).includes('granted') ? '' : 'This request is no longer available.'};
}

export async function openCrosstalkCentralApproval(item, {onResolved = () => {}, onClose = () => {}} = {}) {
  const review = item.safeReview || {};
  const itemUrl = '/api/attention/items/' + encodeURIComponent(item.id);
  const detail = await readJson(itemUrl);
  const view = crosstalkReviewState(detail.review?.application_result || null, item.actions, review);
  const duration = document.createElement('select');
  for (const [value, label] of durations) { const option = document.createElement('option'); option.value = value; option.textContent = label; duration.append(option); }
  duration.value = '86400';
  const controls = document.createElement('div'); controls.append(duration);
  const handle = review.handle || review.requester_label || 'A chat';
  const target = review.target_label || review.target_session || 'a session';
  const facts = [['To', target]];
  if (review.intent) facts.push(['Intent', review.intent]);

  async function decide(outcome, decision) {
    const response = await readJson(itemUrl + '/approval-decision', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({outcome, decision}),
    });
    if (response.resolution?.outcome !== outcome) throw new Error('This request was not completed.');
  }

  return openApprovalDialog({
    retained: true,
    review: {
      kind: 'operation', title: review.title || 'Message a session', intro: '',
      requester: {kind: 'From', name: handle},
      facts, controls, durationLabel: 'Keep this channel open for',
      // Central strings carry no newlines, so the message arrives as lines.
      reviewLabel: 'Message', reviewText: Array.isArray(review.message_lines) ? review.message_lines.join('\n') : '',
      consequence: '',
      unavailable: view.unavailable,
    },
    authorize: async (options) => { options.onAuthenticated(); return {}; },
    execute: async () => {
      // The dialog writes the chosen lifetime back into this select.
      await decide('granted', {ttl_seconds: Number(duration.value)});
      onResolved();
      return {approved: true, execution: {ok: true}};
    },
    decline: !view.unavailable && (item.actions || []).includes('declined')
      ? async () => { await decide('declined', {}); onResolved(); } : null,
    result: {working: 'Approving message…', success: 'Message approved', copy: `It is delivered to ${target} next.`,
      fact: {name: target, byline: 'From ' + handle}},
    onClose,
  });
}
