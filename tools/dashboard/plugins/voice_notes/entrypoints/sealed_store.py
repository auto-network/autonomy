"""Note storage as sealed items — the two-compartment model.

A note is stored in the operator's ``voice-notes`` SealedSettings store, split
into two compartments addressed by the SAME blind index so a cold reader can
attribute nothing to a note:

1. **Metadata** (browsable) — ``{title, updated_at, access_token_hash,
   bound_session}`` — sealed by the SealedSettings layer under ``K_meta``.
   Listing the notebook opens the store once (the secured sealed-index unlock)
   and reads all metadata locally, with no per-note audit. The access-control
   fields live here so the handler can check a caller's token BEFORE releasing
   the body.
2. **Body** (the sensitive text) — ``{value: body}`` — an
   ``autonomy.vault.audited`` credential at ``store.blind_index(note_id)``, so
   every read is a server-side audited release, logged per reveal. The body is
   never in the metadata compartment.

This module owns only the note shape over the substrate; it takes an already
constructed :class:`SealedSettings` (its backend having been opened by the
unlock ceremony) plus ``settings_ops`` and the caller org. It performs no
crypto of its own — the layer and the vault do it all.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from tools.graph.schemas.vault_credential import (
    VAULT_AUDITED_SET_ID,
    VAULT_CREDENTIAL_REVISION,
)

TOKEN_BYTES = 32
_TITLE_MAX = 240
_BODY_MAX = 200_000


@dataclass
class NoteView:
    """A note as returned to an authorized caller."""

    note_id: str
    title: str
    body: str
    updated_at: str


def mint_token() -> tuple[str, str]:
    """A fresh access token and its hash. Return the token ONCE; store the hash."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    return token, hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(presented: str, stored_hash: str) -> bool:
    """Constant-time check of a presented token against the stored hash."""
    import hmac as _hmac
    digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    return _hmac.compare_digest(digest, stored_hash)


def _body_key(store, note_id: str) -> str:
    # The audited body shares the note's blind index (the item-layer address),
    # so metadata and body are one logical note under one opaque key.
    return store.blind_index(note_id)


def create_note(
    store, ops, org, *, note_id: str, title: str, body: str, bound_session: str
) -> str:
    """Write a new note's metadata + audited body and return its access token once.

    The metadata records the token's hash and the creating session; the raw
    token is returned to the caller here and never stored.
    """
    _validate(title, body)
    token, token_hash = mint_token()
    now = _now()
    store.put(note_id, {
        "title": title.strip(),
        "updated_at": now,
        "access_token_hash": token_hash,
        "bound_session": bound_session,
    })
    _write_body(ops, org, _body_key(store, note_id), body)
    return token


def write_note(store, ops, org, *, note_id: str, title: str, body: str) -> None:
    """Update an existing note's title/body, preserving its access fields."""
    _validate(title, body)
    current = store.get(note_id) or {}
    store.put(note_id, {
        "title": title.strip(),
        "updated_at": _now(),
        "access_token_hash": current.get("access_token_hash", ""),
        "bound_session": current.get("bound_session", ""),
    })
    _write_body(ops, org, _body_key(store, note_id), body)


def read_note(store, ops, org, *, note_id: str) -> NoteView | None:
    """Return the full note (metadata + audited body), or None if absent."""
    meta = store.get(note_id)
    if meta is None:
        return None
    body = _read_body(ops, org, _body_key(store, note_id))
    if body is None:
        return None
    return NoteView(note_id=note_id, title=meta.get("title", ""),
                    body=body, updated_at=meta.get("updated_at", ""))


def access_fields(store, note_id: str) -> tuple[str, str] | None:
    """The note's ``(access_token_hash, bound_session)`` for the gate, or None."""
    meta = store.get(note_id)
    if meta is None:
        return None
    return meta.get("access_token_hash", ""), meta.get("bound_session", "")


def list_titles(store) -> list[dict]:
    """Every note's browsable metadata (no bodies), newest first."""
    items = [{"note_id": i.name, "title": i.metadata.get("title", ""),
              "updated_at": i.metadata.get("updated_at", "")}
             for i in store.list()]
    items.sort(key=lambda n: n["updated_at"], reverse=True)
    return items


def revoke_note(store, note_id: str) -> bool:
    """Clear the note's access token so it can no longer be opened. Owner-only
    authority is enforced by the caller. Returns False if the note is absent."""
    meta = store.get(note_id)
    if meta is None:
        return False
    store.put(note_id, {
        "title": meta.get("title", ""),
        "updated_at": _now(),
        "access_token_hash": "",          # cleared -> no token opens it
        "bound_session": meta.get("bound_session", ""),
    })
    return True


# -- internals --------------------------------------------------------------- #
def _write_body(ops, org, body_key: str, body: str) -> None:
    ops.write_by_key(
        VAULT_AUDITED_SET_ID, VAULT_CREDENTIAL_REVISION, body_key,
        {"value": body}, org=org, state="raw",
    )


def _read_body(ops, org, body_key: str):
    row = ops.read_set_key(VAULT_AUDITED_SET_ID, body_key, org=org, peers=[])
    if row is None:
        return None
    payload = row.get("payload") if isinstance(row, dict) else None
    return (payload or {}).get("value")


def _validate(title: str, body: str) -> None:
    if not isinstance(title, str) or not isinstance(body, str):
        raise ValueError("title and body must be strings")
    if len(title) > _TITLE_MAX or len(body) > _BODY_MAX:
        raise ValueError("note is too large")


def _now() -> str:
    from datetime import datetime, timezone
    return (datetime.now(timezone.utc).replace(microsecond=0)
            .isoformat().replace("+00:00", "Z"))
