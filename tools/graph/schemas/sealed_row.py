"""``autonomy.sealed-settings.row`` — one opaque row of a sealed store.

This is the container a ``SealedSettings`` store (see
``tools.graph.sealed_settings``) writes its items into. The substrate treats
every row as inert: an opaque key and a single opaque ``ciphertext`` string.
All meaning lives INSIDE the ciphertext, which the layer seals under a key
derived from a factor-released root — the substrate never holds that key and
cannot read the value.

## What a row is

- **The key** is a blind index: ``<domain>:<base64url(HMAC(K_index, name))>``.
  The domain prefix isolates one logical store (a password manager, a notes
  app) so ``read_set(prefix=<domain>)`` enumerates exactly that store; the
  hash hides the logical name. The substrate never sees the plaintext name —
  it lives only inside the sealed value.
- **The value** is ``ciphertext``: a base64url AEAD blob (nonce + tagged
  ciphertext) the layer produced. It is NOT ``@vaulted`` — the layer does its
  own sealing under the factor-derived metadata key, so that browsing the
  store is one factor release (the root open), not one audited release per
  row. Secrets that must be audited on every reveal do NOT live here; they
  live in ``autonomy.vault.audited`` and are addressed by the same blind index.

## Why personal, raw, keyed-per-entity

Personal because a sealed store is the operator's own material, exactly like
``autonomy.vault.secured``. Banded ``raw`` so the ciphertext never federates.
``keyed_per_entity(key_strategy="setting_name")`` imposes no key FORM, so the
``<domain>:<hash>`` blind index passes ``validate_key`` unchanged — the layer
owns the key derivation, the schema only carries the row.
"""

from __future__ import annotations

from .registry import (
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    publication_band,
)

SEALED_ROW_SET_ID = "autonomy.sealed-settings.row"
SEALED_ROW_REVISION = 1


@home("personal")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="setting_name")
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
            "derived from the store's factor-released root."
        ),
    )
