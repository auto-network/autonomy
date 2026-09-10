/** Three-phase sign-in: fetch first, compute locally, submit after root cleanup. */
import { hexToBytes } from './primitives.js';
import { openWithEncapsulationPrivateKey } from './sealing.js';
import { deriveAuditedRecipient, prepareVault, submitVault } from './vault-unlock.js';
import { prepareStorageDelegate } from './org-storage-delegate.js';
import { completeFleetEnrollment, mintFleetRuntimeCredential } from './fleet-enrollment.js';
import { reportStepOutcome } from './step-report.js';

const PURPOSE = 'autonomy/identity/sign-in-preparation/v1';

export async function fetchPreparation(fetchImpl = fetch) {
  const response = await fetchImpl('/api/identity/unlock/preparation', { cache: 'no-store' });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'sign-in preparation unavailable');
  return body;
}

export async function prepareSignon(rootSeed, encrypted, signon) {
  const audited = await deriveAuditedRecipient(rootSeed);
  const raw = await openWithEncapsulationPrivateKey(hexToBytes(encrypted.sealed),
    audited.privateKeyHex, PURPOSE);
  let inputs;
  try { inputs = JSON.parse(new TextDecoder().decode(raw)); }
  finally { raw.fill(0); }
  const vault = await prepareVault(rootSeed, inputs.vault, audited);
  vault.keys.organization_delegates = [];
  for (const org of inputs.organizations) {
    vault.keys.organization_delegates.push(await prepareStorageDelegate(rootSeed, org.storage_delegate));
  }
  const posts = await signon._internals.prepareRootMaintenance(rootSeed,
    inputs.organizations, inputs.runtime, inputs.personal_serve);
  if (inputs.completion) {
    const c = inputs.completion;
    posts.push({ step: 'fleet', url: '/api/fleet/enrollment/local-completion', body: await completeFleetEnrollment({
      personalRootSeed: new Uint8Array(rootSeed), requestId: c.request_id,
      request: c.request, channelBinding: c.channel_binding, approval: c.approval,
      rosterEntry: c.roster_entry,
    }) });
  } else if (inputs.runtime.enabled) {
    const rc = inputs.runtime;
    posts.push({ step: 'fleet', url: '/api/fleet/runtime', body: await mintFleetRuntimeCredential({
      personalRootSeed: new Uint8Array(rootSeed), rootPub: rc.personal_root_pub,
      machineId: rc.machine_id, machinePub: rc.machine_pub,
      orgUuid: rc.org_uuid || rc.personal_org_uuid || null,
      servingOrgs: rc.serving_orgs || [],
    }) });
  }
  return { vault, posts,
    ready: inputs.organizations.filter(org => !org.serve_cert.required).map(org => org.slug),
    fleetEnabled: Boolean(inputs.completion || inputs.runtime.enabled) };
}

export async function submitSignon(prepared, fetchImpl = fetch) {
  const report = { repaired: [], ready: prepared.ready || [], bindings: [], failed: [] };
  if (!prepared.fleetEnabled) report.fleet_arming = { attempted: false, outcome: 'fleet-not-enabled' };
  try {
    try {
      await submitVault(prepared.vault, fetchImpl);
      reportStepOutcome('vault-wake', { ready: true }, { fetchImpl });
    } catch (error) {
      reportStepOutcome('vault-wake', { ready: false, reason: error.message }, { fetchImpl });
      throw error;
    }
    for (const post of prepared.posts) {
      try {
        if (post.error) throw new Error(post.error);
        const headers = { 'Content-Type': 'application/json' };
        if (post.org) headers['X-Graph-Org'] = post.org;
        const response = await fetchImpl(post.url, {
          method: 'POST', headers, body: JSON.stringify(post.body),
        });
        const body = await response.json();
        if (!response.ok || body.ok === false) throw new Error(body.error || 'sign-in handoff failed');
        reportStepOutcome(post.step, { status: 'ran' }, { fetchImpl, org: post.org });
        if (post.step === 'serve-cert') report.repaired.push(post.org || 'personal');
        if (post.step === 'binding') report.bindings.push({ org: post.org || 'personal', action: 'renewed' });
        if (post.step === 'fleet') report.fleet_arming = { attempted: true, outcome: 'armed' };
      } catch (error) {
        reportStepOutcome(post.step, { status: 'failed', reason: error.message }, { fetchImpl, org: post.org });
        report.failed.push({ org: post.org || 'personal', step: post.step, error: error.message });
        if (post.step === 'fleet') {
          report.fleet_arming = { attempted: true, outcome: 'mint-failed', error: error.message };
          throw error;
        }
        // Existing per-org maintenance is opportunistic; another org still runs.
      }
    }
    return report;
  } finally {
    prepared.vault.keys = null;
    prepared.posts = [];
    if (typeof window !== 'undefined') window.__autonomyServeRepair = report;
    try {
      await fetchImpl('/api/network/unlock-report', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(report) });
    } catch (_) { /* diagnostics must not change the sign-in outcome */ }
  }
}
