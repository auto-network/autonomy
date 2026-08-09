# MCP Relay — ChatGPT ↔ Autonomy bridge

## Purpose

Expose a scoped slice of the Autonomy graph to ChatGPT as native MCP tools,
reached through OpenAI's Secure MCP Tunnel so nothing inbound is ever exposed.
ChatGPT gets: graph search/read, session tail/list, CrossTalk send + log
(agent-to-agent messaging), and note writing — each gated by a per-peer,
operator-approved scope grant.

Origin: ChatGPT design conversation imported 2026-08-09
(`/workspace/output/chatgpt-share-autonomy-openai-connector.txt` /
`https://chatgpt.com/share/6a77ee68-a0a4-83ea-b4da-0bd4b8ea3dcd`).

## Architecture

```
ChatGPT (Developer Mode connector, Tunnel mode)
   │  MCP JSON-RPC
   ▼
OpenAI control plane  ←──outbound long-poll──  tunnel-client daemon
                                                   │ http
                                                   ▼
                                        gateway.py  127.0.0.1:8787/mcp
                                                   │ subprocess
                                                   ▼
                                               graph CLI → graph.db
```

- `gateway.py` — stdlib-only stateless MCP server (spec 2026-07-28, plus the
  legacy `initialize` handshake for older clients). Plain JSON responses, no
  SSE. Binds loopback only.
- `tunnel-client` — OpenAI's open-source daemon (github.com/openai/tunnel-client);
  authenticates outbound to the control plane with a runtime API key. The MCP
  server is reachable **only** while this daemon runs.

## Peer model — one approval per peer, not per message

1. ChatGPT calls the `hello` tool with a stable `peer_name` → peer is created
   **pending**, a CrossTalk broadcast alerts live sessions.
2. Operator approves once:
   `python3 tools/mcp_relay/gateway.py approve <peer> --scopes read,send[,write]`
3. ChatGPT calls `hello` again → receives its `peer_token`, passes it on every
   later call. Scope classes: `read` (search/read/tail/sessions/crosstalk_log),
   `write` (note), `send` (crosstalk_send).
4. `revoke <peer>` kills the token immediately.

Registry + call log live in `--data-dir` (default `data/mcp_relay/`,
gitignored; tokens never belong in git). The peer layer is allowlisting on top
of the tunnel's primary trust boundary (your OpenAI org), not a substitute
for it.

## Tools exposed

| Tool | Scope | Backing command |
|------|-------|-----------------|
| `hello` | — | peer registration/token pickup |
| `search` | read | `graph search <q> --json` (deduped per source) |
| `read` | read | `graph read <id> --max-chars N` |
| `tail` | read | `graph tail <session> N` |
| `sessions` | read | `graph sessions --status --topics` |
| `crosstalk_log` | read | `graph crosstalk --since D` |
| `note` | write | `graph note ... --author chatgpt:<peer> --tags chatgpt,...` |
| `crosstalk_send` | send | `graph crosstalk send <session> "[from ChatGPT peer …] msg"` |

Notes written by ChatGPT are always tagged `chatgpt` and authored
`chatgpt:<peer>`; CrossTalk messages are prefixed with their origin so
receiving sessions know the sender is external.

## Running

```bash
# server (loopback)
python3 tools/mcp_relay/gateway.py run [--port 8787] [--data-dir DIR]

# peer administration
python3 tools/mcp_relay/gateway.py peers
python3 tools/mcp_relay/gateway.py approve <peer> --scopes read,send
python3 tools/mcp_relay/gateway.py revoke <peer>
```

Env: `GRAPH_BIN` (default `graph`), `GRAPH_TIMEOUT` (default 45s).

Tunnel side (binary + profile prepared under `/workspace/output/`, see the
runbook there): create a tunnel at platform.openai.com → Tunnels, mint a
runtime API key, then

```bash
export CONTROL_PLANE_API_KEY=sk-...
tunnel-client run --profile autonomy-relay --profile-dir <profiles-dir>
```

and register the connector at chatgpt.com → Settings → Connectors (Tunnel
mode) **while the daemon is running**.

## Spec notes (2026-07-28)

- Stateless: no sessions, no handshake; `server/discover` implemented;
  `resultType`/`_meta.serverInfo` stamped on every result; `tools/list`
  carries `ttlMs`/`cacheScope`.
- `Mcp-Method`/`Mcp-Name` headers validated when present (HeaderMismatch
  `-32020`), tolerated when absent (older clients).
- Unknown GET paths return **404** deliberately: tunnel-client probes for
  OAuth protected-resource metadata and treats 404-on-all-candidates as
  "plain no-auth MCP server"; a 405 keeps readiness degraded.

## Tests

```bash
python3 -m pytest tools/mcp_relay/test_gateway.py -q
```

L1, hermetic: runs the real server subprocess against a stubbed `graph`
binary; covers spec plumbing, the peer lifecycle, scope gating, and argv
injection guards.
