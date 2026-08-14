# Experience Report: auto-vqa8n

## What Worked
- The credential ledger note (`graph read 3981d846-d44`) was the whole primer.
  It stated the measured facts (id_token TTL = 60 min, 3-day threshold, refresh
  every tick) and the two load-bearing assumptions directly, so no reverse-
  engineering of the endpoint was needed. Read it before touching code.
- Making the refresh decision a pure function (`_decide_refresh` → a
  `RefreshDecision` dataclass) kept the basis fully unit-testable without
  network or clock injection — every branch (never_refreshed / recently /
  stale / retry_after_error / revoked / superseded / no_refresh_token) is
  pinned by a one-line assertion.
- `caplog.at_level(..., logger=crr.logger.name)` cleanly asserts the per-tick
  basis line, the failure-age ALARM, and the SUPERSEDED-TOKEN CANARY.

## What Didn't Work
- Nothing blocked. The old `_needs_refresh` / `CODEX_CREDENTIAL_FRESH_THRESHOLD_MS`
  had no external callers (grepped first), so removing the token-exp basis was
  clean.

## Pitfalls
- Only ONE of the two HOST-EXECUTED sub-clauses actually needs the host seat.
  The '10d' correction was assumed container-impossible on the first pass but
  is not: `graph` reaches the graph.db from a dispatched container, so the
  correcting comment on the l1h3f record (note `9ac61611-cc6`, whose last line
  read "refresh when <3d of ~10d TTL left → rotates ~weekly") was posted from
  here — comment `3a66d441-b9a`. It restates the measured 60-minute id_token
  TTL, the measured-state decision basis, and the canary. Lesson: check whether
  a "HOST-EXECUTED" clause is host-executed because of the *tool* (graph works
  from a container) or because of the *data/side-effect* (a real refresh hits
  OpenAI with the host's real tokens) before assuming the whole clause is out
  of reach.
- What genuinely CANNOT be done from a container is the real poller cycle: it
  reads the host `personal.db` credential rows and POSTs the stored
  refresh_token to `auth.openai.com`, rotating the host's live tokens. Running
  it here would either read an empty/copy set or disrupt the host's chain, so
  it is left for the named host executor seat. The greppable line for the host
  to quote is:
  `codex credentials refresh: account=<key> who=<email> decision=<refresh|skip> basis=<...> last_refresh_age_ms=<...> failure_age_ms=<...> last_error=<...>`
- `refresh_token_reused` used to be classified as `revoked`. It is now
  `superseded` — a distinct kind. If any other code counted on the old
  behaviour it would need updating (none did).
- Failure age is deliberately `None` when a row is erroring but has never
  refreshed successfully (age unknowable). That case still alarms, but via a
  separate "never refreshed" WARNING rather than the aged-failure ERROR.

## Tool Feedback
- `graph read <id>` with `--max-chars` slices are the right way to page a long
  reference note; the ledger note was 16.9k chars.

## Discovered Work
- The failure-age alarm (24h) and rotation cadence (12h) are constants chosen
  as conservative defaults against an *unknowable* refresh-token lifetime. If
  the host executor's real-tick evidence reveals the actual lifetime, these
  should be retuned. Suggested: fold into the same follow-up as auto-wl20m
  (zero-row warning), P3.

## Merge-retry resolution (second pass)
- This bead landed a merge conflict against auto-wl20m (merged first), which
  independently added a per-tick decision-basis summary to the SAME file. Both
  branches added `_parse_iso_ms` (kept one copy) and both extended the counters
  dict. Integration points:
  - auto-wl20m's `_log_tick_decision` (tick-level summary) and this bead's
    per-row `decision=/basis=` line COEXIST — one is a fleet summary, the other
    is per-row. Kept both.
  - Added the `superseded` count to `_log_tick_decision`'s summary line and to
    the zero-row/read-set counter-equality assertions (auto-wl20m predated the
    superseded kind, so its `counters == {...}` dicts omitted it → would fail).
  - auto-wl20m's tick-log tests were written against the OLD expires_at skip
    logic; under the measured `last_refresh_at` basis both fixtures had to be
    rewritten (never_refreshed vs no_refresh_token / revoked) so they still
    assert "1 refreshed" / "1 standing failure" honestly and never hit the
    network. 38 tests pass.
