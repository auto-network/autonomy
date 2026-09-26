# Workflow goals — generated from registry.yaml by gen.py; do not edit.

Artifacts are workflow prerequisites that are not keys. Workflow mutations
record what each step opens (root and persona need a human root window;
delegate and none run on a machine), what it requires (AND/OR) and what it
produces. A bare per-actor artifact inside a mutation binds to the executing
actor. The graph is monotone: freshness conditions such as a ledger head
being present at adoption are outside it and are modeled in TLA+.
See the [workflow register](workflows.md) and the [reading guide](../GUIDE.md).

## Actors

- **founder** — The org's founding member; sponsor of the invite and the only checkpointer in this workflow.
- **joiner** — The invited member, on a machine with its own personal fleet.

## Artifacts

One row per copy: a per-actor artifact has one copy per actor. Origin
given = held by a starting state, established before the workflow by
the listed mutations; the planner never re-produces it.

| Artifact copy | Status | Origin | Produced by (workflow) | Given by | Required by |
|---|---|---|---|---|---|
| <a id="artifact-admission_event"></a>admission_event | built | workflow | ceremony.admission_event | - | route.claim_admission |
| <a id="artifact-adopted_checkpoint"></a>adopted_checkpoint@founder | built | workflow | ceremony.checkpoint_publish, delegate.checkpoint_publish | - | goal org_sync_pull |
| adopted_checkpoint@joiner | built | workflow | route.checkpoint_adopt, route.join_install | - | goal org_sync_pull |
| <a id="artifact-bootstrap_snapshot"></a>bootstrap_snapshot | built | workflow | route.join_bootstrap | - | route.join_install |
| <a id="artifact-checkpoint_delegate_grant"></a>checkpoint_delegate_grant@founder | built | given | ceremony.org_found, ceremony.organization_storage_delegate | ceremony.organization_storage_delegate | delegate.checkpoint_publish, goal org_founded |
| checkpoint_delegate_grant@joiner | built | workflow | ceremony.organization_storage_delegate | - | - |
| <a id="artifact-checkpoint_including_joiner"></a>checkpoint_including_joiner | built | workflow | ceremony.checkpoint_publish, delegate.checkpoint_publish | - | route.checkpoint_adopt, route.join_install |
| <a id="artifact-checkpoint_seed"></a>checkpoint_seed | built | given | ceremony.checkpoint_seed | ceremony.checkpoint_seed | ceremony.checkpoint_publish, ceremony.serve_cert_mint, delegate.checkpoint_publish, goal org_founded |
| <a id="artifact-claim_approval"></a>claim_approval | built | workflow | ceremony.admission_event, ceremony.claim_approval | - | - |
| <a id="artifact-claim_staged"></a>claim_staged | built | workflow | route.claim_submit_stage | - | ceremony.admission_event, ceremony.claim_approval |
| <a id="artifact-delegate_grant"></a>delegate_grant@founder | built | workflow | ceremony.organization_storage_delegate | - | - |
| delegate_grant@joiner | built | workflow | ceremony.organization_storage_delegate | - | - |
| <a id="artifact-fleet_roster"></a>fleet_roster@founder | built | given | - | fleet.enroll | ceremony.fleet_runtime_mint |
| fleet_roster@joiner | built | given | - | fleet.enroll | ceremony.fleet_runtime_mint |
| <a id="artifact-install_seed_addresses"></a>install_seed_addresses | built | workflow | route.join_install | - | goal org_sync_pull |
| <a id="artifact-invite_event"></a>invite_event | built | workflow | ceremony.org_invite_mint | - | approval.link_publish, route.join_context |
| <a id="artifact-invite_ref_resolved"></a>invite_ref_resolved | built | workflow | route.invite_resolve | - | route.join_context |
| <a id="artifact-join_context"></a>join_context | built | workflow | route.join_context | - | ceremony.member_claim_mint |
| <a id="artifact-join_link_grant"></a>join_link_grant | built | workflow | route.link_publish | - | route.invite_resolve |
| <a id="artifact-ledger_heads"></a>ledger_heads@founder | built | given | ceremony.org_found | fold.genesis | ceremony.checkpoint_publish, ceremony.checkpoint_seed, ceremony.fleet_runtime_mint, ceremony.org_invite_mint, ceremony.organization_storage_delegate, ceremony.registration, delegate.checkpoint_publish, goal org_founded |
| ledger_heads@joiner | built | workflow | route.join_install | - | ceremony.fleet_runtime_mint, ceremony.organization_storage_delegate, route.checkpoint_adopt, goal org_sync_pull |
| <a id="artifact-link_publish_approval"></a>link_publish_approval | built | workflow | approval.link_publish | - | route.link_publish |
| <a id="artifact-member_admitted"></a>member_admitted | built | workflow | route.claim_admission, route.claim_submit_admit | - | ceremony.checkpoint_publish, delegate.checkpoint_publish, route.join_bootstrap |
| <a id="artifact-member_claim"></a>member_claim | built | workflow | ceremony.member_claim_mint | - | route.claim_submit_admit, route.claim_submit_stage |
| <a id="artifact-org_shell"></a>org_shell | built | workflow | route.org_shell_create | - | ceremony.org_found |
| <a id="artifact-persona_cert_fleet_sync"></a>persona_cert_fleet_sync@founder | built | given | ceremony.fleet_runtime_mint | ceremony.fleet_runtime_mint | module.org_reachability_publish, goal org_sync_pull |
| persona_cert_fleet_sync@joiner | built | workflow | ceremony.fleet_runtime_mint | - | module.org_reachability_publish, goal org_sync_pull |
| <a id="artifact-personal_identity"></a>personal_identity@founder | built | given | ceremony.personal_identity_create | ceremony.personal_identity_create | route.org_shell_create |
| personal_identity@joiner | built | given | ceremony.personal_identity_create | ceremony.personal_identity_create | - |
| <a id="artifact-policy_approval"></a>policy_approval | built | given | - | fold.role_define | route.claim_submit_stage |
| <a id="artifact-policy_self_admit"></a>policy_self_admit | built | given | - | fold.role_define | route.claim_submit_admit |
| <a id="artifact-reachability_row"></a>reachability_row@founder | built | workflow | module.org_reachability_publish | - | - |
| reachability_row@joiner | built | workflow | module.org_reachability_publish | - | - |
| <a id="artifact-registered_serving_key"></a>registered_serving_key@founder | built | open | **none — open question** | - | - |
| registered_serving_key@joiner | built | open | **none — open question** | - | - |
| <a id="artifact-registry_binding"></a>registry_binding@founder | built | given | ceremony.registration | ceremony.registration | ceremony.checkpoint_seed, ceremony.serve_cert_mint, goal org_founded |
| registry_binding@joiner | built | workflow | route.join_install | - | ceremony.serve_cert_mint |
| <a id="artifact-relay_slot"></a>relay_slot@founder | built | given | route.relay_connect | route.relay_connect | goal org_sync_pull |
| relay_slot@joiner | built | workflow | route.relay_connect | - | goal org_sync_pull |
| <a id="artifact-sealed_org_root"></a>sealed_org_root | built | workflow | ceremony.org_found | - | ceremony.checkpoint_seed, ceremony.registration, goal org_founded |
| <a id="artifact-serve_cert"></a>serve_cert@founder | built | given | ceremony.serve_cert_mint | ceremony.serve_cert_mint | route.link_publish, route.relay_connect, goal org_founded |
| serve_cert@joiner | built | workflow | ceremony.serve_cert_mint | - | route.relay_connect |

Artifact details:

- **admission_event** — The approver-signed member.admission event carrying the invitee's unchanged claim and the approvals. Code: `tools/dashboard/static/js/ceremony/claim.js:signAdmission` · `tools/network/ledger/claims.py:make_admission`
- **adopted_checkpoint** — The actor's adopted-checkpoint cache holds a checkpoint including the joiner (adopted_checkpoint_founder, adopted_checkpoint_joiner). Code: `tools/dashboard/membership_checkpoint.py:record_adopted`
- **bootstrap_snapshot** — The founder's ledger events, binding and profiles as of the bootstrap moment. Code: `tools/dashboard/claim_service.py:bootstrap`
- **checkpoint_delegate_grant** — A hot delegate grant whose scope includes signing advancing checkpoints (a checkpointer's storage delegate). The founder's is minted at its sign-on before any join (F0). Code: `tools/network/storagekit/delegate.py:storage_delegate_scopes` · `tools/dashboard/org_storage_delegate.py:prepare` · `tools/network/ledger/scopes.py:self_delegable_exact`
- **checkpoint_including_joiner** — A registry checkpoint seq n+1 whose members_root includes the joiner (checkpoint_seq_n). Code: `tools/dashboard/network_routes.py:post_membership_checkpoint`
- **checkpoint_seed** — Registry membership checkpoint seq 0, signed by the org root. Code: `tools/dashboard/membership_checkpoint.py:checkpoint_due`
- **claim_approval** — The founder's countersignature on a staged claim. Code: `tools/dashboard/static/js/ceremony/claim.js:signClaimApproval` · `tools/dashboard/claim_service.py:countersign`
- **claim_staged** — A claim staged pending approvals. Code: `tools/dashboard/claim_service.py:submit`
- **delegate_grant** — The actor's storage delegate event, persona-signed, parents = local heads. Code: `tools/dashboard/org_storage_delegate.py:accept`. Not read by _sync_org_peers; whether a pull needs it is unproven.
- **fleet_roster** — A personal fleet roster entry for the actor's machine; required by the runtime mint (fleet_enrollment_routes.py:_runtime_context). Code: `tools/dashboard/fleet_enrollment_routes.py:_runtime_context`. For a fresh joiner with no personal fleet, unverified (graph://cde6c8c6-041 §1 J6).
- **install_seed_addresses** — Co-member addresses in the joiner's install seed. Code: `tools/dashboard/org_install_seed.py:seed_reachability`. Produced by route.join_install from the bootstrap's reachability_rows (fixed in b01b938e; route test test_join_outcome_route.py test_install_seeds_the_sponsor_addresses_it_was_sent).
- **invite_event** — The persona-signed invite event. Code: `tools/network/ledger/events.py:_v_invite` · `tools/dashboard/static/js/ceremony/org-invite.js:mintOrgInvite`
- **invite_ref_resolved** — {org, invite_ref} resolved from the registry envelope of the link. Code: `tools/dashboard/network_routes.py:post_invite_resolve`
- **join_context** — The claim context served by the founder's connector (genesis_id, heads, granted_role, binding, sponsor). Code: `tools/dashboard/claim_service.py:context`
- **join_link_grant** — The org:join grant row, the relay link and the stored invite bearer. Code: `tools/dashboard/link_approvals.py:_execute_link_publish`
- **ledger_heads** — The actor's copy of the org ledger and its current heads (founder_heads, joiner_heads). Code: `tools/network/ledger/settings_bridge.py` · `tools/network/ledger/ledger.py:heads`
- **link_publish_approval** — The approved link_publish decision with its Gate 2 org-scoped sign-on. Code: `tools/dashboard/link_approvals.py:_org_join_request`
- **member_admitted** — The claim appended to the founder's ledger. Code: `tools/dashboard/claim_service.py:submit`
- **member_claim** — The persona-signed member.claim event. Code: `tools/dashboard/static/js/ceremony/claim.js:mintMemberClaim` · `tools/network/ledger/events.py:_v_member_claim`
- **org_shell** — The founder's local organization shell (slug, identity), not yet founded. Code: `tools/dashboard/server.py:api_orgs_create`
- **persona_cert_fleet_sync** — The org sync certificate (persona -> serving machine key, scope fleet:sync) with its serving seed, installed as an OrgFleetAuthenticator. Code: `tools/dashboard/fleet_enrollment_routes.py:_activate_runtime` · `tools/network/fleet_org_channel.py:OrgFleetAuthenticator`
- **personal_identity** — The actor's personal identity - the armored personal root and its root_pub. Code: `tools/dashboard/identity_routes.py:post_personal`
- **policy_approval** — The invite's role requires approvals before admission. Code: `tools/dashboard/claim_service.py:R_APPROVAL_MISSING`
- **policy_self_admit** — The invite's role admits a claim without approvals. Code: `tools/dashboard/claim_service.py:R_APPROVAL_MISSING`
- **reachability_row** — The actor machine's signed reachability row in the org scope. Code: `tools/network/fleet_sync_scheduler.py:_publish_org_reachability`
- **registered_serving_key** — The actor's serving machine key in the registry's per-org serve_machine_keys allow-set. Code: `tools/network/registry/backfill_serving_keys.py`. Enforced only when the set is non-empty (relay.py:973-1000, store.py:1576-1583); the only writer in the tree is this backfill. Whether any live org's set is non-empty is an open question; the goal does not require it.
- **registry_binding** — The org's registry binding {org_uuid, root key, registry_url}. Code: `tools/graph/schemas/network_identity.py:NETWORK_BINDING_SET_ID`
- **relay_slot** — A live serving slot of the actor's connector at the org's relay. Code: `tools/dashboard/org_sync_channels.py:relay_slots_provider`
- **sealed_org_root** — The org root sealed to the founder's personal-root KEM key; any founder personal-root window unseals it (openSealedArmor). The row does not replicate, so a joiner never holds it. Code: `tools/dashboard/network_routes.py:post_sealed_org_key`
- **serve_cert** — The actor's persona-signed serving certificate and key. Code: `tools/dashboard/network_routes.py:_post_serve_cert_v3`

## Workflow mutations

| Mutation | Status | Actors | Opens | Requires | Produces |
|---|---|---|---|---|---|
| approval.link_publish | built | founder | root | personal_root_seed AND invite_event | link_publish_approval |
| ceremony.admission_event | built | founder | persona | persona_signing_key AND claim_staged | claim_approval, admission_event |
| ceremony.checkpoint_publish | built | founder | persona | persona_signing_key AND member_admitted AND ledger_heads AND checkpoint_seed | checkpoint_including_joiner, adopted_checkpoint |
| ceremony.checkpoint_seed | built | founder | root | sealed_org_root AND registry_binding AND ledger_heads | checkpoint_seed |
| ceremony.claim_approval | built | founder | persona | persona_signing_key AND claim_staged | claim_approval |
| ceremony.fleet_runtime_mint | built | founder, joiner | root | personal_root_seed AND fleet_roster AND ledger_heads | persona_cert_fleet_sync |
| ceremony.member_claim_mint | built | joiner | persona | persona_signing_key AND join_context | member_claim |
| ceremony.org_found | built | founder | root | org_shell | sealed_org_root, ledger_heads, checkpoint_delegate_grant |
| ceremony.org_invite_mint | built | founder | persona | persona_signing_key AND ledger_heads | invite_event |
| ceremony.organization_storage_delegate | built | founder, joiner | persona | persona_signing_key AND ledger_heads | delegate_grant, checkpoint_delegate_grant |
| ceremony.personal_identity_create | built | founder, joiner | root | nothing | personal_identity |
| ceremony.registration | built | founder | root | sealed_org_root AND ledger_heads | registry_binding |
| ceremony.serve_cert_mint | built | founder, joiner | persona | persona_signing_key AND registry_binding AND checkpoint_seed | serve_cert |
| delegate.checkpoint_publish | built | founder | delegate | agent_delegate_signing_key AND checkpoint_delegate_grant AND member_admitted AND ledger_heads AND checkpoint_seed | checkpoint_including_joiner, adopted_checkpoint |
| module.org_reachability_publish | built | founder, joiner | none | persona_cert_fleet_sync | reachability_row |
| route.checkpoint_adopt | built | joiner | none | checkpoint_including_joiner AND ledger_heads | adopted_checkpoint |
| route.claim_admission | built | founder | none | admission_event | member_admitted |
| route.claim_submit_admit | built | founder | none | member_claim AND policy_self_admit | member_admitted |
| route.claim_submit_stage | built | founder | none | (member_claim AND policy_approval) | claim_staged |
| route.invite_resolve | built | joiner | none | join_link_grant | invite_ref_resolved |
| route.join_bootstrap | built | founder | none | member_admitted | bootstrap_snapshot |
| route.join_context | built | founder | none | invite_ref_resolved AND invite_event | join_context |
| route.join_install | built | joiner | none | bootstrap_snapshot AND checkpoint_including_joiner | ledger_heads, registry_binding, install_seed_addresses, adopted_checkpoint |
| route.link_publish | built | founder | delegate | agent_delegate_signing_key AND link_publish_approval AND serve_cert | join_link_grant |
| route.org_shell_create | built | founder | none | personal_identity | org_shell |
| route.relay_connect | built | founder, joiner | none | serve_cert | relay_slot |

## Goal org_founded

The founder has founded an organization and it is live on the network: the founder holds the org ledger and its sealed org root, its storage delegate carries the checkpoint scope, the registry binds the org, the registry holds the seq-0 membership checkpoint, and the founder holds a persona serve certificate. Everything org_sync_pull's starting states assume as "F0 done" that founding itself must produce.

**Requires:** ledger_heads@founder AND sealed_org_root AND checkpoint_delegate_grant@founder AND registry_binding@founder AND checkpoint_seed AND serve_cert@founder

Built order (bead auto-2vseu): founding (founding.js:foundExistingOrganizationShell) keeps the in-memory org root and the held personal seed past the founding batch, signs the registration with the org root (founding.js:registerFoundedOrganization), then runs the org-scoped sign-on phases with the seed (founding.js:finishFoundedOrganization → signon-phases.js), which mint the checkpoint seed and the serve certificate; both keys are zeroed afterwards. A refused registration is reported on the success screen with a "Register now" action that repeats the same steps from a fresh opening (founding.js:registerFoundedOrganizationLater). The compose simulation asserts the registry holds the founding checkpoint before any invite step.

### Starting states

- **fresh** — No identity yet (first run). Holds: 
- **identity_held** — The founder has a personal identity and creates an organization from the dashboard. Holds: personal_identity@founder

### Recorded current order

| Step | Actor | Root opening | Runs | Only from |
|---|---|---|---|---|
| I1 | founder | yes | ceremony.personal_identity_create | fresh |
| F0.shell | founder | no | route.org_shell_create | all |
| F0 | founder | yes | ceremony.org_found, ceremony.registration, ceremony.checkpoint_seed, ceremony.serve_cert_mint | all |

### Scenario from fresh, built rules

Recorded: current {'founder': 2, 'joiner': 0}, minimal {'founder': 1, 'joiner': 0}.

```text
goal org_founded from fresh (built rules)
root openings: founder 1, joiner 0 (total 1); steps 6

 1. ceremony.personal_identity_create[founder]       window founder#1     -> personal_identity@founder
 2. route.org_shell_create[founder]                  machine (none)       -> org_shell
 3. ceremony.org_found[founder]                      window founder#1     -> checkpoint_delegate_grant@founder, ledger_heads@founder, sealed_org_root
 4. ceremony.registration[founder]                   window founder#1     -> registry_binding@founder
 5. ceremony.checkpoint_seed[founder]                window founder#1     -> checkpoint_seed
 6. ceremony.serve_cert_mint[founder]                window founder#1     -> serve_cert@founder

goal org_founded from fresh: current order reaches the goal
current root openings: founder 2, joiner 0
minimal root openings: founder 1, joiner 0 (built rules)


needed I1         founder  needed: first opening of founder
EXTRA  F0         founder  mergeable into I1: I1 closed without: checkpoint_seed (from ceremony.checkpoint_seed[founder]), ledger_heads@founder (from ceremony.org_found[founder]), org_shell (from route.org_shell_create[founder]), registry_binding@founder (from ceremony.registration[founder]), sealed_org_root (from ceremony.org_found[founder])
```

### Scenario from identity_held, built rules

Recorded: current {'founder': 1, 'joiner': 0}, minimal {'founder': 1, 'joiner': 0}.

```text
goal org_founded from identity_held (built rules)
root openings: founder 1, joiner 0 (total 1); steps 5

 1. route.org_shell_create[founder]                  machine (none)       -> org_shell
 2. ceremony.org_found[founder]                      window founder#1     -> checkpoint_delegate_grant@founder, ledger_heads@founder, sealed_org_root
 3. ceremony.registration[founder]                   window founder#1     -> registry_binding@founder
 4. ceremony.checkpoint_seed[founder]                window founder#1     -> checkpoint_seed
 5. ceremony.serve_cert_mint[founder]                window founder#1     -> serve_cert@founder

goal org_founded from identity_held: current order reaches the goal
current root openings: founder 1, joiner 0
minimal root openings: founder 1, joiner 0 (built rules)


needed F0         founder  needed: first opening of founder
```

## Goal org_sync_pull

Founder and joiner can each pull the other over org sync: both hold an org channel (sync certificate + serving seed), both have adopted a checkpoint whose members_root includes the joiner (the verifier admits a pull only under an adopted seq with inclusion, fleet_org_channel.py: 286-309), the joiner holds the org ledger, and the joiner has a route to the founder (the founder reaches the joiner through its dial-in).

**Requires:** persona_cert_fleet_sync@founder AND persona_cert_fleet_sync@joiner AND adopted_checkpoint@founder AND adopted_checkpoint@joiner AND ledger_heads@joiner AND (install_seed_addresses OR (relay_slot@joiner AND relay_slot@founder))

Peer selection sources (fleet_sync_scheduler.py:_org_peer_candidates): reachability rows (need a prior pull), install seed, relay slots through the dialer's own connector, dialed-in peers. The founder-pulls-joiner direction riding the joiner's dial-in is taken from the record, not re-traced. The single-joiner AND/OR graph cannot express head presence at adoption; OrgAdmission.tla carries that. Final rules (auto-qrmlg.12): the org hello accepts E-any-adm (a proof under an older authentic record when the prover is in the verifier's newest member set and the record is at or after its current admission) and prover-downgrade (a side proves back under the seq the other side adopted, from the rider's checkpoint_seq); both change which pulls are admitted, not which artifacts exist, so they leave this requirement set and the counts unchanged. Liveness and safety under the final rules: OrgAdmissionFinalApproval.cfg, OrgAdmissionFinalSelfAdmit.cfg.

### Starting states

- **self_admit** — Founded org (F0 done), invite role self-admits. F0 includes the founder's sign-on on checkpoint-at-admission code, which gives its storage delegate the checkpoint scope; an org founded earlier holds that only after its founder's next sign-on, until which J3.submit publishes nothing and the founder's next sign-on does (founder 3). Holds: checkpoint_delegate_grant@founder, ledger_heads@founder, registry_binding@founder, checkpoint_seed, serve_cert@founder, relay_slot@founder, persona_cert_fleet_sync@founder, fleet_roster@founder, fleet_roster@joiner, policy_self_admit
- **approval** — Founded org (F0 done), invite role requires the founder's approval. As for self_admit, F0 includes a sign-on on checkpoint-at-admission code; without it F3.admit publishes the persona form in the approver's window instead (same count). Holds: checkpoint_delegate_grant@founder, ledger_heads@founder, registry_binding@founder, checkpoint_seed, serve_cert@founder, relay_slot@founder, persona_cert_fleet_sync@founder, fleet_roster@founder, fleet_roster@joiner, policy_approval

### Recorded current order

| Step | Actor | Root opening | Runs | Only from |
|---|---|---|---|---|
| F1 | founder | yes | ceremony.org_invite_mint | all |
| F2 | founder | yes | approval.link_publish, route.link_publish | all |
| J1 | joiner | no | route.invite_resolve | all |
| J2 | founder | no | route.join_context | all |
| J3 | joiner | yes | ceremony.member_claim_mint | all |
| J3.submit | founder | no | route.claim_submit_admit, delegate.checkpoint_publish | self_admit |
| J3.stage | founder | no | route.claim_submit_stage | approval |
| F3 | founder | yes | ceremony.claim_approval, ceremony.admission_event | approval |
| F3.admit | founder | no | route.claim_admission, delegate.checkpoint_publish | approval |
| J5.serve | founder | no | route.join_bootstrap | all |
| J5 | joiner | yes | route.join_install, ceremony.organization_storage_delegate, ceremony.serve_cert_mint, ceremony.fleet_runtime_mint | self_admit |
| J5 | joiner | yes | route.join_install, ceremony.organization_storage_delegate, ceremony.serve_cert_mint, ceremony.fleet_runtime_mint | approval |
| J5.connect | joiner | no | route.relay_connect | all |

### Scenario from self_admit, built rules

Recorded: current {'founder': 2, 'joiner': 1}, minimal {'founder': 1, 'joiner': 1}.

```text
goal org_sync_pull from self_admit (built rules)
root openings: founder 1, joiner 1 (total 2); steps 11

 1. ceremony.org_invite_mint[founder]                window founder#1     -> invite_event
 2. approval.link_publish[founder]                   window founder#1     -> link_publish_approval
 3. route.link_publish[founder]                      machine (delegate)   -> join_link_grant
 4. route.invite_resolve[joiner]                     machine (none)       -> invite_ref_resolved
 5. route.join_context[founder]                      machine (none)       -> join_context
 6. ceremony.member_claim_mint[joiner]               window joiner#1      -> member_claim
 7. route.claim_submit_admit[founder]                machine (none)       -> member_admitted
 8. delegate.checkpoint_publish[founder]             machine (delegate)   -> adopted_checkpoint@founder, checkpoint_including_joiner
 9. route.join_bootstrap[founder]                    machine (none)       -> bootstrap_snapshot
10. route.join_install[joiner]                       machine (none)       -> adopted_checkpoint@joiner, install_seed_addresses, ledger_heads@joiner, registry_binding@joiner
11. ceremony.fleet_runtime_mint[joiner]              window joiner#1      -> persona_cert_fleet_sync@joiner

goal org_sync_pull from self_admit: current order reaches the goal
current root openings: founder 2, joiner 1
minimal root openings: founder 1, joiner 1 (built rules)


needed F1         founder  needed: first opening of founder
EXTRA  F2         founder  mergeable into F1: F1 closed without: link_publish_approval (from approval.link_publish[founder])
needed J3         joiner   needed: first opening of joiner
```

### Scenario from approval, built rules

Recorded: current {'founder': 3, 'joiner': 2}, minimal {'founder': 2, 'joiner': 2}.

```text
goal org_sync_pull from approval (built rules)
root openings: founder 2, joiner 2 (total 4); steps 13

 1. ceremony.org_invite_mint[founder]                window founder#1     -> invite_event
 2. approval.link_publish[founder]                   window founder#1     -> link_publish_approval
 3. route.link_publish[founder]                      machine (delegate)   -> join_link_grant
 4. route.invite_resolve[joiner]                     machine (none)       -> invite_ref_resolved
 5. route.join_context[founder]                      machine (none)       -> join_context
 6. ceremony.member_claim_mint[joiner]               window joiner#1      -> member_claim
 7. route.claim_submit_stage[founder]                machine (none)       -> claim_staged
 8. ceremony.admission_event[founder]                window founder#2     -> admission_event, claim_approval
 9. route.claim_admission[founder]                   machine (none)       -> member_admitted
10. ceremony.checkpoint_publish[founder]             window founder#2     -> adopted_checkpoint@founder, checkpoint_including_joiner
11. route.join_bootstrap[founder]                    machine (none)       -> bootstrap_snapshot
12. route.join_install[joiner]                       machine (none)       -> adopted_checkpoint@joiner, install_seed_addresses, ledger_heads@joiner, registry_binding@joiner
13. ceremony.fleet_runtime_mint[joiner]              window joiner#2      -> persona_cert_fleet_sync@joiner

goal org_sync_pull from approval: current order reaches the goal
current root openings: founder 3, joiner 2
minimal root openings: founder 2, joiner 2 (built rules)


needed F1         founder  needed: first opening of founder
EXTRA  F2         founder  mergeable into F1: F1 closed without: link_publish_approval (from approval.link_publish[founder])
needed J3         joiner   needed: first opening of joiner
needed F3         founder  needed: not obtainable in F2's window; it lacked: claim_staged
needed J5         joiner   needed: not obtainable in J3's window; it lacked: bootstrap_snapshot, checkpoint_including_joiner, ledger_heads@joiner, registry_binding@joiner
```
- proof: tla OrgAdmission: EveryAdmittedMemberPulls
- proof: tla OrgAdmission: NoPullWithoutInclusion
- proof: tla OrgAdmission: ApprovedIsAdmitted
- proof: tla OrgAdmission: RemovedExcluded
- proof: tla OrgAdmissionLeaves: RekeyedOldKeyExcluded
- proof: tla OrgAdmissionLeaves: ReAdmitAfterRemoval
