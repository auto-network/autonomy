"""Schema: ``autonomy.org.bootstrap-allowlist#1``.

Personal Setting recording the bootstrap public-surface allowlist of an
upstream reference org — which of that org's notes form the canonical /
published surface this deployment subscribes to. Lives in
``personal.db`` (like ``autonomy.org.peer-subscription#1``): what
reference content an operator pulls is per-operator, not shared.

Key: the upstream org slug (``autonomy`` for the canonical reference
content shipped with the repo). Payload mirrors the committed curation
allowlist (``tools/graph/curation/autonomy-bootstrap-allowlist.yaml``):
a ``version`` plus ``canonical`` / ``published`` source-id-prefix lists.

On the org's home deployment the allowlist is *executed* against the
org DB (bulk publication_state promotion, bead auto-mu1n1). On a fresh
deployment there is no content to promote — first-run init
(``tools/init``) seeds this Setting instead, so the deployment knows
the reference surface to fetch once federation transports exist.

Charter: graph://93cf3026-1df (Bootstrap Allowlist). Three-axis model:
graph://8cf067e3-ca3. First-run init design: graph://dc310166-911.
"""

from __future__ import annotations

from .registry import (
    home,
    keyed_per_entity,
    SchemaValidationError,
    SettingSchema,
)


SET_ID = "autonomy.org.bootstrap-allowlist"
SCHEMA_REVISION = 1


SYNOPSIS = {
    "summary": (
        "Bootstrap public-surface allowlist of an upstream reference org"
    ),
    "nouns": [
        "allowlist", "bootstrap", "curation", "public surface",
        "canonical", "published", "reference content",
    ],
    "related_set_ids": [
        "autonomy.org#1",
        "autonomy.org.peer-subscription#1",
    ],
}


_LIST_FIELDS = ("canonical", "published")


#: Not forced into any one store. This records that the question was
#: ASKED -- must this live in the operator's own database, or on
#: this machine alone? -- and answered no, which is different
#: from nobody having considered it.
#:
#: It is not a prohibition. The operator owns workspaces, so
#: their database is the organizational home of their own
#: things; reading this as "anywhere but personal" refuses
#: writes that are correct.
@home("organization")
@keyed_per_entity(key_strategy="org_slug")
class OrgBootstrapAllowlistV1(SettingSchema):
    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    _field_metadata: dict[str, dict] = {
        "version": {
            "type": "integer",
            "required": True,
            "description": (
                "Allowlist version, mirrors the committed curation YAML"
            ),
        },
        "canonical": {
            "type": "array",
            "required": True,
            "description": (
                "Source-id prefixes promoted to canonical on the upstream org"
            ),
            "element": {"type": "string"},
        },
        "published": {
            "type": "array",
            "required": True,
            "description": (
                "Source-id prefixes promoted to published on the upstream org"
            ),
            "element": {"type": "string"},
        },
        "source": {
            "type": "string",
            "description": (
                "Repo-relative path of the committed allowlist YAML this "
                "Setting was seeded from"
            ),
        },
    }

    @classmethod
    def validate(cls, payload: dict) -> None:
        super().validate(payload)

        extra = set(payload) - {"version", "source", *_LIST_FIELDS}
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown fields {sorted(extra)!r}; "
                f"the upstream org slug lives in the Setting key"
            )

        if "version" not in payload:
            raise SchemaValidationError(
                f"{cls.__name__}: 'version' is required"
            )
        if not isinstance(payload["version"], int) or isinstance(
            payload["version"], bool
        ):
            raise SchemaValidationError(
                f"{cls.__name__}: 'version' must be an integer, got "
                f"{type(payload['version']).__name__}"
            )

        if "source" in payload:
            src = payload["source"]
            if not isinstance(src, str) or not src:
                raise SchemaValidationError(
                    f"{cls.__name__}: 'source' must be a non-empty string"
                )

        for name in _LIST_FIELDS:
            if name not in payload:
                raise SchemaValidationError(
                    f"{cls.__name__}: {name!r} is required (use an empty "
                    f"list when the tier has no entries)"
                )
            entries = payload[name]
            if not isinstance(entries, list):
                raise SchemaValidationError(
                    f"{cls.__name__}: {name!r} must be a list, got "
                    f"{type(entries).__name__}"
                )
            for i, prefix in enumerate(entries):
                if not isinstance(prefix, str) or not prefix:
                    raise SchemaValidationError(
                        f"{cls.__name__}: {name}[{i}] must be a non-empty "
                        f"string source-id prefix"
                    )
