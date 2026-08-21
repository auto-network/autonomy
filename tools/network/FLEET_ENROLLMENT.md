# Fleet invitation and initial enrollment

This is the implementation contract for the machine-neutral invitation channel
and the transition to roster-authorized personal-database synchronization. The
design authorities are `graph://0c655045-ee4` and `graph://e2de97e8-630`; the
operator confirmed the channel/request binding described here on 2026-08-21.
The ceremony/machine identity split is recorded at `graph://341a1761-a1f`.

## State transition

```text
published invitation (no machine)
  -> pending request (invitation + exact channel + enrollment nonce)
  -> approved authorization (root-signed roster record committed)
  -> delivered identity (existing armor + signed bootstrap on that channel)
  -> active fleet member (first roster-authorized proof-of-possession)
```

The invitation is a bearer capability to request, never to enroll. It is
root-signed and carried by a temporary auto.network RelayKit channel, but names
no machine. A joining installation contributes only a one-ceremony
``enrollment_nonce`` through that channel.

Both the installation and original Dashboard render a shared verification code
over the full request. Operator comparison of those displays selects the exact
request and channel being authorized. The enrollment nonce is discarded after
approval or decline; it is not a machine identity and never enters the roster.

After the comparison succeeds, trusted browser code opens the personal-root
armor, deterministically assigns the durable ``machine_id`` from the personal
root and approved request, derives the machine authorization key, and signs the
authorization evidence. The root seed is zeroed before the browser ceremony
returns; it never enters Dashboard's Python process. The roster stores the
derived public key; the new machine stores the assigned id so it can re-derive
the private half whenever the root is unlocked.

The personal root derives purpose-separated machine authentication and
encryption keys. Their public halves are authorized by the roster; their
private halves are derived locally after root delivery and are never sent or
stored independently.

The browser produces two root-signed, domain-separated records in one ceremony:

* a durable ``RosterEntry`` binding the assigned ``machine_id``, derived
  machine authorization public key, and its ``personal_root_holder`` standing;
  and
* a transient enrollment approval binding the server-frozen request, exact
  invitation channel, and durable roster entry id.

The server resolves the trusted root public key from
``autonomy.identity.personal`` rather than from either submitted record. It
verifies both signatures and their cross-binding, commits the roster entry, and
only then makes the existing armored personal root deliverable on the bound
channel. The transient approval and comparison nonce are discarded after the
ceremony; neither becomes roster identity.

The joining machine derives its durable ID locally from the root and approved
request, compares it with the public ID in the signed roster entry, and stores
it. The roster copy is evidence, not a secret credential; no private machine
key is delivered. Root delivery before authorization is forbidden. The
assignment is deterministic, so a post-commit disconnect is retryable without
creating another machine identity or requiring another operator decision.

The production ``authorize_request`` handoff accepts browser-minted signed
evidence, never a personal root or seed. It verifies that evidence against the
stored root anchor, persists the roster entry, and returns delivery material
only after that write succeeds. Seed-taking functions are confined to the
joining side, after unchanged armor delivery and local browser unlock.

The approval response also carries enough signed roster/bootstrap material for
the new machine to authenticate its first fleet connection. The invitation
channel then ends. Synchronization is admitted only after the new machine
proves possession of its derived authentication key on the separate fleet
channel.

Personal armor, passkeys, root material, and machine-local state never enter
the synchronization catalog. Vault ciphertext and signed key-control grants do.

## Failure rules

- A copied invitation may submit another request but cannot approve it.
- A decline or expiry writes no roster record and delivers no armor.
- Approval cannot be redirected to another channel using the same invitation.
- A roster entry alone is authorization, not live-peer authentication.
- No implementation may seal the root to the derived machine key: that key
  cannot exist before the root arrives.
- No server-side approval path may receive the personal-root seed or a root
  signing key.
- Browser and Python canonicalization/signing must share a fixed conformance
  vector proving browser-minted evidence verifies in the server.
