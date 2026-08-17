"""Schema: ``autonomy.source_control.review_state#1``.

Capability-cached vendor-shape review snapshot, keyed by
``<repo_slug>:<review_id>``. The dashboard reads this cache when
composing the per-row source_control snapshot; the operator-explicit
refresh path repopulates it via REST (separate quota from GraphQL,
ETag 304s are free).

Lifetime: per-org, persists across worktree cleanup. The binding row
dies on worktree discard but the cache survives so reopening the same
PR doesn't re-pay the fetch cost.

Atomicity: every field overwrites together on each refresh — there is
no partial update path.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .registry import (
    home,
    home,
    keyed_per_entity,
    SchemaValidationError,
    SettingSchema,
    cache,
    field,
)


SET_ID = "autonomy.source_control.review_state"
SCHEMA_REVISION = 1


VALID_REVIEW_STATES = ("open", "closed", "merged")
VALID_CHECK_STATUSES = ("pass", "fail", "running", "pending")


SYNOPSIS = {
    "summary": (
        "Cached PR/review state snapshots from source-control providers. "
        "One row per (repo_slug, review_id); refreshed via REST with ETag."
    ),
    "nouns": [
        "review state", "PR cache", "review cache",
        "head sha", "etag", "checks",
    ],
    "related_set_ids": [
        "autonomy.worktree.review_binding#1",
    ],
}


def _validate_check(entry: Any, idx: int, cls_name: str) -> None:
    if not isinstance(entry, dict):
        raise SchemaValidationError(
            f"{cls_name}: checks[{idx}] must be an object, "
            f"got {type(entry).__name__}"
        )
    for required in ("id", "label", "status"):
        if required not in entry:
            raise SchemaValidationError(
                f"{cls_name}: checks[{idx}] missing required field {required!r}"
            )
        val = entry[required]
        if not isinstance(val, str) or not val:
            raise SchemaValidationError(
                f"{cls_name}: checks[{idx}].{required} must be a non-empty string"
            )
    status = entry["status"]
    if status not in VALID_CHECK_STATUSES:
        raise SchemaValidationError(
            f"{cls_name}: checks[{idx}].status must be one of "
            f"{VALID_CHECK_STATUSES}, got {status!r}"
        )
    if "detail" in entry and entry["detail"] is not None \
            and not isinstance(entry["detail"], str):
        raise SchemaValidationError(
            f"{cls_name}: checks[{idx}].detail must be a string or null"
        )
    extra = set(entry) - {"id", "label", "status", "detail"}
    if extra:
        raise SchemaValidationError(
            f"{cls_name}: checks[{idx}] has unknown field(s): {sorted(extra)}"
        )


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
@cache(ttl=timedelta(days=30))
@keyed_per_entity(key_strategy="repo_slug:review_id")
class SourceControlReviewStateV1(SettingSchema):
    """Capability-cached vendor-shape review snapshot.

    Key: ``<repo_slug>:<review_id>``.
    Lifetime: per-org, persists across worktree cleanup.
    Atomicity: every field overwrites together on each refresh.
    Cache TTL: 30 days.

    NOT stored (derivable, computed at resolver/render time):

    * ``terminal: bool`` — use :attr:`is_terminal`
    * ``running: bool`` — derive from ``checks``
    * ``aggregate_state: str`` — derive from ``checks`` + ``state``
    * ``number: int`` — UI does ``'PR #' + review_id`` for github provider
    * per-check ``icon`` — UI computes from ``label``
    * ``commit_shas`` — out of cache scope
    * ``fetched_at`` / ``refresh_after`` — Setting row's intrinsic
      ``updated_at`` covers it
    """

    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    title: str = field(required=True, description="Review title")
    body: str = field(required=True, description="Review body / description")
    state: str = field(
        required=True,
        enum=list(VALID_REVIEW_STATES),
        description="Review lifecycle state",
    )
    node_id: str = field(
        required=False,
        description="GitHub node ID / subscribable ID for PR-level subscription",
    )
    head_sha: str = field(
        required=True,
        description="Latest commit SHA on the review's head branch",
    )
    base_sha: str = field(
        required=True,
        description="Review's base commit SHA at fetch time",
    )
    base_branch: str = field(
        required=True,
        description="Review's base ref name (e.g. 'main')",
    )
    is_draft: bool = field(
        default=False,
        description="True if the review is in draft state",
    )
    provider: str = field(
        required=True,
        description="Source-control provider slug (e.g. 'github')",
    )
    provider_state: str = field(
        required=False,
        description="Provider-native opaque JSON blob (debug payload)",
    )
    etag: str = field(
        required=False,
        description="HTTP ETag returned by the provider for conditional refresh",
    )
    url: str = field(
        required=False,
        description="Web URL for the review (for badge link-out)",
    )
    checks: list = field(
        required=False,
        description=(
            "Latest fetched check entries for ``head_sha``; running or terminal"
        ),
        element={
            "id":     {"type": "string", "required": True},
            "label":  {"type": "string", "required": True},
            "status": {"type": "string", "required": True,
                       "enum": list(VALID_CHECK_STATUSES)},
            "detail": {"type": "string"},
        },
    )
    checks_stable: bool = field(
        default=False,
        description=(
            "True once this exact head_sha has reported an identical "
            "check count on two consecutive fetches. GitHub does not "
            "create all of a push's check-runs atomically, so a single "
            "fetch showing nothing running/pending can still be an "
            "incomplete set; the terminal-fire notifier requires this "
            "flag before treating the review as truly done."
        ),
    )

    @classmethod
    def is_terminal_payload(cls, payload: dict) -> bool:
        """True iff no checks are running/pending. Empty is vacuously terminal.

        Module-level helper because the schema is normally consumed as a
        dict from ``read_set`` (no instance is constructed); attribute-style
        access on the dict would fail. Kept on the class for discoverability.
        """
        checks = payload.get("checks") or []
        return all(
            (c.get("status") not in ("running", "pending"))
            for c in checks
        )

    @property
    def is_terminal(self) -> bool:
        """Instance form of :meth:`is_terminal_payload` for typed callers."""
        return self.is_terminal_payload(
            {"checks": getattr(self, "checks", None) or []}
        )

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )
        for required in ("title", "body", "state", "head_sha",
                         "base_sha", "base_branch", "provider"):
            if required not in payload:
                raise SchemaValidationError(
                    f"{cls.__name__}: missing required field {required!r}"
                )
            val = payload[required]
            if not isinstance(val, str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {required!r} must be a string, "
                    f"got {type(val).__name__}"
                )
        # ``head_sha`` and ``base_sha`` may legitimately be empty strings
        # only at the boundary where the provider hasn't reported a SHA
        # yet; reject empty for the others.
        for required in ("title", "state", "base_branch", "provider"):
            if not payload[required]:
                raise SchemaValidationError(
                    f"{cls.__name__}: {required!r} must be non-empty"
                )
        if payload["state"] not in VALID_REVIEW_STATES:
            raise SchemaValidationError(
                f"{cls.__name__}: 'state' must be one of "
                f"{VALID_REVIEW_STATES}, got {payload['state']!r}"
            )
        if "is_draft" in payload \
                and not isinstance(payload["is_draft"], bool):
            raise SchemaValidationError(
                f"{cls.__name__}: 'is_draft' must be a bool"
            )
        if "checks_stable" in payload \
                and not isinstance(payload["checks_stable"], bool):
            raise SchemaValidationError(
                f"{cls.__name__}: 'checks_stable' must be a bool"
            )
        for opt in ("provider_state", "etag", "url", "node_id"):
            if opt in payload and payload[opt] is not None \
                    and not isinstance(payload[opt], str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {opt!r} must be a string or null"
                )
        if "checks" in payload and payload["checks"] is not None:
            checks = payload["checks"]
            if not isinstance(checks, list):
                raise SchemaValidationError(
                    f"{cls.__name__}: 'checks' must be a list"
                )
            for idx, entry in enumerate(checks):
                _validate_check(entry, idx, cls.__name__)


def parse_state_key(key: str) -> tuple[str, str]:
    """Split a state key into ``(repo_slug, review_id)``.

    ``repo_slug`` is provider-shaped (e.g. ``owner/name``); ``review_id``
    is always the trailing colon-separated segment so a single
    ``rsplit`` reads correctly even when the slug carries a ``/``.
    """
    if ":" not in key:
        raise ValueError(
            f"state key must be '<repo_slug>:<review_id>', got {key!r}"
        )
    repo_slug, review_id = key.rsplit(":", 1)
    if not repo_slug or not review_id:
        raise ValueError(
            f"state key has empty segment: {key!r}"
        )
    return repo_slug, review_id
