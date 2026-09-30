/* Central renderer for a ``vault_seal`` request: an agent asked the operator
 * to vault a secret it does not hold. Data/operation adapter only — the shared
 * approval control presents it. The typed value goes to the deposit route and
 * is sealed there; the decision carries only the sealed row's id. */
import { openApprovalDialog, localHref } from './approval-dialog.js';

async function request(url, body) {
  const response = await fetch(url, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  let value = null;
  try { value = await response.json(); } catch (_) { value = null; }
  if (!response.ok) throw new Error((value && value.error) || 'The request could not be completed.');
  return value || {};
}

export function releaseLabel(tier) {
  return tier === 'audited' ? 'Unattended, to authorized sessions' : 'Only with your approval, each time';
}

export function openVaultSealApproval(item, { onResolved, onClose }) {
  const review = item.safeReview || {};
  const path = '/api/attention/items/' + encodeURIComponent(item.id);
  const approvalId = review.approval_id;
  const name = review.name || 'secret';
  const requester = {
    name: review.requester_label || 'Requesting session',
    href: localHref(item.requester && item.requester.href),
    byline: (item.requester && item.requester.byline) || '',
  };
  const decide = async (outcome, decision) => {
    const response = await request(path + '/approval-decision', { outcome, decision });
    if (!response.resolution || response.resolution.outcome !== outcome) {
      throw new Error('This request was not completed.');
    }
    onResolved();
    if (outcome !== 'granted') return;
    const detail = await request(path);
    const result = detail.review && detail.review.application_result;
    if (!result || !result.execution || result.execution.ok !== true) {
      throw new Error((result && result.execution && result.execution.error) ||
        'The approval was recorded, but the deposit has not been confirmed.');
    }
    return result;
  };
  const facts = [['Release', releaseLabel(review.tier)]];
  if (review.replace) facts.push(['Replaces', 'the existing value under this name']);
  return openApprovalDialog({
    review: {
      kind: 'vault-seal', title: 'Vault a secret',
      intro: review.detail || '',
      target: { type: 'Secret', name, byline: review.key && review.key !== name ? 'Stored as ' + review.key : '' },
      requester, facts,
      consequence: 'The value is sealed into your vault on this dashboard. The requesting session learns only that it exists until the vault releases it.',
      secretEntry: { label: 'Secret value', placeholder: 'Paste or type the secret' },
      unavailable: !approvalId || !item.actions.includes('granted') ? 'This request is no longer available.' : '',
    },
    async authorize(options, snapshot) {
      const value = snapshot && typeof snapshot.secret === 'string' ? snapshot.secret : '';
      if (snapshot) delete snapshot.secret;
      if (!value.trim()) throw new Error('Enter the secret before approving.');
      if (options.signal && options.signal.aborted) throw new Error('Approval cancelled.');
      const receipt = await request('/api/vault/deposit/' + encodeURIComponent(approvalId), { value });
      if (!receipt.setting_id) throw new Error('The vault did not confirm the deposit.');
      if (options.onAuthenticated) options.onAuthenticated();
      return { setting_id: receipt.setting_id };
    },
    execute: decision => decide('granted', decision),
    decline: item.actions.includes('declined') ? () => decide('declined', {}) : null,
    result: {
      working: 'Sealing secret…', success: 'Secret vaulted', copy: '',
      fact: { name, byline: releaseLabel(review.tier), href: requester.href, linkLabel: 'View requesting session' },
    },
    onClose,
  });
}
