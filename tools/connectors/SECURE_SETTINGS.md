# Agent-triggered secure settings input

This is a provisioning primitive, not a password-manager UI. A capability
invokes it when setup needs a credential (for example, the Eversource
connector). The dashboard displays a one-time, provider-labelled dialog on the
operator's phone.

The request supplies:

```json
{
  "target_setting": "autonomy.org.capability.install#1",
  "target_key": "eversource.primary",
  "origin": "www.eversource.com",
  "schema": {"username": "username", "password": "password"}
}
```

The host creates or selects the capability recipient key and returns its public
key to the client dialog. The client encrypts the structured map using the
`autonomy.secure-setting.v1` envelope and posts only ciphertext. The dashboard
stores that envelope directly in the requested setting. It never receives the
plaintext values.

The temporary `repl_login` key provider keeps one private X25519 key in a
capability-owned host file with mode `0600`. Encryption uses Autonomy's existing
HPKE suite (`X25519 / HKDF-SHA-256 / ChaCha20-Poly1305`) and the same sealed
record framing already used by the vault/root-key ceremony. `repl_login`
decrypts the setting only in host memory. Once the native vault is available,
the same request and envelope format can use the vault's public key; no dialog
or provider primer change is required.

The request is bound to the session, capability, target setting/key, origin,
and schema. The dialog is single-use, but the stored setting has no expiration.

## Dashboard handoff contract

The agent-side capability should use the existing generalized approval
rendezvous (`POST /api/approvals`) with a new `secure_setting` kind, rather
than introducing a credential-specific dialog route. The existing SSE pending
events, reconnect behavior, operator decision, and signing/authorization hooks
remain unchanged.

The request payload is equivalent to:

```text
POST /api/approvals
```

The approval enrichment returns a `prompt_id`, recipient `public_key`, `key_id`,
and a single-use nonce. The existing approval overlay renders the
provider-labelled secure fields. Its per-kind decision handler posts the
resulting envelope as the decision body:

```text
POST /api/approvals/{prompt_id}/decision
```

The completion response contains only `{stored: true, target_key: ...}`. The
existing approval executor validates the prompt binding and envelope
associated data before writing the setting. A separate host-only resolver loads
the envelope later for `repl_login`; the agent-facing result is only an
authentication state.

## v2 — workspace-bound records and authenticated callers (bead auto-0qxna)

Two changes landed together; both fail closed.

**Caller identity is authenticated, never supplied.** Every POST to the
stealth REPL (`/api/login`, `/api/command`, raw `/`) requires
`Authorization: Bearer $CROSSTALK_TOKEN`. The REPL resolves
`sha256(token)` in the dashboard's auth DB to the launcher-stamped
session, derives the workspace from that session's `project` row, and
requires `<workspace-id>:repl_login` in
`autonomy.workspace.capability.enable` (read fresh per request — a
revoked grant or token takes effect immediately, no restart). The caller
supplies no session name and no workspace: there is nothing to assert,
so nothing to forge. Unauthenticated `/health` is liveness-only.

*Accepted coupling:* `CROSSTALK_TOKEN` is thereby both the container's
messaging identity and its credential-decryption identity. Same trust
boundary (the container env), but anyone widening CrossTalk token
distribution must know browser-login credential access rides on it.

**The workspace allowlist is inside the seal.** A provisioning request
names `workspaces`; the operator sees the allowlist in the approval
overlay and the browser seals the credential once per workspace under

    autonomy.secure-setting.v2|<org>|<target_key>|<nonce>|workspace=<ws>

The stored record keeps one ciphertext per workspace and NO purpose
string. At login the REPL reconstructs the label from its own derived
view of the caller's workspace (`repl_login.py`). Editing the stored
Setting to widen the allowlist produces labels that do not decrypt —
enforcement is the AEAD tag, not a check. Re-provisioning through a
fresh operator approval is therefore the only way to widen an
allowlist; that is a feature, not a limitation. Revision-1 records
(single ciphertext, stored purpose) are refused outright: accepting
them would bypass the binding.
