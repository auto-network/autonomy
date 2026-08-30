/* Bring the Fleet serving runtime back after a root is open — the shared body
 * of unlock's post-root maintenance and the profile tray's restart button.
 *
 * The serving credential is MEMORY-class: it arrives when a root opens, and it
 * dies when the serving process restarts (crib §10, same shape as the delegate
 * in vault-unlock.js). Nothing hands it a new one on its own, so after a
 * restart every sync request is refused until a root ceremony mints one again.
 * This module is that mint, seed-based, so the ONE implementation is shared and
 * the two callers can never drift — the divergence that once left a passkey
 * unlock unable to activate sync (see unlock.js `_fleetCompleteOrMint`).
 *
 * Best-effort: a failure here must never turn a successful root ceremony into a
 * lockout. The caller owns the seed's outer lifecycle; the ceremonies invoked
 * here zero their own copies.
 */

async function fetchJson(fetchImpl, url) {
  const resp = await fetchImpl(url, {
    credentials: 'same-origin', headers: { Accept: 'application/json' },
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.error || ('request failed: ' + url));
  return body;
}

async function postJson(fetchImpl, url, body) {
  const resp = await fetchImpl(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok || data.ok === false) {
    throw new Error(data.error || ('request failed: ' + url + ' → ' + resp.status));
  }
  return data;
}

/**
 * Complete a pending Fleet join if one is waiting, else re-mint and install the
 * Fleet runtime (serving) credential for this already-enrolled machine.
 *
 * @param {Uint8Array} seed the open personal root seed. This function makes its
 *   own fresh copies for the ceremonies (each zeroes its copy) and zeroes the
 *   passed array before returning.
 * @param {{fetchImpl?: function, signon?: object}} deps
 * @returns {Promise<{armed: boolean, reason: string|null}>}
 */
export async function restoreFleetRuntime(seed, {
  fetchImpl = fetch,
  signon = (typeof window !== 'undefined' ? window.AutonomyNetworkSession : null),
} = {}) {
  const internals = (signon && signon._internals) || {};
  try {
    const completion = await fetchJson(
      fetchImpl, '/api/fleet/enrollment/local-completion',
    );
    if (completion.pending) {
      const fc = await import('./fleet-enrollment.js');
      const proof = await fc.completeFleetEnrollment({
        personalRootSeed: seed,
        requestId: completion.request_id,
        request: completion.request,
        channelBinding: completion.channel_binding,
        approval: completion.approval,
        rosterEntry: completion.roster_entry,
      });
      await postJson(fetchImpl, '/api/fleet/enrollment/local-completion', proof);
      return { armed: true, reason: 'enrolled' };
    }

    let rc = await fetchJson(fetchImpl, '/api/fleet/runtime');
    if (!rc.enabled) {
      seed.fill(0);
      return { armed: false, reason: 'fleet-not-enabled' };
    }
    // Bring the PERSONAL TUNNEL online before minting the runtime credential,
    // registering (and, on the serving machine, provisioning) the personal org
    // when it isn't yet. Idempotent and best-effort — a failure here degrades to
    // a sync-only credential, never a lockout. Mirrors unlock.js exactly.
    if (rc.personal_org_uuid && (!rc.org_uuid || rc.serves)
        && typeof internals.provisionPersonalNetworkIdentity === 'function') {
      try {
        await internals.provisionPersonalNetworkIdentity({
          personalRootSeed: new Uint8Array(seed),  // ceremony zeroes its copy
          orgUuid: rc.personal_org_uuid,
          rootPub: rc.personal_root_pub,
          serve: !!rc.serves,
        });
        rc = await fetchJson(fetchImpl, '/api/fleet/runtime');
      } catch (e) {
        if (typeof console !== 'undefined' && console.warn) {
          console.warn('personal tunnel provisioning failed:', (e && e.message) || e);
        }
      }
    }
    const frc = await import('./fleet-enrollment.js');
    const cred = await frc.mintFleetRuntimeCredential({
      personalRootSeed: new Uint8Array(seed),  // fresh copy; mint zeroes it
      rootPub: rc.personal_root_pub,
      machineId: rc.machine_id,
      machinePub: rc.machine_pub,
      orgUuid: rc.org_uuid || null,
    });
    await postJson(fetchImpl, '/api/fleet/runtime', cred);
    return { armed: true, reason: 'minted' };
  } finally {
    if (seed && seed.fill) seed.fill(0);
  }
}

export default restoreFleetRuntime;
