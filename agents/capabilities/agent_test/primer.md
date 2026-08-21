## Agent Test

`agent-test` is the only supported Python test entry point in this workspace.
It retains complete evidence, bounds what it prints, runs in the background,
notifies this session on completion, estimates duration from organization
history, and refuses unchanged reruns.

```bash
agent-test plan                              # selectors for the current diff
agent-test run --changed                     # start the bounded diff plan
agent-test run PATH_OR_NODEID                # start an explicit selection
agent-test failures RUN_ID                   # retained failures, five by default
agent-test trace RUN_ID 1                     # one retained traceback
agent-test output RUN_ID --limit-lines 40    # bounded retained output
agent-test timings PATH_OR_NODEID             # latest timing samples
agent-test doctor                             # diagnose Python/venv; never installs
```

After `run`, keep working. Completion arrives as a task notification; polling
or repeating the run is unnecessary. Do not invoke `pytest`, `py.test`, or
`python -m pytest` directly.
