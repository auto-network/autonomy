# Relay tunnel ownership — abstraction ledger and findings

## Source machine

The model follows two source paths:

- `registry/relay.py::TunnelHub.register` installs the latest authenticated
  tunnel in an org-keyed dict and returns the displaced tunnel. The endpoint
  then awaits `close(4409)` on that displaced WebSocket. Its `finally` calls
  `unregister`, which deletes only when the dict still contains that exact
  tunnel object.
- `relaykit/connector.py::TunnelConnector.run` retries every close. A
  successful hello resets backoff to the minimum before `_serve` waits for
  the next close.

`Register`, `EvictPrevious`, `HelloOK`, `CleanupClosed`, `HandleClose`, and
`Tick` are separate actions at those observable/await boundaries.

The same ownership shape also exists in `relaykit/peer.py::_handle_park`:
last-writer replacement keyed by node public key, close code 4409, and the
same inherited `TunnelConnector.run` retry loop. The one-key state machine
therefore applies to duplicate peer-park connectors as well, although the
incident configuration names the registry org key.

## Load-bearing model decision

One org is sufficient because hub ownership is keyed independently by org,
but the configured connector set is not unique per org. `CurrentLivelock.cfg`
maps both `old` and `new` to `org1`; the three-connector restart scenario maps
all three to it. This deliberately models the deployment state rather than
the intended process topology.

## Fairness

Connector retry loops, timer progress, and server endpoint continuations are
weakly fair. Relay restart is an unfair, bounded environment action. Without
fair retry/timer actions, an infinite stutter could satisfy stability by
simply refusing to run the connector and would conceal the defect.

## Results

The checked configurations answer the incident questions:

1. **Current behavior does not converge.** With two same-org connectors,
   TLC finds an infinite lasso: one hello succeeds and resets its backoff;
   the other retries, registers, closes it with 4409, succeeds, and resets;
   then the first repeats. Jitter changes which connector wins each turn,
   not whether another turn occurs.
2. **Recognizing 4409 and permanently standing down does converge** under
   the modeled assumptions, including one relay restart. This is the only
   tested one-line-class candidate that makes `EventuallyStable` true.
3. **A finite hard backoff does not converge.** Any policy that eventually
   retries every replaced connector permits the same lasso at a slower rate.
4. **Stand-down does not select newest code.** Last-writer-wins has no version
   order. TLC finds a behavior where `old` registers last, `new` stands down,
   and stale code owns the org forever. Stand-down also removes failover from
   the losing process until that process is restarted; permanent process loss
   is outside this model.
5. **A successful hello does not certify current ownership.** The split
   endpoint actions admit this real interleaving: connector A registers;
   connector B registers and becomes the dict holder; A's already-pending
   `{ok:true}` send completes and resets A's backoff; B then closes A. Thus
   `connected` is an availability hint, not a linearizable ownership claim.
6. **Split register-then-evict does not itself empty the hub.** The newly
   registered tunnel is installed first, and identity-guarded cleanup of the
   loser cannot delete it. `HolderHasOpenSocket` and `NoUncausedVacuum` hold.
   A relay restart intentionally clears the in-memory hub and is the modeled
   source of a temporary no-holder state; fair retry restores availability.
7. **Jitter does not guarantee herd avoidance.** It is modeled as a
   nondeterministic delay range. Equal draws are permitted, and TLC finds a
   restart behavior in which multiple same-org retries become ready together.
   Jitter may reduce collision probability operationally, but it is not a
   correctness mechanism.

## Deliberate abstractions

- Tunnel payloads, viewer channels, authentication cryptography, and orgs
  without duplicate connectors are omitted; they do not affect ownership.
- A connector has at most one current WebSocket attempt. The server endpoint
  may continue its pending close after that socket is itself displaced, but
  connector redial waits for endpoint cleanup in the model. The production
  identity guard makes late cleanup ownership-neutral.
- `RelayRestart` is an atomic stop/start boundary. It retains existing sleep
  timers and schedules ordinary retries for sockets live at the boundary.
  Outage duration and failed TCP dials while the relay is down are omitted.
- Random jitter is nondeterminism, not probability. The model proves or
  refutes guarantees; it does not estimate incident frequency.
- Permanent connector process death and operator process restarts are not
  modeled. They matter to the availability tradeoff of permanent stand-down
  and must be considered before adopting it.
