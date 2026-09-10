# Agent Test

Agent Test is the supported agent-facing interface for pytest. Runs are
asynchronous, complete evidence is kept privately under `/tmp`, and every query is bounded.

```bash
agent-test run tools/graph/tests/test_ops.py
# returns immediately; keep working until the completion notification arrives

agent-test status
agent-test doctor
agent-test profiles
agent-test plan
agent-test run --changed
agent-test collect tools/graph/tests
agent-test inventory <run-id> --match parser
agent-test validate tools/graph/tests/test_ops.py::test_name --run <run-id>
agent-test failures <run-id>
agent-test rerun-failures <run-id>
agent-test trace <run-id> 1
agent-test output <run-id> --limit-lines 40
agent-test retain <run-id>
agent-test coverage <run-id>
agent-test baseline <run-id>
agent-test capacity
agent-test metrics
agent-test timings tools/graph/tests/test_ops.py::test_name
agent-test stop
```

Do not poll a live run. Do not start another run. Do not pipe Agent Test through
`head`, `tail`, or `grep`; query the temporary evidence instead. Use `retain`
only when the raw run should outlive the session. Organization statistics are
stored separately in Settings and do not depend on raw artifact retention.
`status` reports a history-based ETA for a live run when timing samples exist;
use it once when needed, then wait for the completion notification.

The resident supervisor launches isolated run process groups. Completion is
delivered through the dashboard's typed task-notification endpoint for both
Claude and Codex; self-CrossTalk remains a compatibility fallback.

The machine capacity coordinator, duration history, and notifications all
live on the dashboard. Its address resolves from `AGENT_TEST_DASHBOARD`,
then `GRAPH_API` (what the session launcher exports, and the only name that is
right in a compose session, where localhost has nothing listening), then
`https://localhost:8080` for a host terminal. The CLI hands that address, the
session identity, and the bearer to each worker at start, so a value exported
after the supervisor came up still reaches the next run. A run that ended in
`error` or `stopped` never judged the code, so it does not trip the
unchanged-run guard; only a `passed`, `failed`, or `collected` run does.

`agent-test doctor` finds a project virtualenv and invokes its interpreter
directly. Agents do not source activation scripts, and Agent Test never
installs dependencies. Named profiles can be declared in `pyproject.toml`:

```toml
[tool.agent-test]
python = ".venv/bin/python"
quarantine = "tests/quarantine_baseline.txt"

[tool.agent-test.profiles.smoke]
selectors = ["tools/graph/tests/test_ops.py"]
pytest_args = ["-q"]
resources = { tests = 4, browsers = 1 }
```

An unchanged selection against unchanged code is refused. Use retained
evidence, edit the code, or use `rerun-failures` to run only failed nodes.
Failures are labelled `new`, `known`, or `quarantined` from retained history
and the configured quarantine baseline.

Before pytest starts, the background worker acquires its declared resources
from a machine-store lease ledger shared by every container. The default
machine capacity is 16 test slots and 4 browser slots. Runs queue without
blocking the agent when capacity is exhausted; leases renew while running and
expire after a crashed container. `agent-test capacity` reports a bounded
machine-wide summary.

Each completed pytest node contributes its total setup, call, and teardown
duration to an immutable machine-store observation. The dashboard retains the
latest 10 observations per repository and node, pruning only older rows after
an append. `run` and `plan` report a median-based estimate before execution.
Parallel profiles use the larger of the longest known test and aggregate known
work divided by effective workers, so one long test is never divided across
idle workers; machine-capacity weights remain independent.
`timings` shows each node's observed minimum, median, maximum, and bounded
latest samples without starting tests. Exact node selections are complete
only when every requested node has history. File, class, and directory
selectors are explicitly open-ended: their estimate covers previously seen
descendants but remains a known-work floor because collection may discover
new nodes.

Pytest's captured output, including output from passing tests, is written to
the retained run log. It is never streamed into the launching agent's context.

`plan` reads the current Python diff and ranks selectors using direct test
naming, prior retained per-test line coverage, and bounded import/reference
search. It reports files for which no defensible selector was found rather
than pretending a guess is complete. `run --changed` freezes that plan and the
changed-line set into the run manifest. Line coverage is retained by default
without requiring pytest-cov; source-path resolution is memoized per run so
coverage does not perform a filesystem lookup for every traced line.
`coverage` compares executed lines only with the lines changed at launch and
bounds every missing-line listing.

`baseline` publishes one retained run as the durable workspace comparison
point. `metrics` reports machine-wide run/refusal telemetry without exposing
individual command arguments.

Raw `pytest`, `py.test`, and `python -m pytest` are refused inside Autonomy
agent sessions. The console commands are replaced in the session command
surface, and the repository pytest hook covers module invocation. Agent Test's
owned worker carries the internal authorization required to invoke pytest.
