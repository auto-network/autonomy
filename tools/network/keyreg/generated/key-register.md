# Per-key register — generated from registry.yaml by gen.py; do not edit.

The layout mirrors the crib sheet's section 9 register (graph note
1e005d5c-c11) so the two can be compared side by side.

## COLD

### class_key — symmetric, cold (Released only inside a factor ceremony and never across the release boundary — exporting it would release the whole class.)

- **minted** at random
- **reaches** Every content key sealed under that class generation.
- **snapshot** The class's secrets for generations this key belongs to.
- **live?** No.
- **revoke** Factor revocation appends the next generation sealed to the survivors and the anchor; it applies at the next write and touches no existing sealed material.
- **bound** The class's membership at each generation.
- **code** `tools/vault/policy_class.py:Generation` · `tools/vault/store.py:put_class`
- **crib** §3, §18

### factor_recipient — kem, cold (Re-derivable only from the cold factor seed.)

- **derived** from `factor_seed` via derive_encapsulation_keypair
- **reaches** The policy shares sealed to it inside the armor envelope.
- **snapshot** Those shares; the policy expression decides what they combine to.
- **live?** No.
- **revoke** Factor revocation; the envelope is re-signed without the share.
- **bound** The policy expression.
- **code** `tools/network/idkit/sealing.py:derive_encapsulation_keypair` · `tools/network/idkit/root_factor_policy.py`
- **crib** §2

### factor_seed — seed, cold (Producing it requires the human gesture — typing the password or a passkey PRF assertion (crib Q0).)

- **minted** at random
- **reaches** The factor's derived recipient (its share of the root factor policy) and its derived dashboard access key. Only a factor set satisfying the policy expression reaches the root; unlock-only factors never do.
- **snapshot** That factor's policy shares. Whether the root follows depends on the policy expression, not on this key alone.
- **live?** No.
- **revoke** Factor revocation re-signs the armor envelope without it.
- **bound** The policy expression.
- **code** `tools/network/idkit/root_factor_policy.py`
- **crib** §2

### k_index_k_meta — derived-secret, cold (Subkeys of the sealed index; neither exists outside a release.) · DESIGNED

- **derived** from `sealed_index` via HKDF
- **reaches** K_index computes blind row addresses; K_meta seals row values. The blind index is the item-to-row mapping; nothing stores it.
- **snapshot** Nothing — they are never at rest.
- **live?** No.
- **revoke** Follows the sealed index.
- **bound** The one store.
- **crib** §23

### master_kek — symmetric, cold (Opened only inside a root ceremony gated on the factor policy.)

- **minted** at random
- **reaches** The root armor it seals.
- **snapshot** The armored root, opened only through a satisfying factor set.
- **live?** No.
- **revoke** Re-sealed on every factor change (a new armor generation).
- **bound** The factor policy.
- **code** `tools/network/idkit/armor.py`
- **crib** §2

### member_recovery_key — signing, cold (Derived only from the cold recovery code.)

- **derived** from `recovery_code` via HKDF(genesis_id)
- **reaches** Exactly one act: the recovery-authorized door of member.rekey. The genesis_id in the derivation keeps an identical printed code from linking personas across organizations.
- **snapshot** Nothing — no plaintext, no content, no authority beyond the one door.
- **live?** Yes — the rekey event must reach the ledger fold.
- **revoke** Enrolled at member.claim; changing it requires the current member recovery key or the org root. No event adds one after member.claim.
- **bound** The one act; the fold rejects everything else.
- **code** `tools/network/idkit/recovery.py:member_recovery_key` · `tools/network/ledger/fold.py:_h_member_rekey`
- **crib** §9

### org_root_signing_key — signing, cold (Constitutional acts are intent acts (crib Q0).)

- **minted** at random
- **reaches** Genesis, key rotation, serve-certificate minting, registration, and rebind. It is not a domain principal: it holds no KEM credential and receives no capability grants.
- **snapshot** No content.
- **live?** Yes — it must reach the ledger or registry to be used.
- **revoke** Org-vouch or dual-signed org-root rotation; both are ceremonies.
- **bound** The ceremony.
- **code** `tools/network/ledger/fold.py:_h_key_rotate`
- **crib** §8, §9

### per_credential_wrapping_key — kem, cold (The private half derives from the credential's PRF output on-device; using it requires a user-verified assertion at that machine.)

- **derived** from `factor_seed` via PRF-derived X25519
- **reaches** Payloads sealed to the credential's device: cross-device Tier-2 renewals and master-KEK re-wraps. The public key is valid only when carried in a root-signed enrollment statement, verified before any seal is addressed to it.
- **snapshot** Nothing without the device's PRF assertion.
- **live?** Yes — opening a seal needs the assertion at that machine.
- **revoke** Passkey revocation (root-signed RevocationRecord).
- **bound** The credential.
- **code** `tools/network/idkit/enrollment.py:PasskeyEnrollmentStatement`
- **crib** §2

### persona_signing_key — signing, cold (Deriving it requires the personal root seed.)

- **derived** from `personal_root_seed` via derive_persona(genesis_id)
- **reaches** Authorship of the persona's ledger events and key-control records in one organization. The genesis_id input keeps personas unlinkable across organizations.
- **snapshot** Authority to sign as the persona until rekeyed away.
- **live?** Yes — records must reach honest nodes.
- **revoke** member.rekey moves the persona to a new key.
- **bound** The persona's membership.
- **code** `tools/network/idkit/persona.py:derive_persona`
- **crib** §2

### personal_root_seed — seed, cold (Every act needing it expresses human intent (crib Q0), and its snapshot is total; no execution-class question is reached.)

- **minted** at random
- **reaches** Every purpose key ever derivable from it, and every vault policy class through each class's root-anchor wrap. Root authority reconstitutes the whole identity.
- **snapshot** Total and unbounded, across every organization.
- **live?** No.
- **revoke** None. The remedy is personal-root rotation, which requires the old root, the new root, and the enrolled recovery key (make_rotation in tools/network/idkit/root_rotation.py).
- **bound** None.
- **code** `tools/network/idkit/armor.py` · `tools/network/idkit/root_factor_policy.py` · `tools/network/idkit/root_rotation.py:make_rotation`
- **crib** §2, §9

### recovery_code — seed, cold (Its whole security property is being disjoint from the hot path; it is printed once and never touches a machine except in a recovery ceremony.)

- **minted** at random
- **reaches** Three domain-separated derivations, none reaching another: the armor recovery slot recipient, the recovery signing key, and the per-organization member recovery keys.
- **snapshot** The sealed root seed, opened only by the code. No org content.
- **live?** No.
- **revoke** Replacement with the code in hand runs replace_recovery_slot (tools/network/idkit/root_factor_policy.py). Regeneration with the code lost is the witnessed succession window: declaration, blocking alert, cancellation or veto, completion after the window.
- **bound** None.
- **code** `tools/network/idkit/recovery.py`
- **crib** §9

### recovery_signing_key — signing, cold (Derived only from the cold recovery code.)

- **derived** from `recovery_code` via HKDF
- **reaches** The recovery co-signature on personal-root rotation and on recovery-code succession declarations.
- **snapshot** Nothing; it signs and never decrypts.
- **live?** Yes — its signatures must reach a verifier.
- **revoke** Recovery-code succession (the witnessed window).
- **bound** The acts it co-signs.
- **code** `tools/network/idkit/recovery.py:recovery_signing_key` · `tools/network/idkit/root_rotation.py:rotation_recovery_input`
- **crib** §9

### recovery_slot_recipient — kem, cold (Derived only from the cold recovery code.)

- **derived** from `recovery_code` via HKDF
- **reaches** The root seed sealed into the armor's recovery slot.
- **snapshot** The sealed root seed, and nothing else.
- **live?** No.
- **revoke** replace_recovery_slot re-seals the slot to a new code's recipient.
- **bound** None — it opens the root.
- **code** `tools/network/idkit/recovery.py` · `tools/network/idkit/root_factor_policy.py:replace_recovery_slot`
- **crib** §2, §9

### root_anchor_seed — seed, cold (Sealed to a root-derived recipient; opening it requires root authority.)

- **minted** at random
- **reaches** Every vault policy class, through each class generation's mandatory anchor wrap — the indirection that lets root-factor churn touch only the armor.
- **snapshot** Nothing while sealed; every class once the root is open.
- **live?** No.
- **revoke** Irrevocable by design: revoke_factor refuses to drop it and the store refuses to persist a class generation without it.
- **bound** None — it is the root's guaranteed floor under every class.
- **code** `tools/vault/root_anchor.py` · `tools/vault/store.py:put_root_class_once`
- **crib** §18

### sealed_index — seed, cold (Exists only in sealed form; released by a factor ceremony.) · DESIGNED

- **minted** at random
- **reaches** A sealed store's entire index — its address key and value key derive from it.
- **snapshot** Nothing while sealed; the store's item map once released.
- **live?** No.
- **revoke** Re-mint the store under a new index.
- **bound** The one store.
- **crib** §23

### vault_factor_recipient — kem, cold (Re-derivable only from the cold factor seed.)

- **derived** from `factor_seed` via derive_encapsulation_keypair
- **reaches** The vault class-key wraps sealed to it.
- **snapshot** Those wraps; each class generation it belongs to opens.
- **live?** No.
- **revoke** Vault class-factor revocation reseals the next generation without it.
- **bound** The classes it is enrolled in.
- **code** `tools/vault/recipients.py`
- **crib** §18

## MEMORY

### agent_delegate_signing_key — signing, memory (Passes the crib's Q2: scope-attenuated, TTL-bounded, fold-enforced at acceptance, revocable, and required for unattended operation.)

- **minted** at random
- **reaches** Exactly two execution scopes, storage:state:advance and storage:capability:grant, and nothing else. It signs; it opens nothing. It authorizes only by resolving its delegation chain to a member persona: a chain terminating outside the member roster is void, and generic scope-holding is never consulted for these two scopes.
- **snapshot** Nothing.
- **live?** Yes — records must reach honest nodes, which verify them against the fold.
- **revoke** RevocationRecord plus TTL. Not a ceremony.
- **bound** The delegating persona's own membership; the fold rejects any overreach.
- **code** `tools/network/storagekit/acceptance.py`
- **crib** §7, §9, §11

### generation_secret — symmetric, memory (Snapshot is total for its branch, offline.)

- **minted** at random
- **reaches** Every object under the generation and every ancestor via bridges.
- **snapshot** Total for that branch, offline.
- **live?** No.
- **revoke** None — a generation secret is never retired.
- **bound** None.
- **code** `tools/network/storagekit/state.py:generate`
- **crib** §6, §9

### object_cek — symmetric, memory (Unique per immutable revision and encrypts nothing else, so releasing it equals releasing that one plaintext — it is the unit allowed across the release boundary.)

- **minted** at random
- **reaches** One object revision's plaintext.
- **snapshot** That one revision.
- **live?** No.
- **revoke** Never — the revision is immutable; withdrawal is a new key for new writes.
- **bound** The one revision.
- **code** `tools/vault/policy_class.py:_cek_public_seal_purpose`
- **crib** §4, §18, §23

### object_wrap_key — derived-secret, memory (Re-derivable from the generation secret during the warm period.)

- **derived** from `generation_secret` via HKDF(genesis_id, domain_id, state_id, object_id, revision, content_hash)
- **reaches** One object revision's wrapped content key.
- **snapshot** That revision, given the ciphertext.
- **live?** No.
- **revoke** Follows the generation secret; never individually retired.
- **bound** The one revision.
- **code** `tools/network/storagekit/objects.py`
- **crib** §6

### pepper — symmetric, memory (An audited vault setting: released unattended once the vault is open; encrypted at rest so a cold dump cannot run the offline location oracle.) · DESIGNED

- **minted** at random
- **reaches** The row addresses of every secret vault (location, not content).
- **snapshot** Encrypted at rest, nothing. Held warm, the ability to test password guesses against vault locations offline.
- **live?** No.
- **revoke** Re-mint and re-address every secret vault.
- **bound** Location secrecy only; contents keep their own seals.
- **crib** §24

### persona_kem_private — kem, memory (Snapshot is total: this key plus a copy of the replicated store is the whole domain history, offline. Not disk-eligible at any price.)

- **derived** from `personal_root_seed` via derive_kem_seed(counter)
- **reaches** Every capability grant addressed to this persona, hence any head secret, hence all domain content including full history through the bridges.
- **snapshot** Total for the domain — key plus store copy is the whole history.
- **live?** No.
- **revoke** Publish a superseding PersonaKemCredential. Stops future grants only; undoes nothing already opened.
- **bound** None — damage is complete at the instant of theft.
- **code** `tools/network/storagekit/credentials.py:derive_kem_seed`
- **crib** §9, §13, §14

## DISK

### browser_session_key — signing, disk (Persisted in the browser's IndexedDB and usable with no further human interaction until its certificate expires; theft through the page is prevented by WebCrypto non-extractability, and the bound is the certificate validity window plus revocation.)

- **minted** at random
- **reaches** The scopes its persona-signed delegation certificate carries (SESSION_SCOPES in network-signon.mjs): delegate:agent, link:publish, link:revoke, tunnel:serve, turn:allocate, and viewer:identify — one certificate per organization persona, all over the single session key.
- **snapshot** Nothing through the page: the key is created extractable:false and _installSession refuses any key claiming otherwise, so no script can export it; it appears in no replicated store.
- **live?** Yes — its signatures must reach the dashboard or registry.
- **revoke** The certificate validity window (default twenty-four hours, maximum thirty days), enforced at load and by an expiry watchdog that signs the browser out; a root-signed revocation record is the explicit path. Not a ceremony.
- **bound** The certificate's validity window and scope list.
- **code** `tools/dashboard/static/js/network-signon.mjs:signRegistryRequest` · `tools/dashboard/static/js/network-signon.mjs:_installSession` · `tools/network/registry/signing.py`
- **crib** §2, §9

### per_machine_key — kem, disk (Snapshot is zero — it decrypts no existing record; it only receives future distributions while the machine remains in the fleet. Placement is an operator setting.)

- **derived** from `personal_root_seed` via derive_machine_key(machine_id)
- **reaches** Distributions sealed to it after the theft — future persona KEM keys.
- **snapshot** Zero. It decrypts no existing record.
- **live?** Yes — the thief must still be in the fleet and reachable.
- **revoke** Remove the machine from the fleet. Root-signed, not a ceremony.
- **bound** The removal.
- **code** `tools/network/idkit/persona.py:derive_machine_key` · `tools/network/fleet_roster.py:RosterEntry`
- **crib** §9, §10

### serving_delegate_key — signing, disk (Snapshot is none — it reaches no plaintext and authorizes nothing.)

- **minted** at random
- **reaches** Authentication of one outbound serving tunnel (tunnel:serve only).
- **snapshot** None.
- **live?** Yes.
- **revoke** Re-mint; thirty-day TTL, renewal below twenty days remaining.
- **bound** The TTL.
- **code** `tools/network/idkit/certs.py`
- **crib** §8, §9
