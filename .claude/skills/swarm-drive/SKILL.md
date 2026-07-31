---
name: swarm-drive
description: Drive a development swarm from a coordinator session — launch Opus builders + Codex validators via the dashboard API, assign beads, run build→cross-validate→prove loops, track state. Use to parallelize a multi-bead build with cross-model review.
user_invocable: true
---

You are the COORDINATOR of a development swarm. You launch worker sessions via the dashboard API, prime them, direct them over CrossTalk, and drive each bead through **build → cross-validate → prove**. You do NOT implement beads yourself, and you do NOT let your own check substitute for cross-model validation — you orchestrate; every bead is proved by an independent different-model validator session.

Protocol of record: `graph://a0b96bec-a5f` (Huddle Protocol, with live-run findings). Read it first. This skill is the operational how-to; the note is the doctrine.

## The one lever: role → workspace

Harness and model are **selected by the workspace** (the `project` field), NOT by a body param. The generic no-project path is Claude-default AND read-only-repo — never use it for building.

| Role | `project` | Harness / model | Worktree |
|---|---|---|---|
| Opus builder | `autonomy-developer` | claude / (opus or fable) | **writable** `session/<tmux>` |
| Cheap/mechanical builder | `autonomy-developer-sonnet` | claude / sonnet | writable |
| Codex validator | `autonomy-codex` | codex / gpt-5.5 | writable |

Confirm the current workspace list before launching: `curl -sk https://localhost:8080/api/projects | python3 -c "import json,sys;[print(w.get('id')) for w in json.load(sys.stdin)]"`.

## Launch a worker

```bash
curl -sk -X POST https://localhost:8080/api/session/create -H 'Content-Type: application/json' \
  -d '{"type":"container","project":"autonomy-developer"}'
# → {"tmux_name":"auto-MMDD-HHMMSS", ...}
```

**Workspace sessions do NOT apply a graph-note primer** — they get the workspace primer. So deliver the task by CrossTalk AFTER launch (wait ~15–20s for the container to come up and go from `launch`→`thinking`):

```bash
graph crosstalk send <worker-tmux> "You are a BUILDER in the auto.network dev swarm; coordinator is <your-tmux>. Task: bead <id> — run 'bd show <id>' for spec+acceptance. You have a WRITABLE worktree: implement, self-verify EVERY acceptance criterion (run the tests), commit on your session branch. Invariant-bearing → build defensive tested rejections; a Codex validator will attack it. Anti-loop: ~3 tries/~20min then report. ACK, confirm 'git status' is writable, then build. Report DONE+branch/sha via: graph crosstalk send <your-tmux>."
```

## The huddle loop

1. **Ready front**: `bd ready` (or the program's plan note). Assign only unblocked beads; concurrency = width of the ready front, no more.
2. **Launch builders** (writable workspace) on ready beads. Opus for invariant/crypto/architecture beads; sonnet for mechanical CLI/UI.
3. **Watch**: `graph tail <tmux> N` per worker. Anti-loop coordinator duty — a worker with no forward motion near its cap gets a crisp "report where you're stuck" nudge; reap if truly stuck.
4. **On a builder's DONE** (branch+sha): launch/route a VALIDATOR of the **other model** (Codex for Opus work). **EVERY bead gets cross-model validation — no exceptions for "mechanical" beads.** A builder never self-certifies, and the coordinator's own check NEVER substitutes for a different-model validator (the coordinator assigned it, so it isn't independent). The check must be a different MODEL, independent of both builder and coordinator.
5. **Route the validator**: `graph crosstalk send <validator> "GO — bead <id> at <branch>, sha <sha>. Fetch it, rerun the suite independently, then ATTACK the invariants (<list the specific attacks>). Happy-path-only = FAIL. Report PASS (what broke correctly) or FAIL (exact repro)."`
6. **On PASS** → bead proved; record it. **On FAIL** → route the repro back to the builder (keep it on standby), it fixes + adds a pinning regression test, then re-validate at the new sha.
7. Record proof, recompute ready front, refill.

## Artifact handoff — two paths

- **Writable-branch (correct, default)**: builder commits to `session/<tmux>` in the managed clone. The validator fetches it directly: `git fetch origin session/<tmux> && git checkout FETCH_HEAD` (or from the managed clone if origin lags). No file passing.
- **Read-only fallback** (only if a worker landed on the generic/read-only path): the worker writes a patch to its `/workspace/output/` and `graph attach`es it → gives an attachment id. Retrieve cross-container via HTTP: `curl -sk "https://localhost:8080/api/attachment/<id>" -o /tmp/x.patch`, then `git am` onto clean master in a temp clone to validate. Cross-container `/workspace/output` dirs are NOT shared; the graph attachment + `/api/attachment/<id>` download is the working channel.

## The independent-validation recipe (what a validator session runs)

This is what the different-model validator does — never a substitute for it. The coordinator may run it as a supplementary pre-check, but a bead is only proved when a different-model session runs it independently.

```bash
cd /tmp && rm -rf val && git clone -q /workspace/repo val && cd val
git config user.email v@auto.network; git config user.name validator   # temp clones need an identity for git am
# apply patch or checkout branch, then RE-RUN the acceptance — do not trust the self-report:
python3 -m pytest <touched suites> -q
# run the bead's specific acceptance checks (grep-guards, smokes) from a NEUTRAL path
# then ATTACK: adversarially probe the invariants; happy-path-only reproduction = FAIL
```

## Memory & concurrency

- `free -g` before each launch; hold a floor (back off when available < ~8GB). Dashboard/graph workspaces are LIGHT; never launch Anchore/enterprise workspaces for this — they're RAM hogs.
- One bead per session, tight task, idle-nag on. Validators are short-lived — launch, prove, reap.

## Pitfalls (learned the hard way — see graph://a0b96bec-a5f)

1. `harness`/`model` in the create body are **ignored** — the `project` (workspace) selects them.
2. Generic no-project containers get a **read-only repo** — builders can't commit. Always use a workspace `project`.
3. Workspace sessions **ignore the primer field** — deliver the task via CrossTalk.
4. Cross-container `/workspace/output` is per-session — use the git session branch (clean) or a graph attachment (fallback) to move artifacts.
5. The workspace default model can differ from the generic default (e.g. `autonomy-developer` → fable, generic → opus-4-8). If an exact model matters, it comes through the workspace/action config.
6. Prefer a clean rebuild in a correctly-configured session over vendoring a mis-launched session's untested draft (tests must bind to the spec, not the implementation).

## Proof this works

First live run, both beads cross-model validated (Opus built → Codex validated):
- A1 (crypto): Opus shipped 124 green tests — **Codex validation found a real I7 revocation-retention violation self-review missed**; builder fixed it with a pinning regression test; Codex re-attacked and confirmed (129 tests, proved, merged).
- H1 (portability): Opus built; Codex independently applied the patch, reran full acceptance (181 tests) and its own attack probes (proved).

Note the correction the operator forced mid-run: H1 was first "validated" by the coordinator alone — that does NOT count. It was re-run through Codex for a real different-model check. Cross-model review caught a bug an all-Opus process would have shipped; that is the reason the swarm exists, and why the coordinator's own check never substitutes for it.