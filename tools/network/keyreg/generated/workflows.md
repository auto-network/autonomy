# Workflow register

Generated from registry.yaml by gen.py; do not edit.

A mutation records a key-lifecycle operation, not necessarily a complete user ceremony.
Authority lists name participants or authority sources; they do not encode AND/OR policy.
Read preconditions, notes, and the linked implementation together.
See the [reading guide](../GUIDE.md) and [key register](key-register.md).

## Find a workflow

- [approval.link_publish](#workflow-approval-link_publish)
- [armor.enroll_factor](#workflow-armor-enroll_factor)
- [armor.replace_recovery_slot](#workflow-armor-replace_recovery_slot)
- [armor.revoke_factor](#workflow-armor-revoke_factor)
- [armor.set_recovery](#workflow-armor-set_recovery)
- [ceremony.admission_event](#workflow-ceremony-admission_event)
- [ceremony.checkpoint_publish](#workflow-ceremony-checkpoint_publish)
- [ceremony.checkpoint_seed](#workflow-ceremony-checkpoint_seed)
- [ceremony.claim_approval](#workflow-ceremony-claim_approval)
- [ceremony.fleet_runtime_mint](#workflow-ceremony-fleet_runtime_mint)
- [ceremony.member_claim_mint](#workflow-ceremony-member_claim_mint)
- [ceremony.org_found](#workflow-ceremony-org_found)
- [ceremony.org_invite_mint](#workflow-ceremony-org_invite_mint)
- [ceremony.organization_grant_recovery](#workflow-ceremony-organization_grant_recovery)
- [ceremony.organization_storage_delegate](#workflow-ceremony-organization_storage_delegate)
- [ceremony.personal_identity_create](#workflow-ceremony-personal_identity_create)
- [ceremony.personal_serve_cert_mint](#workflow-ceremony-personal_serve_cert_mint)
- [ceremony.recovery_policy_change](#workflow-ceremony-recovery_policy_change)
- [ceremony.registration](#workflow-ceremony-registration)
- [ceremony.serve_cert_mint](#workflow-ceremony-serve_cert_mint)
- [ceremony.vault_master_read](#workflow-ceremony-vault_master_read)
- [delegate.checkpoint_publish](#workflow-delegate-checkpoint_publish)
- [fleet.distribute_kem](#workflow-fleet-distribute_kem)
- [fleet.enroll](#workflow-fleet-enroll)
- [fleet.kick](#workflow-fleet-kick)
- [fold.checkpoint](#workflow-fold-checkpoint)
- [fold.delegate](#workflow-fold-delegate)
- [fold.genesis](#workflow-fold-genesis)
- [fold.invite](#workflow-fold-invite)
- [fold.key_epoch](#workflow-fold-key_epoch)
- [fold.key_rotate](#workflow-fold-key_rotate)
- [fold.member_admission](#workflow-fold-member_admission)
- [fold.member_claim](#workflow-fold-member_claim)
- [fold.member_rekey](#workflow-fold-member_rekey)
- [fold.revoke](#workflow-fold-revoke)
- [fold.role_define](#workflow-fold-role_define)
- [fold.role_grant](#workflow-fold-role_grant)
- [fold.role_revoke](#workflow-fold-role_revoke)
- [link.mint_channel_key](#workflow-link-mint_channel_key)
- [module.org_reachability_publish](#workflow-module-org_reachability_publish)
- [passkey.enroll](#workflow-passkey-enroll)
- [passkey.revoke](#workflow-passkey-revoke)
- [recovery.succession](#workflow-recovery-succession)
- [rekey.frontier_marker](#workflow-rekey-frontier_marker)
- [root.rotation](#workflow-root-rotation)
- [route.checkpoint_adopt](#workflow-route-checkpoint_adopt)
- [route.claim_admission](#workflow-route-claim_admission)
- [route.claim_submit_admit](#workflow-route-claim_submit_admit)
- [route.claim_submit_stage](#workflow-route-claim_submit_stage)
- [route.invite_resolve](#workflow-route-invite_resolve)
- [route.join_bootstrap](#workflow-route-join_bootstrap)
- [route.join_context](#workflow-route-join_context)
- [route.join_install](#workflow-route-join_install)
- [route.link_publish](#workflow-route-link_publish)
- [route.org_shell_create](#workflow-route-org_shell_create)
- [route.relay_connect](#workflow-route-relay_connect)
- [storage.advance_state](#workflow-storage-advance_state)
- [storage.issue_grant](#workflow-storage-issue_grant)
- [storage.issue_receipt](#workflow-storage-issue_receipt)
- [storage.mint_credential](#workflow-storage-mint_credential)
- [storage.provision_missing](#workflow-storage-provision_missing)
- [vault.create_class](#workflow-vault-create_class)
- [vault.revoke_class_factor](#workflow-vault-revoke_class_factor)

<a id="workflow-approval-link_publish"></a>
## approval.link_publish

**Status:** built

**Authority:** personal_root_seed

**Preconditions**

- A pending link_publish approval row {org, target_uuid, target_type org:join, invite_ref, expires_at, meta} (org-membership.js:1008-1030).

**Writes**

- approved link_publish decision; Gate 2 org-scoped sign-on

**Workflow:** actors founder; opens root; requires personal_root_seed AND invite_event; produces link_publish_approval

**Source:** `tools/dashboard/static/js/pages/worktrees.js:signOnWithRootSeed` (ceremony)

**Crib:** §8

Step F2 (Gate 2, worktrees.js:173-203, :267). The root is opened for the approval, not reused from F1.

<a id="workflow-armor-enroll_factor"></a>
## armor.enroll_factor

**Status:** built

**Authority:** personal_root_seed

**Mints**

- factor_recipient

**Seals**

- personal_root_seed shares to factor_recipient

**Writes**

- next armor generation
- root-signed

**Source:** `tools/network/idkit/root_factor_policy.py` (module-op)

**Crib:** §2

<a id="workflow-armor-replace_recovery_slot"></a>
## armor.replace_recovery_slot

**Status:** built

**Authority:** recovery_code, personal_root_seed

**Seals**

- personal_root_seed to the successor code's recovery_slot_recipient

**Writes**

- replaced armor recovery slot
- root re-signed

**Source:** `tools/network/idkit/root_factor_policy.py:replace_recovery_slot` (module-op)

**Crib:** §9

Built at the armor layer; exposed by no route or UI yet.

<a id="workflow-armor-revoke_factor"></a>
## armor.revoke_factor

**Status:** built

**Authority:** personal_root_seed

**Writes**

- next armor generation without the revoked factor
- root-signed

**Source:** `tools/network/idkit/root_factor_policy.py` (module-op)

**Crib:** §2

<a id="workflow-armor-set_recovery"></a>
## armor.set_recovery

**Status:** built

**Authority:** personal_root_seed

**Preconditions**

- Enroll-only; it refuses to overwrite or clear an existing recovery slot.

**Seals**

- personal_root_seed to recovery_slot_recipient

**Writes**

- armor recovery slot

**Source:** `tools/network/idkit/root_factor_policy.py:set_recovery` (module-op)

**Crib:** §9

<a id="workflow-ceremony-admission_event"></a>
## ceremony.admission_event

**Status:** built

**Authority:** persona_signing_key

**Preconditions**

- The fold admits on the event only when: the carried claim verifies under the invitee's key and is the invitee's own claim at its staged position; at least the role's threshold of distinct authorized approvals; the invite is not already redeemed; the claim's position is after the persona's latest removal; the persona is not a member.

**Writes**

- an approver-authored admission event carrying the invitee's unchanged signed member.claim plus the approvals (operator ruling 2026-09-25); in the same window the founder publishes the checkpoint reflecting it (P2)

**Workflow:** actors founder; opens persona; requires persona_signing_key AND claim_staged; produces claim_approval, admission_event

**Source:** `tools/dashboard/static/js/ceremony/claim.js:signAdmission` (ceremony)

**Crib:** §8

Realizes P3 without re-signing the claim: a claim's author signature covers its whole payload including approvals and parents (events.py:618-619), and an approval covers only (kind, invite_ref, persona) (events.py:737-744). Built (auto-qrmlg.3 C5): the fold handler is fold.member_admission; the approver's browser signs the event right after the countersign that completed the threshold (org-membership.js approve), with no second invitee ceremony.

<a id="workflow-ceremony-checkpoint_publish"></a>
## ceremony.checkpoint_publish

**Status:** built

**Authority:** persona_signing_key

**Preconditions**

- The signer is in the checkpointers set of the previously adopted checkpoint (membership_checkpoint.py:139-149).
- ledger_head is the signer's first sorted head at assembly (membership_checkpoint.py:106, _first_head).

**Writes**

- checkpoint {v, org, seq, prev, ledger_head, members_root, checkpointers_root, ts, signer, proof, proof_index} at the registry (network_routes.py:post_membership_checkpoint); the signer caches it adopted

**Workflow:** actors founder; opens persona; requires persona_signing_key AND member_admitted AND ledger_heads AND checkpoint_seed; produces checkpoint_including_joiner, adopted_checkpoint

**Source:** `tools/dashboard/membership_checkpoint.py:checkpoint_due` (ceremony)

**Crib:** §8

The persona-signed form: a full sign-in (signon_preparation.py), the scoped sign-on (network-signon.mjs), and the approver's window right after an admission when no checkpoint-scoped delegate can publish (claim_service._persona_checkpoint_work, C5b). At admission the built path is delegate.checkpoint_publish.

<a id="workflow-ceremony-checkpoint_seed"></a>
## ceremony.checkpoint_seed

**Status:** built

**Authority:** org_root_signing_key

**Preconditions**

- No adopted checkpoint is cached locally (membership_checkpoint.py:119-132).

**Writes**

- checkpoint seq 0 {v, org, seq 0, prev genesis_id, ledger_head, members_root, checkpointers_root, ts}, signed by the org root opened from its sealed armor (network-signon.mjs:812-826), at the registry; cached adopted

**Workflow:** actors founder; opens root; requires sealed_org_root AND registry_binding AND ledger_heads; produces checkpoint_seed

**Source:** `tools/dashboard/static/js/network-signon.mjs:_publishMembershipCheckpoint` (ceremony)

**Crib:** §8

Signed in the founding window right after registration (the org-scoped sign-on phases founding.js:finishFoundedOrganization runs with the held seed), and again by any later sign-on: the server assembles the record only for a committed organization (bound and founded, network_routes.py:_org_unlock_plan), and the browser signs it with the org root unsealed from sealed_org_root (network-signon.mjs:prepareRootMaintenance, rootKeyFor). The advancing form is ceremony.checkpoint_publish.

<a id="workflow-ceremony-claim_approval"></a>
## ceremony.claim_approval

**Status:** built

**Authority:** persona_signing_key

**Writes**

- approval stored against the staged claim (POST /api/network/ledger/claim/{key}/approval, network_routes.py:post_ledger_claim_approval); the claim becomes ready, not admitted

**Workflow:** actors founder; opens persona; requires persona_signing_key AND claim_staged; produces claim_approval

**Source:** `tools/dashboard/static/js/ceremony/claim.js:signClaimApproval` (ceremony)

**Crib:** §8

Step F3. Root opened at org-membership.js:1082.

<a id="workflow-ceremony-fleet_runtime_mint"></a>
## ceremony.fleet_runtime_mint

**Status:** built

**Authority:** personal_root_seed, fleet_operating_signing_key, persona_signing_key

**Preconditions**

- The machine is an active roster member; the server re-checks machine_pub against the current roster (fleet_runtime.FleetRuntimeCredential.from_browser_payload).
- The operating seed and the reachability certificate are minted only when the personal organization holds a registry org_uuid.
- One serving seed and one org sync certificate per organization the machine is provisioned to serve and holds a persona in (serving_orgs, sync_orgs from /api/fleet/runtime).

**Mints**

- fleet_process_signing_key
- serving_machine_signing_key

**Writes**

- fleet process delegation certificate (fleet_operating_signing_key -> fleet_process_signing_key, scope fleet:sync, machine-direct, thirty-day maximum)
- reachability certificate (personal_root_seed -> fleet_operating_signing_key, scopes node:announce and node:lookup, org = registry org_uuid, seven-day default)
- org sync certificate per organization (persona_signing_key -> serving_machine_signing_key, scope fleet:sync, org = genesis id)
- the complete credential sealed into autonomy.machine.vault.audited row fleet-runtime (machine store, audited tier, opened unattended by the warm delegate; graph://67d0aa5f-885 D3)

**Refusals**

- not-active-roster-machine
- delegation-chain-invalid
- machine-id-mismatch
- reachability-pair-incomplete

**Workflow:** actors founder, joiner; opens root; requires personal_root_seed AND fleet_roster AND ledger_heads; produces persona_cert_fleet_sync

**Source:** `tools/dashboard/static/js/ceremony/fleet-enrollment.js:mintFleetRuntimeCredential` (ceremony)

**Crib:** §10, §12

Helpers: mintRuntimeCredential, mintReachabilityCert, mintOrgSyncCerts. One mint, two call sites (sign-on and first publication) through fleetRuntimePost since b3e67994. The payload is POSTed to /api/fleet/runtime (fleet_enrollment_routes._activate_runtime), which verifies it, peels the serving seeds and org sync certificates, installs the org channels, arms every connector cache, and caches the whole payload in ramfs for replay after a restart; a reboot clears ramfs and fails closed to a sign-on. Recorded 2026-09-20 after the three certificates were found absent from this registry (keyreg-forensics.md, session auto-0919-212441).

<a id="workflow-ceremony-member_claim_mint"></a>
## ceremony.member_claim_mint

**Status:** built

**Authority:** persona_signing_key

**Preconditions**

- Parents are the context heads (claim.js:234-247); a submit whose parents are not the founder's current heads is refused stale-heads (claim_service.py:170-171).

**Writes**

- member.claim {invite_ref, persona_pub, profile, approvals, kem_credential, token?}

**Workflow:** actors joiner; opens persona; requires persona_signing_key AND join_context; produces member_claim

**Source:** `tools/dashboard/static/js/ceremony/claim.js:mintMemberClaim` (ceremony)

**Crib:** §8

Step J3.

<a id="workflow-ceremony-org_found"></a>
## ceremony.org_found

**Status:** built

**Authority:** personal_root_seed, org_root_signing_key, persona_signing_key

**Mints**

- org_root_signing_key

**Writes**

- the org root sealed to the personal root's KEM key (POST /api/network/org-key/sealed, network_routes.py:post_sealed_org_key)
- the founding batch genesis, role.define, invite, member.claim (POST /api/network/ledger/found, network_routes.py:post_ledger_found)
- the organization KEM key and the storage delegate with the membership:checkpoint scope (vault-unlock.js submitVault)

**Workflow:** actors founder; opens root; requires org_shell; produces sealed_org_root, ledger_heads, checkpoint_delegate_grant

**Source:** `tools/dashboard/static/js/ceremony/founding.js:foundExistingOrganizationShell` (ceremony)

**Crib:** §7, §8

One personal-root opening ("Set up organization authority"). The org root is minted in memory and signs the genesis; the seed and the org root stay in memory past the founding batch so the same window signs the registration (org root) and drives the checkpoint seed and serve-cert phases (held seed); both are zeroed afterwards (founding.js:foundExistingOrganizationShell, auto-2vseu).

<a id="workflow-ceremony-org_invite_mint"></a>
## ceremony.org_invite_mint

**Status:** built

**Authority:** persona_signing_key

**Preconditions**

- Parents are the founder's current heads (GET /api/network/ledger/heads).

**Writes**

- invite event {granted_role, expiry, sponsor, token_hash|invite_pub, max_uses?} appended through POST /api/network/ledger/invite

**Workflow:** actors founder; opens persona; requires persona_signing_key AND ledger_heads; produces invite_event

**Source:** `tools/dashboard/static/js/ceremony/org-invite.js:mintOrgInvite` (ceremony)

**Crib:** §8

Step F1. Root opened at tools/dashboard/static/js/org-membership.js:861; the seed is zeroed at :886 after the publish chain settles, and is not passed to publishMint (:1008), so F2 opens the root again.

<a id="workflow-ceremony-organization_grant_recovery"></a>
## ceremony.organization_grant_recovery

**Status:** built

**Authority:** persona_kem_private, persona_signing_key

**Writes**

- memory-held generation secrets; optional initial signed public KEM credential; no grants or storage states

**Source:** `tools/dashboard/unlock_routes.py:_accept_organization_kem_key` (ceremony)

**Crib:** §1c, §12, §14

graph://35308bf7-584. Organization sign-in validates scoped KEM keys against current credentials and consumes the existing grant opener. The same opener recovers addressed, unheld states at read time and after RAM-only reload retention (auto-a1pub). Root stays in browser; networking resumes only after cleanup. Live receiver proof is tracked by auto-hwlcy. Initial setup (graph://dc405ba4-eef) may publish a missing current member credential using the founding signer and existing KeyControlStore. It never replaces an existing credential or persists a KEM private key.

<a id="workflow-ceremony-organization_storage_delegate"></a>
## ceremony.organization_storage_delegate

**Status:** built

**Authority:** persona_signing_key

**Mints**

- agent_delegate_signing_key when absent or below thirty days remaining

**Writes**

- ninety-day signed delegate event and personal audited seed on re-mint only

**Workflow:** actors founder, joiner; opens persona; requires persona_signing_key AND ledger_heads; produces delegate_grant, checkpoint_delegate_grant

**Source:** `tools/dashboard/static/js/ceremony/org-storage-delegate.js:prepareStorageDelegate` (ceremony)

**Crib:** §7, §11

A reuse sign-in adds no ledger event. Workflow graph://b437ecfb-e23. For a checkpointer the grant also carries membership:checkpoint (storage_delegate_scopes(checkpointer=True); operator ruling 1, 2026-09-25; built c7d6992a), and a recorded grant without it is re-minted at the next sign-on (org_storage_delegate.prepare remint_required). The proof-of-possession form is unchanged.

<a id="workflow-ceremony-personal_identity_create"></a>
## ceremony.personal_identity_create

**Status:** built

**Authority:** personal_root_seed

**Mints**

- personal_root_seed

**Writes**

- the armored personal root and its root_pub (POST /api/identity/personal
- identity_routes.py:post_personal)

**Workflow:** actors founder, joiner; opens root; requires nothing; produces personal_identity

**Source:** `tools/dashboard/static/js/network-onboarding.js:_createIdentity` (ceremony)

**Crib:** §2

The seed is generated and armored in this window; opens root models the human ceremony that creates it.

<a id="workflow-ceremony-personal_serve_cert_mint"></a>
## ceremony.personal_serve_cert_mint

**Status:** built

**Authority:** personal_root_seed

**Mints**

- serving_delegate_key

**Writes**

- personal-org bootstrap cert, legacy viewer_cert, dns01_cert over one child
- the serving key as autonomy.machine.vault.audited row serving-key.<org_uuid>; the certificates as the autonomy.machine.serve-cert row keyed by org_uuid (graph://67d0aa5f-885 D4, D5)

**Source:** `tools/dashboard/static/js/network-signon.mjs:_mintServeCredential` (ceremony)

**Crib:** §8

Existing personal branch still emits viewer_cert; do not infer organization content-viewer certificate use from this bootstrap field. Subject (auto-8sdrr, decision 2026-09-27): the personal scope's persona, which is the personal root itself (runtime personal_persona_pub), deterministic across every machine of one identity; personal Services are reserved under it and the registry binds their labels to the tunnel's subject. A stored delegate naming anything else stays ok for fleet sync and reports remint_required, so the next sign-on re-mints it without an outage.

<a id="workflow-ceremony-recovery_policy_change"></a>
## ceremony.recovery_policy_change

**Status:** designed

**Authority:** personal_root_seed, recovery_signing_key

**Writes**

- recovery policy transition

**Source:** `tools/network/registry/app.py` (ceremony)

**Crib:** §8, §9

<a id="workflow-ceremony-registration"></a>
## ceremony.registration

**Status:** built

**Authority:** org_root_signing_key, personal_root_seed

**Writes**

- registry binding
- including the recovery policy declaration when pinned

**Workflow:** actors founder; opens root; requires sealed_org_root AND ledger_heads; produces registry_binding

**Source:** `tools/network/registry/app.py` (route)

**Crib:** §8, §9

A root-direct POST /v1/orgs {org_uuid, root_pub, recovery_policy}. An organization's registration is signed by its ORG root (network-signon.mjs:_signRootRequest, network-identity.js:signRegistration), reached in a personal-root window by unsealing sealed_org_root with the personal seed (openSealedArmor) or, inside founding, by the root the founding just minted; only the personal org's registration is signed by the personal root (provisionPersonalNetworkIdentity). The workflow fields model the organization form. A first registration never runs in sign-on maintenance: signon_preparation.organization_plans and _maintainBinding skip an organization with no binding (they renew or reclaim only).

<a id="workflow-ceremony-serve_cert_mint"></a>
## ceremony.serve_cert_mint

**Status:** built

**Authority:** persona_signing_key

**Mints**

- serving_delegate_key

**Writes**

- registry tunnel certificate and DNS01 certificate over the same child
- the serving key as autonomy.machine.vault.audited row serving-key.<org_uuid>; the certificates as the autonomy.machine.serve-cert row keyed by org_uuid (graph://67d0aa5f-885 D4, D5)

**Workflow:** actors founder, joiner; opens persona; requires persona_signing_key AND registry_binding AND checkpoint_seed; produces serve_cert

**Source:** `tools/dashboard/static/js/network-signon.mjs:_mintServeCredentialPersona` (ceremony)

**Crib:** §8

Organization branch; no content viewer certificate.

<a id="workflow-ceremony-vault_master_read"></a>
## ceremony.vault_master_read

**Status:** built

**Authority:** persona_kem_private

**Writes**

- memory-held generation secrets recovered from CapabilityGrants

**Source:** `tools/vault/unlock.py:open_generation_keys` (ceremony)

**Crib:** §8, §18

Grant opening primitive, not a human-factor authorization or an audited-object release. Org integration remains pending.

<a id="workflow-delegate-checkpoint_publish"></a>
## delegate.checkpoint_publish

**Status:** built

**Authority:** agent_delegate_signing_key

**Writes**

- advancing checkpoint signed by the checkpoint-scoped delegate at admission
- no ceremony

**Workflow:** actors founder; opens delegate; requires agent_delegate_signing_key AND checkpoint_delegate_grant AND member_admitted AND ledger_heads AND checkpoint_seed; produces checkpoint_including_joiner, adopted_checkpoint

**Source:** `tools/dashboard/membership_checkpoint.py:publish_after_membership_change` (module-op)

**Crib:** §8, §11

Checkpoint at admission with a hot signer (operator ruling 1, 2026-09-25; built c7d6992a..3f15ea10): after a claim admits, a revocation or a role change, when either root of the fold differs from the newest retained record, the delegate-signed record is posted and retained; a registry seq/prev refusal re-reads and re-assembles, bounded; chaining uses the registry's signed bytes kept beside the adopted tuple.

<a id="workflow-fleet-distribute_kem"></a>
## fleet.distribute_kem

**Status:** designed

**Authority:** personal_root_seed

**Seals**

- persona_kem_private to per_machine_key
- one seal per fleet machine not removed

**Source:** `tools/network/storagekit/tamarin/VaultFleetDist.spthy` (module-op)

**Crib:** §1d, §1e

The KEM-distribution record is the unbuilt half of the fleet layer (bead auto-pw9bs.6); the cited theory is its formal spec.

<a id="workflow-fleet-enroll"></a>
## fleet.enroll

**Status:** built

**Authority:** personal_root_seed

**Writes**

- root-signed RosterEntry addressing one machine

**Source:** `tools/network/fleet_roster.py:enroll` (module-op)

**Crib:** §1b, §10

Renewal is a new entry with a higher sequence number superseding the old.

<a id="workflow-fleet-kick"></a>
## fleet.kick

**Status:** built

**Authority:** personal_root_seed

**Revokes**

- per_machine_key

**Writes**

- root-signed kick tombstone; stops future distribution to that machine

**Source:** `tools/network/fleet_roster.py:kick` (module-op)

**Crib:** §1b, §1d, §10

<a id="workflow-fold-checkpoint"></a>
## fold.checkpoint

**Status:** built

**Authority:** holders of checkpoint

**Writes**

- checkpoint record

**Refusals**

- checkpoint-unauthorized

**Source:** `tools/network/ledger/fold.py:_h_checkpoint` (fold)

**Crib:** §8

Checkpoint is decided-removed from the design vocabulary; the handler remains in code, so it remains in this inventory.

<a id="workflow-fold-delegate"></a>
## fold.delegate

**Status:** built

**Authority:** persona_signing_key, browser_session_key

**Preconditions**

- The delegated scope set never exceeds the delegator's (no scope escalation).
- A non-redelegable certificate cannot mint children.

**Mints**

- agent_delegate_signing_key

**Writes**

- delegation certificate

**Refusals**

- scope-escalation
- not-redelegable
- delegate-unproven
- delegate-nonce-reused

**Source:** `tools/network/ledger/fold.py:_h_delegate` (fold)

**Crib:** §8, §11

<a id="workflow-fold-genesis"></a>
## fold.genesis

**Status:** built

**Authority:** org_root_signing_key

**Writes**

- genesis record
- org recovery_pub declaration

**Refusals**

- recovery-equals-root

**Source:** `tools/network/ledger/fold.py:_h_genesis` (fold)

**Crib:** §7, §8

<a id="workflow-fold-invite"></a>
## fold.invite

**Status:** built

**Authority:** holders of invite:<role>

**Writes**

- invite record

**Refusals**

- invite-overreach
- invite-sponsor-mismatch
- invite-not-in-ancestry
- invite-expired
- invite-dead

**Source:** `tools/network/ledger/fold.py:_h_invite` (fold)

**Crib:** §8

<a id="workflow-fold-key_epoch"></a>
## fold.key_epoch

**Status:** built

**Authority:** org_root_signing_key

**Writes**

- key epoch marker

**Refusals**

- key-epoch-unauthorized

**Source:** `tools/network/ledger/fold.py:_h_key_epoch` (fold)

**Crib:** §8

<a id="workflow-fold-key_rotate"></a>
## fold.key_rotate

**Status:** built

**Authority:** org_root_signing_key, recovery_signing_key

**Preconditions**

- Under recovery policy recovery-key, the declared recovery key co-signs the exact transition.
- The new key proves control through the continuity signature.

**Mints**

- org_root_signing_key

**Revokes**

- org_root_signing_key

**Writes**

- rotation record

**Refusals**

- not-root
- rotate-wrong-old
- bad-continuity
- recovery-not-enrolled
- recovery-sig-bad
- recovery-continuity-missing
- bad-recovery-continuity
- recovery-continuity-not-declared

**Source:** `tools/network/ledger/fold.py:_h_key_rotate` (fold)

**Crib:** §8, §9

<a id="workflow-fold-member_admission"></a>
## fold.member_admission

**Status:** built

**Authority:** approver persona key, org_root_signing_key

**Preconditions**

- The carried member.claim verifies under the invitee's key and its parents are in this event's ancestry (its staged position).
- The author is one of the carried approvers, or the root.
- No removal of the persona at or after the claim's timestamp.
- Every member.claim rule, with the carried approvals in place of the claim's own.

**Writes**

- member claim (record keyed by the admission event id)
- member recovery_pub enrollment

**Refusals**

- admission-bad-claim
- admission-claim-position
- admission-unauthorized
- admission-after-removal
- claim-wrong-key
- claim-bad-token
- claim-bad-credential
- invite-already-claimed
- persona-exists
- approval-missing

**Source:** `tools/network/ledger/fold.py:_h_member_admission` (fold)

**Crib:** §8, §9

OrgAdmission.tla admission event (auto-qrmlg.12; operator ruling 2026-09-25): the approver admits without a second invitee ceremony.

<a id="workflow-fold-member_claim"></a>
## fold.member_claim

**Status:** built

**Authority:** invited persona key

**Preconditions**

- The claim's recovery enrollment is validated here; recovery_pub equal to the org root is refused.

**Writes**

- member claim
- member recovery_pub enrollment

**Refusals**

- claim-wrong-key
- claim-bad-token
- claim-bad-credential
- invite-already-claimed
- persona-exists

**Source:** `tools/network/ledger/fold.py:_h_member_claim` (fold)

**Crib:** §8, §9

<a id="workflow-fold-member_rekey"></a>
## fold.member_rekey

**Status:** built

**Authority:** persona_signing_key, org_root_signing_key, member_recovery_key

**Preconditions**

- The cited old key must be the persona's current key.
- A revoked key cannot self-authorize its own rekey.
- The new key proves control through the continuity signature.
- Under recovery policy none, a recovery-authorized rekey is refused, never ignored.

**Mints**

- persona_signing_key

**Revokes**

- persona_signing_key

**Writes**

- rekey record
- implicit revoke of the departed key on the recovery door

**Refusals**

- rekey-wrong-key
- rekey-unauthorized
- rekey-revoked-key
- recovery-not-enrolled
- recovery-sig-bad
- bad-continuity
- persona-exists
- unknown-persona
- bad-approval

**Source:** `tools/network/ledger/fold.py:_h_member_rekey` (fold)

**Crib:** §8, §9

<a id="workflow-fold-revoke"></a>
## fold.revoke

**Status:** built

**Authority:** persona_signing_key, org_root_signing_key

**Revokes**

- agent_delegate_signing_key
- persona_signing_key

**Writes**

- revocation record

**Refusals**

- revoke-unauthorized
- revoke-bad-target
- revoke-target-not-in-ancestry

**Source:** `tools/network/ledger/fold.py:_h_revoke` (fold)

**Crib:** §8

<a id="workflow-fold-role_define"></a>
## fold.role_define

**Status:** built

**Authority:** holders of role:define

**Writes**

- role definition

**Refusals**

- role-define-unauthorized
- role-define-overreach

**Source:** `tools/network/ledger/fold.py:_h_role_define` (fold)

**Crib:** §8

<a id="workflow-fold-role_grant"></a>
## fold.role_grant

**Status:** built

**Authority:** holders of role:grant:<role>

**Writes**

- role grant

**Refusals**

- role-grant-unauthorized
- role-undefined
- approval-missing
- bad-approval

**Source:** `tools/network/ledger/fold.py:_h_role_grant` (fold)

**Crib:** §8

<a id="workflow-fold-role_revoke"></a>
## fold.role_revoke

**Status:** built

**Authority:** holders of role:grant:<role>

**Writes**

- role revocation

**Refusals**

- role-revoke-unauthorized

**Source:** `tools/network/ledger/fold.py:_h_role_revoke` (fold)

**Crib:** §8

<a id="workflow-link-mint_channel_key"></a>
## link.mint_channel_key

**Status:** built

**Authority:** agent_delegate_signing_key

**Mints**

- link_channel_signing_key

**Writes**

- org audited channel-key Setting; public half returned for grant and fragment

**Source:** `tools/dashboard/link_channel_key.py:mint_channel_key` (module-op)

**Crib:** §8

<a id="workflow-module-org_reachability_publish"></a>
## module.org_reachability_publish

**Status:** built

**Authority:** serving_machine_signing_key

**Writes**

- the machine's reachability row in the org scope
- replicated by org sync

**Workflow:** actors founder, joiner; opens none; requires persona_cert_fleet_sync; produces reachability_row

**Source:** `tools/network/fleet_sync_scheduler.py:_publish_org_reachability` (module-op)

**Crib:** §10

A co-member reads the row only after a pull, so it cannot seed a first contact.

<a id="workflow-passkey-enroll"></a>
## passkey.enroll

**Status:** built

**Authority:** personal_root_seed

**Writes**

- root-signed enrollment statement binding the per_credential_wrapping_key public half

**Source:** `tools/network/idkit/enrollment.py:mint` (ceremony)

**Crib:** §2, §8

<a id="workflow-passkey-revoke"></a>
## passkey.revoke

**Status:** built

**Authority:** personal_root_seed

**Revokes**

- per_credential_wrapping_key
- browser_session_key

**Writes**

- root-signed revocation record

**Source:** `tools/network/idkit/revocation.py:RevocationRecord` (ceremony)

**Crib:** §2, §8

<a id="workflow-recovery-succession"></a>
## recovery.succession

**Status:** designed

**Authority:** personal_root_seed, recovery_signing_key

**Preconditions**

- Completion requires the elapsed witness window over a chain containing the declaration and no cancellation and no veto.

**Mints**

- recovery_signing_key

**Revokes**

- recovery_signing_key

**Writes**

- witnessed declaration
- then completion attestation

**Source:** `tools/network/storagekit/tamarin/VaultRecoverySuccession.spthy` (module-op)

**Crib:** §9

<a id="workflow-rekey-frontier_marker"></a>
## rekey.frontier_marker

**Status:** designed

**Authority:** persona_signing_key

**Writes**

- fold-inert frontier marker citing all locally current authority heads

**Source:** `tools/network/storagekit/tamarin/VaultRekeyMarker.spthy` (module-op)

**Crib:** §1d

Without the marker the re-key achieves nothing (the F-001 defect); the calibration theory VaultRekeyF001 demonstrates exactly that.

<a id="workflow-root-rotation"></a>
## root.rotation

**Status:** built

**Authority:** personal_root_seed, recovery_signing_key

**Preconditions**

- Three signatures — old root, enrolled recovery key, and new root. The succession convinces its relying parties, not the local machine - the fleet accepts a successor only against root-signed RosterEntry material, and the auto.network registry rebinds only for a request signed by the recovery key it pinned at enrollment.

**Mints**

- personal_root_seed

**Revokes**

- personal_root_seed

**Writes**

- succession record

**Source:** `tools/network/idkit/root_rotation.py:make_rotation` (module-op)

**Crib:** §9

A local verification has no relying party (crib section 0: the owner rewriting their own store is a feature); the load-bearing checks are the registry rebind gate (tools/network/registry/app.py, which resolves the pinned recovery key server-side) and the fleet's root-signed RosterEntry acceptance (tools/network/fleet_roster.py).

<a id="workflow-route-checkpoint_adopt"></a>
## route.checkpoint_adopt

**Status:** built

**Authority:** registry membership state; this node's own ledger

**Preconditions**

- Skip when the cached seq >= the registry seq (network_routes.py:756-758).
- Fold the local ledger at the registry's ledger_head and require members_root equality; an id the local ledger lacks raises in ancestry/get and nothing is adopted (network_routes.py:759-768, fold.py:499-500, ledger.py:36-42, 66-77).

**Writes**

- adopted checkpoint cache row (membership_checkpoint.py:record_adopted)

**Refusals**

- no registry binding
- registry unreachable
- head absent from the local ledger
- members_root mismatch

**Workflow:** actors joiner; opens none; requires checkpoint_including_joiner AND ledger_heads; produces adopted_checkpoint

**Source:** `tools/dashboard/network_routes.py:_adopt_registry_checkpoint` (route)

**Crib:** §8

Never signs (docstring, network_routes.py:724-733). Callers: join install (:945), sign-on preparation (signon_preparation.py:140), and the route POST /api/network/membership-checkpoint/adopt; no background caller. The head-presence precondition is not monotone and is not expressed in requires; OrgAdmission.tla models it: under these rules a member bootstrapped before a later admission that the next checkpoint covers can never adopt it, and no pull is admitted (OrgAdmissionCurrent.cfg, EveryAdmittedMemberPulls violated). The proposed adopt-by-verification rule (signer in the previous checkpointers root, prev chain, no local head) with checkpoint-at-admission removes the deadlock (OrgAdmissionProposed.cfg). Under that rule the prover's own inclusion path cannot come from a local fold at the head (org_sync_channels.py:173-179, 197-205); it must arrive with the checkpoint.

<a id="workflow-route-claim_admission"></a>
## route.claim_admission

**Status:** built

**Authority:** approver persona key

**Writes**

- member.admission appended to the founder's ledger; the staged claim dropped; the checkpoint reflecting the member published (P2)

**Workflow:** actors founder; opens none; requires admission_event; produces member_admitted

**Source:** `tools/dashboard/claim_service.py:admit` (route)

**Crib:** §8

Step F3, admit half, on the founder's machine (POST /api/network/ledger/admission).

<a id="workflow-route-claim_submit_admit"></a>
## route.claim_submit_admit

**Status:** built

**Authority:** invited persona key

**Writes**

- member.claim appended to the founder's ledger (claim_service.py:172-175)

**Workflow:** actors founder; opens none; requires member_claim AND policy_self_admit; produces member_admitted

**Source:** `tools/dashboard/claim_service.py:submit` (route)

**Crib:** §8

Step J3, submit half, on the founder's machine: the fold admits a key-bound self-admitting claim at once. Under an approval role the claim is staged (route.claim_submit_stage) and route.claim_admission admits it. The admitting append publishes the checkpoint (P2).

<a id="workflow-route-claim_submit_stage"></a>
## route.claim_submit_stage

**Status:** built

**Authority:** invited persona key

**Writes**

- pending claim staged (claim_service.py:189-193); returns pending {have, need}

**Workflow:** actors founder; opens none; requires (member_claim AND policy_approval); produces claim_staged

**Source:** `tools/dashboard/claim_service.py:submit` (route)

**Crib:** §8

Step J3 under a role whose policy requires approvals.

<a id="workflow-route-invite_resolve"></a>
## route.invite_resolve

**Status:** built

**Authority:** bearer of the org:join link

**Writes**

- nothing durable; returns {org, invite_ref} from the registry envelope

**Workflow:** actors joiner; opens none; requires join_link_grant; produces invite_ref_resolved

**Source:** `tools/dashboard/network_routes.py:post_invite_resolve` (route)

**Crib:** §8

Step J1.

<a id="workflow-route-join_bootstrap"></a>
## route.join_bootstrap

**Status:** built

**Authority:** admitted persona

**Preconditions**

- The fold must show the persona as a valid member; otherwise pending (claim_service.py:228-231).

**Writes**

- nothing durable; returns {events, more, binding, member_profiles, reachability_rows, brand} as of this moment (claim_service.py:233-235)

**Workflow:** actors founder; opens none; requires member_admitted; produces bootstrap_snapshot

**Source:** `tools/dashboard/claim_service.py:bootstrap` (route)

**Crib:** §8

Step J5, served half, on the founder's machine.

<a id="workflow-route-join_context"></a>
## route.join_context

**Status:** built

**Authority:** bearer of the org:join link

**Preconditions**

- Served by the founder's connector over the link channel; the founder machine must be online (link_serving.py:1007-1033).

**Writes**

- nothing durable; returns {genesis_id, heads, max_hlc, granted_role, binding, invite_expiry, sponsor_pub, brand, sponsor profile}

**Workflow:** actors founder; opens none; requires invite_ref_resolved AND invite_event; produces join_context

**Source:** `tools/dashboard/claim_service.py:context` (route)

**Crib:** §8

Step J2. Executes on the founder's machine.

<a id="workflow-route-join_install"></a>
## route.join_install

**Status:** built

**Authority:** admitted persona

**Writes**

- org DB, ledger re-folded from genesis, binding, persona row, directory row, install seed (profiles and the sponsor's addresses)
- adoption BY FOLD of the checkpoint carried in the join bundle, bounded by the registry's record, atomically with the snapshot (OrgAdmission.tla bundle_adopt; OrgAdmissionBundleBound.tla)

**Workflow:** actors joiner; opens none; requires bootstrap_snapshot AND checkpoint_including_joiner; produces ledger_heads, registry_binding, install_seed_addresses, adopted_checkpoint

**Source:** `tools/dashboard/network_routes.py:post_join_outcome` (route)

**Crib:** §8

Step J5 (built 6d6f04ec..3f15ea10). The page forwards the bootstrap's reachability_rows and the bundled checkpoint; the route seeds the addresses and adopts the checkpoint by fold. Deferred here: storage delegate, serve cert, fleet runtime, sync cert (C6).

<a id="workflow-route-link_publish"></a>
## route.link_publish

**Status:** built

**Authority:** agent_delegate_signing_key

**Preconditions**

- Serving must be startable for the org (link_approvals.py:_require_startable_serving).

**Writes**

- grant row {grant_id, target_uuid, target_type, meta, subject, issued_at, channel_pub, invite_ref}, relay create-link, invite bearer (POST ledger/invite/bearer)

**Workflow:** actors founder; opens delegate; requires agent_delegate_signing_key AND link_publish_approval AND serve_cert; produces join_link_grant

**Source:** `tools/dashboard/link_approvals.py:_execute_link_publish` (route)

**Crib:** §8

Step F2, executor half. The channel key is minted by link.mint_channel_key.

<a id="workflow-route-org_shell_create"></a>
## route.org_shell_create

**Status:** built

**Authority:** the operator's authenticated dashboard session

**Writes**

- the local organization shell (slug
- identity) with founded false

**Workflow:** actors founder; opens none; requires personal_identity; produces org_shell

**Source:** `tools/dashboard/server.py:api_orgs_create` (route)

**Crib:** §7

<a id="workflow-route-relay_connect"></a>
## route.relay_connect

**Status:** built

**Authority:** serving_delegate_key

**Writes**

- a live serving slot at the org's relay

**Workflow:** actors founder, joiner; opens none; requires serve_cert; produces relay_slot

**Source:** `tools/network/relaykit/connector.py:TunnelConnector` (module-op)

**Crib:** §8

Slots are read for peer selection through org_sync_channels.relay_slots_provider. Whether the relay's per-org serve_machine_keys allow-set admits a new member's machine is an open question (relay.py:973-1000, store.py:1576-1583); unproven.

<a id="workflow-storage-advance_state"></a>
## storage.advance_state

**Status:** built

**Authority:** agent_delegate_signing_key, persona_signing_key

**Mints**

- generation_secret

**Writes**

- StorageStateDescriptor
- ParentBridge

**Refusals**

- StateAdvanceRequired on a stale write

**Source:** `tools/network/storagekit/lifecycle.py:advance_state` (module-op)

**Crib:** §1d, §6, §7

<a id="workflow-storage-issue_grant"></a>
## storage.issue_grant

**Status:** built

**Authority:** agent_delegate_signing_key, persona_signing_key

**Seals**

- generation_secret to persona_kem_private

**Writes**

- CapabilityGrant

**Source:** `tools/network/storagekit/capability.py:issue` (module-op)

**Crib:** §6, §7

<a id="workflow-storage-issue_receipt"></a>
## storage.issue_receipt

**Status:** built

**Authority:** persona_kem_private

**Writes**

- CapabilityReceipt — proof of knowledge
- not an assertion

**Source:** `tools/network/storagekit/capability.py:issue_receipt` (module-op)

**Crib:** §6, §16

<a id="workflow-storage-mint_credential"></a>
## storage.mint_credential

**Status:** built

**Authority:** persona_signing_key

**Mints**

- persona_kem_private

**Writes**

- PersonaKemCredential citing the current authority frontier

**Source:** `tools/network/storagekit/credentials.py:build` (module-op)

**Crib:** §1d, §6, §13

<a id="workflow-storage-provision_missing"></a>
## storage.provision_missing

**Status:** built

**Authority:** persona_kem_private

**Seals**

- generation_secret to an unprovisioned member's current PersonaKemCredential

**Writes**

- repair CapabilityGrant

**Source:** `tools/network/storagekit/distribution.py:provision_missing` (module-op)

**Crib:** §15

<a id="workflow-vault-create_class"></a>
## vault.create_class

**Status:** built

**Authority:** personal_root_seed

**Preconditions**

- A member class without the root-anchor wrap is refused at creation and again at the store (put_class, put_root_class_once).

**Mints**

- class_key

**Seals**

- class_key to factor recipients and to root_anchor_seed

**Writes**

- PolicyClassRecord generation

**Source:** `tools/vault/policy_class.py:create_class` (module-op)

**Crib:** §18

<a id="workflow-vault-revoke_class_factor"></a>
## vault.revoke_class_factor

**Status:** built

**Authority:** personal_root_seed

**Preconditions**

- The root-anchor wrap cannot be dropped.

**Mints**

- class_key

**Seals**

- class_key to surviving factors and to root_anchor_seed

**Writes**

- next PolicyClassRecord generation; applies at next write
- existing sealed material untouched

**Source:** `tools/vault/policy_class.py:revoke_factor` (module-op)

**Crib:** §3, §18
