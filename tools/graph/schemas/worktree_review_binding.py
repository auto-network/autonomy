"""Schema: ``autonomy.worktree.review_binding#1``.

Operator/agent declaration that ties a worktree row to a specific code
review (e.g. a GitHub PR). Replaces the lossy branch-scan auto-detector
that broke under squash-merge ghost-commits and rate-limited under
fan-out: when a binding exists, the dashboard fetches PR state by id
over REST instead of scanning the branch.

Composite key: ``<session_name>:<repo_name>:<branch>:<review_id>``.

* Branch in the key enables prefix lookup ("all reviews on this branch").
* ``review_id`` in the key enables stacked PRs — N rows per branch, one
  binding per review. The resolver aggregates rows into a list.

``review_id`` is a string for vendor neutrality (GitHub PR numbers, Jira
keys, Linear issue identifiers all coexist under the same primitive).

Only ``base_sha`` lives on the binding. The cache
(``autonomy.source_control.review_state``) carries the authoritative
``head_sha`` so amends don't drift the binding row.

Lifetime: a binding dies when the worktree is discarded (cleanup
deletes by ``<session_name>:<repo_name>`` prefix). The cache survives.
"""

from __future__ import annotations

import re
from typing import Any

from .registry import (
    home,
    home,
    SchemaValidationError,
    SettingSchema,
    field,
    keyed_per_entity,
)


SET_ID = "autonomy.worktree.review_binding"
SCHEMA_REVISION = 1


SYNOPSIS = {
    "summary": (
        "Operator-declared binding from a worktree row to a code review. "
        "One row per (session, repo, branch, review_id); base_sha scopes "
        "the per-review diff."
    ),
    "nouns": [
        "review binding", "worktree binding", "PR binding",
        "stacked PR", "base sha",
    ],
    "related_set_ids": [
        "autonomy.source_control.review_state#1",
        "autonomy.workspace#1",
    ],
}


_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


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
@keyed_per_entity(key_strategy="session_name:repo:branch:review_id")
class WorktreeReviewBindingV1(SettingSchema):
    """Operator/agent declaration about a worktree row.

    Composite key: ``<session_name>:<repo_name>:<branch>:<review_id>``.
    Branch in the key enables prefix lookup "all reviews on this branch".
    ``review_id`` in the key enables stacked PRs (N rows per branch).
    Each row = one review. The resolver aggregates rows into a list.

    Lifetime: dies on worktree discard.
    """

    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    base_sha: str = field(
        required=True,
        description=(
            "Review's base commit SHA. For a non-stacked PR: the fork "
            "point with the integration branch. For a stacked PR: the "
            "previous PR's head_sha so the per-review diff is scoped "
            "to just this review's commits."
        ),
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
        if "base_sha" not in payload:
            raise SchemaValidationError(
                f"{cls.__name__}: missing required field 'base_sha'"
            )
        base_sha = payload["base_sha"]
        if not isinstance(base_sha, str) or not base_sha:
            raise SchemaValidationError(
                f"{cls.__name__}: 'base_sha' must be a non-empty string"
            )
        if not _HEX_RE.match(base_sha):
            raise SchemaValidationError(
                f"{cls.__name__}: 'base_sha' must be a hex string, "
                f"got {base_sha!r}"
            )


def parse_binding_key(key: str) -> tuple[str, str, str, str]:
    """Split a binding key into ``(session, repo, branch, review_id)``.

    Branch may legitimately contain ``/`` or other characters but never
    ``:`` for our session-named branches, so a simple 3-way split from
    the left followed by a single ``rsplit`` on ``:`` for ``review_id``
    is correct. ``review_id`` is always the last segment.
    """
    parts = key.split(":", 2)
    if len(parts) != 3:
        raise ValueError(
            f"binding key must be '<session>:<repo>:<branch>:<review_id>', "
            f"got {key!r}"
        )
    session, repo, tail = parts
    if ":" not in tail:
        raise ValueError(
            f"binding key missing review_id segment: {key!r}"
        )
    branch, review_id = tail.rsplit(":", 1)
    if not session or not repo or not branch or not review_id:
        raise ValueError(
            f"binding key has empty segment: {key!r}"
        )
    return session, repo, branch, review_id
