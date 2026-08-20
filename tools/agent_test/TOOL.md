# Agent Test

Agent Test is the supported agent-facing interface for pytest. Stage 1 makes a
run asynchronous, retains complete structured evidence, and keeps every query
bounded.

```bash
agent-test run tools/graph/tests/test_ops.py
# returns immediately; keep working until the completion notification arrives

agent-test status
agent-test failures <run-id>
agent-test trace <run-id> 1
agent-test output <run-id> --limit-lines 40
agent-test stop
```

Do not poll a live run. Do not start another run. Do not pipe Agent Test through
`head`, `tail`, or `grep`; query the retained evidence instead.

Stage 1 completion notification uses self-CrossTalk when an Autonomy session
token is available. The typed dashboard notification route replaces this
fallback in Stage 2.

Pytest's captured output, including output from passing tests, is written to
the retained run log. It is never streamed into the launching agent's context.
