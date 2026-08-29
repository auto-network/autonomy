# Proof coverage — generated from registry.yaml by gen.py; do not edit.

One row per key and per mutation: the machine-checked lemmas covering
it, or GAP. Gap rows feed the formal-modeling queue on tracker note
8277c76c-ad1.

| entry | kind | proofs |
|---|---|---|
| agent_delegate_signing_key | key | VaultDelegateChain: write_resolves_to_current_member<br>VaultDelegateChain: delegate_scopes_only<br>VaultDelegateChain: write_requires_live_delegate |
| class_key | key | VaultPolicyClass: revoked_factor_excluded_forward<br>VaultPolicyClass: revoked_factor_keeps_old |
| factor_recipient | key | **GAP** |
| factor_seed | key | VaultFactorPolicy: password_alone_no_root<br>VaultFactorPolicy: passkeys_both_no_root<br>VaultFactorPolicy: unlock_only_no_root |
| generation_secret | key | VaultRekeyMarker: exclusion_forward<br>VaultConcurrentRekey: concurrent_grant_secret |
| k_index_k_meta | key | **GAP** |
| master_kek | key | **GAP** |
| member_recovery_key | key | VaultRecoveryRace: recovery_key_secret<br>VaultRecoveryRace: recovery_beats_thief<br>RecoveryUnlink: Observational_equivalence |
| object_cek | key | **GAP** |
| object_wrap_key | key | **GAP** |
| org_root_signing_key | key | **GAP** |
| pepper | key | VaultDeniability: Observational_equivalence |
| per_credential_wrapping_key | key | VaultOpenStore: no_key_to_forged_pk |
| per_machine_key | key | VaultFleetDist: machine_key_yields_nothing_undistributed<br>VaultFleetDist: kicked_machine_excluded |
| persona_kem_private | key | VaultFleetDist: persona_kem_snapshot_total<br>VaultRekeyMarker: exclusion_forward<br>VaultRekeyMarkerChain: exclusion_through_chain<br>VaultD006Window: post_rekey_excluded |
| persona_signing_key | key | **GAP** |
| personal_root_seed | key | VaultOpenStore: root_secret_open_store<br>VaultRekeyMarker: root_secret<br>VaultFactorPolicy: password_and_passkey1_release_root<br>VaultRootRotation: owner_rotates_away_from_stolen_root |
| recovery_code | key | **GAP** |
| recovery_signing_key | key | VaultRootRotation: thief_cannot_rotate<br>VaultRootRotation: code_finder_cannot_rotate |
| recovery_slot_recipient | key | **GAP** |
| root_anchor_seed | key | VaultPolicyClass: root_reaches_every_generation |
| sealed_index | key | VaultDeniability: Observational_equivalence |
| serving_delegate_key | key | **GAP** |
| tier2_session_key | key | **GAP** |
| vault_factor_recipient | key | **GAP** |
| armor.enroll_factor | mutation | **GAP** |
| armor.replace_recovery_slot | mutation | **GAP** |
| armor.revoke_factor | mutation | **GAP** |
| armor.set_recovery | mutation | **GAP** |
| ceremony.recovery_policy_change | mutation | **GAP** |
| ceremony.registration | mutation | **GAP** |
| ceremony.serve_cert_mint | mutation | **GAP** |
| ceremony.vault_master_read | mutation | **GAP** |
| fleet.distribute_kem | mutation | VaultFleetDist: machine_key_yields_nothing_undistributed |
| fleet.enroll | mutation | **GAP** |
| fleet.kick | mutation | VaultFleetDist: kicked_machine_excluded<br>VaultD006Window: halt_window_confidential |
| fold.checkpoint | mutation | **GAP** |
| fold.delegate | mutation | VaultDelegateChain: delegate_scopes_only |
| fold.genesis | mutation | **GAP** |
| fold.invite | mutation | **GAP** |
| fold.key_epoch | mutation | **GAP** |
| fold.key_rotate | mutation | **GAP** |
| fold.member_claim | mutation | **GAP** |
| fold.member_rekey | mutation | VaultRecoveryRace: recovery_beats_thief<br>VaultRecoveryRace: recovery_key_secret |
| fold.revoke | mutation | VaultDelegateChain: write_requires_live_delegate |
| fold.role_define | mutation | **GAP** |
| fold.role_grant | mutation | **GAP** |
| fold.role_revoke | mutation | **GAP** |
| passkey.enroll | mutation | VaultOpenStore: no_key_to_forged_pk |
| passkey.revoke | mutation | **GAP** |
| recovery.succession | mutation | VaultRecoverySuccession: window_cannot_be_fast_forwarded<br>VaultRecoverySuccession: cancellation_blocks_completion<br>VaultRecoverySuccession: veto_blocks_completion<br>VaultWitnessAccountability: poll_yields_alert_or_fraud_proof |
| rekey.frontier_marker | mutation | VaultRekeyMarker: exclusion_forward<br>VaultRekeyMarkerChain: exclusion_through_chain |
| root.rotation | mutation | VaultRootRotation: thief_cannot_rotate<br>VaultRootRotation: owner_rotates_away_from_stolen_root |
| storage.advance_state | mutation | **GAP** |
| storage.issue_grant | mutation | VaultRekeyMarker: exclusion_forward |
| storage.issue_receipt | mutation | **GAP** |
| storage.mint_credential | mutation | VaultConcurrentRekey: concurrent_grant_secret |
| storage.provision_missing | mutation | **GAP** |
| vault.create_class | mutation | VaultPolicyClass: root_reaches_every_generation |
| vault.revoke_class_factor | mutation | VaultPolicyClass: revoked_factor_excluded_forward<br>VaultPolicyClass: revoked_factor_keeps_old |

26 of 60 entries carry at least one proof; 34 gaps.
