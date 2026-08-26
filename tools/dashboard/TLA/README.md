# TLA+ model — dashboard rollout ingestion (bead auto-suvcp)

Formal model of the per-file rollout ingestion FSM and its drain-ownership
protocol.  `MODEL.md` is the abstraction ledger — read it before trusting
any green run.  Method and provenance: the Anchore job-framework model
(graph notes `c9258052-67e`, `523f9406-6ba`).

## Run

```bash
# toolchain: a JRE/JDK plus tla2tools.jar
#   default locations: ~/tools/jdk-*/bin/java and ~/tools/tla2tools.jar
#   overrides: TLA_JAVA, TLA_TOOLS_JAR
python3 tools/dashboard/TLA/run_tlc.py              # full suite (~10 min)
python3 tools/dashboard/TLA/run_tlc.py GreenCore    # one config
```

The suite passes only when every green configuration checks clean AND
every calibration configuration fails with a real violation.  The runner
greps for explicit violation markers, so a crashed or unparseable
calibration reads as BROKEN — never as "failed as required".

## Layout

- `RolloutIngestion.tla` — the model: all state, actions, switches,
  invariants, temporal properties.
- `Scen2F.tla` / `Scen3M.tla` — scenario root modules (main + sibling
  subagent; three-file rollover chain) injecting the constant functions.
- `*.cfg` — green configurations (must be clean).
- `calibration/*.cfg` — one restored broken design each (must fail).
- `run_tlc.py` — runner with the must-pass/must-fail gate.

## The honesty rule (the change rule)

Any change to `session_monitor.py` / `session_harness.py` that adds or
reorders an observation path, changes classification or promotion,
changes drain ownership/persistence order, or changes reconciliation
behavior **must change `RolloutIngestion.tla` in the same commit**, and
`run_tlc.py` must pass.  When you fix a bug in this machinery, add a
calibration switch that restores the broken behavior and verify TLC
rediscovers the failure on its own — that is what keeps a green run
meaning something.

## Implementation requirements the model ASSUMES (cannot check)

- The claim (`request_drain`) and the final dirty-check/release sequence
  are actually await-free (bead invariants 4/7), including the CPython
  try-claim fast path.
- Publication is publish-then-persist (`PersistOrder = "processFirst"`);
  crash-window duplicates are operator-accepted and bounded
  (`BoundedDuplicates`), with two proven no-failure-duplicate rules:
  the handover advances the linked file's offset, and linking never
  rewinds publication (see MODEL.md, "A4 adjudication, superseded").
- (wd, epoch) joint identity on every inotify dispatch (host review B6;
  wd identity is abstracted here).
- Deadline/interval sizing (the model proves "eventually", not "within
  deadline + one tick").
- Poller promotion VALIDITY.  `PollerSet` is an unconstrained
  environment action: the model proves the drain machinery safe under
  arbitrary composer promotion, but cannot check that the poller only
  promotes when the CURRENT process's composer is actually ready.  The
  grace-fallback timer reset in `arm_startup_state`
  (`_harness_ready_grace` / `_screen_stuck_since` / `_self_repair_filed`
  cleared at every launch entry) is therefore an implementation
  requirement — a stale expired timer from a failed attempt instant-
  promoted the next attempt (auto-0821-154759).
- Per-window read size caps (`_TAIL_WINDOW_MAX_BYTES`) need no model
  change: a capped complete-line read is indistinguishable from a read
  that ran before the writer's last appends, an interleaving the model
  already contains.
