"""``autonomy.org.follow#1`` — an organization this node follows.

A follow is a per-operator declaration that this node mirrors another
organization's *public surface* (published/canonical rows) into a local
read-only database ``data/orgs/<slug>.db`` whose ``orgs.type`` is
``followed``. The follower dials the org's standing ``org:follow`` public
link with only its fragment key, pulls the public projection over the
credential-free follow loop, and applies it into the mirror; every local
writer refuses the mirror (``FollowedOrgReadOnly``).

Homed on ``personal`` so the operator's fleet follows the same
organizations (the row replicates like every personal row) while each
machine holds its own mirror. Keyed by the followed org's *slug*.

Fields:

* ``org_uuid`` — the followed org's registry uuid, the handshake domain the
  viewer channel authenticates against (``verify_link_server_hello``) and
  the identity the local mirror's ``orgs`` row carries.
* ``rendezvous`` — the standing ``org:follow`` link URL (relay base + the
  ``/l/<token>`` path), minus its fragment; the loop derives the relay
  websocket base and the link token from it.
* ``link_pub`` — the link's fragment public key, the whole authentication
  of the follow channel (there is no membership and no client credential).
* ``registry_url`` (optional) — the registry API base the link envelope was
  fetched from, kept so ``graph follow add`` can be re-run / re-resolved.
* ``enabled`` — whether the follow loop pulls this org.
* ``added_at`` — ISO-8601 timestamp the follow was added.

Design of record: graph://5f2f5a49-00d §10.4.
"""

from __future__ import annotations

import re
from typing import Any

SYNOPSIS = {
    "summary": (
        "One organization this node follows: its public surface is pulled, "
        "credential-free, over the org's standing org:follow link into a "
        "read-only local mirror. Personal-homed so the operator's whole fleet "
        "follows the same organizations; each machine holds its own mirror."
    ),
    "nouns": [
        "follow", "followed organization", "follow link", "rendezvous",
        "mirror", "public surface",
    ],
    "related_set_ids": ["autonomy.network.link-grant#1", "autonomy.org#1"],
}

from .registry import (
    keyed_per_entity,
    SettingSchema,
    SchemaValidationError,
    home,
    publication_band,
)

ORG_FOLLOW_SET_ID = "autonomy.org.follow"
ORG_FOLLOW_REVISION = 1

#: The Setting key is the followed org's slug: lowercase alphanumeric with
#: dashes, exactly as ``db.is_org_slug`` accepts (a mirror file is named for
#: it, so the two must agree).
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


_HEX64 = re.compile(r"[0-9a-f]{64}")


def normalize_link_pub(value: str) -> str:
    """The follow row's ``link_pub`` (64 lowercase hex) from either form a
    published link key arrives in.

    ``fragment_url`` (tools.dashboard.link_channel_key) writes the channel
    public key into the shared URL as unpadded base64url (43 chars); the
    follow row, the viewer handshake (``verify_link_server_hello``) and this
    schema all want the 64-hex form. ``graph follow add`` and the first-run
    seed pass whatever the URL or the allowlist carries through here.
    Raises ``ValueError`` for anything that is neither a 64-hex key nor a
    base64url encoding of exactly 32 bytes (a wrong-length key must never
    be written and discovered only at dial time)."""
    import base64

    if not isinstance(value, str):
        raise ValueError("link_pub must be a string")
    text = value.strip()
    if _HEX64.fullmatch(text.lower()):
        return text.lower()
    pad = "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(text + pad)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            "link_pub is neither 64 hex chars nor a base64url channel key"
        ) from exc
    if len(raw) != 32:
        raise ValueError(
            f"link_pub decodes to {len(raw)} bytes; a channel key is 32"
        )
    return raw.hex()


@publication_band(min="raw", max="raw")
@home("personal")
@keyed_per_entity(key_strategy="org_slug")
class OrgFollowV1(SettingSchema):
    """One organization this node follows, keyed by its slug."""

    set_id = ORG_FOLLOW_SET_ID
    schema_revision = ORG_FOLLOW_REVISION

    _field_metadata: dict[str, dict] = {
        "org_uuid": {
            "type": "string", "required": True,
            "description": (
                "The followed org's registry uuid — the viewer-channel "
                "handshake domain and the identity the local mirror carries."
            ),
        },
        "rendezvous": {
            "type": "string", "required": True,
            "description": (
                "The org:follow link URL (relay base + /l/<token>), minus its "
                "fragment; the follow loop derives the relay ws base and token."
            ),
        },
        "link_pub": {
            "type": "string", "required": True,
            "description": (
                "The link's fragment public key (64 hex) — the whole "
                "authentication of the credential-free follow channel."
            ),
        },
        "registry_url": {
            "type": "string", "required": False,
            "description": (
                "The registry API base the link envelope was fetched from."
            ),
        },
        "enabled": {
            "type": "boolean", "required": True,
            "description": "Whether the follow loop pulls this organization.",
        },
        "added_at": {
            "type": "string", "required": True,
            "description": "ISO-8601 timestamp the follow was added.",
        },
    }

    _ALLOWED = frozenset(_field_metadata)

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, got "
                f"{type(payload).__name__}"
            )
        extra = set(payload) - cls._ALLOWED
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown fields {sorted(extra)!r}; the "
                f"followed org slug lives in the Setting key"
            )
        for field in ("org_uuid", "rendezvous", "link_pub", "added_at"):
            value = payload.get(field)
            if not isinstance(value, str) or not value:
                raise SchemaValidationError(
                    f"{cls.__name__}: {field!r} is required and must be a "
                    f"non-empty string"
                )
        link_pub = payload["link_pub"]
        if len(link_pub) != 64 or any(
            ch not in "0123456789abcdef" for ch in link_pub
        ):
            raise SchemaValidationError(
                f"{cls.__name__}: 'link_pub' must be 64 lowercase hex chars "
                f"(the link fragment key), got {link_pub!r}"
            )
        registry_url = payload.get("registry_url")
        if registry_url is not None and (
            not isinstance(registry_url, str) or len(registry_url) > 2048
        ):
            raise SchemaValidationError(
                f"{cls.__name__}: 'registry_url' must be a string under 2048 "
                f"chars"
            )
        if not isinstance(payload.get("enabled"), bool):
            raise SchemaValidationError(
                f"{cls.__name__}: 'enabled' is required and must be a boolean"
            )

    @classmethod
    def validate_member_key(cls, key: str) -> None:
        if not _SLUG_RE.match(key or ""):
            raise SchemaValidationError(
                f"{cls.__name__}: the key is the followed org slug "
                f"(lowercase alphanumeric with dashes), got {key!r}"
            )
