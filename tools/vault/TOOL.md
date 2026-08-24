# Vault — per-policy key-encryption classes

One key-encryption key per access policy, so enrolling a factor does not re-wrap
every secret (bead auto-39d26, crib `graph://1e005d5c-c11` §18, design note
`graph://0c206bd8-1c6`). Phase one ships the `password` policy; `prf` and `both`
are constructible for the cross-model attack but their real factor seed is a
WebAuthn PRF output whose library is out of this epic.

## The idea

A setting's data key (CEK) is **not** wrapped to factors. It is sealed to a
*policy class* public key. The class's private seed is wrapped to factors and
never persisted in plaintext. Enrolling a factor adds one wrap and touches no
secret; individual secrets hold no factor wraps, so they cannot diverge. This is
the storage-state DAG move applied to human factors: indirect through the class,
re-wrap the class, never the objects.

```
setting.sealed_cek  =  HPKE(class_sealing_public_key, cek, context)
class secret        =  (password/prf) sealed to each factor
                       (both) share_a ^ share_b, sealed to a password + a passkey
class keypair        =  X25519 keypair derived from the class secret;
                       only the public half is persisted
factor wrap          =  idkit.sealing.seal(...)  — RFC 9180 HPKE, X25519
factor seed          =  password → idkit.armor (PBKDF2-600k → AES-256-GCM)
                       passkey  → WebAuthn PRF output (library out of epic)
```

## Key generations (why revocation is not a re-wrap)

Revoking a factor **appends a new generation**: a fresh class secret sealed to
the survivors, with a new public sealing key. Old generations are retained untouched — survivors keep reading
existing secrets, and the revoked factor is excluded from the new generation's
writes (crib §3: revocation ≡ excluded from FUTURE seals, never "loses synced
data"). A `sealed_cek` records its generation; new writes use the current one.
Bulk re-wrap of existing data keys is a **correctness defect**, never done: a
revoked device that already holds an old key opens anything it can still fetch
ciphertext for. The two honest operations are cycling the key forward and
destroying plaintext.

Operator text must NOT describe revocation as removing access to existing
secrets — a test (`test_service.py`) enforces this over the package.

## Layout

| File | What |
|------|------|
| `policy_class.py` | The construction: records + create/open/extend/revoke/seal_cek/open_cek |
| `storage_object.py` | A setting revision as an org-domain content object: the derived object/revision ids, the opaque locator a row stores, and the write that mints its own key generation when it must |
| `factors.py` | Password factor (armored) and passkey factor (PRF stand-in) |
| `store.py` | SQLite persistence: classes, factor material, vault secrets (with class reference) |
| `service.py` | Store-backed orchestration shared by the CLI and tests |
| `cli.py` | Headless CLI (`python3 -m tools.vault.cli`) — the API/view-state surface (crib §21) |
| `testkit.py` | Throwaway test-identity / test-genesis factory (crib §21 prerequisite) |
| `tests/` | Acceptance tests + cross-model attack regressions |

Graph Setting schemas (the persisted-shape contract, `@home("personal")`, `raw`
— all secrets live in the operator's own store):
`tools/graph/schemas/vault_policy_class.py` (`autonomy.vault.policy-class#1`) and
`tools/graph/schemas/vault_secret.py` (`autonomy.vault.secret#1`, whose
`policy_class_id` is the vault secret's reference to its class).

## Settings whose payload is a secret

A settings set marks itself with `@vaulted("audited"|"secured")`
(`tools/graph/schemas/registry.py`). Its rows then hold a locator instead of a
payload: `settings_ops.add_setting` writes the first revision and
`override_setting` appends each later one, both sealing through the sealer a
host process registers with `settings_ops.set_vault_sealer`. With no sealer
registered the write is REFUSED — writing the value in the clear is not the
fallback. Ordinary sets are untouched and need no sealer.

One setting is one object and one row is one revision; both identifiers are
derived, never stored, so two nodes agree without coordinating. The locator is
an opaque scalar because settings resolution merge-patches candidate rows
before anything is decrypted, and an object-shaped locator would be spliced
field by field across two writes.

## Headless run

```bash
python3 -m tools.vault.cli demo                       # whole path end-to-end
python3 -m tools.vault.cli --store /tmp/v.db enroll-password --factor-id pw-1 --password alpha
python3 -m tools.vault.cli --store /tmp/v.db create-class --policy password --factors pw-1
python3 -m tools.vault.cli --store /tmp/v.db seal-setting --name s1 --class <CID> \
    --genesis g1 --policy password
python3 -m tools.vault.cli --store /tmp/v.db open-setting --name s1 --opener pw-1:alpha
```

## Write and read are intentionally asymmetric

Sealing uses only the class generation's public key, so an application may
write a secured setting unattended. The vault does not decide who may create or
overwrite a row; that is Settings/application authorization. Opening derives
the matching private key from the factor-wrapped class secret and therefore
still requires the declared password, passkey PRF, or both. No class secret or
private sealing key is cached or returned.

For a raw secret value, the production CLI accepts bytes without putting them
in argv or the environment:

```bash
graph set seal --key mac.ssh --policy-class <CID> --from-file /run/secrets/key
# also: --from-fd N, --prompt for one line, or stdin
```

## This construction was attacked before it was consumed

New and unreviewed at authoring (2026-08-01). Per the bead, a cross-model attack
(different model from the implementer) ran against it before any other bead
wrapped a secret under a policy class; the findings and their resolution are in
`tests/test_attack.py` and `experience_report.md`.
