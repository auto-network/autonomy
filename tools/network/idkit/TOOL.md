# idkit — auto.network identity crypto primitives

Pure Python library (no service, no UI, no I/O) providing the cryptographic
vocabulary for auto.network identity. Shared foundation for the dashboard
(Track C) and the registry service (Track B) — dependency-light by design:
**`cryptography` is the only dependency**, so the registry repo can vendor
or pip-install it.

Spec: graph note `a17c8657-939` §2, §3, §7 (invariants I2, I4, I7).
Design thread: `e86f2203-89f`. Bead: `auto-o2b2g` (A1).

## What it provides

| Primitive | Module | Summary |
|---|---|---|
| `KeyPair` | `keys.py` | Ed25519 generate/sign/verify; key id = hex of the raw public key |
| `DelegationCert`, `Subject`, `issue_cert` | `certs.py` | Canonical-JSON-signed delegation statements with the parent chain embedded |
| `verify_chain` | `verify.py` | Full chain verification down from an org root public key |
| `generate_token` | `tokens.py` | 128-bit CSPRNG share-link tokens, hex |
| `RevocationRecord`, `issue_revocation`, `verify_revocation`, `RevocationSet` | `revocation.py` | Root-/parent-signed denylist entries with bounded retention |
| `canonical_json` | `canonical.py` | The single byte form every signature covers |

## Usage

```python
from tools.network.idkit import (
    KeyPair, Subject, issue_cert, verify_chain, generate_token,
    issue_revocation, verify_revocation, RevocationSet,
)

root = KeyPair.generate()                      # org root (ceremony-held)
session = KeyPair.generate()                   # operator sign-on
session_cert = issue_cert(
    root, session.public_hex,
    scope=("delegate:agent", "link:publish", "link:revoke"),
    org=org_uuid, subject=Subject("operator", "op-1"),
    not_before=now, not_after=now + 86_400,
)

agent = KeyPair.generate()                     # narrow promptless delegate
agent_cert = issue_cert(
    session, agent.public_hex, scope=("link:publish",),
    org=org_uuid, subject=Subject("agent", "sess-42"),
    not_before=now, not_after=now + 3_600,
    parent_cert=session_cert,                  # chain travels with the cert
)

# Registry side: nothing but the bound root pub is needed (I4).
result = verify_chain(
    agent_cert, root.public_hex, org=org_uuid,
    revocations=revocation_set, required_scope="link:publish",
)

token = generate_token()                       # I2: takes NO inputs at all
```

## Verification rules (the security boundary)

`verify_chain` enforces, for every hop, in order: **signature** (domain-
separated, against the parent key — org root for the first hop) →
**org match** → **time validity** (one expired ancestor kills the chain) →
**strict narrowing** (child scope a *strict subset* of the parent's;
validity window nested with a strictly earlier `not_after`; `target_types`
never escalating — spec §3) → **revocation** (any revoked key id anywhere
in the chain rejects it).

Every rejection raises a distinct exception (`errors.py`), so callers can
distinguish *expired* from *escalating* from *revoked* from *forged*.

Revocations verify against the org root; a delegated key may revoke **its
own descendants only** — the verifier demands the revoked key's cert chain
as proof of descent. `verify_revocation` requires the revoked key's cert
for **every** issuer shape (root included): it is the only trustworthy
source of the key's natural `not_after`, and the record's mandatory
`expires_at` must not outlive it. `RevocationSet.purge_expired` drops dead
records (I7).

## Format notes

- **Canonical JSON**: sorted keys, no whitespace, ASCII-only, no floats;
  scope/target_types lists must be sorted and duplicate-free. One accepted
  byte form per object → deserialize/re-sign is bit-identical (Ed25519 is
  deterministic).
- **Anti-malleability**: `from_json` (certs and revocation records) accepts
  exactly the canonical wire bytes — reordered keys, whitespace, unicode
  escapes, and duplicate JSON keys are rejected, not normalized.
  `from_dict` remains available for already-parsed objects.
- **Domain separation**: cert signatures cover `CERT_DOMAIN || payload`,
  revocations `REVOCATION_DOMAIN || payload` — no cross-kind replay.
- **Strict parsing**: unknown fields, wrong types, non-canonical lists and
  out-of-range timestamps are rejected; chain depth is capped
  (`MAX_CHAIN_DEPTH = 16`) before any signature work.

## Tests

```bash
pytest tools/network/idkit/tests/
```

Acceptance is test-pinned (bead `auto-o2b2g`): valid root→session→agent
chain accepted; expired hop / scope escalation / revoked key / wrong org /
tampered signature rejected; tokens 128-bit hex with no derivable inputs
(I2 by construction — the generator takes no arguments); revocation
authority + I7 retention; canonical round-trip with bit-identical re-sign.
