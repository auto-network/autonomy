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
channel. The transient approval and comparison nonce remain only as retry state
until the first roster-authorized handshake proves completion; they are then
deleted and neither becomes roster identity.

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

## Invitation rendezvous wire

Fleet invitations use a RelayKit grant whose target type is ``fleet:join``.
They do not reuse ``org:join``: organization admission and personal fleet
authorization are different authority domains. The browser-signed invitation
names the exact ``https://.../l/<grant-token>`` rendezvous and is registered
against the grant before requests are accepted. Publication therefore happens
first; while the personal root is open, browser code verifies that the opened
seed matches the stored public anchor, signs that exact URL, emits the portable
installation code, and zeroes the seed on success or failure.

The first encrypted-channel message is:

```json
{"v":1,"op":"fleet.request","request":{"enrollment_nonce":"<hex>","personal_root_pub":"<hex>","invite_id":"<hex>"}}
```

The origin stores the complete request under a deterministic, domain-separated
content id and returns ``request_id``, the shared ``verification_code``, and a
random ``resume_token``. The raw resume token is returned only over the channel
that first created the request; origin storage keeps only its domain-separated
hash. The joining installation keeps the raw token in its own ``machine.db``
until enrollment completes, alongside the public request and invitation but no
machine id, machine key, or root armor. Repeating the same request on that live
channel is idempotent. Repeating it on another channel returns
``resume-required`` rather than minting another credential.

A reconnect proves continuity with:

```json
{"v":1,"op":"fleet.resume","request_id":"<hex>","resume_token":"<hex>"}
```

Before approval it receives only pending public state. After approval it
receives the transient signed approval, durable signed roster entry, and the
byte-identical encrypted personal-root armor. Unknown requests, wrong resume
tokens, unsupported versions, mismatched invitations, and expired grants fail
closed. Several requests may use one invitation, but their content ids,
channel bindings, operator comparisons, and approvals remain distinct.

The fresh-install client fetches the public registry envelope from the signed
rendezvous origin, verifies that its target is ``fleet:join``, and uses the
envelope's organization root only as the ordinary RelayKit serving-key pin.
That transport pin is not fleet authority: the invitation's personal-root
signature authenticates the enrollment destination, while the later roster
entry supplies durable machine authority. Public approval evidence is verified
before encrypted armor is handed to the local browser unlock ceremony.

The invitation and pending-request tables live in ``machine.db``. They are
local rendezvous state, not personal graph data, and therefore are excluded as
a whole from fleet synchronization. The operator API requires the human
Dashboard session: an authenticated agent bearer cannot register, approve, or
decline on the operator's behalf.

Personal armor, passkeys, root material, and machine-local state never enter
the synchronization catalog. Vault ciphertext and signed key-control grants do.

## Temporary tunnel-serving assignment

The production registry currently accepts only one serving tunnel per
organization. Until cooperative tunnel pools ship in ``auto-0rc9f``, one
active Fleet roster member is selected to run this person's auto.network
serving connectors. This is not a primary-machine role and it grants no Fleet
authority.

``autonomy.fleet.tunnel-server#1`` is a raw personal singleton containing the
selected durable ``machine_id``. Personal synchronization carries the row to
each Fleet Dashboard. The serving supervisor starts or retains connectors only
when the selection resolves to an active root-signed roster entry and equals
the machine-local ``autonomy.machine.identity`` row. A non-selected node stops
its connectors. A multi-member roster with no valid selection fails closed and
reports the assignment state instead of entering the registry's persistent
``4409`` replacement cycle.

A pre-Fleet installation with neither roster state nor machine identity keeps
legacy single-node serving. One initialized active member may serve implicitly.
Immediately before verified enrollment grows a one-member roster, the executor
materializes that same member as the explicit selection; adding a machine does
not accidentally turn off serving everywhere. Concurrent growth preserves the
same singleton value. A roster that is already multi-member and unassigned
remains failed closed rather than inventing a winner.
Any local machine identity or synced selection without its roster is partial
Fleet synchronization and fails closed; it is never treated as legacy.

This compatibility setting and every reader, writer, status, and test for it
are removed by ``auto-clune.7`` after real two-connector pool acceptance. At
that point all healthy eligible tunnels coexist and no roster machine is
designated to serve.

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
