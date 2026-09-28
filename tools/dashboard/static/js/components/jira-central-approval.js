/* jira_write review in the shared approval control (Central, operator).
 *
 * Design of record: bc4d034a revision 42774731, states "Jira · add comment",
 * "· transition", "· create issue", "· change field", "· change type",
 * "· story points", "· attachment". Data mapping only; the dialog,
 * authentication, progress and result are the shared control's. The decision
 * carries nothing. */
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

function text(review) {
  return Array.isArray(review.content?.lines) ? review.content.lines.join('\n') : '';
}

/* Each operation exactly as the design state words it. */
export function jiraReview(r) {
  const key = r.target || '';
  switch (r.op) {
    case 'comment':
      return {review: {title: 'Post this comment', intro: 'Review the text that will be added to this issue.',
        target: {type: 'Subject', name: key}, facts: [], reviewLabel: 'Comment', reviewText: text(r)},
        result: {working: 'Posting comment…', success: 'Comment posted', copy: `The comment was posted to ${key}.`}};
    case 'transition':
      return {review: {title: 'Change issue status', target: {type: 'Subject', name: key},
        facts: [['New status', r.to_status || r.transition || '']]},
        result: {working: 'Updating issue…', success: 'Issue updated', copy: `${key} was moved to ${r.to_status || r.transition || ''}.`}};
    case 'create':
      return {review: {title: 'Create this issue', target: {type: 'Project', name: r.project || key},
        facts: [['Issue type', r.issue_type || '']], reviewLabel: 'Issue summary', reviewText: r.summary || ''},
        result: {working: 'Creating issue…', success: 'Issue created', copy: `The issue was created in ${r.project || key}.`}};
    case 'set_field':
      return {review: {title: 'Update this issue', target: {name: key},
        facts: [['Field', r.field || ''], ['New value', text(r)]]},
        result: {working: 'Updating issue…', success: 'Issue updated',
          copy: `The ${String(r.field || 'field').toLowerCase()} of ${key} was updated.`}};
    case 'change_type':
      return {review: {title: 'Change issue type', target: {name: key}, facts: [['New type', r.issue_type || '']]},
        result: {working: 'Updating issue…', success: 'Issue updated', copy: `${key} is now a ${r.issue_type || ''}.`}};
    case 'set_story_points':
      return {review: {title: 'Update the estimate', target: {name: key}, facts: [['Story points', String(r.value ?? '')]]},
        result: {working: 'Updating estimate…', success: 'Estimate updated', copy: `${key} is estimated at ${r.value} story points.`}};
    case 'attach':
      return {review: {title: 'Attach this file', target: {name: key}, facts: [['File', r.filename || '']]},
        result: {working: 'Attaching file…', success: 'File attached', copy: `${r.filename} was attached to ${key}.`}};
    default:
      return {review: {title: 'Update this issue', target: {name: key}, facts: []},
        result: {working: 'Updating issue…', success: 'Issue updated', copy: `${key} was updated.`}};
  }
}

function unavailableFor(result, actions, r) {
  const state = result?.state || 'pending';
  const machine = result?.machine_label || r.machine_label || 'another machine';
  if (state === 'elsewhere') return `Requested on ${machine}; decide it there.`;
  if (state === 'awaiting_execution') return 'Approved; running.';
  if (state === 'done') return 'Done.';
  if (state === 'failed') return `Jira refused it: ${result.reason || 'unknown reason'}.`;
  if (state === 'unknown') return 'Interrupted; it may or may not have been applied. Check the ticket before retrying.';
  if (state === 'expired_unexecuted') return 'Approved too late; nothing was sent.';
  return (actions || []).includes('granted') ? '' : 'This request is no longer available.';
}

export async function openJiraCentralApproval(item, {onResolved = () => {}, onClose = () => {}} = {}) {
  const r = item.safeReview || {};
  const label = r.requester_label || 'A session';
  const session = await requestingSession(label.split(' · ')[0], label);
  const organization = session.organization || null;
  const itemUrl = '/api/attention/items/' + encodeURIComponent(item.id);
  const detail = await readJson(itemUrl);
  let unavailable = unavailableFor(detail.review?.application_result || null, item.actions, r);
  let shown = r;
  // A long comment or value arrives as a prefix: read it whole from the
  // machine that holds it. Without the whole text there is nothing to approve.
  if (!unavailable && ['comment', 'set_field'].includes(r.op) && r.content?.complete === false) {
    try {
      const whole = await readJson(itemUrl + '/jira-write-content');
      shown = {...r, content: {...r.content, lines: whole.lines, complete: true}};
    } catch (error) {
      unavailable = error.status === 409
        ? `Requested on ${r.machine_label || 'another machine'}; decide it there.`
        : 'The full text is unavailable, so this cannot be approved.';
    }
  }
  const {review, result} = jiraReview(shown);
  // The design's wording where a state has no intro of its own.
  review.intro = review.intro || 'Review the change before continuing.';

  async function decide(outcome) {
    const response = await readJson(itemUrl + '/approval-decision', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({outcome, decision: {}}),
    });
    if (response.resolution?.outcome !== outcome) throw new Error('This request was not completed.');
  }

  return openApprovalDialog({
    retained: true,
    review: {
      kind: 'operation', ...review,
      organization: organization
        ? {name: organization.name, image: organization.favicon || organization.icon_data_uri || ''}
        : undefined,
      requester: {kind: 'Requested by', name: session.name, byline: session.byline || '',
        href: session.href || localHref(item.requester?.href)},
      consequence: '',
      unavailable,
    },
    authorize: async (options) => { options.onAuthenticated(); return {}; },
    execute: async () => { await decide('granted'); onResolved(); return {approved: true, execution: {ok: true}}; },
    // Decline stays available even when the review cannot be completed here.
    decline: (item.actions || []).includes('declined')
      ? async () => { await decide('declined'); onResolved(); } : null,
    result: {...result, fact: {name: r.target || '', byline: ''}},
    onClose,
  });
}
