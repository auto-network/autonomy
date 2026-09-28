/* visitor_token review in the shared approval control (Central, operator).
 *
 * Design of record: bc4d034a revision 42774731, state "Approve a visitor".
 * Data mapping only; the dialog, authentication, progress and result are the
 * shared control's. The decision carries nothing. */
import {openApprovalDialog, requestingSession, localHref} from './approval-dialog.js';

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

/* The review and result for one visitor request, exactly as the design state
 * words them. */
export function visitorReview(safeReview, session, unavailable = '') {
  const name = safeReview.display_name || 'This person';
  const organization = session.organization || null;
  return {
    review: {
      kind: 'operation',
      title: 'Let this person in',
      intro: 'Review who is asking to enter.',
      organization: organization
        ? {name: organization.name, image: organization.favicon || organization.icon_data_uri || ''}
        : undefined,
      requester: {kind: 'Requested by', name: session.name, byline: session.byline || '', href: session.href},
      target: {type: 'Subject', name},
      facts: [],
      reviewLabel: 'Reason for visiting',
      reviewText: safeReview.reason || '',
      consequence: '',
      unavailable,
    },
    result: {
      working: 'Allowing visitor…',
      success: 'Visitor approved',
      copy: `${name}’s visitor request was approved.`,
      fact: {name, byline: ''},
    },
  };
}

function unavailableFor(result, actions, safeReview) {
  const state = result?.state || 'pending';
  const machine = result?.machine_label || safeReview.machine_label || 'another machine';
  if (state === 'elsewhere') return `Requested on ${machine}; decide it there.`;
  if (state === 'awaiting_collection') return `The link is minted when ${safeReview.requester_label || 'the session'} collects it.`;
  if (state === 'minted') return `${result.display_name || safeReview.display_name || 'This person'} was let in.`;
  return (actions || []).includes('granted') ? '' : 'This request is no longer available.';
}

export async function openVisitorCentralApproval(item, {onResolved = () => {}, onClose = () => {}} = {}) {
  const safeReview = item.safeReview || {};
  const label = safeReview.requester_label || 'A session';
  const session = await requestingSession(label.split(' · ')[0], label);
  session.href = session.href || localHref(item.requester?.href);
  const itemUrl = '/api/attention/items/' + encodeURIComponent(item.id);
  const detail = await readJson(itemUrl);
  const unavailable = unavailableFor(detail.review?.application_result || null, item.actions, safeReview);
  const {review, result} = visitorReview(safeReview, session, unavailable);

  async function decide(outcome) {
    const response = await readJson(itemUrl + '/approval-decision', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({outcome, decision: {}}),
    });
    if (response.resolution?.outcome !== outcome) throw new Error('This request was not completed.');
  }

  return openApprovalDialog({
    retained: true,
    review,
    authorize: async (options) => { options.onAuthenticated(); return {}; },
    execute: async () => { await decide('granted'); onResolved(); return {approved: true, execution: {ok: true}}; },
    decline: !unavailable && (item.actions || []).includes('declined')
      ? async () => { await decide('declined'); onResolved(); } : null,
    result,
    onClose,
  });
}
