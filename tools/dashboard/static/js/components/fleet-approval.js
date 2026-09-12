import { openApprovalDialog } from './approval-dialog.js';
import { openRoot } from '../ceremony/open-root.js';
import { mintFleetEnrollmentEvidence } from '../ceremony/fleet-enrollment.js';

async function request(url, body) {
  const response = await fetch(url, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || 'The request could not be completed.');
  return value;
}

export function openFleetApproval(item, { onResolved, onClose }) {
  const fleet = item.safeReview.fleet;
  const path = '/api/attention/items/' + encodeURIComponent(item.id);
  // Deliberately outside the durable decision and shared presentation state.
  let localRuntime;
  const decide = async (outcome, decision) => {
    const response = await request(path + '/approval-decision', { outcome, decision });
    if (response.resolution?.outcome !== outcome) throw new Error('This request was not approved.');
    onResolved();
    if (outcome !== 'granted') return;
    const detail = await request(path);
    const execution = detail.review?.application_result?.execution;
    if (execution?.ok !== true) throw new Error(execution?.error || 'The approval was recorded, but adding the machine has not completed.');
    if (localRuntime) {
      try {
        await request('/api/fleet/runtime', localRuntime);
      } catch (_) {
        throw new Error('Machine added, but this dashboard needs unlocking to synchronize.');
      } finally {
        localRuntime = null;
      }
    }
    return detail.review.application_result;
  };
  return openApprovalDialog({
    review: {
      kind: 'fleet', title: 'Add this machine?',
      intro: 'Compare the code with the one on the new machine before adding it to your fleet.',
      target: { type: 'New machine', name: 'New machine', byline: 'Only approve a machine you recognize.' },
      requester: { kind: 'Joining machine', name: 'New machine', byline: 'Your personal fleet' },
      code: item.safeReview.verification_code, machineName: '', facts: [],
      consequence: 'The machine will become a member of your personal fleet.',
      unavailable: !fleet || !item.actions.includes('granted') ? 'This request is no longer available.' : '',
    },
    async authorize(options, snapshot) {
      const name = snapshot.machineName.trim();
      if (!name || Array.from(name).length > 80 || /[\x00-\x1f\x7f]/.test(name)) {
        throw new Error('Machine name must be 1–80 characters without ASCII controls.');
      }
      const opened = await openRoot({ ...options, title: 'Add this machine?' });
      if (!opened) throw new Error('Approval cancelled.');
      try {
        if (opened.rootPub !== fleet.personal_root_pub) throw new Error('This request belongs to a different personal fleet.');
        if (options.signal?.aborted) throw new DOMException('Approval cancelled.', 'AbortError');
        options.onAuthenticated?.();
        const evidence = await mintFleetEnrollmentEvidence({
          personalRootSeed: opened.seed, rootPub: opened.rootPub,
          request: fleet.request, channelBinding: fleet.channel_binding,
          localBootstrapMachineId: fleet.local_bootstrap_machine_id,
          issuedAt: fleet.issued_at, seq: 0,
        });
        localRuntime = evidence.localRuntime || null;
        const decision = { machine_name: name, approval: evidence.approval, roster_entry: evidence.rosterEntry };
        if (evidence.localRosterEntry) decision.local_roster_entry = evidence.localRosterEntry;
        return decision;
      } finally {
        if (opened.seed) opened.seed.fill(0);
        opened.seed = null;
      }
    },
    execute: decision => decide('granted', decision),
    decline: item.actions.includes('declined') ? () => decide('declined', {}) : null,
    result: { working: 'Adding machine…', success: 'Machine added', copy: '',
      fact: { name: 'New machine', byline: 'Added to your personal fleet', href: '/fleet', linkLabel: 'View fleet' } },
    onClose() { localRuntime = null; onClose(); },
  });
}
