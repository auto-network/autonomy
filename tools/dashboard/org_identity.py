"""Org identity resolver — three-level fallback cascade.

Implements the design from ``graph://497cdc20-d43``: render an org's visual
identity (name, byline, color, favicon) by cascading per-field through

    1. operator-local override   (``agents/projects.yaml:orgs:<slug>``)
    2. subscribed canonical      (the org's own graph.db, post auto-hoi4)
    3. generated fallback        (deterministic from slug)

The cascade is per-field, not all-or-nothing: setting only ``color`` for
``anchore`` keeps generated ``name`` and ``favicon``. Stage 1 collapses
the canonical layer to the empty dict — the slot is wired so federation
(``auto-hoi4``) lands additively without changing call-sites.

Sessions don't carry an org slug today; they carry a ``project`` field
that is either a workspace id (``enterprise-ng``) or an org slug
(``autonomy``). :func:`session_org_slug` normalises that.
"""

from __future__ import annotations

import hashlib
from typing import Any

from agents import project_config

# Curated palette — 16 distinct, accessible colours that read on the
# dashboard's #0f172a background. Ordered to maximise visual distance
# between adjacent slots so hash collisions on neighbouring slugs are
# still distinguishable.
_PALETTE: tuple[str, ...] = (
    "#E63946", "#F77F00", "#FCBF49", "#06A77D",
    "#2A9D8F", "#118AB2", "#264653", "#6C63FF",
    "#9D4EDD", "#E07A5F", "#3D5A80", "#81B29A",
    "#F4A261", "#E76F51", "#588157", "#A4036F",
)

UNKNOWN_SLUG = "unknown"

# Neutral gray painted behind the "?" glyph when the org is unresolved
# (legacy sessions with path-derived project junk, missing project field).
UNRESOLVED_COLOR = "#4b5563"


def _hash_color(slug: str) -> str:
    """Pick a stable palette colour from the slug's blake2b hash."""
    digest = hashlib.blake2b(slug.encode("utf-8"), digest_size=4).digest()
    idx = int.from_bytes(digest, "big") % len(_PALETTE)
    return _PALETTE[idx]


def _initial(name_or_slug: str) -> str:
    """First alphanumeric character, uppercased. Returns '?' if none found.

    Skips non-alphanumeric characters so path-derived junk like
    ``-workspace-repo`` yields ``"?"`` rather than ``"-"``.
    """
    for ch in name_or_slug:
        if ch.isalnum():
            return ch.upper()
    return "?"


def _generated_identity(slug: str) -> dict[str, str]:
    """Deterministic identity for orgs with no override and no canonical."""
    color = _hash_color(slug or UNKNOWN_SLUG)
    return {
        "name": slug or UNKNOWN_SLUG,
        "color": color,
        "initial": _initial(slug or UNKNOWN_SLUG),
    }


def _canonical_identity(slug: str) -> dict[str, Any]:
    """Identity published by the org itself.

    Stage 1: empty. Once the federation read layer ships (``auto-hoi4``)
    this fetches from the subscribed org's own graph.db, with a local
    cache under ``data/org-cache/<uuid>/``.
    """
    # TODO(auto-hoi4): pull from subscribed org graph.db; cache under
    # data/org-cache/<uuid>/. See graph://497cdc20-d43.
    return {}


def resolve_org_identity(slug: str | None) -> dict[str, Any]:
    """Resolve the visual identity for ``slug`` via the three-level cascade.

    Returns a dict with stable keys:

        slug      — the input slug, normalised (empty → "unknown")
        name      — display name
        byline    — short tagline (may be empty string)
        color     — hex CSS colour for the org indicator background
        favicon   — URL/path to a square icon, or ``None`` (renderer
                    paints ``initial`` on a circle of ``color`` instead)
        initial   — single uppercase character for the no-favicon case
        resolved  — ``True`` when the slug is a real org (anything other
                    than ``UNKNOWN_SLUG``); ``False`` for legacy / unknown
                    sessions, in which case the renderer paints ``?`` on
                    neutral gray.

    Per-field cascade — operator override → canonical → generated. An
    empty string in an override is treated as "no value" (falls through);
    use null/omit to be explicit.
    """
    slug = (slug or "").strip() or UNKNOWN_SLUG

    if slug == UNKNOWN_SLUG:
        return {
            "slug": slug,
            "name": slug,
            "byline": "",
            "color": UNRESOLVED_COLOR,
            "favicon": None,
            "initial": "?",
            "resolved": False,
        }

    overrides = project_config.load_org_overrides()
    override = overrides.get(slug)
    canonical = _canonical_identity(slug)
    generated = _generated_identity(slug)

    def pick(field: str, default: Any) -> Any:
        if override is not None:
            value = getattr(override, field, None)
            if value:
                return value
        canon_value = canonical.get(field)
        if canon_value:
            return canon_value
        return default

    name = pick("name", generated["name"])
    color = pick("color", generated["color"])
    favicon = pick("favicon", None)
    byline = pick("byline", "")

    return {
        "slug": slug,
        "name": name,
        "byline": byline,
        "color": color,
        "favicon": favicon,
        "initial": _initial(name),
        "resolved": True,
    }


def session_org_slug(session: dict) -> str:
    """Derive an org slug from a session payload.

    Sessions carry a ``project`` field whose meaning has shifted with
    schema evolution:

      * Container sessions registered through the dispatcher store the
        workspace id (``enterprise``, ``enterprise-ng``).
      * Sessions ingested from graph.db sources store the workspace's
        ``graph_project`` (the org slug — ``anchore``, ``autonomy``).
      * Recent-sessions output wraps the value in brackets (``[autonomy]``).

    We strip the brackets, then map workspace ids to their owning org
    via ``project_config``. Unknown values pass through as the slug
    itself so the generated fallback still gives a stable colour.
    """
    raw = session.get("project") or ""
    if isinstance(raw, str):
        raw = raw.strip().strip("[]").strip()
    if not raw:
        return UNKNOWN_SLUG
    # Path-derived ingest junk (e.g. "-workspace-repo") never identifies
    # an org. Treat it as unresolved so the renderer paints "?".
    if raw.startswith("-"):
        return UNKNOWN_SLUG
    try:
        workspace = project_config.get_project(raw)
        return workspace.graph_project
    except KeyError:
        return raw


def resolve_session_org(session: dict) -> dict[str, Any]:
    """Convenience: resolve identity for the org owning ``session``."""
    return resolve_org_identity(session_org_slug(session))
