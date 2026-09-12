"""The mutable Personal profile service (``autonomy.user#1/default``).

The person's ONE Personal presentation record — the source Profile Settings'
**Personal** screen edits and the source the invitation "Joining as" preview
reads. Distinct from the immutable personal ROOT
(``autonomy.identity.personal``, which supplies only the read-time legacy
display-name fallback), from organization branding (``autonomy.org``), and
from org-member presentation (``autonomy.org.member-profile``). Decided by
``graph://4f9e881c-a9`` §7 and comments ``682c929c-7b8`` / ``8cc8b2ed-5ae``.

This module owns:

* :func:`personal_identity_member` — the canonical personal-identity row
  selection (moved here from ``identity_routes._personal_member``; that name
  remains as an import alias so every existing caller and its tests are
  unchanged).
* :func:`profile_member` — the canonical ``autonomy.user#1/default`` row.
* :func:`effective_initials` — explicit override wins, else derive from the
  display name.
* :func:`get_effective_profile` — the profile to present, synthesizing an
  UNPERSISTED baseline from the personal root when no profile row exists.
  Reads never write.
* :func:`update_profile` — the PATCH merge (text fields only, avatar fields
  preserved, ``updated_at`` stamped, written under
  ``settings_ops.identity_write_context``).
* :func:`serialize_profile` — the wire shape shared by the profile route and
  identity status.

Every read and write is pinned to the personal store: the readers use
``read_owned_set(..., org=None)`` (``peers=[]``) and the writer
``upsert_by_key(..., org=None)``, so a request organization header can never
move Personal presentation into or out of an organization database.
"""

from __future__ import annotations

import time
from typing import Any

from tools.dashboard.network_routes import _first_member
from tools.graph import settings_ops
from tools.graph.schemas.personal_identity import PERSONAL_IDENTITY_SET_ID
from tools.graph.schemas.user import (
    USER_PROFILE_CANONICAL_LABEL,
    USER_PROFILE_REVISION,
    USER_PROFILE_SET_ID,
    DISPLAY_NAME_MAX,
    BIOGRAPHY_MAX,
    INITIALS_MAX,
)

#: The one label the canonical personal identity and Personal profile are
#: ever written under (mirrors ``identity_routes.PERSONAL_CANONICAL_LABEL``,
#: re-exported there from this module so there is one source of truth).
PERSONAL_CANONICAL_LABEL = "default"

#: The exact fields a PATCH may carry — text only. Avatar references are
#: server-owned and written solely by the follow-on avatar routes.
PATCH_FIELDS = ("display_name", "biography", "initials")


class ProfileValidationError(ValueError):
    """A profile PATCH body was malformed (unknown field, wrong type, or an
    out-of-bounds value). Surfaces as 400."""


class NoPersonalIdentity(RuntimeError):
    """A profile write was attempted with no canonical personal identity.

    A Personal profile cannot exist before the personal root does; the write
    is refused rather than creating an ownerless row. Surfaces as 409."""


# ── canonical row selection ───────────────────────────────────


def personal_identity_member():
    """The canonical personal identity row.

    Moved verbatim from ``identity_routes._personal_member`` (which now imports
    this as an alias). Defense-in-depth against a shadowing row:
    ``_first_member`` picks the lexically-FIRST key, so a stray
    ``autonomy.identity.personal`` row with a low-sorting key would shadow the
    operator's ``default``. The selection is pinned to the canonical
    ``default`` label; only when no ``default`` exists (legacy rows predating
    the label) does it fall back to the first member.
    """
    members = [
        m for m in settings_ops.read_owned_set(
            PERSONAL_IDENTITY_SET_ID, org=None,
        ).members
        if isinstance(m.payload, dict)
    ]
    for m in members:
        if m.key == PERSONAL_CANONICAL_LABEL:
            return m
    return _first_member(PERSONAL_IDENTITY_SET_ID, None)


def profile_member():
    """The canonical ``autonomy.user#1/default`` profile row, or ``None``.

    Pinned strictly to the singleton ``default`` key — there is no
    first-member fallback, because the set is protected and never carries a
    legacy alternate label.
    """
    members = [
        m for m in settings_ops.read_owned_set(
            USER_PROFILE_SET_ID, org=None,
        ).members
        if isinstance(m.payload, dict)
    ]
    for m in members:
        if m.key == USER_PROFILE_CANONICAL_LABEL:
            return m
    return None


def _canonical_identity_member():
    """The personal identity row when it is a usable canonical root.

    A row without stored armor is not a canonical personal identity (it cannot
    identify a durable owner), matching the ``armored_private_key`` gate the
    status route already applies. Returns ``None`` otherwise.
    """
    member = personal_identity_member()
    if member is None or not isinstance(member.payload, dict):
        return None
    if not member.payload.get("armored_private_key"):
        return None
    return member


# ── initials derivation ───────────────────────────────────────


def derive_initials(display_name: Any) -> str:
    """Initials derived from a display name: first letter of the first two
    words, uppercased. Empty when the name has no usable characters."""
    if not isinstance(display_name, str):
        return ""
    words = [w for w in display_name.strip().split() if w]
    letters = [w[0] for w in words[:2]]
    return "".join(letters).upper()


def effective_initials(display_name: Any, explicit: Any = None) -> str:
    """The initials to present: an explicit non-empty override wins;
    otherwise derive from ``display_name``."""
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return derive_initials(display_name)


# ── effective profile (read) ──────────────────────────────────


def get_effective_profile() -> dict | None:
    """The Personal profile to present, or ``None`` with no personal identity.

    * No canonical personal identity → ``None`` (there is no person to profile).
    * A canonical root but no profile row → an UNPERSISTED baseline: the root
      display name, blank biography, derived initials, no avatar
      (``persisted: False``). This synthesizes; it never writes.
    * A profile row → its stored values (``persisted: True``).

    The returned dict carries the raw stored ``initials`` OVERRIDE (or ``None``)
    under ``initials``; :func:`serialize_profile` computes the effective
    initials for the wire.
    """
    identity = _canonical_identity_member()
    if identity is None:
        return None
    profile = profile_member()
    if profile is not None:
        p = profile.payload
        return {
            "display_name": p.get("display_name") or "",
            "biography": p.get("biography", "") or "",
            "initials": p.get("initials"),
            "avatar_attachment_id": p.get("avatar_attachment_id"),
            "avatar_icon_data_uri": p.get("avatar_icon_data_uri"),
            "updated_at": p.get("updated_at"),
            "persisted": True,
        }
    # Root identity but no profile row: synthesize, do not persist.
    root_name = (identity.payload.get("display_name") or "").strip()
    return {
        "display_name": root_name,
        "biography": "",
        "initials": None,
        "avatar_attachment_id": None,
        "avatar_icon_data_uri": None,
        "updated_at": None,
        "persisted": False,
    }


def serialize_profile(effective: dict | None) -> dict | None:
    """The wire shape shared by ``GET /api/identity/profile`` and status.

    Adds the derived effective ``initials`` and surfaces the explicit override
    separately as ``initials_override`` (``None`` when derivation is in
    force). Returns ``None`` unchanged so ``profile: null`` is honest."""
    if effective is None:
        return None
    display_name = effective.get("display_name") or ""
    explicit = effective.get("initials")
    override = explicit if (isinstance(explicit, str) and explicit) else None
    return {
        "display_name": display_name,
        "biography": effective.get("biography", "") or "",
        "initials": effective_initials(display_name, explicit),
        "initials_override": override,
        "avatar_attachment_id": effective.get("avatar_attachment_id"),
        "avatar_icon_data_uri": effective.get("avatar_icon_data_uri"),
        "updated_at": effective.get("updated_at"),
        "persisted": bool(effective.get("persisted")),
    }


# ── update (write) ────────────────────────────────────────────


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _persist_profile(base: dict) -> dict | None:
    """Commit the Personal profile row, then invalidate its identity projection.

    The single seam every Personal-profile mutation — text
    (:func:`update_profile`), avatar activation (:func:`set_avatar`), and avatar
    removal (:func:`clear_avatar`) — writes through. The write goes through the
    protected :func:`settings_ops.identity_write_context` pinned to ``org=None``
    (the generic Settings API cannot touch this set). After it commits, the
    org-identity resolver's Personal generation is advanced so
    ``resolve_org_identity("personal")`` rebuilds with the new name / initials /
    avatar on the very next read — no other org's cached identity is disturbed
    and no process restart is required (auto-vlt7j.3). Returns the freshly
    serialized effective profile.
    """
    with settings_ops.identity_write_context():
        settings_ops.upsert_by_key(
            USER_PROFILE_SET_ID, USER_PROFILE_REVISION,
            USER_PROFILE_CANONICAL_LABEL, base, org=None,
        )
    # Lazy import avoids an org_identity ↔ personal_profile import cycle: the
    # resolver reads this module's effective profile during the overlay.
    from tools.dashboard import org_identity
    org_identity.invalidate_personal_identity()
    return serialize_profile(get_effective_profile())


def _merge_text_field(base: dict, key: str, value: Any) -> None:
    """Apply one validated PATCH text field onto ``base`` in place."""
    if not isinstance(value, str):
        raise ProfileValidationError(f"{key!r} must be a string")
    trimmed = value.strip()
    if key == "display_name":
        if not (1 <= len(trimmed) <= DISPLAY_NAME_MAX):
            raise ProfileValidationError(
                f"'display_name' must be 1–{DISPLAY_NAME_MAX} characters after "
                f"trimming"
            )
        base["display_name"] = trimmed
    elif key == "biography":
        if len(trimmed) > BIOGRAPHY_MAX:
            raise ProfileValidationError(
                f"'biography' must be at most {BIOGRAPHY_MAX} characters"
            )
        # An empty biography is a valid cleared value.
        base["biography"] = trimmed
    elif key == "initials":
        # A blank initials value restores derivation — the override is removed
        # rather than stored as an empty string.
        if not trimmed:
            base.pop("initials", None)
        elif len(trimmed) > INITIALS_MAX:
            raise ProfileValidationError(
                f"'initials' must be 1–{INITIALS_MAX} characters when set"
            )
        else:
            base["initials"] = trimmed


def update_profile(fields: Any) -> dict | None:
    """Merge a PATCH of the Personal profile's text fields and persist it.

    Accepts exactly :data:`PATCH_FIELDS` (``display_name``, ``biography``,
    ``initials``); at least one must be present. Omitted values are read-merged
    from the current row (or the root baseline when no row exists yet), so a
    partial PATCH never drops fields. Every avatar reference already stored is
    preserved untouched. ``updated_at`` is stamped and the write goes through
    :func:`settings_ops.identity_write_context` — the generic Settings API
    cannot write this protected set.

    Raises :class:`NoPersonalIdentity` when no canonical personal root exists
    (a profile cannot be created before its owner) and
    :class:`ProfileValidationError` for a malformed body.
    """
    if not isinstance(fields, dict):
        raise ProfileValidationError("body must be a JSON object")
    unknown = set(fields) - set(PATCH_FIELDS)
    if unknown:
        raise ProfileValidationError(
            f"unknown field(s): {sorted(unknown)}; PATCH accepts only "
            f"{list(PATCH_FIELDS)}"
        )
    present = [k for k in PATCH_FIELDS if k in fields]
    if not present:
        raise ProfileValidationError(
            f"at least one of {list(PATCH_FIELDS)} is required"
        )

    identity = _canonical_identity_member()
    if identity is None:
        raise NoPersonalIdentity(
            "no canonical personal identity exists — create the personal root "
            "before writing a Personal profile"
        )

    existing = profile_member()
    if existing is not None:
        base = dict(existing.payload)
    else:
        base = {"display_name": (identity.payload.get("display_name") or "").strip()}

    for key in present:
        _merge_text_field(base, key, fields[key])

    base["updated_at"] = _now_iso()

    return _persist_profile(base)


# ── avatar (server-owned references) ──────────────────────────
#
# The two avatar references (``avatar_attachment_id`` + ``avatar_icon_data_uri``)
# are NOT part of :data:`PATCH_FIELDS`; a text PATCH can neither set nor erase
# them. They are set only here, by the avatar routes, after the shared
# :mod:`profile_image` processor has produced the canonical WebP (stored as a
# Personal attachment) and the bounded compact data URI. Both operations
# require the canonical personal root, preserve every text field, stamp
# ``updated_at``, and write through the same protected
# :func:`settings_ops.identity_write_context` seam pinned to ``org=None`` — so
# a caller-org header can never move the Personal row into an org database.


def set_avatar(attachment_id: str, icon_data_uri: str) -> dict | None:
    """Activate the two server-owned avatar references on the Personal profile.

    ``attachment_id`` is the canonical 512x512 WebP's Personal attachment id and
    ``icon_data_uri`` the bounded compact 64x64 WebP ``data:`` URI. Every text
    field already stored is preserved; only the two avatar fields (and
    ``updated_at``) change. When no profile row exists yet the row is created
    from the personal-root baseline, so a person can set a photo before entering
    any text. The write is the LAST step of an upload — it activates a reference
    only after the attachment has been stored.

    Raises :class:`NoPersonalIdentity` when no canonical personal root exists.
    """
    identity = _canonical_identity_member()
    if identity is None:
        raise NoPersonalIdentity(
            "no canonical personal identity exists — create the personal root "
            "before setting a Personal avatar"
        )
    existing = profile_member()
    if existing is not None:
        base = dict(existing.payload)
    else:
        base = {"display_name": (identity.payload.get("display_name") or "").strip()}

    base["avatar_attachment_id"] = attachment_id
    base["avatar_icon_data_uri"] = icon_data_uri
    base["updated_at"] = _now_iso()

    return _persist_profile(base)


def clear_avatar() -> dict | None:
    """Drop both avatar references, returning to the initials/color fallback.

    Idempotent: with a canonical root but no profile row, or a row that already
    carries no avatar, nothing is written and the current effective profile is
    returned unchanged. The immutable, content-addressed attachment blob is
    NEVER deleted — only the active references are cleared, so a shared or
    re-referenced blob survives. Every text field is preserved.

    Raises :class:`NoPersonalIdentity` when no canonical personal root exists.
    """
    identity = _canonical_identity_member()
    if identity is None:
        raise NoPersonalIdentity(
            "no canonical personal identity exists — create the personal root "
            "before clearing a Personal avatar"
        )
    existing = profile_member()
    if existing is None:
        # No row to clear; removal is idempotent and never creates one.
        return serialize_profile(get_effective_profile())
    payload = existing.payload
    if not (payload.get("avatar_attachment_id") or payload.get("avatar_icon_data_uri")):
        # Already avatar-free — do not bump updated_at for a no-op removal.
        return serialize_profile(get_effective_profile())

    base = dict(payload)
    base.pop("avatar_attachment_id", None)
    base.pop("avatar_icon_data_uri", None)
    base["updated_at"] = _now_iso()

    return _persist_profile(base)
