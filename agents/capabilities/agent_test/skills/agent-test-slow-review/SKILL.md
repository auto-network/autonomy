---
name: agent-test-slow-review
description: Review retained Agent Test timings and source code to rank slow tests by waste risk and test value without executing them. Use after a long suite, when investigating expensive tests, or when deciding what to optimize next.
---

# Agent Test slow review

Use this skill when a suite or individual test is unexpectedly expensive. This
is an investigation workflow, not a test-running workflow: never invoke
`agent-test run`, `pytest`, browser automation, or a test fixture while doing
this review.

## 1. Establish the machine context

Capture one point-in-time resource snapshot before interpreting a long run:

```bash
agent-test capacity
agent-test metrics
```

Record the machine limits and current usage, especially `tests` and
`browsers`. A long run with browsers unused is not browser-pool starvation;
an eight-worker run consuming eight of sixteen test slots is not machine
concurrency saturation. Treat a run's declared resources and the live lease
snapshot as evidence, not guesses.

## 2. Collect retained timing evidence

Use the organization-scoped Testing summary for the repository. The bounded
route returns recent runs, per-node timing ranks, sample depth, failure rate,
and flaky status. The bundled collector writes only the requested bounded
snapshot and never starts a test:

```bash
python3 agents/capabilities/agent_test/skills/agent-test-slow-review/scripts/collect_stats.py \
  --repository github-autonomy/auto-network/autonomy \
  --limit 25 \
  --output /workspace/output/agent-test/slow-review-stats.json
```

Use the slowest nodes as candidates, not conclusions. Prefer nodes with at
least three samples; label one-sample rankings as provisional. Compare median,
minimum, maximum, sample count, outcome mix, and whether the estimate was
complete. Never rerun a slow node to improve the evidence.

## 3. Inspect code without executing it

For each candidate, open the owning test and its fixtures. Look for:

- unconditional sleeps, polling loops, retry delays, and generous timeouts;
- browser or service startup per test instead of per module/session;
- subprocess, Docker, network, filesystem, or cryptographic work in setup;
- serial parameterization that could be isolated or parallelized safely;
- tests that assert a real multi-step contract and therefore have legitimate cost;
- duplicated coverage that another test already provides.

Read retained failures, tracebacks, coverage, and source only. Do not infer
test value from the name alone, and do not change code during the review.

## 4. Score candidates

Assign two independent integer scores from 1 to 5:

**Waste-risk score** — `1` means likely wasteful and `5` means the cost is
well-justified or already efficient.

**Test-value score** — `1` means little decision or regression value and `5`
means it protects an important, difficult-to-reproduce contract.

Include a short evidence sentence for each score. A slow integration test that
proves cross-organization isolation may be `5` waste-risk / `5` value. A test
that sleeps in a loop without asserting a meaningful contract may be `1` / `1`.
Do not use duration alone to assign either score.

Rank the review queue by optimization leverage: longest median duration first,
then waste-risk ascending, then test value descending. Keep the report bounded
to the requested limit (maximum 50).

## 5. Write the durable report

Write the review to `/workspace/output/agent-test/slow-test-review-<UTC date>.md`.
Use this table shape:

| Test node | Median | Samples | Waste risk (1–5) | Value (1–5) | Evidence and recommended investigation |
|---|---:|---:|---:|---:|---|

Start with a short resource snapshot and a conclusion about whether the suite
was resource-bound. End with three bounded lists: highest-priority fixes,
legitimate expensive tests to leave alone, and candidates needing better
measurement. Preserve the raw stats JSON beside the report.

This report is an analysis artifact, not a new test baseline or a failure
quarantine. Do not modify Agent Test history merely because a test is slow.
