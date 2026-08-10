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
