#!/bin/sh
python3 -c 'from tools.agent_test.lease_client import telemetry_request; telemetry_request(event="raw_pytest_refused")' >/dev/null 2>&1 || true
cat >&2 <<'EOF'
Raw pytest is disabled for agent sessions.
Use: agent-test plan
Then: agent-test run PATH_OR_NODEID
Retained results: agent-test status
EOF
exit 64
