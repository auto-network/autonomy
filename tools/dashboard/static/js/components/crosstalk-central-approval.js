/* mcp_crosstalk review in the shared approval control (Central, operator).
 *
 * A chat on the relay asks to message one of the operator's sessions. The
 * operator authorizes exactly this text, so the review shows the whole
 * message, never a summary; a long message makes the sheet scroll, as an email
 * review does. The Grant carries only how long the channel stays open. */
import {openApprovalDialog, requestingSession} from './approval-dialog.js';


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
  if (state === 'superseded') return {state, unavailable: 'A newer message from this chat replaced this one.'};
  return {state, unavailable: (actions || []).includes('granted') ? '' : 'This request is no longer available.'};
}

export async function openCrosstalkCentralApproval(item, {onResolved = () => {}, onClose = () => {}} = {}) {
  const review = item.safeReview || {};
  const itemUrl = '/api/attention/items/' + encodeURIComponent(item.id);
  const detail = await readJson(itemUrl);
  const view = crosstalkReviewState(detail.review?.application_result || null, item.actions, review);
  const handle = review.handle || 'A chat';
  const target = review.target_label || review.target_session || 'a session';
  // The organization is the destination session's.
  const destination = await requestingSession(review.target_session, target);
  const organization = destination.organization || null;

  async function decide(outcome, decision) {
    const response = await readJson(itemUrl + '/approval-decision', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({outcome, decision}),
    });
    if (response.resolution?.outcome !== outcome) throw new Error('This request was not completed.');
  }

  // Design of record: bc4d034a revision 42774731, state "Approve a message".
  // The permission lasts one day, as the design states it.
  return openApprovalDialog({
    retained: true,
    review: {
      kind: 'operation', title: 'Allow this message', intro: 'Review the message and destination before allowing it.',
      organization: organization
        ? {name: organization.name, image: organization.favicon || organization.icon_data_uri || ''}
        : undefined,
      requester: {kind: 'Requested by', name: review.requester_label || 'ChatGPT relay', byline: organization?.name || ''},
      target: {type: 'Subject', name: target},
      facts: [['From', handle], ['Permission lasts', '1 day']],
      // Central strings carry no newlines, so the message arrives as lines.
      reviewLabel: 'Message', reviewText: Array.isArray(review.message_lines) ? review.message_lines.join('\n') : '',
      consequence: '',
      unavailable: view.unavailable,
    },
    authorize: async (options) => { options.onAuthenticated(); return {}; },
    execute: async () => {
      await decide('granted', {ttl_seconds: 86400});
      onResolved();
      return {approved: true, execution: {ok: true}};
    },
    decline: !view.unavailable && (item.actions || []).includes('declined')
      ? async () => { await decide('declined', {}); onResolved(); } : null,
    result: {working: 'Sending message\u2026', success: 'Message sent', copy: 'The message was sent to the requesting session.',
      fact: {name: target, byline: 'From ' + handle}},
    onClose,
  });
}
