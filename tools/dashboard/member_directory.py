"""The organization member directory (``autonomy.org.member-profile#1``):
one row per member, keyed by persona public key, carrying the presentation
that member chose for this organization.

Three writers, each at the one moment the value exists (design
graph://4f9e881c-a9 §7 and §10, punch list item 31):

* :func:`write_founder` — founding: the founder's Personal profile becomes
  their organization presentation.
* :func:`project_claim` — admission on the organization's side: the profile
  the joiner signed into their claim (name, biography, initials; never a
  photo, the ledger is not a blob store) becomes their row.
* :func:`write_self` — the joiner's own machine at install: their Personal
  profile, photo included, becomes their row. The rows the sponsor served
  for OTHER members are not written here: they are the machine-local
  install seed (tools/dashboard/org_install_seed), never this member's
  authored write.

Rows are org-homed and replicate with the organization, so they carry
CONTENT (the small icon as a data: URI), never a machine-local attachment
path. The Personal profile is a snapshot here, not a live alias: renaming
Personal later does not rewrite an organization's member row.
"""

from __future__ import annotations

from typing import Any

from tools.graph import settings_ops
from tools.graph.schemas.org_member_profile import (
    MEMBER_PROFILE_REVISION,
    MEMBER_PROFILE_SET_ID,
)

NAME_MAX = 200
BYLINE_MAX = 300
#: The 64x64 icon data URI the profile processor emits is at most 24,000
#: characters; anything larger is not an icon and is dropped.
AVATAR_DATA_URI_MAX = 24_000
_DATA_URI_PREFIXES = ("data:image/webp;base64,", "data:image/png;base64,",
                      "data:image/jpeg;base64,")


def _clean(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def bounded_avatar(value: Any) -> str:
    """A row-safe avatar: a bounded inline image data URI, else ''."""
    if not isinstance(value, str) or len(value) > AVATAR_DATA_URI_MAX:
        return ""
    return value if value.startswith(_DATA_URI_PREFIXES) else ""


def presentation_from_personal_profile() -> dict | None:
    """This machine's operator as they present themselves: the Personal
    profile's name, biography and icon. None with no personal identity."""
    from tools.dashboard import personal_profile

    profile = personal_profile.get_effective_profile()
    if not isinstance(profile, dict):
        return None
    name = _clean(profile.get("display_name"), NAME_MAX)
    if not name:
        return None
    return {
        "display_name": name,
        "byline": _clean(profile.get("biography"), BYLINE_MAX),
        "avatar": bounded_avatar(profile.get("avatar_icon_data_uri")),
        "color": "",
    }


def presentation_from_claim_profile(profile: Any) -> dict | None:
    """The presentation a joiner signed into their claim (auto-vlt7j/§10)."""
    if not isinstance(profile, dict):
        return None
    name = _clean(profile.get("display_name"), NAME_MAX)
    if not name:
        return None
    return {
        "display_name": name,
        "byline": _clean(profile.get("biography") or profile.get("byline"), BYLINE_MAX),
        "avatar": "",
        "color": "",
    }


def write_row(slug: str, persona_pub: str, presentation: dict) -> None:
    payload = {
        "display_name": _clean(presentation.get("display_name"), NAME_MAX),
        "byline": _clean(presentation.get("byline"), BYLINE_MAX),
        "avatar": bounded_avatar(presentation.get("avatar")),
        "color": _clean(presentation.get("color"), 32),
    }
    if not payload["display_name"]:
        return
    settings_ops.upsert_by_key(
        MEMBER_PROFILE_SET_ID, MEMBER_PROFILE_REVISION, persona_pub, payload,
        org=slug, state="published",
    )


def write_founder(slug: str, persona_pub: str) -> bool:
    presentation = presentation_from_personal_profile()
    if presentation is None:
        return False
    write_row(slug, persona_pub, presentation)
    return True


def write_self(slug: str, persona_pub: str) -> bool:
    return write_founder(slug, persona_pub)


def project_claim(slug: str, persona_pub: str, profile: Any) -> bool:
    """Admission on the org's side: the claim's signed profile becomes the
    joiner's row unless the joiner already authored one here."""
    presentation = presentation_from_claim_profile(profile)
    if presentation is None:
        return False
    try:
        existing = settings_ops.read_set_key(MEMBER_PROFILE_SET_ID, persona_pub, org=slug)
    except Exception:
        existing = None
    if existing is not None:
        return False
    write_row(slug, persona_pub, presentation)
    return True


def rows(slug: str) -> list[dict]:
    """Every directory row of *slug*, for a joiner's first install."""
    try:
        members = settings_ops.read_owned_set(
            MEMBER_PROFILE_SET_ID, org=slug, target_revision=MEMBER_PROFILE_REVISION,
        ).members
    except Exception:
        return []
    out = []
    for member in members:
        if isinstance(member.payload, dict) and member.payload.get("display_name"):
            out.append({"persona_pub": str(member.key), **member.payload})
    return out
