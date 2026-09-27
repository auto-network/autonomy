/* email_send review in the shared approval control (Central, operator session).
 *
 * The operator sees exactly what will leave the organization: the subject as
 * the named item, From / To / Cc as facts, and the message as the session wrote
 * it. From is resolved on the server from the organization's mailbox install;
 * the requesting session cannot choose it. Sending needs no factor beyond the
 * signed-in dashboard, and a short grace keeps a mistaken tap cancelable.
 * The result reports what the mail server accepted, never a guess. */
import {localHref, openApprovalDialog, requestingSession} from './approval-dialog.js';

async function readJson(url, init) {
  const response = await fetch(url, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || data.detail || 'This request is no longer available.');
    error.status = response.status;
    throw error;
  }
  return data;
}

export async function openEmailApproval(item, {onResolved = () => {}, onClose = () => {}} = {}) {
  const review = item.safeReview || {};
  const label = review.requester_label || 'A session';
  const session = await requestingSession(label.split(' · ')[0], label);
  const organization = session.organization || null;
  const to = review.to || '';
  const cc = review.cc || '';
  const subject = review.subject || '(no subject)';
  const lines = Array.isArray(review.body_lines) ? review.body_lines : [];
  const facts = [['From', review.from_addr || 'Unknown sender'], ['To', to]];
  if (cc) facts.push(['Cc', cc]);
  const itemUrl = '/api/attention/items/' + encodeURIComponent(item.id);

  async function decide(outcome) {
    const response = await readJson(itemUrl + '/approval-decision', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({outcome, decision: {}}),
    });
    if (response.resolution?.outcome !== outcome) throw new Error('This request was not completed.');
  }

  async function sent() {
    // The send runs on the Dashboard after the approval is recorded; read its
    // outcome rather than assume it. Bounded: an unconfirmed outcome is shown
    // as unconfirmed, never as sent.
    for (let attempt = 0; attempt < 20; attempt++) {
      const detail = await readJson(itemUrl);
      const result = detail.review?.application_result;
      if (result && result.execution) return result;
      await new Promise((resolve) => setTimeout(resolve, 750));
    }
    return null;
  }

  return openApprovalDialog({
    retained: true,
    review: {
      kind: 'operation',
      title: 'Send this email',
      intro: 'A session wrote this message. It is sent as soon as you approve.',
      organization: organization ? {name: organization.name, image: organization.favicon || organization.icon_data_uri || ''} : undefined,
      requester: {kind: 'Requesting session', name: session.name || label, byline: session.byline || '', href: session.href || localHref(item.requester?.href)},
      target: {type: 'Subject', name: subject, byline: ''},
      facts,
      actionLabel: 'Send',
      pendingLabel: 'Sending…',
      reviewLabel: 'Message',
      reviewText: lines.join('\n'),
      consequence: 'A sent email cannot be recalled.',
      unavailable: !item.actions.includes('granted') ? 'This request is no longer available.' : '',
    },
    authorize: async (options) => { options.onAuthenticated(); return {}; },
    execute: async () => {
      await decide('granted');
      const result = await sent();
      onResolved();
      return result;
    },
    decline: item.actions.includes('declined') ? async () => { await decide('declined'); onResolved(); } : null,
    result: {
      working: 'Sending email…',
      success: 'Email sent',
      copy: 'The mail server accepted the message for ' + to + '.',
      fact: {name: subject, byline: 'To ' + to + (cc ? ' · Cc ' + cc : ''), href: session.href, linkLabel: 'View requesting session'},
    },
    onClose,
  });
}
