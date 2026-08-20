# Agent Test

Agent Test is the supported agent-facing interface for pytest. Runs are
asynchronous, complete evidence is retained, and every query is bounded.

```bash
agent-test run tools/graph/tests/test_ops.py
# returns immediately; keep working until the completion notification arrives

agent-test status
agent-test doctor
agent-test profiles
agent-test collect tools/graph/tests
agent-test inventory <run-id> --match parser
agent-test validate tools/graph/tests/test_ops.py::test_name --run <run-id>
agent-test failures <run-id>
agent-test rerun-failures <run-id>
agent-test trace <run-id> 1
agent-test output <run-id> --limit-lines 40
agent-test capacity
agent-test stop
```

Do not poll a live run. Do not start another run. Do not pipe Agent Test through
`head`, `tail`, or `grep`; query the retained evidence instead.

The resident supervisor launches isolated run process groups. Completion is
delivered through the dashboard's typed task-notification endpoint for both
Claude and Codex; self-CrossTalk remains a compatibility fallback.

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

Pytest's captured output, including output from passing tests, is written to
the retained run log. It is never streamed into the launching agent's context.
