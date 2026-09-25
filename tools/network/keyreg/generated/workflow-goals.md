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

| Artifact | Per actor | Status | Store | Home | Produced by | Required by |
|---|---|---|---|---|---|---|
| <a id="artifact-adopted_checkpoint"></a>adopted_checkpoint | yes | built | NETWORK_CHECKPOINT_CACHE_SET_ID row | the actor's machine | ceremony.checkpoint_publish, delegate.checkpoint_publish, route.checkpoint_adopt | goal org_sync_pull |
| <a id="artifact-bootstrap_snapshot"></a>bootstrap_snapshot | no | built | joiner browser memory | joiner machine | route.join_bootstrap | route.join_install |
| <a id="artifact-checkpoint_delegate_grant"></a>checkpoint_delegate_grant | no | designed | founder's org ledger and audited vault | founder machine | ceremony.checkpoint_delegate_grant | delegate.checkpoint_publish |
| <a id="artifact-checkpoint_including_joiner"></a>checkpoint_including_joiner | no | built | registry /v1/orgs/{uuid}/membership-checkpoints | registry | ceremony.checkpoint_publish, delegate.checkpoint_publish | route.checkpoint_adopt |
| <a id="artifact-checkpoint_seed"></a>checkpoint_seed | no | built | registry /v1/orgs/{uuid}/membership-checkpoints | registry | **none** | ceremony.checkpoint_publish, ceremony.serve_cert_mint, delegate.checkpoint_publish |
| <a id="artifact-claim_approval"></a>claim_approval | no | built | founder's pending-claim store | founder machine | ceremony.claim_approval, route.admit_on_approval | ceremony.claim_finalize |
| <a id="artifact-claim_staged"></a>claim_staged | no | built | founder's pending-claim store | founder machine | route.claim_submit_stage | ceremony.claim_approval, route.admit_on_approval |
| <a id="artifact-delegate_grant"></a>delegate_grant | yes | built | the actor's org ledger and audited vault | the actor's machine | ceremony.organization_storage_delegate | - |
| <a id="artifact-fleet_roster"></a>fleet_roster | yes | built | personal fleet roster | the actor's machine | **none** | ceremony.fleet_runtime_mint |
| <a id="artifact-install_seed_addresses"></a>install_seed_addresses | no | built | joiner install seed | joiner machine | **none** | goal org_sync_pull |
| <a id="artifact-invite_event"></a>invite_event | no | built | founder's org ledger | founder machine | ceremony.org_invite_mint | approval.link_publish, route.join_context |
| <a id="artifact-invite_ref_resolved"></a>invite_ref_resolved | no | built | joiner browser memory | joiner machine | route.invite_resolve | route.join_context |
| <a id="artifact-join_context"></a>join_context | no | built | joiner browser memory | joiner machine | route.join_context | ceremony.member_claim_mint |
| <a id="artifact-join_link_grant"></a>join_link_grant | no | built | founder org Settings; relay | founder machine and relay | route.link_publish | route.invite_resolve |
| <a id="artifact-ledger_heads"></a>ledger_heads | yes | built | autonomy.org.ledger-event#1 rows in the org DB | the actor's machine | route.join_install | ceremony.checkpoint_delegate_grant, ceremony.checkpoint_publish, ceremony.fleet_runtime_mint, ceremony.org_invite_mint, ceremony.organization_storage_delegate, delegate.checkpoint_publish, route.checkpoint_adopt, goal org_sync_pull |
| <a id="artifact-link_publish_approval"></a>link_publish_approval | no | built | approvals table | founder machine | approval.link_publish | route.link_publish |
| <a id="artifact-member_admitted"></a>member_admitted | no | built | founder's org ledger | founder machine | route.admit_on_approval, route.claim_submit_admit | ceremony.checkpoint_publish, delegate.checkpoint_publish, route.join_bootstrap |
| <a id="artifact-member_claim"></a>member_claim | no | built | joiner browser memory until submitted | joiner machine | ceremony.member_claim_mint | route.claim_submit_admit, route.claim_submit_stage |
| <a id="artifact-member_claim_final"></a>member_claim_final | no | built | joiner browser memory until submitted | joiner machine | ceremony.claim_finalize | route.claim_submit_admit |
| <a id="artifact-persona_cert_fleet_sync"></a>persona_cert_fleet_sync | yes | built | memory and the machine vault | the actor's machine | ceremony.fleet_runtime_mint | module.org_reachability_publish, goal org_sync_pull |
| <a id="artifact-policy_approval"></a>policy_approval | no | built | founder's org ledger (role definition) | founder machine | **none** | route.claim_submit_stage |
| <a id="artifact-policy_self_admit"></a>policy_self_admit | no | built | founder's org ledger (role definition) | founder machine | **none** | route.claim_submit_admit |
| <a id="artifact-reachability_row"></a>reachability_row | yes | built | org scope Settings, replicated by org sync | the actor's machine; co-members after a pull | module.org_reachability_publish | - |
| <a id="artifact-registered_serving_key"></a>registered_serving_key | yes | built | registry store | registry | **none** | - |
| <a id="artifact-registry_binding"></a>registry_binding | yes | built | NETWORK_BINDING_SET_ID Setting | the actor's machine | route.join_install | ceremony.serve_cert_mint |
| <a id="artifact-relay_slot"></a>relay_slot | yes | built | relay memory | relay | route.relay_connect | goal org_sync_pull |
| <a id="artifact-serve_cert"></a>serve_cert | yes | built | autonomy.machine.serve-cert row; serving key in autonomy.machine.vault.audited | the actor's machine | ceremony.serve_cert_mint | route.link_publish, route.relay_connect |

Artifact details:

- **adopted_checkpoint** — The actor's adopted-checkpoint cache holds a checkpoint including the joiner (adopted_checkpoint_founder, adopted_checkpoint_joiner). Code: `tools/dashboard/membership_checkpoint.py:record_adopted`
- **bootstrap_snapshot** — The founder's ledger events, binding and profiles as of the bootstrap moment. Code: `tools/dashboard/claim_service.py:bootstrap`
- **checkpoint_delegate_grant** — A hot delegate grant whose scope includes signing advancing checkpoints. Code: `tools/network/storagekit/delegate.py:storage_delegate_scopes`
- **checkpoint_including_joiner** — A registry checkpoint seq n+1 whose members_root includes the joiner (checkpoint_seq_n). Code: `tools/dashboard/network_routes.py:post_membership_checkpoint`
- **checkpoint_seed** — Registry membership checkpoint seq 0, signed by the org root. Code: `tools/dashboard/membership_checkpoint.py:checkpoint_due`
- **claim_approval** — The founder's countersignature on a staged claim. Code: `tools/dashboard/static/js/ceremony/claim.js:signClaimApproval` · `tools/dashboard/claim_service.py:countersign`
- **claim_staged** — A claim staged pending approvals. Code: `tools/dashboard/claim_service.py:submit`
- **delegate_grant** — The actor's storage delegate event, persona-signed, parents = local heads. Code: `tools/dashboard/org_storage_delegate.py:accept`. Not read by _sync_org_peers; whether a pull needs it is unproven.
- **fleet_roster** — A personal fleet roster entry for the actor's machine; required by the runtime mint (fleet_enrollment_routes.py:_runtime_context). Code: `tools/dashboard/fleet_enrollment_routes.py:_runtime_context`. For a fresh joiner with no personal fleet, unverified (graph://cde6c8c6-041 §1 J6).
- **install_seed_addresses** — Co-member addresses in the joiner's install seed. Code: `tools/dashboard/org_install_seed.py:seed_reachability`. No built producer; the join page does not forward reachability_rows (network-join.js:313-327).
- **invite_event** — The persona-signed invite event. Code: `tools/network/ledger/events.py:_v_invite` · `tools/dashboard/static/js/ceremony/org-invite.js:mintOrgInvite`
- **invite_ref_resolved** — {org, invite_ref} resolved from the registry envelope of the link. Code: `tools/dashboard/network_routes.py:post_invite_resolve`
- **join_context** — The claim context served by the founder's connector (genesis_id, heads, granted_role, binding, sponsor). Code: `tools/dashboard/claim_service.py:context`
- **join_link_grant** — The org:join grant row, the relay link and the stored invite bearer. Code: `tools/dashboard/link_approvals.py:_execute_link_publish`
- **ledger_heads** — The actor's copy of the org ledger and its current heads (founder_heads, joiner_heads). Code: `tools/network/ledger/settings_bridge.py` · `tools/network/ledger/ledger.py:heads`
- **link_publish_approval** — The approved link_publish decision with its Gate 2 org-scoped sign-on. Code: `tools/dashboard/link_approvals.py:_org_join_request`
- **member_admitted** — The claim appended to the founder's ledger. Code: `tools/dashboard/claim_service.py:submit`
- **member_claim** — The persona-signed member.claim event. Code: `tools/dashboard/static/js/ceremony/claim.js:mintMemberClaim` · `tools/network/ledger/events.py:_v_member_claim`
- **member_claim_final** — The claim re-minted at the pinned position carrying the approvals. Code: `tools/dashboard/static/js/join/accept-controller.js:finalize`
- **persona_cert_fleet_sync** — The org sync certificate (persona -> serving machine key, scope fleet:sync) with its serving seed, installed as an OrgFleetAuthenticator. Code: `tools/dashboard/fleet_enrollment_routes.py:_activate_runtime` · `tools/network/fleet_org_channel.py:OrgFleetAuthenticator`
- **policy_approval** — The invite's role requires approvals before admission. Code: `tools/dashboard/claim_service.py:R_APPROVAL_MISSING`
- **policy_self_admit** — The invite's role admits a claim without approvals. Code: `tools/dashboard/claim_service.py:R_APPROVAL_MISSING`
- **reachability_row** — The actor machine's signed reachability row in the org scope. Code: `tools/network/fleet_sync_scheduler.py:_publish_org_reachability`
- **registered_serving_key** — The actor's serving machine key in the registry's per-org serve_machine_keys allow-set. Code: `tools/network/registry/backfill_serving_keys.py`. Enforced only when the set is non-empty (relay.py:973-1000, store.py:1576-1583); the only writer in the tree is this backfill. Whether any live org's set is non-empty is an open question; the goal does not require it.
- **registry_binding** — The org's registry binding {org_uuid, root key, registry_url}. Code: `tools/graph/schemas/network_identity.py:NETWORK_BINDING_SET_ID`
- **relay_slot** — A live serving slot of the actor's connector at the org's relay. Code: `tools/dashboard/org_sync_channels.py:relay_slots_provider`
- **serve_cert** — The actor's persona-signed serving certificate and key. Code: `tools/dashboard/network_routes.py:_post_serve_cert_v3`

## Workflow mutations

| Mutation | Status | Actors | Opens | Requires | Produces |
|---|---|---|---|---|---|
| approval.link_publish | built | founder | root | personal_root_seed AND invite_event | link_publish_approval |
| ceremony.checkpoint_delegate_grant | designed (rule delegate_checkpoint) | founder | persona | persona_signing_key AND ledger_heads | checkpoint_delegate_grant |
| ceremony.checkpoint_publish | built | founder | persona | persona_signing_key AND member_admitted AND ledger_heads AND checkpoint_seed | checkpoint_including_joiner, adopted_checkpoint |
| ceremony.claim_approval | built | founder | persona | persona_signing_key AND claim_staged | claim_approval |
| ceremony.claim_finalize | built | joiner | persona | persona_signing_key AND claim_approval | member_claim_final |
| ceremony.fleet_runtime_mint | built | founder, joiner | root | personal_root_seed AND fleet_roster AND ledger_heads | persona_cert_fleet_sync |
| ceremony.member_claim_mint | built | joiner | persona | persona_signing_key AND join_context | member_claim |
| ceremony.org_invite_mint | built | founder | persona | persona_signing_key AND ledger_heads | invite_event |
| ceremony.organization_storage_delegate | built | founder, joiner | persona | persona_signing_key AND ledger_heads | delegate_grant |
| ceremony.serve_cert_mint | built | founder, joiner | persona | persona_signing_key AND registry_binding AND checkpoint_seed | serve_cert |
| delegate.checkpoint_publish | designed (rule delegate_checkpoint) | founder | delegate | agent_delegate_signing_key AND checkpoint_delegate_grant AND member_admitted AND ledger_heads AND checkpoint_seed | checkpoint_including_joiner, adopted_checkpoint |
| module.org_reachability_publish | built | founder, joiner | none | persona_cert_fleet_sync | reachability_row |
| route.admit_on_approval | designed (rule admit_on_approval) | founder | persona | persona_signing_key AND claim_staged | claim_approval, member_admitted |
| route.checkpoint_adopt | built | joiner | none | checkpoint_including_joiner AND ledger_heads | adopted_checkpoint |
| route.claim_submit_admit | built | founder | none | ((member_claim AND policy_self_admit) OR member_claim_final) | member_admitted |
| route.claim_submit_stage | built | founder | none | (member_claim AND policy_approval) | claim_staged |
| route.invite_resolve | built | joiner | none | join_link_grant | invite_ref_resolved |
| route.join_bootstrap | built | founder | none | member_admitted | bootstrap_snapshot |
| route.join_context | built | founder | none | invite_ref_resolved AND invite_event | join_context |
| route.join_install | built | joiner | none | bootstrap_snapshot | ledger_heads, registry_binding |
| route.link_publish | built | founder | delegate | agent_delegate_signing_key AND link_publish_approval AND serve_cert | join_link_grant |
| route.relay_connect | built | founder, joiner | none | serve_cert | relay_slot |

## Goal org_sync_pull

Founder and joiner can each pull the other over org sync: both hold an org channel (sync certificate + serving seed), both have adopted a checkpoint whose members_root includes the joiner (the verifier admits a pull only under an adopted seq with inclusion, fleet_org_channel.py: 286-309), the joiner holds the org ledger, and the joiner has a route to the founder (the founder reaches the joiner through its dial-in).

**Requires:** persona_cert_fleet_sync@founder AND persona_cert_fleet_sync@joiner AND adopted_checkpoint@founder AND adopted_checkpoint@joiner AND ledger_heads@joiner AND (install_seed_addresses OR (relay_slot@joiner AND relay_slot@founder))

Peer selection sources (fleet_sync_scheduler.py:_org_peer_candidates): reachability rows (need a prior pull), install seed, relay slots through the dialer's own connector, dialed-in peers. The founder-pulls-joiner direction riding the joiner's dial-in is taken from the record, not re-traced. The single-joiner AND/OR graph cannot express head presence at adoption; OrgAdmission.tla carries that.

### Starting states

- **self_admit** — Founded org (F0 done), invite role self-admits. Holds: ledger_heads@founder, registry_binding@founder, checkpoint_seed, serve_cert@founder, relay_slot@founder, persona_cert_fleet_sync@founder, fleet_roster@founder, fleet_roster@joiner, policy_self_admit
- **approval** — Founded org (F0 done), invite role requires the founder's approval. Holds: ledger_heads@founder, registry_binding@founder, checkpoint_seed, serve_cert@founder, relay_slot@founder, persona_cert_fleet_sync@founder, fleet_roster@founder, fleet_roster@joiner, policy_approval

### Recorded current order

| Step | Actor | Root opening | Runs | Only from |
|---|---|---|---|---|
| F1 | founder | yes | ceremony.org_invite_mint | all |
| F2 | founder | yes | approval.link_publish, route.link_publish | all |
| J1 | joiner | no | route.invite_resolve | all |
| J2 | founder | no | route.join_context | all |
| J3 | joiner | yes | ceremony.member_claim_mint | all |
| J3.submit | founder | no | route.claim_submit_admit | self_admit |
| J3.stage | founder | no | route.claim_submit_stage | approval |
| F3 | founder | yes | ceremony.claim_approval | approval |
| J4 | joiner | yes | ceremony.claim_finalize | approval |
| J4.submit | founder | no | route.claim_submit_admit | approval |
| J5.serve | founder | no | route.join_bootstrap | all |
| J5 | joiner | no | route.join_install, route.checkpoint_adopt | all |
| J6 | joiner | yes | route.checkpoint_adopt, ceremony.organization_storage_delegate, ceremony.serve_cert_mint, ceremony.fleet_runtime_mint | all |
| J6.connect | joiner | no | route.relay_connect | all |
| F4 | founder | yes | ceremony.checkpoint_publish | all |
| J6.repeat | joiner | yes | route.checkpoint_adopt, ceremony.organization_storage_delegate, ceremony.serve_cert_mint, ceremony.fleet_runtime_mint | all |

### Known defects (named by lint.py, not failed)

- step J5 runs route.checkpoint_adopt without checkpoint_including_joiner — graph://cde6c8c6-041 §1 J5 (network_routes.py:945)
- step J6 runs route.checkpoint_adopt without checkpoint_including_joiner — graph://cde6c8c6-041 §3 (J6 repeat)
- artifact install_seed_addresses has no built producer — graph://cde6c8c6-041 §5 J5 reachability seed (network-join.js:313-327)
