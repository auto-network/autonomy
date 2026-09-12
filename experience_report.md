# Experience Report: auto-4d6qm

## What Worked
- The existing `JoinSession`, RelayKit browser core, and organization-side join dispatcher composed cleanly once the page stopped using the server-side resolver.
- Injected RelayKit tests made it straightforward to prove the exact `linkPub` handshake contract and operation ordering without duplicating protocol code.
- Focused `agent-test` selectors caught stale root-pinned expectations and preserved the fragment-only handoff contract.

## What Didn't Work
- The `agent-test` command was not on `PATH`; invoking `./tools/agent_test/agent-test` was required.
- A broad changed-file run selected hundreds of unrelated route tests and exposed pre-existing parallel state interference in network identity/email tests. Focused selectors were stable.

## Pitfalls
- Relay routing origin must travel separately from fragment secrets. Falling back to the fixed production relay silently breaks pasted links from another deployment stage.
- RelayKit handshake errors do not all carry a typed error kind. Signature and malformed `SERVER_HELLO` failures need classification at the channel-factory boundary so they are not reported as ledger closure or ordinary unreachability.
- Public envelope identity fields must never be rendered. Only the authenticated `context` reply may populate human presentation.

## Tool Feedback
- Put the repository `agent-test` launcher on the dispatched shell's `PATH`, or print its repository-relative path in the pytest refusal message.

## Discovered Work
- The broad suite showed order-sensitive failures in network identity and invite-email tests when run with the full changed-file plan; suggested P3 test-isolation investigation. The focused invitation selectors pass.
