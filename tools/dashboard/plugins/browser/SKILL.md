# Browser broker (the `browser` plugin)

Lends your session a real, headed Chrome in its own capped container, driven
by structured commands. The operator can watch it and take control from
`/browser` (and from the globe on your session viewer). Your workspace needs
the `browser` capability; secure sign-in also needs `repl_login`.

Every call carries your session token (`Authorization: Bearer ...`) against
`$GRAPH_API`.

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
- A lease ends at its expiry even if you never release it.

With secure sign-in you never see the credential: the broker types it and
tells you whether the sign-in succeeded.
