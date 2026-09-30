---
name: browser
description: Start and drive a shared, visible Chrome that the operator can watch and take over (the dashboard's browser broker). Use when a person must see or act on the same page as the agent.
---

# Browser broker (the `browser` capability)

Lends your session a real, headed Chrome in its own capped container, driven
by structured commands. The operator can watch it and take control from
`/browser` (and from the globe on your session viewer). Your workspace needs
the `browser` capability; secure sign-in also needs `repl_login`.

Every call carries your session token (`Authorization: Bearer $CROSSTALK_TOKEN`)
against `$GRAPH_API`. Runbook (enabling, operator view, limits, failure modes):
graph://12bf59f5-813.

```text
POST   /api/browser/leases                       {"adapter":"chrome-headed","profile":{"kind":"ephemeral"},"ttl_s":1800}
                                                 profile may be {"kind":"persistent","store":"<name>"} (cookies survive)
GET    /api/browser/leases/{lease}               state starting → ready; wait for ready before commands
POST   /api/browser/leases/{lease}/commands      {"op":"goto","args":{"url":"https://..."}}
POST   /api/browser/leases/{lease}/secure-login  {"target_key":"...","fields":{...},"submit":...}
DELETE /api/browser/leases/{lease}               release it when done
```

Operations: `goto snapshot screenshot click fill type press wait url title
text download`, each as `{"op": ..., "args": {...}}`: for example `goto {url}`,
`click {target}`, `fill {target, value}`, `type {target, text}`, `press {key}`,
`wait {text}`. `screenshot` returns `result.png_base64`. Target elements with a `{"ref"}` from `snapshot`, or
`{"role","name"}`, `{"label"}`, `{"text"}`, `{"css"}`. No operation runs
your own script.

Answers to expect:
- `409` while the operator holds control or a command is running: wait and retry.
- `503 {reason}`: the broker is starting, at its lease limit, short of disk,
  or Docker is down; the reason says which.
- A lease ends at its time limit even if you never release it (ephemeral:
  30 min), and after 10 min with no command or operator input.
- A link whose key is in its `#fragment` must be opened with the fragment.

With secure sign-in you never see the credential: the broker types it and
tells you whether the sign-in succeeded.
