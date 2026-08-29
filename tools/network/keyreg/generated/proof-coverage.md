# Proof coverage — generated from registry.yaml by gen.py; do not edit.

One row per key and per mutation: the machine-checked lemmas covering
it, or GAP. Gap rows feed the formal-modeling queue on tracker note
8277c76c-ad1.

| entry | kind | proofs |
|---|---|---|
| agent_delegate_signing_key | key | **GAP** |
| class_key | key | **GAP** |
| factor_recipient | key | **GAP** |
| factor_seed | key | VaultFactorPolicy: password_alone_no_root<br>VaultFactorPolicy: passkeys_both_no_root<br>VaultFactorPolicy: unlock_only_no_root |
| generation_secret | key | VaultRekeyMarker: exclusion_forward<br>VaultConcurrentRekey: concurrent_grant_secret |
| k_index_k_meta | key | **GAP** |
| master_kek | key | **GAP** |
| member_recovery_key | key | VaultRecoveryRace: recovery_key_secret<br>VaultRecoveryRace: recovery_beats_thief |
| object_cek | key | **GAP** |
| object_wrap_key | key | **GAP** |
| org_root_signing_key | key | **GAP** |
| pepper | key | **GAP** |
| per_credential_wrapping_key | key | VaultOpenStore: no_key_to_forged_pk |
| per_machine_key | key | VaultFleetDist: machine_key_yields_nothing_undistributed<br>VaultFleetDist: kicked_machine_excluded |
| persona_kem_private | key | VaultFleetDist: persona_kem_snapshot_total<br>VaultRekeyMarker: exclusion_forward |
| persona_signing_key | key | **GAP** |
| personal_root_seed | key | VaultOpenStore: root_secret_open_store<br>VaultRekeyMarker: root_secret<br>VaultFactorPolicy: password_and_passkey1_release_root |
| recovery_code | key | **GAP** |
| recovery_signing_key | key | **GAP** |
| recovery_slot_recipient | key | **GAP** |
| root_anchor_seed | key | **GAP** |
| sealed_index | key | **GAP** |
| serving_delegate_key | key | **GAP** |
| tier2_session_key | key | **GAP** |
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
| fleet.kick | mutation | VaultFleetDist: kicked_machine_excluded |
| fold.checkpoint | mutation | **GAP** |
| fold.delegate | mutation | **GAP** |
| fold.genesis | mutation | **GAP** |
| fold.invite | mutation | **GAP** |
| fold.key_epoch | mutation | **GAP** |
| fold.key_rotate | mutation | **GAP** |
| fold.member_claim | mutation | **GAP** |
| fold.member_rekey | mutation | VaultRecoveryRace: recovery_beats_thief<br>VaultRecoveryRace: recovery_key_secret |
| fold.revoke | mutation | **GAP** |
| fold.role_define | mutation | **GAP** |
| fold.role_grant | mutation | **GAP** |
| fold.role_revoke | mutation | **GAP** |
| passkey.enroll | mutation | VaultOpenStore: no_key_to_forged_pk |
| passkey.revoke | mutation | **GAP** |
| recovery.succession | mutation | VaultRecoverySuccession: window_cannot_be_fast_forwarded<br>VaultRecoverySuccession: cancellation_blocks_completion<br>VaultRecoverySuccession: veto_blocks_completion |
| rekey.frontier_marker | mutation | VaultRekeyMarker: exclusion_forward |
| root.rotation | mutation | **GAP** |
| storage.advance_state | mutation | **GAP** |
| storage.issue_grant | mutation | VaultRekeyMarker: exclusion_forward |
| storage.issue_receipt | mutation | **GAP** |
| storage.mint_credential | mutation | VaultConcurrentRekey: concurrent_grant_secret |
| storage.provision_missing | mutation | **GAP** |
| vault.create_class | mutation | **GAP** |
| vault.revoke_class_factor | mutation | **GAP** |

15 of 59 entries carry at least one proof; 44 gaps.
