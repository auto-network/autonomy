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
  if (response.status === 409 && body.error === 'recipient_missing') {
    throw new Error('Sign-in is blocked because this dashboard is missing part of your identity’s encryption setup.');
  }
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
  // These failures describe unavailable maintenance inputs, not failed
  // authentication. Keep them local until phase 3 can report them safely.
  const failures = [];
  const organizations = inputs.organizations.filter(org => {
    if (!org.error) return true;
    failures.push({ step: 'organization-preparation', org: org.slug, error: org.error });
    return false;
  });
  let vault = null;
  if (inputs.vault.error) {
    failures.push({ step: 'vault-wake', error: inputs.vault.error });
  } else {
    vault = await prepareVault(rootSeed, inputs.vault, audited);
    vault.keys.organization_delegates = [];
    for (const org of organizations) {
      vault.keys.organization_delegates.push(await prepareStorageDelegate(rootSeed, org.storage_delegate));
    }
  }
  const fleetError = inputs.completion?.error || inputs.runtime.error;
  if (fleetError) failures.push({ step: 'fleet', error: fleetError });
  const personalServeError = inputs.personal_serve?.error;
  if (personalServeError) failures.push({ step: 'serve-cert', error: personalServeError });
  const posts = await signon._internals.prepareRootMaintenance(rootSeed,
    organizations, fleetError || personalServeError ? null : inputs.runtime, inputs.personal_serve);
  if (!fleetError && inputs.completion) {
    const c = inputs.completion;
    posts.push({ step: 'fleet', url: '/api/fleet/enrollment/local-completion', body: await completeFleetEnrollment({
      personalRootSeed: new Uint8Array(rootSeed), requestId: c.request_id,
      request: c.request, channelBinding: c.channel_binding, approval: c.approval,
      rosterEntry: c.roster_entry,
    }) });
  } else if (!fleetError && inputs.runtime.enabled) {
    const rc = inputs.runtime;
    posts.push({ step: 'fleet', url: '/api/fleet/runtime', body: await mintFleetRuntimeCredential({
      personalRootSeed: new Uint8Array(rootSeed), rootPub: rc.personal_root_pub,
      machineId: rc.machine_id, machinePub: rc.machine_pub,
      orgUuid: rc.org_uuid || rc.personal_org_uuid || null,
      servingOrgs: rc.serving_orgs || [],
    }) });
  }
  return { vault, posts, failures,
    ready: vault ? organizations.filter(org => !org.serve_cert.required).map(org => org.slug) : [],
    fleetEnabled: !fleetError && Boolean(inputs.completion || inputs.runtime.enabled) };
}

export async function submitSignon(prepared, fetchImpl = fetch) {
  const report = { repaired: [], ready: prepared.ready || [], bindings: [], failed: [...(prepared.failures || [])] };
  const fleetFailure = report.failed.find(item => item.step === 'fleet');
  if (fleetFailure) report.fleet_arming = { attempted: false, outcome: fleetFailure.error };
  else if (!prepared.fleetEnabled) report.fleet_arming = { attempted: false, outcome: 'fleet-not-enabled' };
  try {
    for (const failure of report.failed) {
      reportStepOutcome(failure.step, { status: 'failed', reason: failure.error },
        { fetchImpl, org: failure.org });
    }
    try {
      if (prepared.vault) {
        await submitVault(prepared.vault, fetchImpl);
        reportStepOutcome('vault-wake', { ready: true }, { fetchImpl });
      }
    } catch (error) {
      reportStepOutcome('vault-wake', { ready: false, reason: error.message }, { fetchImpl });
      report.failed.push({ org: 'personal', step: 'vault-wake', error: error.message });
      report.ready = [];
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
        }
        // Authentication already succeeded. Report maintenance failures and
        // keep processing independent work without blocking dashboard entry.
      }
    }
    return report;
  } finally {
    if (prepared.vault) prepared.vault.keys = null;
    prepared.posts = [];
    if (typeof window !== 'undefined') window.__autonomyServeRepair = report;
    try {
      await fetchImpl('/api/network/unlock-report', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(report) });
    } catch (_) { /* diagnostics must not change the sign-in outcome */ }
  }
}
