"""Schema: ``autonomy.harness.bootstrap#1``.

The pre-agent Layer-0 discovery record: *which coding harness this host has
verified as runnable, right now*. One row per harness slug (``claude`` /
``codex``), written by the deterministic first-launch bootstrap flow
(``GET /bootstrap`` — the one part of clean-room install that cannot be
agent-driven, because the onboarding agent needs a harness to exist first).

A row is written only after the bootstrap flow has confirmed the CLI resolves
on PATH with a parseable ``--version`` AND (when ``auth == "ok"``) that one
authenticated no-op invocation succeeded. Layer 1 (agent-driven onboarding,
H4 auto-inpkd) reads this set to decide it can launch a real session.

Contract (bead auto-n130b): the payload carries **discovery results only** —
harness slug, resolved path, version string, an ``auth`` flag, and a
verification timestamp. It NEVER carries tokens or credential material.
Credential import is auto-5bq85's lane; secure vaulting is C5. Auth always
stays inside the harness's own tooling — this row only records *that* the
harness authenticated, never *how*.

Authority is ``personal``: the correct value depends entirely on this host
(what is installed here, at what path, at what version), so it can only be
right for this operator — cf. the org-scope rubric graph://4d88c2ad-625 and
the sibling ``dashboard.harness.usage`` set.

Design: harness-bootstrap comment on graph://dc310166-911.
"""

from __future__ import annotations

from typing import Any

from .registry import (
    home,
    SchemaValidationError,
    SettingSchema,
    field,
    keyed_per_entity,
)


SET_ID = "autonomy.harness.bootstrap"
SCHEMA_REVISION = 1

# Host-local operator fact — see rubric graph://4d88c2ad-625 § "one-line rule".
HARNESS_BOOTSTRAP_ORG = "machine"

VALID_HARNESSES = ("claude", "codex")
VALID_AUTH = ("ok", "missing")


SYNOPSIS = {
    "summary": (
        "Pre-agent harness bootstrap discovery record. One row per verified "
        "coding harness (claude/codex) on this host: slug, resolved path, "
        "version, an auth flag, and verification timestamp. Discovery results "
        "only — never tokens or credential material. Read by Layer-1 "
        "onboarding to decide a real session can launch."
    ),
    "nouns": [
        "harness bootstrap",
        "clean-room install",
        "claude code",
        "codex",
        "harness discovery",
        "first launch",
    ],
    "related_set_ids": [
        "dashboard.harness.usage#1",
        "dashboard.claude.setup_tokens#1",
    ],
}


@home("machine")
@keyed_per_entity(key_strategy="harness_name")
class HarnessBootstrapV1(SettingSchema):
    """Per-harness clean-room bootstrap discovery row.

    Keyed by harness slug (``claude`` / ``codex``). ``auth == "ok"`` means the
    flow ran one authenticated no-op that exited 0; ``auth == "missing"`` means
    the CLI resolved and reported a version but the no-op failed (state
    ``installed-needs-sign-in``). The bootstrap gate treats only ``auth ==
    "ok"`` rows as a verified harness.
    """

    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    harness: str = field(
        required=True,
        enum=list(VALID_HARNESSES),
        description="Harness slug this row reports for (matches the key).",
    )
    path: str = field(
        required=True,
        exists="executable",
        exists_frame="platform-host",
        description="Resolved absolute path of the CLI on this host's PATH.",
    )
    version: str = field(
        required=True,
        description="Parsed version string reported by the CLI's --version.",
    )
    auth: str = field(
        required=True,
        enum=list(VALID_AUTH),
        description=(
            "'ok' when one authenticated no-op invocation succeeded; "
            "'missing' when the CLI is present but not signed in. Never a "
            "token or any credential material."
        ),
    )
    verified_at: str = field(
        required=True,
        description="ISO-8601 timestamp of the verification that wrote this row.",
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
        for req in ("harness", "path", "version", "auth", "verified_at"):
            if not payload.get(req):
                raise SchemaValidationError(
                    f"{cls.__name__}: '{req}' is required"
                )
        harness = payload.get("harness")
        if harness not in VALID_HARNESSES:
            raise SchemaValidationError(
                f"{cls.__name__}: harness must be one of {VALID_HARNESSES}, "
                f"got {harness!r}"
            )
        auth = payload.get("auth")
        if auth not in VALID_AUTH:
            raise SchemaValidationError(
                f"{cls.__name__}: auth must be one of {VALID_AUTH}, "
                f"got {auth!r}"
            )
