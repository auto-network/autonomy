# Workflow register

Generated from registry.yaml by gen.py; do not edit.

A mutation records a key-lifecycle operation, not necessarily a complete user ceremony.
Authority lists name participants or authority sources; they do not encode AND/OR policy.
Read preconditions, notes, and the linked implementation together.
See the [reading guide](../GUIDE.md) and [key register](key-register.md).

## Find a workflow

- [armor.enroll_factor](#workflow-armor-enroll_factor)
- [armor.replace_recovery_slot](#workflow-armor-replace_recovery_slot)
- [armor.revoke_factor](#workflow-armor-revoke_factor)
- [armor.set_recovery](#workflow-armor-set_recovery)
- [ceremony.fleet_runtime_mint](#workflow-ceremony-fleet_runtime_mint)
- [ceremony.organization_grant_recovery](#workflow-ceremony-organization_grant_recovery)
- [ceremony.organization_storage_delegate](#workflow-ceremony-organization_storage_delegate)
- [ceremony.personal_serve_cert_mint](#workflow-ceremony-personal_serve_cert_mint)
- [ceremony.recovery_policy_change](#workflow-ceremony-recovery_policy_change)
- [ceremony.registration](#workflow-ceremony-registration)
- [ceremony.serve_cert_mint](#workflow-ceremony-serve_cert_mint)
- [ceremony.vault_master_read](#workflow-ceremony-vault_master_read)
- [fleet.distribute_kem](#workflow-fleet-distribute_kem)
- [fleet.enroll](#workflow-fleet-enroll)
- [fleet.kick](#workflow-fleet-kick)
- [fold.checkpoint](#workflow-fold-checkpoint)
- [fold.delegate](#workflow-fold-delegate)
- [fold.genesis](#workflow-fold-genesis)
- [fold.invite](#workflow-fold-invite)
- [fold.key_epoch](#workflow-fold-key_epoch)
- [fold.key_rotate](#workflow-fold-key_rotate)
- [fold.member_claim](#workflow-fold-member_claim)
- [fold.member_rekey](#workflow-fold-member_rekey)
- [fold.revoke](#workflow-fold-revoke)
- [fold.role_define](#workflow-fold-role_define)
- [fold.role_grant](#workflow-fold-role_grant)
- [fold.role_revoke](#workflow-fold-role_revoke)
- [link.mint_channel_key](#workflow-link-mint_channel_key)
- [passkey.enroll](#workflow-passkey-enroll)
- [passkey.revoke](#workflow-passkey-revoke)
- [recovery.succession](#workflow-recovery-succession)
- [rekey.frontier_marker](#workflow-rekey-frontier_marker)
- [root.rotation](#workflow-root-rotation)
- [storage.advance_state](#workflow-storage-advance_state)
- [storage.issue_grant](#workflow-storage-issue_grant)
- [storage.issue_receipt](#workflow-storage-issue_receipt)
- [storage.mint_credential](#workflow-storage-mint_credential)
- [storage.provision_missing](#workflow-storage-provision_missing)
- [vault.create_class](#workflow-vault-create_class)
- [vault.revoke_class_factor](#workflow-vault-revoke_class_factor)

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

**Source:** `tools/dashboard/static/js/ceremony/fleet-enrollment.js:mintFleetRuntimeCredential` (ceremony)

**Crib:** §10, §12

Helpers: mintRuntimeCredential, mintReachabilityCert, mintOrgSyncCerts. One mint, two call sites (sign-on and first publication) through fleetRuntimePost since b3e67994. The payload is POSTed to /api/fleet/runtime (fleet_enrollment_routes._activate_runtime), which verifies it, peels the serving seeds and org sync certificates, installs the org channels, arms every connector cache, and caches the whole payload in ramfs for replay after a restart; a reboot clears ramfs and fails closed to a sign-on. Recorded 2026-09-20 after the three certificates were found absent from this registry (keyreg-forensics.md, session auto-0919-212441).

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

**Source:** `tools/dashboard/static/js/ceremony/org-storage-delegate.js:prepareStorageDelegate` (ceremony)

**Crib:** §7, §11

A reuse sign-in adds no ledger event. Workflow graph://b437ecfb-e23.

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

Existing personal branch still emits viewer_cert; do not infer organization content-viewer certificate use from this bootstrap field.

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

**Authority:** personal_root_seed

**Writes**

- registry binding
- including the recovery policy declaration when pinned

**Source:** `tools/network/registry/app.py` (route)

**Crib:** §8, §9

<a id="workflow-ceremony-serve_cert_mint"></a>
## ceremony.serve_cert_mint

**Status:** built

**Authority:** persona_signing_key

**Mints**

- serving_delegate_key

**Writes**

- registry tunnel certificate and DNS01 certificate over the same child
- the serving key as autonomy.machine.vault.audited row serving-key.<org_uuid>; the certificates as the autonomy.machine.serve-cert row keyed by org_uuid (graph://67d0aa5f-885 D4, D5)

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
