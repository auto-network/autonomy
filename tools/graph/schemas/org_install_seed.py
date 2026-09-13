"""``autonomy.machine.org-install-seed`` — what a joiner's machine was told
about an organization at install, kept where it can never replicate.

When this machine is ADMITTED to an organization, the sponsor serves it
some install material over the org:join channel: a few directory rows
(names to show) and a few reachability rows (machines to dial). Those
rows are the SPONSOR'S and its peers' facts. Writing them into this
machine's copy of the org-homed sets would make them THIS machine's
authored writes, and the next org pull would carry them back to every
member under this member's identity (finding 2026-09-13: Bob writing
Alice's rows, which then synced back to Alice).

So the seed lives here, in the machine store, which never leaves the
machine. The two readers (the member listing, the org peer candidates)
consult it ONLY for a persona or machine that has no replicated row yet;
the first org pull brings the real rows, and from then on the seed is
shadowed. The joiner's own directory row is not seed: it is written from
their Personal profile into the org set, because that one is theirs.

KEY — ``<org slug>:<kind>:<row key>``; ``kind`` is ``member_profile`` (row
key: persona public key) or ``reachability`` (row key: machine public
key). The payload carries the served row verbatim under ``row``; the
reachability row keeps its persona certificate and signature, so a
reader verifies it exactly as it verifies a replicated row.
"""
from __future__ import annotations

import re
from typing import Any

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    home,
    keyed_per_entity,
    publication_band,
)

ORG_INSTALL_SEED_SET_ID = "autonomy.machine.org-install-seed"
ORG_INSTALL_SEED_REVISION = 1

KIND_MEMBER_PROFILE = "member_profile"
KIND_REACHABILITY = "reachability"
KINDS = (KIND_MEMBER_PROFILE, KIND_REACHABILITY)

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

SYNOPSIS = {
    "summary": (
        "Machine-local install seed for an organization this machine joined: "
        "the directory and reachability rows the sponsor served over org:join, "
        "read only where no replicated row exists yet. Never replicates."
    ),
    "nouns": ["install", "seed", "join", "bootstrap", "directory", "reachability"],
    "related_set_ids": [
        "autonomy.org.member-profile", "autonomy.org.fleet-reachability",
    ],
}


def seed_key(slug: str, kind: str, row_key: str) -> str:
    return f"{slug}:{kind}:{row_key}"


def seed_prefix(slug: str, kind: str) -> str:
    """The ``prefix=`` for a settings read of one org's rows of one kind
    (settings_ops appends the ``:`` separator itself)."""
    return f"{slug}:{kind}"


@publication_band(max="raw")
@home("machine")
@keyed_per_entity(key_strategy="org_kind_row")
class OrgInstallSeedV1(SettingSchema):
    """One served row, held locally until the replicated row arrives."""

    set_id = ORG_INSTALL_SEED_SET_ID
    schema_revision = ORG_INSTALL_SEED_REVISION

    _field_metadata: dict[str, dict] = {
        "organization": {
            "type": "string", "required": True,
            "description": "The local org slug the seed was installed for",
        },
        "kind": {
            "type": "string", "required": True,
            "description": "member_profile (persona-keyed) or reachability (machine-keyed)",
        },
        "row_key": {
            "type": "string", "required": True,
            "description": "The row's key in its replicated set (64 hex)",
        },
        "row": {
            "type": "object", "required": True,
            "description": "The served row, as the replicated set would hold it",
        },
        "installed_at": {
            "type": "integer", "required": True,
            "description": "Unix seconds when the seed was installed",
        },
    }

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, got {type(payload).__name__}"
            )
        slug = payload.get("organization")
        if not isinstance(slug, str) or not slug:
            raise SchemaValidationError(f"{cls.__name__}: 'organization' must be a non-empty string")
        if payload.get("kind") not in KINDS:
            raise SchemaValidationError(f"{cls.__name__}: 'kind' must be one of {KINDS}")
        row_key = payload.get("row_key")
        if not isinstance(row_key, str) or not _HEX64_RE.match(row_key):
            raise SchemaValidationError(f"{cls.__name__}: 'row_key' must be 64 lowercase hex chars")
        if not isinstance(payload.get("row"), dict):
            raise SchemaValidationError(f"{cls.__name__}: 'row' must be an object")
        installed_at = payload.get("installed_at")
        if isinstance(installed_at, bool) or not isinstance(installed_at, int) or installed_at < 0:
            raise SchemaValidationError(f"{cls.__name__}: 'installed_at' must be a non-negative integer")
