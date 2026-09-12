"""``autonomy.user#1`` — the person's one mutable Personal profile.

The single Personal presentation record a person authors for themselves —
the source the Profile Settings **Personal** screen edits and the source the
invitation screen's "Joining as" preview reads before it proposes an
organization member profile. Decided by ``graph://4f9e881c-a9`` §7 and
comments ``682c929c-7b8`` (Personal screen + copy-on-join boundary) and
``8cc8b2ed-5ae`` (server-owned canonical avatar).

This is DELIBERATELY NOT an organization profile and never lives in an
organization database:

* ``autonomy.identity.personal#1`` is the immutable personal ROOT (the
  encrypted armor + a display-name-only legacy fallback). This set never
  touches the root; the root supplies only the read-time name fallback when
  no profile row exists yet.
* ``autonomy.org#3`` is organization BRANDING and ``autonomy.org.member-profile#1``
  is a person's presentation INSIDE one organization. Both are org-scoped and
  cannot host mutable Personal presentation without coupling it to the wrong
  authority scope.

SCOPE — personal: always the operator's own store, home ``personal``, banded
``raw`` so it never federates to a peer. CARDINALITY — one row, singleton key
``default`` (mirrors the personal root's canonical label). PROTECTION — the
set is added to ``settings_ops.PROTECTED_IDENTITY_SET_IDS`` so a generic
Settings write is refused; only the identity/profile routes carry the
capability. Text fields (display name, biography, initials override) are
mutated by the profile routes; the two avatar references are server-owned and
written only by the follow-on avatar routes through the same protected seam.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid as _uuid
from datetime import datetime
from typing import Any

from .registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    publication_band,
    singleton,
)


USER_PROFILE_SET_ID = "autonomy.user"
USER_PROFILE_REVISION = 1

#: The one label the Personal profile is ever written under — the same
#: canonical label the personal root uses (``identity_routes`` PERSONAL_
#: CANONICAL_LABEL). Singleton, latest-write-wins.
USER_PROFILE_CANONICAL_LABEL = "default"

#: Field bounds (comment 682c929c-7b8).
DISPLAY_NAME_MAX = 120
BIOGRAPHY_MAX = 500
INITIALS_MAX = 4

#: The compact avatar derivative is a bounded 64x64 WebP encoded as a
#: ``data:image/webp;base64,...`` URI — the exact shape the org icon path
#: writes (see :data:`org.ORG_ICON_DATA_URI_MAX_CHARS`). Kept as its own
#: constant so the Personal contract does not depend on the org module, but
#: deliberately the same bound: both are the portable compact tile.
AVATAR_ICON_DATA_URI_PREFIX = "data:image/webp;base64,"
AVATAR_ICON_DATA_URI_MAX_CHARS = 24_000

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


SYNOPSIS = {
    "summary": (
        "The person's one mutable Personal profile (autonomy.user): trimmed "
        "display name, optional biography, optional explicit initials "
        "override, server-owned avatar references (attachment id + bounded "
        "compact data: URI), and an update timestamp. Personal-scoped and "
        "singleton — distinct from the immutable personal root "
        "(autonomy.identity.personal), org branding (autonomy.org) and "
        "org-member presentation (autonomy.org.member-profile)."
    ),
    "nouns": [
        "personal profile", "profile", "display name", "biography", "byline",
        "initials", "avatar", "profile photo", "profile settings",
        "joining as", "personal presentation",
    ],
    "related_set_ids": [
        "autonomy.identity.personal#1",
        "autonomy.org#3",
        "autonomy.org.member-profile#1",
    ],
}


def _require_trimmed_str(
    payload: dict, key: str, cls_name: str, *, min_len: int, max_len: int,
) -> str:
    """A present string field that is already trimmed and within bounds.

    Rejects a non-string, a leading/trailing-whitespace value (the row stores
    only the trimmed form the service produced), and anything outside
    ``[min_len, max_len]``.
    """
    value = payload.get(key)
    if not isinstance(value, str):
        raise SchemaValidationError(
            f"{cls_name}: {key!r} must be a string"
        )
    if value != value.strip():
        raise SchemaValidationError(
            f"{cls_name}: {key!r} must not have leading or trailing whitespace"
        )
    if not (min_len <= len(value) <= max_len):
        raise SchemaValidationError(
            f"{cls_name}: {key!r} must be between {min_len} and {max_len} "
            f"characters, got {len(value)}"
        )
    return value


@home("personal")
@publication_band(max="raw")
@singleton(key="default")
class UserProfileV1(SettingSchema):
    """The person's one mutable Personal profile.

    Key: the singleton ``default`` label. Payload: the mutable presentation
    a person authors for themselves. ``display_name`` is required; the
    remaining text fields are optional; the two avatar references are optional
    and server-owned; ``updated_at`` is required and stamped on every write.
    """

    set_id = USER_PROFILE_SET_ID
    schema_revision = USER_PROFILE_REVISION

    display_name: str = field(
        required=True,
        description=(
            "The person's chosen display name — the Personal presentation "
            "other surfaces consume. Trimmed, 1–120 characters."
        ),
    )
    biography: str = field(
        required=False,
        description=(
            "Optional short biography/byline. Trimmed, at most 500 "
            "characters; an empty string is a valid cleared value."
        ),
    )
    initials: str = field(
        required=False,
        description=(
            "Optional explicit initials override, trimmed, 1–4 characters. "
            "Absent means 'derive from display_name'; an empty value is never "
            "persisted as an override."
        ),
    )
    avatar_attachment_id: str = field(
        required=False,
        description=(
            "UUID of the canonical 512x512 WebP in the personal attachment "
            "store; server-owned, written only by the avatar routes."
        ),
    )
    avatar_icon_data_uri: str = field(
        required=False,
        description=(
            "Bounded 64x64 WebP data: URI — the portable compact avatar shown "
            "inline; server-owned, written only by the avatar routes."
        ),
    )
    updated_at: str = field(
        required=True,
        description="ISO-8601 UTC timestamp of the last profile write.",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return

        allowed = {
            "display_name", "biography", "initials",
            "avatar_attachment_id", "avatar_icon_data_uri", "updated_at",
        }
        extra = set(payload) - allowed
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )

        if "display_name" not in payload:
            raise SchemaValidationError(
                f"{cls.__name__}: missing required field 'display_name'"
            )
        _require_trimmed_str(
            payload, "display_name", cls.__name__,
            min_len=1, max_len=DISPLAY_NAME_MAX,
        )

        # Biography: optional; empty string is a valid cleared value.
        if "biography" in payload:
            _require_trimmed_str(
                payload, "biography", cls.__name__,
                min_len=0, max_len=BIOGRAPHY_MAX,
            )

        # Initials: optional EXPLICIT override. An empty value means "derive"
        # and must never be stored as an override, so 1–4 chars when present.
        if "initials" in payload:
            _require_trimmed_str(
                payload, "initials", cls.__name__,
                min_len=1, max_len=INITIALS_MAX,
            )

        if "avatar_attachment_id" in payload:
            cls._validate_attachment_id(payload["avatar_attachment_id"])

        if "avatar_icon_data_uri" in payload:
            cls._validate_icon_data_uri(payload["avatar_icon_data_uri"])

        if "updated_at" not in payload:
            raise SchemaValidationError(
                f"{cls.__name__}: missing required field 'updated_at'"
            )
        cls._validate_iso_ts(payload["updated_at"])

    # ── field validators ──────────────────────────────────────

    @classmethod
    def _validate_attachment_id(cls, value: Any) -> None:
        if not isinstance(value, str):
            raise SchemaValidationError(
                f"{cls.__name__}: 'avatar_attachment_id' must be a string"
            )
        try:
            canonical = str(_uuid.UUID(value))
        except (ValueError, AttributeError, TypeError) as exc:
            raise SchemaValidationError(
                f"{cls.__name__}: 'avatar_attachment_id' must be a canonical "
                f"UUID, got {value!r}"
            ) from exc
        # Reject a non-canonical spelling (uppercase, braces, urn:) so the
        # stored id is byte-identical to the attachment store's own id.
        if canonical != value:
            raise SchemaValidationError(
                f"{cls.__name__}: 'avatar_attachment_id' must be the canonical "
                f"lowercase UUID form, got {value!r}"
            )

    @classmethod
    def _validate_icon_data_uri(cls, value: Any) -> None:
        if not isinstance(value, str) or not value.startswith(
            AVATAR_ICON_DATA_URI_PREFIX
        ):
            raise SchemaValidationError(
                f"{cls.__name__}: 'avatar_icon_data_uri' must be a "
                f"'{AVATAR_ICON_DATA_URI_PREFIX}...' URI"
            )
        if len(value) > AVATAR_ICON_DATA_URI_MAX_CHARS:
            raise SchemaValidationError(
                f"{cls.__name__}: 'avatar_icon_data_uri' exceeds "
                f"{AVATAR_ICON_DATA_URI_MAX_CHARS} characters"
            )
        b64 = value[len(AVATAR_ICON_DATA_URI_PREFIX):]
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise SchemaValidationError(
                f"{cls.__name__}: 'avatar_icon_data_uri' payload is not valid "
                f"base64"
            ) from exc
        if not raw:
            raise SchemaValidationError(
                f"{cls.__name__}: 'avatar_icon_data_uri' decodes to empty bytes"
            )

    @classmethod
    def _validate_iso_ts(cls, value: Any) -> None:
        if not isinstance(value, str) or len(value) > 32:
            raise SchemaValidationError(
                f"{cls.__name__}: 'updated_at' must be an ISO-8601 UTC "
                f"timestamp string"
            )
        try:
            datetime.strptime(value, _ISO_FORMAT)
        except ValueError as exc:
            raise SchemaValidationError(
                f"{cls.__name__}: 'updated_at' must be an ISO-8601 UTC "
                f"timestamp like 2026-07-17T00:00:00Z, got {value!r}"
            ) from exc
