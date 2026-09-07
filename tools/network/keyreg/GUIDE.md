# A field guide to the key registry

The registry is a map of the system: which keys exist, what they do, where
they belong, and which operations change them. It gives people and agents
shared names and a route into the implementation. It is not an executable
authorization policy or a claim of whole-system proof.

## Choose your route

| Your question | Start here |
|---|---|
| What does this key do, and where is it held? | [Key register](generated/key-register.md) |
| What changes during enrollment, removal, or recovery? | [Workflow register](generated/workflows.md) |
| What derives from, or is sealed to, what? | [Relationship diagram](generated/key-graph.mmd) |
| Which models discuss this key or operation? | [Model reference inventory](generated/proof-coverage.md) |
| What should a machine consume? | [Registry YAML](registry.yaml) or [generated JSON](generated/registry.json) |
| How do I query, correct, and regenerate it? | [Tool instructions](TOOL.md) |

## The structure in one pass

**Identity is not one all-purpose key.** Your personal root is the foundation
of your identity. Organization-specific personas represent you in each
organization. The organization's independent signing root establishes its
constitutional authority. Signing keys authorize statements; encapsulation
keys receive encrypted secrets. Similar derivation shapes do not make these
roles interchangeable.

**Membership determines authority; storage keys determine decryption.** The
membership ledger records signed authority changes. Storage state secrets,
grants, and backward bridges determine which encrypted history a member can
open. An agent delegation describes authority to act, not an automatic grant
of every decryption key. Follow the particular operation's verifier rather
than treating every member-derived key as equivalent.

**Vaults distinguish unattended release from a human opening ceremony.**
Audited access allows an authorized worker to obtain a secret with an
attributed release. Secured access additionally requires the opening material
from a human-factor ceremony. Vault policy classes group opening conditions;
the release boundary must not export a class-wide opener when only one object
was approved. A sealed index protects a store's names and metadata separately
from each item's body policy.

**Fleet identity and serving identity have different audiences.** The
personal fleet operating key identifies a machine within the owner's fleet.
The organization-specific serving-machine key additionally binds the
organization, so the relay does not receive the same machine public key for
every organization. Neither signing-key role is itself a content-decryption
recipient.

## Why the recovery code has two heads

The cold recovery code derives material for two different jobs:

1. **Recover access:** open the matching encrypted recovery record and restore
   the personal root, without requiring the normal password or passkey.
2. **Authorize replacement:** derive a separate recovery signing key that
   co-signs rotation with the old root. The public recovery key is declared
   before compromise, not chosen by whoever first asks to rotate.

The point is not to stop a recovery-code holder from recovering. The point is
to stop someone who stole the **hot root but not the cold code** from silently
replacing your identity. The code's two derived functions are domain-separated;
they are not two independently held factors.

| Situation | Intended route | Why it exists |
|---|---|---|
| You have the code and matching recovery record | Recover the root | Emergency access independent of normal factors |
| You hold the root and enrolled recovery signing authority | Complete-authority rotation | Replace a compromised root without a race against a root-only thief |
| You retain the root but have lost the code | Designed, announced and witnessed recovery-key succession | Restore the missing authority without allowing quiet root-only replacement |

The last route binds the proposed successor at declaration, announces the
request, and requires a window with opportunities to cancel or veto before
completion. Someone who still holds the old recovery code can respond through
the complete-authority path. The witness contributes signed time and ceremony
evidence—not a decryption key. This is **not a universal delay on every
rotation**, and the registry marks the succession workflow as designed.

The governing protocol is graph note `714ffa62-e20`; factor-policy and
recovery-slot implementation lives in `tools/network/idkit/root_factor_policy.py`.

## Read the fields precisely

- **Custody** is the intended storage class and the reason for it. It is not
  the same question as what an attacker can do with a usable key.
- **Reaches** describes what the key can derive, open, or authorize. Those
  are different kinds of relationship; read the words as well as the arrows.
- **Snapshot** states consequences under that entry's stated possession
  conditions. “Encrypted at rest” and “attacker holds the usable private key”
  are different starting conditions.
- **Live** asks whether use requires reaching an online verifier or service.
- **Revoke / bound** describe the intended cutoff and its scope, not erasure
  of information already delivered.
- **Built / designed** distinguish recorded implementation from a design-only
  flow. Built does not imply that every enclosing product workflow is deployed.
- **Authority** in a mutation is a list of participants or authority sources,
  not a machine-evaluated AND/OR expression. The source, preconditions and notes
  explain how those participants relate.

Solid diagram arrows run from derivation parent to child. Dashed arrows run
from the material being sealed to its recipient. **A dashed arrow is not a
derivation arrow:** do not traverse both as though they mean “possession of
the left gives possession of the right.” Prose-only recipients remain in the
register rather than becoming invented key nodes.

The `reachable` command follows **derivation edges only**. It answers that
narrow question usefully. To reason through an opening workflow, also consult
sealed records, required factor combinations, available ciphertext and the
relevant mutation. The command does not claim to simulate that ceremony.

## Revocation's baseline—not a recurring defect

Retained ciphertext plus its usable decryption key remains decryptable.
Delivered plaintext cannot be recalled. This is an explicit system assumption,
not an unresolved architectural flaw. Online release can stop a later handoff;
it cannot revoke information already handed over.

Assess revocation against its actual prospective promise: which subsequent
states, grants or releases exclude the removed party, and when honest writers
learn the change. Do not mistake a roster update for completion of every
downstream re-key operation.

## Keeping the map useful

Update registry entries when implementation evidence is unambiguous. Keep
unresolved design/implementation distinctions in `notes` rather than silently
choosing a new protocol. Regenerate the views from the same data file.

Validation checks structure. Lints compare enumerable code surfaces and resolve
anchors and model names. Generation tests keep the published views aligned
with the YAML. These are useful maintenance checks, not a proof that an entire
program implements its description. Model references lead to evidence with
specific assumptions; an absent reference is not itself a security finding.
