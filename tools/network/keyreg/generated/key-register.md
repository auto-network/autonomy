# Per-key register — generated from registry.yaml by gen.py; do not edit.

The layout mirrors the crib sheet's section 9 register (graph note
1e005d5c-c11) so the two can be compared side by side.

Start with the [reading guide](../GUIDE.md). See also the
[workflow register](workflows.md) and [proof references](proof-coverage.md).
Built means an implementation is recorded, not that every surrounding workflow is deployed.
Custody describes intended storage; read each snapshot's stated conditions separately.

## Find a key

| Key | Subsystem | Kind | Custody | Status |
|---|---|---|---|---|
| [agent_delegate_signing_key](#key-agent_delegate_signing_key) | org-authority | signing | memory | built |
| [browser_session_key](#key-browser_session_key) | browser | signing | disk | built |
| [class_key](#key-class_key) | vault-classes | symmetric | cold | built |
| [delegate_audited_recipient](#key-delegate_audited_recipient) | vault-classes | kem | memory | built |
| [factor_recipient](#key-factor_recipient) | identity-armor | kem | cold | built |
| [factor_seed](#key-factor_seed) | identity-armor | seed | cold | built |
| [generation_secret](#key-generation_secret) | domain-storage | symmetric | memory | built |
| [k_index_k_meta](#key-k_index_k_meta) | sealed-stores | derived-secret | cold | built |
| [member_recovery_key](#key-member_recovery_key) | recovery | signing | cold | built |
| [object_cek](#key-object_cek) | vault-classes | symmetric | memory | built |
| [object_wrap_key](#key-object_wrap_key) | domain-storage | derived-secret | memory | built |
| [org_root_signing_key](#key-org_root_signing_key) | org-authority | signing | cold | built |
| [pepper](#key-pepper) | sealed-stores | symmetric | memory | built |
| [per_credential_wrapping_key](#key-per_credential_wrapping_key) | identity-armor | kem | cold | built |
| [per_machine_key](#key-per_machine_key) | fleet | kem | memory | built |
| [persona_kem_private](#key-persona_kem_private) | domain-storage | kem | memory | built |
| [persona_signing_key](#key-persona_signing_key) | org-authority | signing | cold | built |
| [personal_root_seed](#key-personal_root_seed) | identity-armor | seed | cold | built |
| [recovery_code](#key-recovery_code) | recovery | seed | cold | built |
| [recovery_signing_key](#key-recovery_signing_key) | recovery | signing | cold | built |
| [recovery_slot_recipient](#key-recovery_slot_recipient) | recovery | kem | cold | built |
| [root_anchor_seed](#key-root_anchor_seed) | vault-classes | seed | cold | built |
| [sealed_index](#key-sealed_index) | sealed-stores | seed | cold | built |
| [serving_delegate_key](#key-serving_delegate_key) | org-authority | signing | disk | built |
| [serving_machine_signing_key](#key-serving_machine_signing_key) | fleet | signing | memory | built |
| [vault_factor_recipient](#key-vault_factor_recipient) | vault-classes | kem | cold | built |

## COLD

<a id="key-class_key"></a>
### class_key — symmetric, cold (Released only inside a factor ceremony and never across the release boundary — exporting it would release the whole class.)

- **minted** at random
- **reaches** Every content key sealed under that class generation.
- **snapshot** The class's secrets for generations this key belongs to.
- **live?** No.
- **revoke** Factor revocation appends the next generation sealed to the survivors and the anchor; it applies at the next write and touches no existing sealed material.
- **bound** The class's membership at each generation.
- **sealed to** vault_factor_recipient — PolicyClassRecord generation wrap; purpose `autonomy/vault-policy-class/v1`
- **sealed to** root_anchor_seed — mandatory anchor wrap; purpose `autonomy/vault-root-anchor-wrap/v1`
- **code** `tools/vault/policy_class.py:Generation` · `tools/vault/store.py:put_class`
- **crib** §3, §18

<a id="key-factor_recipient"></a>
### factor_recipient — kem, cold (Re-derivable only from the cold factor seed.)

- **derived** from `factor_seed` via derive_encapsulation_keypair
- **derivation purpose** `autonomy/root-factor-recipient/v1`
- **reaches** The policy shares sealed to it inside the armor envelope.
- **snapshot** Those shares; the policy expression decides what they combine to.
- **live?** No.
- **revoke** Factor revocation; the envelope is re-signed without the share.
- **bound** The policy expression.
- **code** `tools/network/idkit/sealing.py:derive_encapsulation_keypair` · `tools/network/idkit/root_factor_policy.py`
- **crib** §2

<a id="key-factor_seed"></a>
### factor_seed — seed, cold (Producing it requires the human gesture — typing the password or a passkey PRF assertion (crib Q0).)

- **minted** at random
- **reaches** The factor's derived recipient (its share of the root factor policy) and its derived dashboard access key. Only a factor set satisfying the policy expression reaches the root; unlock-only factors never do.
- **snapshot** That factor's policy shares. Whether the root follows depends on the policy expression, not on this key alone.
- **live?** No.
- **revoke** Factor revocation re-signs the armor envelope without it.
- **bound** The policy expression.
- **code** `tools/network/idkit/root_factor_policy.py`
- **crib** §2

<a id="key-k_index_k_meta"></a>
### k_index_k_meta — derived-secret, cold (Subkeys of the sealed index; neither exists outside a release.)

- **derived** from `sealed_index` via HKDF(index, metadata)
- **reaches** K_index computes blind row addresses; K_meta seals row values. The blind index is the item-to-row mapping; nothing stores it.
- **snapshot** Nothing — they are never at rest.
- **live?** No.
- **revoke** Follows the sealed index.
- **bound** The one store.
- **code** `tools/graph/sealed_settings.py:_keys` · `tools/graph/sealed_settings.py:blind_index`
- **crib** §23

<a id="key-member_recovery_key"></a>
### member_recovery_key — signing, cold (Derived only from the cold recovery code.)

- **derived** from `recovery_code` via HKDF(genesis_id)
- **derivation purpose** `autonomy.recovery.member-sign.v1`
- **reaches** Exactly one act: the recovery-authorized door of member.rekey. The genesis_id in the derivation keeps an identical printed code from linking personas across organizations.
- **snapshot** Nothing — no plaintext, no content, no authority beyond the one door.
- **live?** Yes — the rekey event must reach the ledger fold.
- **revoke** Enrolled at member.claim; changing it requires the current member recovery key or the org root. No event adds one after member.claim.
- **bound** The one act; the fold rejects everything else.
- **code** `tools/network/idkit/recovery.py:member_recovery_key` · `tools/network/ledger/fold.py:_h_member_rekey`
- **crib** §9

<a id="key-org_root_signing_key"></a>
### org_root_signing_key — signing, cold (Constitutional acts are intent acts (crib Q0).)

- **minted** at random
- **reaches** Genesis, key rotation, serve-certificate minting, registration, and rebind. It is not a domain principal: it holds no KEM credential and receives no capability grants.
- **snapshot** No content.
- **live?** Yes — it must reach the ledger or registry to be used.
- **revoke** Org-vouch or dual-signed org-root rotation; both are ceremonies.
- **bound** The ceremony.
- **code** `tools/network/ledger/fold.py:_h_key_rotate`
- **crib** §8, §9

<a id="key-per_credential_wrapping_key"></a>
### per_credential_wrapping_key — kem, cold (The private half derives from the credential's PRF output on-device; using it requires a user-verified assertion at that machine.)

- **derived** from `factor_seed` via PRF-derived X25519
- **reaches** Payloads sealed to the credential's device. The public key is valid only when carried in a root-signed enrollment statement, verified before any seal is addressed to it.
- **snapshot** Nothing without the device's PRF assertion.
- **live?** Yes — opening a seal needs the assertion at that machine.
- **revoke** Passkey revocation (root-signed RevocationRecord).
- **bound** The credential.
- **code** `tools/network/idkit/enrollment.py:PasskeyEnrollmentStatement`
- **crib** §2

<a id="key-persona_signing_key"></a>
### persona_signing_key — signing, cold (Deriving it requires the personal root seed.)

- **derived** from `personal_root_seed` via derive_persona(genesis_id)
- **reaches** Authorship of the persona's ledger events and key-control records in one organization. The genesis_id input keeps personas unlinkable across organizations.
- **snapshot** Authority to sign as the persona until rekeyed away.
- **live?** Yes — records must reach honest nodes.
- **revoke** member.rekey moves the persona to a new key.
- **bound** The persona's membership.
- **code** `tools/network/idkit/persona.py:derive_persona`
- **crib** §2

<a id="key-personal_root_seed"></a>
### personal_root_seed — seed, cold (Every act needing it expresses human intent (crib Q0), and its snapshot is total; no execution-class question is reached.)

- **minted** at random
- **reaches** Every purpose key ever derivable from it, and every vault policy class through each class's root-anchor wrap. Root authority reconstitutes the whole identity.
- **snapshot** Total and unbounded, across every organization.
- **live?** No.
- **revoke** None. The remedy is personal-root rotation, which requires the old root, the new root, and the enrolled recovery key (make_rotation in tools/network/idkit/root_rotation.py).
- **bound** None.
- **sealed to** factor_recipient — root factor policy envelope share; purpose `autonomy/root-factor-recipient/v1`
- **sealed to** recovery_slot_recipient — armor recovery slot; purpose `autonomy/recovery-armor/v1`
- **code** `tools/network/idkit/armor.py` · `tools/network/idkit/root_factor_policy.py` · `tools/network/idkit/root_rotation.py:make_rotation`
- **crib** §2, §9

<a id="key-recovery_code"></a>
### recovery_code — seed, cold (Its whole security property is being disjoint from the hot path; it is printed once and never touches a machine except in a recovery ceremony.)

- **minted** at random
- **reaches** Three domain-separated derivations, none reaching another: the armor recovery slot recipient, the recovery signing key, and the per-organization member recovery keys. The armor recipient opens the matching recovery slot, restoring personal-root authority.
- **snapshot** With the matching encrypted recovery record, restores the personal root and its downstream authority. This is the intentional master recovery capability, independent of the normal factor policy.
- **live?** No.
- **revoke** Replacement with the code in hand runs replace_recovery_slot (tools/network/idkit/root_factor_policy.py). Regeneration with the code lost is the witnessed succession window: declaration, blocking alert, cancellation or veto, completion after the window.
- **bound** None.
- **code** `tools/network/idkit/recovery.py` · `tools/network/idkit/root_factor_policy.py:open_root_with_recovery`
- **crib** §9
- **notes** One cold code supplies decryption and signing functions. The separate recovery signature prevents a hot-root thief without the cold code from rotating the identity. It does not prevent the code holder from recovering. Ordinary complete-authority rotation and the designed lost-code witnessed succession path are distinct workflows.

<a id="key-recovery_signing_key"></a>
### recovery_signing_key — signing, cold (Derived only from the cold recovery code.)

- **derived** from `recovery_code` via HKDF
- **derivation purpose** `autonomy.recovery.signing.v1`
- **reaches** The recovery co-signature on personal-root rotation and on recovery-code succession declarations.
- **snapshot** Nothing; it signs and never decrypts.
- **live?** Yes — its signatures must reach a verifier.
- **revoke** Recovery-code succession (the witnessed window).
- **bound** The acts it co-signs.
- **code** `tools/network/idkit/recovery.py:recovery_signing_key` · `tools/network/idkit/root_rotation.py:rotation_recovery_input`
- **crib** §9

<a id="key-recovery_slot_recipient"></a>
### recovery_slot_recipient — kem, cold (Derived only from the cold recovery code.)

- **derived** from `recovery_code` via HKDF
- **derivation purpose** `autonomy/recovery-armor/v1`
- **reaches** The root seed sealed into the armor's recovery slot.
- **snapshot** The sealed root seed, and nothing else.
- **live?** No.
- **revoke** replace_recovery_slot re-seals the slot to a new code's recipient.
- **bound** None — it opens the root.
- **code** `tools/network/idkit/recovery.py` · `tools/network/idkit/root_factor_policy.py:replace_recovery_slot`
- **crib** §2, §9

<a id="key-root_anchor_seed"></a>
### root_anchor_seed — seed, cold (Sealed to a root-derived recipient; opening it requires root authority.)

- **minted** at random
- **reaches** Every vault policy class, through each class generation's mandatory anchor wrap — the indirection that lets root-factor churn touch only the armor.
- **snapshot** Nothing while sealed; every class once the root is open.
- **live?** No.
- **revoke** Irrevocable by design: revoke_factor refuses to drop it and the store refuses to persist a class generation without it.
- **bound** None — it is the root's guaranteed floor under every class.
- **code** `tools/vault/root_anchor.py` · `tools/vault/store.py:put_root_class_once`
- **crib** §18

<a id="key-sealed_index"></a>
### sealed_index — seed, cold (Exists only in sealed form; released by a factor ceremony.)

- **minted** at random
- **reaches** A sealed store's entire index — its address key and value key derive from it.
- **snapshot** Nothing while sealed; the store's item map once released.
- **live?** No.
- **revoke** Re-mint the store under a new index.
- **bound** The one store.
- **code** `tools/graph/sealed_settings.py:SealedSettings` · `tools/graph/sealed_settings.py:_get_or_create_sealed_index`
- **crib** §23

<a id="key-vault_factor_recipient"></a>
### vault_factor_recipient — kem, cold (Re-derivable only from the cold factor seed.)

- **derived** from `factor_seed` via derive_encapsulation_keypair
- **derivation purpose** `autonomy/vault-policy-recipient/v1`
- **reaches** The vault class-key wraps sealed to it.
- **snapshot** Those wraps; each class generation it belongs to opens.
- **live?** No.
- **revoke** Vault class-factor revocation reseals the next generation without it.
- **bound** The classes it is enrolled in.
- **code** `tools/vault/recipients.py`
- **crib** §18

## MEMORY

<a id="key-agent_delegate_signing_key"></a>
### agent_delegate_signing_key — signing, memory (Passes the crib's Q2: scope-attenuated, TTL-bounded, fold-enforced at acceptance, revocable, and required for unattended operation.)

- **minted** at random
- **reaches** Exactly two execution scopes, storage:state:advance and storage:capability:grant, and nothing else. It signs; it opens nothing. It authorizes only by resolving its delegation chain to a member persona: a chain terminating outside the member roster is void, and generic scope-holding is never consulted for these two scopes.
- **snapshot** Nothing.
- **live?** Yes — records must reach honest nodes, which verify them against the fold.
- **revoke** RevocationRecord plus TTL. Not a ceremony.
- **bound** The delegating persona's own membership; the fold rejects any overreach.
- **code** `tools/network/storagekit/acceptance.py`
- **crib** §7, §9, §11

<a id="key-delegate_audited_recipient"></a>
### delegate_audited_recipient — kem, memory (Private recipient warms at root unlock for unattended audited reads.)

- **derived** from `personal_root_seed` via derive_encapsulation_keypair
- **derivation purpose** `autonomy/vault/delegate-audited-recipient/v1`
- **reaches** Audited personal-vault object content keys sealed to this recipient.
- **snapshot** Opens retained audited envelopes sealed to this recipient.
- **live?** No — possession of the private key and an envelope permits opening.
- **revoke** Replacement protects newly sealed material; follows personal-root lifecycle.
- **bound** Audited envelopes addressed to this recipient, not secured policy classes.
- **code** `tools/vault/personal_object.py:derive_delegate_audited_recipient`
- **crib** §3, §9
- **notes** X25519 encapsulation key, never a signing key. The public half supports cold writes. Organization isolation of personal-homed audited rows is enforced separately at the API namespace boundary; this derivation itself has no organization input.

<a id="key-generation_secret"></a>
### generation_secret — symmetric, memory (Snapshot is total for its branch, offline.)

- **minted** at random
- **reaches** Every object under the generation and every ancestor via bridges.
- **snapshot** Total for that branch, offline.
- **live?** No.
- **revoke** None — a generation secret is never retired.
- **bound** None.
- **sealed to** persona_kem_private — CapabilityGrant; purpose `autonomy/capability-grant/v1`
- **code** `tools/network/storagekit/state.py:generate`
- **crib** §6, §9

<a id="key-object_cek"></a>
### object_cek — symmetric, memory (Unique per immutable revision and encrypts nothing else, so releasing it equals releasing that one plaintext — it is the unit allowed across the release boundary.)

- **minted** at random
- **reaches** One object revision's plaintext.
- **snapshot** That one revision.
- **live?** No.
- **revoke** Never — the revision is immutable; withdrawal is a new key for new writes.
- **bound** The one revision.
- **sealed to** class_key — sealed content key; purpose `autonomy/vault-policy-class/cek/v1`
- **code** `tools/vault/policy_class.py:_cek_public_seal_purpose`
- **crib** §4, §18, §23

<a id="key-object_wrap_key"></a>
### object_wrap_key — derived-secret, memory (Re-derivable from the generation secret during the warm period.)

- **derived** from `generation_secret` via HKDF(genesis_id, domain_id, state_id, object_id, revision, content_hash)
- **derivation purpose** `object-wrap`
- **reaches** One object revision's wrapped content key.
- **snapshot** That revision, given the ciphertext.
- **live?** No.
- **revoke** Follows the generation secret; never individually retired.
- **bound** The one revision.
- **code** `tools/network/storagekit/objects.py`
- **crib** §6

<a id="key-pepper"></a>
### pepper — symmetric, memory (An audited vault setting: released unattended once the vault is open; encrypted at rest so a cold dump cannot run the offline location oracle.)

- **minted** at random
- **reaches** Store row addresses within one scope (location, not content). Operator-personal and each organization scope have separate peppers.
- **snapshot** Encrypted at rest, nothing. Held warm, the ability to test password guesses against vault locations offline.
- **live?** No.
- **revoke** Requires re-addressing the affected scope's stores; normal get-or-create preserves the pepper.
- **bound** One scope's location secrecy only; contents keep their own seals.
- **code** `tools/graph/sealed_settings.py:read_pepper` · `tools/graph/sealed_settings.py:ensure_pepper_minted` · `tools/graph/sealed_settings.py:hidden_address`
- **crib** §24

<a id="key-per_machine_key"></a>
### per_machine_key — kem, memory (Derived from the personal root at unlock and never persisted — the machine stores only its public machine_id; the key exists for the warm period and a reboot always waits on a human unlock.)

- **derived** from `personal_root_seed` via derive_machine_key(machine_id)
- **reaches** Distributions sealed to it after the theft — future persona KEM keys.
- **snapshot** Zero. It decrypts no existing record.
- **live?** Yes — the thief must still be in the fleet and reachable.
- **revoke** Remove the machine from the fleet. Root-signed, not a ceremony.
- **bound** The removal.
- **code** `tools/network/idkit/persona.py:derive_machine_key` · `tools/network/machine_boot.py:operating_key` · `tools/network/fleet_roster.py:RosterEntry`
- **crib** §9, §10
- **notes** Reconciliation pending: this entry describes the designed fleet KEM recipient, but derive_machine_key currently returns an Ed25519 signing KeyPair used for fleet authentication. The distribution mutation is explicitly designed, not implemented. Do not infer an implemented decryption recipient from the signing-key code anchors; recipient identity and derivation need reconciliation before this entry can describe both consistently.

<a id="key-persona_kem_private"></a>
### persona_kem_private — kem, memory (Snapshot is total: this key plus a copy of the replicated store is the whole domain history, offline. Not disk-eligible at any price.)

- **derived** from `personal_root_seed` via derive_kem_seed(counter)
- **reaches** Every capability grant addressed to this persona, hence any head secret, hence all domain content including full history through the bridges.
- **snapshot** Total for the domain — key plus store copy is the whole history.
- **live?** No.
- **revoke** Publish a superseding PersonaKemCredential. Stops future grants only; undoes nothing already opened.
- **bound** None — damage is complete at the instant of theft.
- **sealed to** per_machine_key — KEM-distribution record (designed; bead auto-pw9bs.6)
- **code** `tools/network/storagekit/credentials.py:derive_kem_seed`
- **crib** §9, §13, §14

<a id="key-serving_machine_signing_key"></a>
### serving_machine_signing_key — signing, memory (Derived at root unlock for unattended serving; not stored separately.)

- **derived** from `personal_root_seed` via derive_serving_machine_key(genesis_id, machine_id)
- **derivation purpose** `autonomy.identity.serving-machine.v1`
- **reaches** Authentication of a serving machine in one organization.
- **snapshot** No content decryption; this is an Ed25519 signing key.
- **live?** Yes — authentication signatures must reach a verifier.
- **revoke** Follows the accepted serving credentials and fleet authorization.
- **bound** The organization and machine identifiers bound into the derivation.
- **code** `tools/network/idkit/persona.py:derive_serving_machine_key` · `tools/network/fleet_runtime.py:serving_machine_key`
- **crib** §10
- **notes** Distinct from the personal fleet operating key and the serving delegate. Binding both organization and machine avoids presenting the same machine public key across organizations.

## DISK

<a id="key-browser_session_key"></a>
### browser_session_key — signing, disk (Persisted in the browser's IndexedDB and usable with no further human interaction until its certificate expires; theft through the page is prevented by WebCrypto non-extractability, and the bound is the certificate validity window plus revocation.)

- **minted** at random
- **reaches** The scopes its persona-signed delegation certificate carries (SESSION_SCOPES in network-signon.mjs): delegate:agent, link:publish, link:revoke, tunnel:serve, turn:allocate, and viewer:identify — one certificate per organization persona, all over the single session key.
- **snapshot** Nothing through the page: the key is created extractable:false and _installSession refuses any key claiming otherwise, so no script can export it; it appears in no replicated store.
- **live?** Yes — its signatures must reach the dashboard or registry.
- **revoke** The certificate validity window (default twenty-four hours, maximum thirty days), enforced at load and by an expiry watchdog that signs the browser out; a root-signed revocation record is the explicit path. Not a ceremony.
- **bound** The certificate's validity window and scope list.
- **signs** registry request envelopes only (REQUEST_DOMAIN + canonical JSON of v, method, path, ts, signer, payload) — the single crypto.subtle.sign call site for this key in network-signon.mjs
- **code** `tools/dashboard/static/js/network-signon.mjs:signRegistryRequest` · `tools/dashboard/static/js/network-signon.mjs:_installSession` · `tools/network/registry/signing.py`
- **crib** §2, §9
- **notes** Minted per browser during the sign-on ceremony, after the user proves root authority by satisfying the armor's factor policy; routine operations then sign with crypto.subtle.sign and never see a factor again during the certificate's validity. Distinct from the per-operation vault authorize ceremony, which costs one user-verified assertion per class open and involves no session key.

<a id="key-serving_delegate_key"></a>
### serving_delegate_key — signing, disk (Snapshot is none — it reaches no plaintext and authorizes nothing.)

- **minted** at random
- **reaches** Authentication of one outbound serving tunnel (tunnel:serve only).
- **snapshot** None.
- **live?** Yes.
- **revoke** Re-mint; thirty-day TTL, renewal below twenty days remaining.
- **bound** The TTL.
- **code** `tools/network/idkit/certs.py`
- **crib** §8, §9
