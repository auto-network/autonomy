"""``autonomy.org.session-runner#1`` — a member machine offering to run the
organization's members' sessions (Thrust 5, bead auto-a51qv; design
graph://7eb29bc8-31a §9.8).

One row per offering machine, keyed ``<owner persona>:<serving key>`` and
written by that machine alone: turning the org profile's "Allow organization
members to remotely launch Workspaces on this machine" on writes it, turning
it off deprecates it (operator ruling 2026-09-29). A turned-off offer refuses
NEW launches only; running sessions are untouched.

The key strategy ``persona_pub:*`` makes the boundary refuse any signer but
the key's persona, so each key has one eligible slot and the normal read is
correct. The row also carries the persona's certificate to the machine key
and the machine key's signature, as ``autonomy.org.fleet-reachability`` does:
that proves the persona owns the machine the key names
(tools/network/org_session_runner.verify_row).
"""

from __future__ import annotations

import re
from typing import Any

from .registry import (
    SchemaValidationError,
    SettingSchema,
    home,
    keyed_per_entity,
    publication_band,
)

SYNOPSIS = {
    "summary": (
        "A member machine offering to run the organization's members' "
        "sessions: its capacity, harnesses and workspace images. One row per "
        "machine, keyed by its serving key, written by that machine alone and "
        "self-certified like the org's reachability rows."
    ),
    "nouns": ["session runner", "runner offer", "remote launch", "organization member"],
    "related_set_ids": ["autonomy.org.fleet-reachability#2"],
}

ORG_SESSION_RUNNER_SET_ID = "autonomy.org.session-runner"
ORG_SESSION_RUNNER_REVISION = 1
ROW_VERSION = 1
#: Who may launch on an offered runner. The only value in this version.
ADMIT_ALL_MEMBERS = "all-members"
HARNESSES = ("claude", "codex", "grok")
MAX_IMAGES = 64
MAX_LABEL_CHARS = 128
MAX_CAPACITY = 64

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


@publication_band(min="raw", max="published")
@home("organization")
@keyed_per_entity(key_strategy="persona_pub:*")
class OrgSessionRunnerV1(SettingSchema):
    """One machine's offer to run members' sessions, self-certified."""

    set_id = ORG_SESSION_RUNNER_SET_ID
    schema_revision = ORG_SESSION_RUNNER_REVISION

    _field_metadata: dict[str, dict] = {
        "v": {"type": "integer", "required": True, "description": "Row version (1)"},
        "persona_pub": {"type": "string", "required": True,
                        "description": "The member persona that owns this machine (64 hex)"},
        "persona_cert": {"type": "object", "required": True,
                         "description": "The persona's certificate to the machine key "
                                        "(scope fleet:sync, org = the genesis id)"},
        "label": {"type": "string", "required": True,
                  "description": "The machine's display name"},
        "capacity": {"type": "integer", "required": True,
                     "description": "Concurrent member sessions this machine accepts"},
        "harnesses": {"type": "array", "required": True, "items": {"type": "string"},
                      "description": "Harnesses this machine can run"},
        "images": {"type": "array", "required": True, "items": {"type": "string"},
                   "description": "Workspace images present on this machine"},
        "admit": {"type": "string", "required": True,
                  "description": "Who may launch here: all-members"},
        "updated_at": {"type": "integer", "required": True,
                       "description": "Unix seconds when the offer last changed"},
        "sig": {"type": "string", "required": True,
                "description": "Machine-key signature over the domain-separated row"},
    }

    @classmethod
    def validate(cls, payload: Any) -> None:
        name = cls.__name__
        if not isinstance(payload, dict):
            raise SchemaValidationError(f"{name}: payload must be a dict")
        if "machine_pub" in payload:
            raise SchemaValidationError(
                f"{name}: 'machine_pub' is in the row key and must not be repeated")
        if payload.get("v") != ROW_VERSION:
            raise SchemaValidationError(f"{name}: 'v' must be {ROW_VERSION}")
        if not isinstance(payload.get("persona_pub"), str) \
                or not _HEX64_RE.match(payload["persona_pub"]):
            raise SchemaValidationError(f"{name}: 'persona_pub' must be 64 lowercase hex")
        if not isinstance(payload.get("persona_cert"), dict):
            raise SchemaValidationError(f"{name}: 'persona_cert' must be an object")
        label = payload.get("label")
        if not isinstance(label, str) or not label or len(label) > MAX_LABEL_CHARS:
            raise SchemaValidationError(f"{name}: 'label' must be 1-{MAX_LABEL_CHARS} chars")
        capacity = payload.get("capacity")
        if isinstance(capacity, bool) or not isinstance(capacity, int) \
                or not 0 <= capacity <= MAX_CAPACITY:
            raise SchemaValidationError(f"{name}: 'capacity' must be 0-{MAX_CAPACITY}")
        harnesses = payload.get("harnesses")
        if not isinstance(harnesses, list) or any(h not in HARNESSES for h in harnesses):
            raise SchemaValidationError(f"{name}: 'harnesses' must be drawn from {HARNESSES}")
        images = payload.get("images")
        if not isinstance(images, list) or len(images) > MAX_IMAGES \
                or any(not isinstance(i, str) or not i for i in images):
            raise SchemaValidationError(
                f"{name}: 'images' must be at most {MAX_IMAGES} non-empty strings")
        if payload.get("admit") != ADMIT_ALL_MEMBERS:
            raise SchemaValidationError(f"{name}: 'admit' must be {ADMIT_ALL_MEMBERS!r}")
        updated_at = payload.get("updated_at")
        if isinstance(updated_at, bool) or not isinstance(updated_at, int) or updated_at < 0:
            raise SchemaValidationError(f"{name}: 'updated_at' must be a non-negative integer")
        if not isinstance(payload.get("sig"), str) or not payload["sig"]:
            raise SchemaValidationError(f"{name}: 'sig' must be a non-empty string")

    @classmethod
    def validate_member_key(cls, key: str) -> None:
        persona, _, machine = (key or "").partition(":")
        if not _HEX64_RE.match(persona) or not _HEX64_RE.match(machine):
            raise SchemaValidationError(
                f"{cls.__name__}: keys are <persona>:<machine key> (64 hex each), got {key!r}")


def runner_key(persona_pub: str, machine_pub: str) -> str:
    return f"{persona_pub}:{machine_pub}"
