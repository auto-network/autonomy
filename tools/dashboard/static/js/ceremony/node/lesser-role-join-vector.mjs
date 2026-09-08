#!/usr/bin/env node
// The whole lesser-role path, live, with TWO personas — driven through the
// EXACT modules the browser imports (org-role.js, org-invite.js, claim.js)
// against a real server. Nothing here is a stub: the operator persona and the
// joiner persona are different personal roots, and the joiner reaches the org
// with a persona and HTTP only — no dashboard of its own, no provisioned
// machine. Driven by test_lesser_role_join_live.py.
//
// argv: --server URL --org SLUG --genesis HEX
// env:  AUTONOMY_OPERATOR_SEED_HEX, AUTONOMY_JOINER_SEED_HEX (64 hex each)
import { webcrypto } from 'node:crypto';

if (!globalThis.crypto) globalThis.crypto = webcrypto;

const { defineRole, grantRole } = await import('../org-role.js');
const { mintOrgInvite } = await import('../org-invite.js');
const {
  getClaimContext, mintMemberClaim, submitClaim,
  signClaimApproval, submitClaimApproval, getClaimStatus, claimKey,
} = await import('../claim.js');
const { derivePersona } = await import('../ledger-event.js');

function arg(name) {
  const i = process.argv.indexOf(name);
  return i === -1 ? undefined : process.argv[i + 1];
}
function seed(name) {
  const hex = process.env[name] || '';
  if (!/^[0-9a-f]{64}$/.test(hex)) {
    process.stderr.write(name + ' must be 64 hex chars\n');
    process.exit(2);
  }
  return Uint8Array.from(hex.match(/../g).map((b) => parseInt(b, 16)));
}

const server = arg('--server');
const org = arg('--org');
const genesisId = arg('--genesis');
const fetchImpl = globalThis.fetch;
// claim.js's transport issues SAME-ORIGIN paths, which a browser resolves and
// node cannot. Prefix them; absolute URLs pass through untouched.
const originFetch = (url, options) =>
  fetchImpl(String(url).startsWith('http') ? url : server + url, options);
const operatorSeed = seed('AUTONOMY_OPERATOR_SEED_HEX');
const joinerSeed = seed('AUTONOMY_JOINER_SEED_HEX');
const thirdSeed = seed('AUTONOMY_THIRD_SEED_HEX');
const out = { steps: [] };
const step = (name, data) => { out.steps.push({ name, ...data }); };

function context(heads) {
  return {
    transport: { fetch: originFetch, serverUrl: server },
    orgSlug: org,
    genesisId: genesisId,
    heads: heads,
    maxHlc: [Date.now(), 0],
  };
}

try {
  // 1. The operator defines the lesser role with the ORG ROOT.
  const defined = await defineRole({
    fetchImpl, serverUrl: server, org, personalRootSeed: operatorSeed,
    name: 'member', scopeSet: [], claimRequires: 'admin-ack', approverThreshold: 1,
  });
  step('define_member', defined);

  // 2. The operator mints an invitation FOR THAT ROLE.
  const invite = await mintOrgInvite({
    fetchImpl, serverUrl: server, org, genesisId,
    personalRootSeed: operatorSeed, role: 'member',
    expiry: Date.now() + 7 * 86400000, maxUses: 1,
  });
  step('mint_invite', { inviteId: invite.inviteId });

  // 3. THE JOINER — a different personal root — claims it. This is the whole
  //    point: a persona and HTTP, nothing else.
  const joiner = await derivePersona(joinerSeed, genesisId);
  const ctx1 = await getClaimContext({
    transport: { fetch: originFetch, serverUrl: server }, orgSlug: org, inviteRef: invite.inviteId,
  });
  const minted = await mintMemberClaim({
    context: { ...context(ctx1.heads), maxHlc: ctx1.max_hlc || [Date.now(), 0] },
    personalRootSeed: joinerSeed,
    inviteRef: invite.inviteId,
    token: invite.bearer,
    profile: { display_name: 'Second Joiner' },
    kemSeed: joinerSeed,
  });
  const submitted = await submitClaim({
    context: { ...context(ctx1.heads), maxHlc: ctx1.max_hlc || [Date.now(), 0] },
    event: minted.event,
  });
  step('joiner_claim', {
    personaPub: joiner.publicHex,
    claimKey: minted.claimKey || (await claimKey(invite.inviteId, joiner.publicHex)),
    status: submitted && submitted.status,
  });

  // 4. The operator countersigns — admin-ack needs one approval, and a bearer
  //    claim never self-completes.
  const ctx2 = await getClaimContext({
    transport: { fetch: originFetch, serverUrl: server }, orgSlug: org, inviteRef: invite.inviteId,
  });
  const key = minted.claimKey || (await claimKey(invite.inviteId, joiner.publicHex));
  const approval = await signClaimApproval({
    context: { ...context(ctx2.heads), maxHlc: ctx2.max_hlc || [Date.now(), 0] },
    personalRootSeed: operatorSeed,
    inviteRef: invite.inviteId,
    personaPub: joiner.publicHex,
  });
  const admitted = await submitClaimApproval({
    context: { ...context(ctx2.heads), maxHlc: ctx2.max_hlc || [Date.now(), 0] },
    claimKey: key, inviteRef: invite.inviteId, personaPub: joiner.publicHex, approval,
  });
  step('countersign', { result: admitted && (admitted.status || admitted.ok) });

  const status = await getClaimStatus({
    context: { ...context(ctx2.heads), maxHlc: ctx2.max_hlc || [Date.now(), 0] },
    claimKey: key, inviteRef: invite.inviteId, personaPub: joiner.publicHex,
  });
  step('claim_status', { status: status && (status.status || status.state),
                         have: status && status.have, need: status && status.need });

  // 4b. FINALIZE. Approvals reaching the threshold only makes the claim
  //     READY; admission is a second submit of the same claim re-minted at
  //     the EXACT staged position, now carrying the countersignatures.
  //     The joiner does this — it is their membership being redeemed.
  if (status && status.position) {
    const finalized = await mintMemberClaim({
      context: { ...context(status.position.parents), maxHlc: status.position.hlc },
      personalRootSeed: joinerSeed,
      inviteRef: invite.inviteId,
      token: invite.bearer,
      profile: { display_name: 'Second Joiner' },
      approvals: status.approvals || [],
      kemSeed: joinerSeed,
      position: status.position,
      credentialHlc: status.position.hlc,
    });
    const admittedNow = await submitClaim({
      context: { ...context(status.position.parents), maxHlc: status.position.hlc },
      event: finalized.event,
      position: status.position,
    });
    step('finalize', { status: admittedNow && (admittedNow.status || admittedNow.ok) });
  }

  // 5. Widen then narrow the role — both halves of the reach ruling.
  const widened = await defineRole({
    fetchImpl, serverUrl: server, org, personalRootSeed: operatorSeed,
    name: 'member', scopeSet: ['invite:member'], claimRequires: 'admin-ack',
    version: null, approverThreshold: 1,
  });
  step('widen', widened);
  const narrowed = await defineRole({
    fetchImpl, serverUrl: server, org, personalRootSeed: operatorSeed,
    name: 'member', scopeSet: [], claimRequires: 'admin-ack',
    version: null, approverThreshold: 1,
  });
  step('narrow', narrowed);

  // 6. CASE 3 — a non-owner invites. Define Admin, grant it to the joiner,
  //    and have the JOINER mint an invitation from their own authority.
  const admin = await defineRole({
    fetchImpl, serverUrl: server, org, personalRootSeed: operatorSeed,
    name: 'admin', scopeSet: ['invite:member', 'role:grant:member'],
    claimRequires: 'admin-ack', approverThreshold: 1,
  });
  step('define_admin', admin);
  const granted = await grantRole({
    fetchImpl, serverUrl: server, org, genesisId,
    personalRootSeed: operatorSeed, persona: joiner.publicHex, role: 'admin',
  });
  step('grant_admin', granted);

  const secondInvite = await mintOrgInvite({
    fetchImpl, serverUrl: server, org, genesisId,
    personalRootSeed: joinerSeed, role: 'member',
    expiry: Date.now() + 7 * 86400000, maxUses: 1,
  });
  step('member_invites_member', { inviteId: secondInvite.inviteId, by: joiner.publicHex });

  // 7. CASE 5 — N-of-M. A third persona claims that invitation; Member is
  //    threshold 1, so one eligible approver admits — and the JOINER (holding
  //    role:grant:member through Admin) is that approver, not the root.
  const third = await derivePersona(thirdSeed, genesisId);
  const ctx3 = await getClaimContext({
    transport: { fetch: originFetch, serverUrl: server }, orgSlug: org, inviteRef: secondInvite.inviteId,
  });
  const thirdClaim = await mintMemberClaim({
    context: { ...context(ctx3.heads), maxHlc: ctx3.max_hlc || [Date.now(), 0] },
    personalRootSeed: thirdSeed, inviteRef: secondInvite.inviteId,
    token: secondInvite.bearer, profile: { display_name: 'Third Joiner' }, kemSeed: thirdSeed,
  });
  await submitClaim({
    context: { ...context(ctx3.heads), maxHlc: ctx3.max_hlc || [Date.now(), 0] },
    event: thirdClaim.event,
  });
  const thirdKey = thirdClaim.claimKey || (await claimKey(secondInvite.inviteId, third.publicHex));
  const ctx4 = await getClaimContext({
    transport: { fetch: originFetch, serverUrl: server }, orgSlug: org, inviteRef: secondInvite.inviteId,
  });
  const joinerApproval = await signClaimApproval({
    context: { ...context(ctx4.heads), maxHlc: ctx4.max_hlc || [Date.now(), 0] },
    personalRootSeed: joinerSeed, inviteRef: secondInvite.inviteId, personaPub: third.publicHex,
  });
  await submitClaimApproval({
    context: { ...context(ctx4.heads), maxHlc: ctx4.max_hlc || [Date.now(), 0] },
    claimKey: thirdKey, inviteRef: secondInvite.inviteId, personaPub: third.publicHex,
    approval: joinerApproval,
  });
  const thirdStatus = await getClaimStatus({
    context: { ...context(ctx4.heads), maxHlc: ctx4.max_hlc || [Date.now(), 0] },
    claimKey: thirdKey, inviteRef: secondInvite.inviteId, personaPub: third.publicHex,
  });
  step('third_status', { have: thirdStatus && thirdStatus.have, need: thirdStatus && thirdStatus.need });
  if (thirdStatus && thirdStatus.position) {
    const finalThird = await mintMemberClaim({
      context: { ...context(thirdStatus.position.parents), maxHlc: thirdStatus.position.hlc },
      personalRootSeed: thirdSeed, inviteRef: secondInvite.inviteId, token: secondInvite.bearer,
      profile: { display_name: 'Third Joiner' }, approvals: thirdStatus.approvals || [],
      kemSeed: thirdSeed, position: thirdStatus.position, credentialHlc: thirdStatus.position.hlc,
    });
    const admittedThird = await submitClaim({
      context: { ...context(thirdStatus.position.parents), maxHlc: thirdStatus.position.hlc },
      event: finalThird.event, position: thirdStatus.position,
    });
    step('third_finalize', { status: admittedThird && (admittedThird.status || admittedThird.ok) });
  }

  out.ok = true;
  out.joinerPersona = joiner.publicHex;
  out.thirdPersona = third.publicHex;
  process.stdout.write(JSON.stringify(out) + '\n');
} catch (error) {
  out.ok = false;
  out.error = error.message || String(error);
  out.status = error.status || null;
  out.reason = error.reason || null;
  process.stdout.write(JSON.stringify(out) + '\n');
  process.exit(1);
}
