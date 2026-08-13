# Relay tunnel pools — abstraction ledger and findings

This model covers the guaranteed auto.network fallback path. The system's
preferred data path is direct viewer-to-dashboard connectivity after the tiny
bootloader/rendezvous phase. A successful direct session never enters this
relay-pool state machine; a failed or lost direct session does.

## Intended machine

There is no singular owner of an org. Every successfully authenticated tunnel
is a member of that org's pool:

- registration adds the exact tunnel;
- disconnect removes the exact tunnel;
- new viewer admission chooses a least-active-channel member with remaining
  capacity;
- an admitted viewer stays pinned, so changing load does not cause migration
  or tunnel competition;
- loss of one member reopens only the viewers pinned to it;
- after a relay restart, every connector retries and rejoins the pool.

Registration proves authentication, not retry health. `Register` preserves the
connector's accumulated backoff. `MarkStable` is the separate abstraction of
one authenticated `MaxBackoff` service interval; only a later disconnect of a
stable tunnel uses `MinBackoff` for its next retry. A connector that repeatedly
registers and disconnects before `MarkStable` therefore reaches and stays at
`MaxBackoff` instead of resetting to the minimum on every hello.

The current `TunnelHub` dict assignment and 4409 replacement behavior remains
in the model only behind `PoolRegistration = FALSE`. That negative
configuration is a calibration: TLC must rediscover the production lasso or
the model/runner is not trustworthy.

## Load-bearing initial state

`Init` starts every configured tunnel as an independent connector. Multiple
connectors for one org are normal capacity, not an exceptional deployment
state. Assuming connector uniqueness would erase both the incident and the
desired pool behavior.

`Capacity[t]` is concurrent viewer capacity on a tunnel. `OpenViewer` chooses
a member with minimum `Load(t)` among those below capacity. Ties are
nondeterministic; no tie-breaking protocol is required for correct shedding.

## Anycast and multiple relay servers

`TunnelRelay[t]` records the relay node on which an outbound connector
terminated. `ViewerIngress[v]` records the node selected for a viewer, such as
by an anycast route. Admission deliberately ranges over the complete org pool,
not only tunnels terminating at the ingress node.

`AnycastPoolGreen.cfg` makes that assumption observable: the viewer enters
`r2`, the only tunnel terminates on `r1`, and eventual viewer admission still
has to pass. Therefore an implementation needs one of:

1. a shared/replicated directory from org to live tunnel endpoints, followed by
   an internal relay-to-relay hop; or
2. equivalent routing that brings viewer and selected tunnel to the same
   forwarding process.

Anycast alone supplies neither. The model treats lookup/handoff as atomic and
reliable; it does not claim a particular directory design, consensus system,
or cross-relay transport is already implemented.

Multiple connector sockets to the same anycast name may terminate on different
relay nodes, but ordinary ECMP does not guarantee diversity. Diversity and
node steering are deployment questions outside this state machine.

## Fairness

Connector registration/retry, timer progress, and viewer admission are weakly
fair. Tunnel disconnect and relay restart are bounded, unfair environment
actions. Without fair connector actions, an infinite stutter could hide a
broken retry path; without bounded failures, eventual stable service would be
unprovable for any algorithm.

## Checked results

1. **Healthy same-org tunnels coexist.** Registration is set insertion and
   never evicts a healthy peer (`NoHealthyEviction`).
2. **Admission naturally sheds load.** A viewer cannot be assigned to a tunnel
   while another available member is less loaded (`AdmissionUsesLeastLoad`).
   Ties remain free and viewers are not rebalanced after admission.
3. **Capacity is additive.** Every tunnel retains its own cap and the pool can
   admit up to their sum (`CapacityRespected`).
4. **Failure is local.** Disconnect removes exactly one tunnel and only its
   assigned viewers return to admission. Other assignments remain unchanged.
5. **Restart recovers.** After the bounded restart/disconnect budget is spent,
   all connectors eventually rejoin and all provisioned viewers reopen.
6. **Cross-relay admission is permitted.** An anycast ingress can use a tunnel
   terminating elsewhere; the necessary directory/handoff is an explicit
   implementation obligation.
7. **Current last-writer replacement still livelocks.** TLC's calibration
   trace alternates the two connectors forever: each retry displaces the other.
   Stability-gated backoff bounds the attempt rate but cannot repair singular
   ownership; cooperative pool membership removes the cycle.
8. **Arbitrary admission permits avoidable skew.** TLC finds a trace where a
   new viewer chooses a loaded tunnel while an idle member exists.
9. **Hello alone never resets retry health.** The transition that authenticates
   a tunnel leaves `backoff` unchanged. Only a service interval represented by
   `MarkStable` earns a minimum-delay reconnect after later loss. This preserves
   prompt ordinary recovery without letting a post-hello flap hammer the relay.

## Deliberate abstractions

- Tunnel authentication is represented by eligibility to take `Register`; key
  verification and certificate details do not affect pool membership after a
  hello succeeds. Useful lifetime is reduced to the separate `MarkStable`
  transition; the model does not count wall-clock seconds.
- Viewer payloads, encryption, stream retention, and byte backpressure are
  omitted. `PERFORMANCE.md` treats those implementation costs separately.
- A viewer is assigned once per socket. Transparent live migration is not
  modeled and is not required by the algorithm.
- Selection observes active viewer count atomically. Distributed implementations
  can use slightly stale load without compromising membership safety, but the
  model does not quantify skew from stale observations.
- The cross-relay directory/handoff, partitions between relay nodes, and stale
  membership leases are not modeled. They require a later distributed-system
  model before an anycast deployment is claimed safe.
- Jitter is nondeterminism, not probability. It can spread reconnect load but
  is not an ownership or admission correctness mechanism.
