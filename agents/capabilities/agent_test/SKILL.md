---
name: agent-test
description: Run Python tests through Agent Test with private temporary evidence, bounded output, background completion notifications, test selection, changed-line coverage, timing estimates, explicit retention, and rerun-loop prevention.
---

# Agent Test

Use `agent-test`; direct `pytest`, `py.test`, and `python -m pytest` are not
supported agent workflows in repositories that enable this capability.

Start tests asynchronously with an explicit selector:

```bash
agent-test plan
agent-test run --changed
agent-test run path/to/test_file.py::test_name
```

The start command returns promptly. Keep working and wait for the completion
notification; do not poll and do not start the same run again.

Read retained, bounded evidence without rerunning:

```bash
agent-test status
agent-test failures <run-id>
agent-test trace <run-id> 1
agent-test output <run-id> --limit-lines 40
agent-test coverage <run-id>
agent-test retain <run-id>                   # only when durable raw evidence is useful
agent-test timings path/to/test_file.py::test_name
```

Use `agent-test doctor` when the Python environment is unclear. It diagnoses
available environments but never installs dependencies. Use
`agent-test collect` and `agent-test inventory` to discover node ids.
