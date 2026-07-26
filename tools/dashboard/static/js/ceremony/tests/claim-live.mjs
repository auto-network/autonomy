import { readFile } from 'node:fs/promises';

import {
  admittingApprovals,
  buildClaimApproval,
  getClaimContext,
  getClaimStatus,
  mintMemberClaim,
  signClaimApproval,
  submitClaim,
  submitClaimApproval,
} from '../claim.js';
import {
  hexToBytes,
  importEd25519RootSigningKey,
} from '../primitives.js';

const fixturePath = process.argv[2];
if (!fixturePath) {
  throw new Error('usage: node claim-live.mjs FIXTURE.json');
}
const fixture = JSON.parse(await readFile(fixturePath, 'utf8'));
const transport = {
  fetch(route, options) {
    return fetch(new URL(route, fixture.server_url), options);
  },
};

async function heads() {
  const response = await fetch(
    new URL(
      `/api/network/ledger/heads?org=${encodeURIComponent(fixture.org)}`,
      fixture.server_url,
    ),
    { headers: { 'X-Graph-Org': fixture.org } },
  );
  if (!response.ok) throw new Error(`heads failed: ${response.status}`);
  return (await response.json()).heads;
}

async function refused(action) {
  try {
    await action();
  } catch (error) {
    return { status: error.status, body: error.body };
  }
  throw new Error('expected request to be refused');
}

const keyContext = await getClaimContext({
  transport,
  orgSlug: fixture.org,
  inviteRef: fixture.key_invite_ref,
});
const expired = await mintMemberClaim({
  context: keyContext,
  personalRootSeed: hexToBytes(fixture.personal_seed_hex),
  inviteRef: fixture.expired_invite_ref,
  token: fixture.expired_token,
  profile: {},
  kemSeed: hexToBytes(fixture.kem_seed_hex),
  nowMs: fixture.lagging_now_ms,
});
const expiredSubmit = await refused(() => submitClaim({
  context: keyContext,
  event: expired.event,
}));
const expiredStatus = await getClaimStatus({
  context: keyContext,
  claimKey: expired.claimKey,
  inviteRef: fixture.expired_invite_ref,
  personaPub: expired.personaPub,
});

const tokenSelfContext = await getClaimContext({
  transport,
  orgSlug: fixture.org,
  inviteRef: fixture.self_invite_ref,
});
const tokenSelfClaim = await mintMemberClaim({
  context: tokenSelfContext,
  personalRootSeed: hexToBytes(fixture.personal_seed_hex),
  inviteRef: fixture.self_invite_ref,
  token: fixture.self_token,
  profile: {},
  kemSeed: hexToBytes(fixture.kem_seed_hex),
  nowMs: fixture.lagging_now_ms,
});
const tokenSelfSubmit = await submitClaim({
  context: tokenSelfContext,
  event: tokenSelfClaim.event,
});
const tokenSelfStatus = await getClaimStatus({
  context: tokenSelfContext,
  claimKey: tokenSelfClaim.claimKey,
  inviteRef: fixture.self_invite_ref,
  personaPub: tokenSelfClaim.personaPub,
});

const keyClaim = await mintMemberClaim({
  context: keyContext,
  personalRootSeed: hexToBytes(fixture.key_personal_seed_hex),
  inviteRef: fixture.key_invite_ref,
  profile: { display_name: 'Key Bound' },
  kemSeed: hexToBytes(fixture.key_kem_seed_hex),
  nowMs: fixture.lagging_now_ms,
});
const keySubmit = await submitClaim({
  context: keyContext,
  event: keyClaim.event,
});
const keyStatus = await getClaimStatus({
  context: keyContext,
  claimKey: keyClaim.claimKey,
  inviteRef: fixture.key_invite_ref,
  personaPub: keyClaim.personaPub,
});

const bearerContext = await getClaimContext({
  transport,
  orgSlug: fixture.org,
  inviteRef: fixture.invite_ref,
});
const bearerHeads = bearerContext.heads;
const initial = await mintMemberClaim({
  context: bearerContext,
  personalRootSeed: hexToBytes(fixture.personal_seed_hex),
  inviteRef: fixture.invite_ref,
  token: fixture.token,
  profile: fixture.profile,
  kemSeed: hexToBytes(fixture.kem_seed_hex),
  nowMs: fixture.lagging_now_ms,
});
const pending = await submitClaim({
  context: bearerContext,
  event: initial.event,
});
const pendingStatus = await getClaimStatus({
  context: bearerContext,
  claimKey: initial.claimKey,
  inviteRef: fixture.invite_ref,
  personaPub: initial.personaPub,
});
const headsAfterPending = await heads();

const founderApproval = await signClaimApproval({
  context: bearerContext,
  personalRootSeed: hexToBytes(fixture.founder_personal_seed_hex),
  inviteRef: fixture.invite_ref,
  personaPub: initial.personaPub,
});
const outsiderApproval = await signClaimApproval({
  context: bearerContext,
  personalRootSeed: hexToBytes(fixture.outsider_personal_seed_hex),
  inviteRef: fixture.invite_ref,
  personaPub: initial.personaPub,
});
const badSignatureApproval = await refused(() => submitClaimApproval({
  context: bearerContext,
  claimKey: initial.claimKey,
  inviteRef: fixture.invite_ref,
  personaPub: initial.personaPub,
  approval: {
    ...founderApproval,
    sig: `${founderApproval.sig[0] === '0' ? '1' : '0'}`
      + founderApproval.sig.slice(1),
  },
}));
const unauthorizedApproval = await refused(() => submitClaimApproval({
  context: bearerContext,
  claimKey: initial.claimKey,
  inviteRef: fixture.invite_ref,
  personaPub: initial.personaPub,
  approval: outsiderApproval,
}));
const afterRefusals = await getClaimStatus({
  context: bearerContext,
  claimKey: initial.claimKey,
  inviteRef: fixture.invite_ref,
  personaPub: initial.personaPub,
});

const firstApproval = await submitClaimApproval({
  context: bearerContext,
  claimKey: initial.claimKey,
  inviteRef: fixture.invite_ref,
  personaPub: initial.personaPub,
  approval: founderApproval,
});
const headsAfterFirstApproval = await heads();

const rootSigningKey = await importEd25519RootSigningKey(
  hexToBytes(fixture.root_seed_hex),
);
const rootApproval = await buildClaimApproval({
  approverPub: fixture.root_pub,
  approverSigningKey: rootSigningKey,
  inviteRef: fixture.invite_ref,
  personaPub: initial.personaPub,
});
const secondApproval = await submitClaimApproval({
  context: bearerContext,
  claimKey: initial.claimKey,
  inviteRef: fixture.invite_ref,
  personaPub: initial.personaPub,
  approval: rootApproval,
});
const extraApproval = await signClaimApproval({
  context: bearerContext,
  personalRootSeed: hexToBytes(fixture.extra_approver_personal_seed_hex),
  inviteRef: fixture.invite_ref,
  personaPub: initial.personaPub,
});
const readyApproval = await submitClaimApproval({
  context: bearerContext,
  claimKey: initial.claimKey,
  inviteRef: fixture.invite_ref,
  personaPub: initial.personaPub,
  approval: extraApproval,
});
const headsAfterReadyApproval = await heads();
const readyStatus = await getClaimStatus({
  context: bearerContext,
  claimKey: initial.claimKey,
  inviteRef: fixture.invite_ref,
  personaPub: initial.personaPub,
});

const finalApprovals = admittingApprovals(
  readyStatus.approvals,
  readyApproval.admitting,
);
// Simulate a freshly-discovered current frontier that moved while approval
// was pending. The server-supplied position must override it for both the
// event and its KEM credential.
const shiftedFinalizeContext = {
  ...bearerContext,
  heads: [fixture.wrong_invite_ref],
  maxHlc: [readyApproval.position.hlc[0] + 1_000_000, 0],
};
const finalClaim = await mintMemberClaim({
  context: shiftedFinalizeContext,
  personalRootSeed: hexToBytes(fixture.personal_seed_hex),
  inviteRef: fixture.invite_ref,
  token: fixture.token,
  profile: fixture.profile,
  approvals: finalApprovals,
  kemSeed: hexToBytes(fixture.kem_seed_hex),
  nowMs: fixture.lagging_now_ms,
  position: readyApproval.position,
});
let mismatchedCredentialHlcRejected = false;
try {
  await mintMemberClaim({
    context: shiftedFinalizeContext,
    personalRootSeed: hexToBytes(fixture.personal_seed_hex),
    inviteRef: fixture.invite_ref,
    token: fixture.token,
    profile: fixture.profile,
    approvals: finalApprovals,
    kemSeed: hexToBytes(fixture.kem_seed_hex),
    position: readyApproval.position,
    credentialHlc: [
      readyApproval.position.hlc[0],
      readyApproval.position.hlc[1] + 1,
    ],
  });
} catch {
  mismatchedCredentialHlcRejected = true;
}
let mismatchedSubmitPositionRejected = false;
try {
  await submitClaim({
    context: shiftedFinalizeContext,
    event: finalClaim.event,
    position: {
      parents: readyApproval.position.parents,
      hlc: [
        readyApproval.position.hlc[0],
        readyApproval.position.hlc[1] + 1,
      ],
    },
  });
} catch {
  mismatchedSubmitPositionRejected = true;
}
const admitted = await submitClaim({
  context: shiftedFinalizeContext,
  event: finalClaim.event,
  position: readyApproval.position,
});
const admittedStatus = await getClaimStatus({
  context: bearerContext,
  claimKey: initial.claimKey,
  inviteRef: fixture.invite_ref,
  personaPub: initial.personaPub,
});
// The same expired event is now stale too; TTL remains the first decision.
const expiredStaleSubmit = await refused(() => submitClaim({
  context: keyContext,
  event: expired.event,
}));

const finalContext = await getClaimContext({
  transport,
  orgSlug: fixture.org,
  inviteRef: fixture.wrong_invite_ref,
});
const finalHeads = finalContext.heads;
const wrong = await mintMemberClaim({
  context: finalContext,
  personalRootSeed: hexToBytes(fixture.personal_seed_hex),
  inviteRef: fixture.wrong_invite_ref,
  token: 'wrong-token',
  profile: {},
  kemSeed: hexToBytes(fixture.kem_seed_hex),
  nowMs: fixture.lagging_now_ms,
});
const wrongToken = await refused(() => submitClaim({
  context: finalContext,
  event: wrong.event,
}));
const wrongStatus = await getClaimStatus({
  context: finalContext,
  claimKey: wrong.claimKey,
  inviteRef: fixture.wrong_invite_ref,
  personaPub: wrong.personaPub,
});

process.stdout.write(JSON.stringify({
  keyContext: {
    genesisId: keyContext.genesisId,
    heads: keyContext.heads,
    maxHlc: keyContext.maxHlc,
  },
  expired,
  expiredSubmit,
  expiredStatus,
  tokenSelfClaim,
  tokenSelfSubmit,
  tokenSelfStatus,
  keyClaim,
  keySubmit,
  keyStatus,
  bearerHeads,
  initial,
  pending,
  pendingStatus,
  headsAfterPending,
  founderApproval,
  outsiderApproval,
  badSignatureApproval,
  unauthorizedApproval,
  afterRefusals,
  firstApproval,
  headsAfterFirstApproval,
  rootApproval,
  secondApproval,
  extraApproval,
  readyApproval,
  finalApprovals,
  headsAfterReadyApproval,
  readyStatus,
  shiftedFinalizeHeads: shiftedFinalizeContext.heads,
  finalClaim,
  mismatchedCredentialHlcRejected,
  mismatchedSubmitPositionRejected,
  admitted,
  admittedStatus,
  expiredStaleSubmit,
  finalHeads,
  wrong,
  wrongToken,
  wrongStatus,
}));
