"""``autonomy.sealed-settings.row`` — one opaque row of a sealed store.

This is the container a ``SealedSettings`` store (see
``tools.graph.sealed_settings``) writes its items into. The substrate treats
every row as inert: an opaque key and a single opaque ``ciphertext`` string.
All meaning lives INSIDE the ciphertext, which the layer seals under a key
derived from the store's factor-released sealed index — the substrate never
holds that key and cannot read the value.

## What a row is

- **The key** is ``<store-tag>.<base64url(HMAC(K_index, name))>``. The tag is
  the store's pepper-derived hidden address — an opaque segment that groups
  one logical store's rows for enumeration without naming the store or the app
  (seal-all: a cold dump attributes no row to any store); the hash is the
  blind index hiding the logical name. The ``.`` separator is outside the
  base64url alphabet, so tag and blind index are unambiguous AND the whole
  compound is a single legal org-writeback suffix (which forbids ``:``). The
  substrate never sees either plaintext — the item name lives only inside the
  sealed value.
- **The value** is ``ciphertext``: a base64url AEAD blob (nonce + tagged
  ciphertext) the layer produced. It is NOT ``@vaulted`` — the layer does its
  own sealing under the factor-derived metadata key, so that browsing the
  store is one factor release (the sealed-index open), not one audited
  release per row. Secrets that must be audited on every reveal do NOT live here; they
  live in ``autonomy.vault.audited`` and are addressed by the same blind index.

## Why personal + org-writeback, raw, keyed-per-entity

Personal-homed, exactly like ``autonomy.vault.secured``: browser/operator
mints (the password manager, secret vaults) write unprefixed rows into the
operator's own store. ``@org_writeback_namespace`` is what lets an AGENT
SESSION write too — it supplies the bare compound key and the server derives
its bearer's ``<org>:`` prefix, so a session store's rows are org-keyed
writethrough into the personal store (``<org>:<store-tag>.<hash>``), never
into the org database. The org prefix is the accepted, declared leakage
(which principal class minted the store) and the boundary keeping a session
out of the unprefixed space where browser stores live. Banded ``raw`` so the
ciphertext never federates. ``keyed_per_entity(key_strategy="setting_name")``
imposes no key FORM, so both the prefixed and unprefixed compound keys pass
``validate_key`` unchanged — the layer owns the key derivation, the schema
only carries the row.
"""

from __future__ import annotations

from .registry import (
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    org_writeback_namespace,
    publication_band,
)

SEALED_ROW_SET_ID = "autonomy.sealed-settings.row"
SEALED_ROW_REVISION = 1


@home("personal")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="setting_name")
@org_writeback_namespace(suffix="row")
class SealedRowV1(SettingSchema):
    """One opaque, layer-sealed row of a sealed store.

    The substrate stores and returns this verbatim and learns nothing from it:
    the key is a blind index and the value is an AEAD blob whose key the
    substrate does not hold.
    """

    set_id = SEALED_ROW_SET_ID
    schema_revision = SEALED_ROW_REVISION

    ciphertext: str = field(
        required=True,
        description=(
            "base64url(nonce || AEAD ciphertext+tag). Opaque to the substrate; "
            "sealed and opened only by the SealedSettings layer under a key "
            "derived from the store's factor-released sealed index."
        ),
    )
