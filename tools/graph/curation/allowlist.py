"""Allowlist YAML loader for bootstrap public-surface curation.

Format (see ``autonomy-bootstrap-allowlist.yaml``)::

    org: <slug>
    version: <int>
    canonical:
      - <source-id-prefix>  # optional comment
    published:
      - <source-id-prefix>
    audit_notes:            # optional, managed by the runner
      - id: <source-id>
        ts: <iso>
        note: <human-readable summary>

A loaded allowlist is a simple dataclass with a ``tiers()`` view that flattens
``canonical`` and ``published`` into ``(prefix, target_state)`` pairs in the
order the operator committed — canonical first, so bulk promotion transitions
out of ``raw`` in a stable order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import yaml


VALID_TARGET_STATES = ("canonical", "published")
# Tier keys expanded into (prefix, target_state) pairs. Order matches the
# audit-trail ordering in the filed audit note.
TIER_KEYS = ("canonical", "published")


@dataclass
class AllowlistEntry:
    prefix: str
    target_state: str
    comment: str | None = None


@dataclass
class Allowlist:
    org: str
    version: int
    path: Path
    canonical: list[str] = field(default_factory=list)
    published: list[str] = field(default_factory=list)
    audit_notes: list[dict] = field(default_factory=list)
    #: The org:follow rendezvous committed for this org (design of record
    #: graph://5f2f5a49-00d §10.1). ``{org_uuid, rendezvous, link_pub}`` or
    #: None when the org publishes no standing follow link. Validated on load;
    #: promote.py ignores it (it drives the follower/first-run path, not
    #: publication-state promotion).
    follow: dict | None = None

    def tiers(self) -> Iterator[AllowlistEntry]:
        for state in TIER_KEYS:
            for prefix in getattr(self, state):
                yield AllowlistEntry(prefix=prefix, target_state=state)

    def all_prefixes(self) -> list[str]:
        return list(self.canonical) + list(self.published)


class AllowlistError(ValueError):
    """Raised when a YAML file fails validation."""


def load(path: str | Path) -> Allowlist:
    """Parse + validate an allowlist YAML file."""
    p = Path(path)
    if not p.is_file():
        raise AllowlistError(f"allowlist not found: {p}")
    raw = yaml.safe_load(p.read_text()) or {}
    return _from_dict(raw, path=p)


def _from_dict(raw: dict, *, path: Path) -> Allowlist:
    if not isinstance(raw, dict):
        raise AllowlistError(f"{path}: top-level must be a mapping")
    org = raw.get("org")
    if not isinstance(org, str) or not org:
        raise AllowlistError(f"{path}: 'org' is required and must be a non-empty string")
    version = raw.get("version")
    if not isinstance(version, int) or version < 1:
        raise AllowlistError(f"{path}: 'version' must be a positive int")
    lists = {}
    for key in TIER_KEYS:
        entries = raw.get(key) or []
        if not isinstance(entries, list):
            raise AllowlistError(f"{path}: '{key}' must be a list")
        cleaned: list[str] = []
        for item in entries:
            if not isinstance(item, str) or not item.strip():
                raise AllowlistError(
                    f"{path}: '{key}' entries must be non-empty strings (got {item!r})"
                )
            cleaned.append(item.strip())
        lists[key] = cleaned

    overlaps = set(lists["canonical"]) & set(lists["published"])
    if overlaps:
        raise AllowlistError(
            f"{path}: prefixes appear in both canonical and published: {sorted(overlaps)}"
        )

    audit_notes = raw.get("audit_notes") or []
    if not isinstance(audit_notes, list):
        raise AllowlistError(f"{path}: 'audit_notes' must be a list")

    follow = _parse_follow(raw.get("follow"), path=path)

    return Allowlist(
        org=org,
        version=version,
        path=path,
        canonical=lists["canonical"],
        published=lists["published"],
        audit_notes=audit_notes,
        follow=follow,
    )


#: The exact keys an org:follow rendezvous block carries (§10.1). Present ==
#: required; no extras. An optional ``registry_url`` is accepted for parity
#: with the follower row (§10.4) but not required here.
_FOLLOW_REQUIRED = ("org_uuid", "rendezvous", "link_pub")
_FOLLOW_OPTIONAL = ("registry_url",)


def _parse_follow(raw, *, path: Path) -> dict | None:
    """Validate the optional ``follow:`` block. None when absent."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise AllowlistError(f"{path}: 'follow' must be a mapping")
    missing = [k for k in _FOLLOW_REQUIRED if k not in raw]
    if missing:
        raise AllowlistError(
            f"{path}: 'follow' is missing required key(s): {missing}"
        )
    unknown = set(raw) - set(_FOLLOW_REQUIRED) - set(_FOLLOW_OPTIONAL)
    if unknown:
        raise AllowlistError(
            f"{path}: 'follow' carries unknown key(s): {sorted(unknown)}"
        )
    for key in (*_FOLLOW_REQUIRED, *(_FOLLOW_OPTIONAL if any(
        k in raw for k in _FOLLOW_OPTIONAL) else ())):
        if key in raw and (not isinstance(raw[key], str) or not raw[key].strip()):
            raise AllowlistError(
                f"{path}: 'follow.{key}' must be a non-empty string"
            )
    return {k: raw[k].strip() for k in (*_FOLLOW_REQUIRED, *_FOLLOW_OPTIONAL)
            if k in raw}


DEFAULT_AUTONOMY_PATH = Path(__file__).with_name("autonomy-bootstrap-allowlist.yaml")
