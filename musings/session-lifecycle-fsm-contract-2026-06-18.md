# Session lifecycle — the plan (simple)

Co-design: host `host-0610-102410` (Claude) + `auto-0614-191459` (Codex).

## The problem
Session startup/teardown runs on the dashboard's web thread (the asyncio loop), so
git-clone, docker-run, and waiting **freeze the whole API**. Four different places
write `startup_state` to the DB → they **race**. No step has a timeout → a failed
start sits `is_live=1` **forever** (the zombie). Proven 2026-06-18: starting a
BlindHash Operator zombied, hung `recent_sessions` to 30s, and got stuck unaware.

## The fix
**One background worker thread runs the whole lifecycle.** Web handlers only enqueue
jobs and read state — both instant, so the API never blocks. Every step has a
timeout. The worker is the only thing that writes state.

## The states (FSM)
```
requested → preparing → launching → setup → waiting_ready → injecting → running
   (any step times out / errors) → failed(phase, reason)
running → stopping → cleaning → dead
retry = cleanup pass, then re-enqueue
```
The worker still sets the existing granular `startup_state` values for the UI chip as
it passes through `waiting_ready` (harness_starting → composer_ready).

## The worker (core)
- One thread + a `queue.Queue`, started in `_on_startup`.
- Job = `(action, tmux, config)`, action ∈ {start, stop, retry}.
- Loop: `job = q.get(); _run_start(job)` or `_run_stop(job)`.
- `_run_start` is the step-list below; each `fn` is a normal blocking call **on this
  thread** (not the loop), each with a deadline. On failure: `set_state(failed,
  phase, reason); return` — never stuck.

```python
STEPS = [("preparing", prepare_worktrees,    120),
         ("launching", launch_container,       60),
         ("waiting",   wait_for_prompt,        60),   # poll screen, real detection
         ("injecting", inject_echo_verified,   30)]
for name, fn, timeout in STEPS:
    db.set_state(tmux, name)
    try:    fn(job, deadline=timeout)
    except (TimeoutError, StepError) as e:
        db.set_state(tmux, "failed", phase=name, reason=str(e)); return
db.set_state(tmux, "running")
```

## The steps (mostly EXTRACTED from today's create handler — the work already exists)
- `prepare_worktrees` — existing `prepare_session_mounts` (git clones/worktrees).
- `launch_container` — existing `launch_session` + the tmux spawn.
- `wait_for_prompt` — poll the screen until composer_ready (**real detection, not the
  grace timer**); set harness_starting/composer_ready as it goes.
- `inject_echo_verified` — paste → confirm the `❯` input line is non-empty → Enter →
  confirm; retry on drop. **This is the zombie fix.**

## One writer (collapse the races)
- screen-poll loop: stop calling `advance_startup_state`; expose `get_screen_state(tmux)`
  that `wait_for_prompt` reads.
- setup-watcher: stop writing state; expose the `.setup-exit` signal.
- injection: becomes the `injecting` step on the worker.
Only the worker writes state.

## The API (never blocks)
- `api_session_create`: validate → write row `state=requested` → `q.put(job)` → return 202.
- reads: serve the DB row (fast).
- stop / retry: `q.put` → return `accepted`.
- (One worker serializes startups; if several must *run* in parallel, make it a small
  pool, e.g. 4 threads — the API stays instant either way.)

## recent_sessions (SEPARATE bug)
Its 30s is its own problem — per-row `Path.exists` / org-DB fanout on the request path.
Exact slow line not yet pinned (no guessing). Fix is the same principle: refresh it in
the background, reads return the cached result.

## Verify (the watchdog is the judge)
Create AND tear down a blindhash-operations workspace while probing:
- **0 `EVENT-LOOP STALL`** the whole time.
- `ping` / `recent_sessions` / `design` / `worktrees` all **< 150ms** during create AND teardown.
- session reaches **running** OR **failed(reason)** — never stuck.
- stop/retry return immediately; teardown completes to **dead** without hanging.

## Who does what
- **Peer (worktree)**: build the worker thread + extract the 4 steps + rewire
  create→enqueue + collapse the writers + teardown/retry.
- **Host (me)**: the state list + timeout budgets, wire the worker into `_on_startup`,
  fix the trace regression (`32dccb5` bypassed it), pin + fix `recent_sessions`,
  live-verify with the watchdog.

## Agreed correctness additions (peer, 2026-06-18 — required, still minimal)
1. **Deadlines must be enforced INSIDE each blocking step, not just around the call.**
   One worker thread can't preempt a stuck subprocess. So each step helper is
   deadline-aware: `subprocess.run(..., timeout=)` for git fetch/clone and docker/tmux,
   poll-until-deadline for `wait_for_prompt`, retry-until-deadline for inject. If a
   helper can't take a deadline, modify it now — else one hung step wedges the worker.
2. **Bounded queue + `put_nowait` from handlers.** If full → return 503/backpressure
   instantly; request threads never wait on queue space.
3. **Startup recovery** in `_on_startup`: any row left in a non-terminal state
   (requested/preparing/launching/setup/waiting_ready/injecting/stopping/cleaning) on
   restart → mark `failed` or enqueue cleanup. Otherwise a restart recreates the
   "stuck forever" class even with one writer.

## Explicitly cut (keep it minimal)
- No process-memory snapshot for now — a simple indexed DB row read is fine.
- No worker pool until one worker proves too serialized.
- No background writer besides the worker (trace/DB/event-bus all emitted from worker transitions).

## FINALIZED state/persistence contract (host, 2026-06-18 — build against this)
Reuse existing `tmux_sessions` columns + ONE new column (`lifecycle_detail`, shipped `b87a270`):
- `startup_state` (UI chip, forward progress — worker writes these exact values):
  `requesting → preparing_workspace → launching_container → setup_running → harness_starting → composer_ready → awaiting_first_response → NULL`(running)
- `is_live`: 1 while starting/running/stopping/cleaning; 0 when dead/failed.
- `activity_state`: `running | stopping | cleaning | failed | dead`.
- `lifecycle_detail` (nullable JSON, set on failure/cleaning):
  `{failed_phase, reason, retryable, attempt, last_progress_at}`.
- **Coarse lifecycle_state is DERIVED** for the API (no extra column): STARTING / RUNNING / TEARING_DOWN / FAILED / DEAD from the four columns above.

**Timeout budgets** (enforce INSIDE each step): preparing 120s · launching 60s · setup 600s ·
waiting_ready 60s · injecting 30s · cleaning: stop 30s / watchers 5s / deregister 5s / worktree 60s.

## Phases
1. Worker skeleton + `set_state` writer, wired into `_on_startup` (+ recovery).
2. create → enqueue; extract the 4 steps onto the worker.
3. Collapse the writers (screen-poll/setup-watcher → readable signals).
4. Teardown + retry through the worker.
5. `recent_sessions` background cache.
6. Verify with the watchdog (both).
