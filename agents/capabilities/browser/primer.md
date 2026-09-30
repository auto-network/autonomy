## Shared browser

This workspace can start a real, visible Chrome that the operator can watch
and take over: `POST $GRAPH_API/api/browser/leases` with your session token,
then send commands to `/api/browser/leases/{lease}/commands`. The operator
sees it at `/browser` on the dashboard and on the globe in your session
viewer. Use it when a person must see the same page; use `agent-browser`
for private headless browsing. Full usage: the `browser` skill; runbook
graph://12bf59f5-813.
