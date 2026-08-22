## Agent Test

`agent-test` is the only supported Python test entry point in this workspace.
It keeps complete evidence privately under `/tmp`, bounds what it prints, runs in the background,
notifies this session on completion, estimates duration from organization
history, and refuses unchanged reruns.

```bash
agent-test plan                              # selectors for the current diff
agent-test run --changed                     # start the bounded diff plan
agent-test run PATH_OR_NODEID                # start an explicit selection
agent-test failures RUN_ID                   # retained failures, five by default
agent-test trace RUN_ID 1                     # one retained traceback
agent-test output RUN_ID --limit-lines 40    # bounded retained output
agent-test retain RUN_ID                     # explicitly copy this run to workspace output
agent-test timings PATH_OR_NODEID             # per-test median, range, and latest samples
agent-test doctor                             # diagnose Python/venv; never installs
agent-test status                             # one live ETA check; never poll repeatedly
```

After `run`, keep working. Completion arrives as a task notification; polling
or repeating the run is unnecessary. Do not invoke `pytest`, `py.test`, or
`python -m pytest` directly. Raw evidence expires with the session unless you
explicitly retain that run; organization statistics remain in Settings.
Exact test-node selections receive a complete ETA when every node has history.
File, class, and directory selections report only the known-history portion
until collection proves the complete inventory; unseen work is never hidden
inside a confident estimate.

To investigate expensive tests without running them, read the bundled
`skills/agent-test-slow-review/SKILL.md`; it ranks retained timings and source
evidence by waste risk and test value.
