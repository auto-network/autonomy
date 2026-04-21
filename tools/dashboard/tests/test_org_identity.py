"""Tests for tools.dashboard.org_identity — the three-level fallback cascade.

Spec: graph://497cdc20-d43.

Acceptance from auto-jl9dc:
  (a) full override: configured slug returns its overrides
  (b) partial override: only color overridden — keeps generated name/favicon
  (c) no override: pure generated identity
  (d) palette stability: same slug always gets same colour
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from tools.graph.db import GraphDB
from tools.graph.settings_ops import _now_iso


@pytest.fixture
def isolated_orgs(tmp_path, monkeypatch):
    """Populate ``autonomy.workspace#1`` + ``autonomy.org#1`` Settings into
    per-org DBs under a tmp ``data/orgs/`` tree and redirect
    :envvar:`AUTONOMY_ORGS_DIR` at it.

    Tests call the returned ``write(orgs)`` helper to seed (or re-seed) the
    ``autonomy.org#1`` overrides. Workspaces ``autonomy`` and
    ``enterprise-ng`` are always present so ``session_org_slug`` lookups
    resolve.
    """
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    GraphDB.close_all_pooled()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)

    org_slugs = {"autonomy", "anchore", "personal"}

    def _ensure_org_db(slug: str) -> Path:
        path = orgs_dir / f"{slug}.db"
        if not path.exists():
            db = GraphDB.create_org_db(slug, type_="shared", path=path)
            db.close()
        return path

    def _set_workspace(slug: str, workspace_id: str, payload: dict) -> None:
        path = _ensure_org_db(slug)
        db = GraphDB(path)
        try:
            db.conn.execute("DELETE FROM settings WHERE set_id = ? AND key = ?",
                            ("autonomy.workspace", workspace_id))
            db.conn.execute(
                "INSERT INTO settings(id, set_id, schema_revision, key, "
                "payload, publication_state, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (str(uuid4()), "autonomy.workspace", 1, workspace_id,
                 json.dumps(payload), "canonical", _now_iso(), _now_iso()),
            )
            db.conn.commit()
        finally:
            db.close()

    def _set_org_identity(slug: str, payload: dict) -> None:
        path = _ensure_org_db(slug)
        db = GraphDB(path)
        try:
            db.conn.execute("DELETE FROM settings WHERE set_id = ? AND key = ?",
                            ("autonomy.org", slug))
            if payload:
                db.conn.execute(
                    "INSERT INTO settings(id, set_id, schema_revision, key, "
                    "payload, publication_state, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (str(uuid4()), "autonomy.org", 1, slug,
                     json.dumps(payload), "canonical",
                     _now_iso(), _now_iso()),
                )
            db.conn.commit()
        finally:
            db.close()

    _set_workspace("autonomy", "autonomy", {
        "name": "autonomy", "image": "autonomy-agent:dashboard",
    })
    _set_workspace("anchore", "enterprise-ng", {
        "name": "enterprise-ng", "image": "autonomy-agent:enterprise-ng",
    })

    def write(orgs: dict | None) -> None:
        if orgs is None:
            return
        for slug in org_slugs:
            _set_org_identity(slug, {})
        for slug, payload in (orgs or {}).items():
            filtered = {k: v for k, v in payload.items() if v not in (None, "")}
            if filtered:
                _set_org_identity(slug, filtered)
            else:
                _set_org_identity(slug, {})

    write({})

    try:
        yield write
    finally:
        GraphDB.close_all_pooled()


# ── (a) full override ────────────────────────────────────────────────


class TestFullOverride:
    def test_all_fields_come_from_override(self, isolated_orgs):
        isolated_orgs({
            "anchore": {
                "name": "Anchore",
                "byline": "Security platform",
                "color": "#2D7DD2",
                "favicon": "assets/anchore.png",
            },
        })
        from tools.dashboard.org_identity import resolve_org_identity

        identity = resolve_org_identity("anchore")
        assert identity["slug"] == "anchore"
        assert identity["name"] == "Anchore"
        assert identity["byline"] == "Security platform"
        assert identity["color"] == "#2D7DD2"
        assert identity["favicon"] == "assets/anchore.png"
        assert identity["initial"] == "A"


# ── (b) partial override ─────────────────────────────────────────────


class TestPartialOverride:
    def test_color_only_keeps_generated_name_and_favicon(self, isolated_orgs):
        isolated_orgs({"acme": {"color": "#00ff00"}})
        from tools.dashboard.org_identity import resolve_org_identity

        identity = resolve_org_identity("acme")
        assert identity["color"] == "#00ff00"           # overridden
        assert identity["name"] == "acme"                # generated (slug)
        assert identity["favicon"] is None               # generated (no favicon)
        assert identity["byline"] == ""                  # generated (empty)
        assert identity["initial"] == "A"                # from generated name

    def test_name_only_keeps_generated_color(self, isolated_orgs):
        isolated_orgs({"acme": {"name": "ACME Corp"}})
        from tools.dashboard.org_identity import resolve_org_identity

        identity = resolve_org_identity("acme")
        assert identity["name"] == "ACME Corp"
        # Generated colour for "acme" — must match what no-override would yield.
        from tools.dashboard.org_identity import _hash_color
        assert identity["color"] == _hash_color("acme")
        # Initial is taken from the resolved (overridden) name.
        assert identity["initial"] == "A"


# ── (c) no override (pure generated) ─────────────────────────────────


class TestGeneratedFallback:
    def test_unknown_slug_uses_generated_identity(self, isolated_orgs):
        isolated_orgs({})
        from tools.dashboard.org_identity import resolve_org_identity

        identity = resolve_org_identity("brand-new-org-never-configured")
        assert identity["slug"] == "brand-new-org-never-configured"
        assert identity["name"] == "brand-new-org-never-configured"
        assert identity["byline"] == ""
        assert identity["favicon"] is None
        assert identity["initial"] == "B"
        assert identity["color"].startswith("#") and len(identity["color"]) == 7

    def test_empty_or_none_slug_normalises_to_unknown(self, isolated_orgs):
        isolated_orgs({})
        from tools.dashboard.org_identity import resolve_org_identity, UNKNOWN_SLUG

        for value in (None, "", "   "):
            identity = resolve_org_identity(value)
            assert identity["slug"] == UNKNOWN_SLUG
            assert identity["name"] == UNKNOWN_SLUG


# ── (d) palette stability ────────────────────────────────────────────


class TestPaletteStability:
    def test_same_slug_always_yields_same_color(self, isolated_orgs):
        isolated_orgs({})
        from tools.dashboard.org_identity import resolve_org_identity

        a = resolve_org_identity("acme")["color"]
        b = resolve_org_identity("acme")["color"]
        assert a == b

    def test_different_slugs_can_yield_different_colors(self, isolated_orgs):
        # 16 slugs across the 16-slot palette — expect at least 6 distinct
        # colours (loose bound; collisions are allowed but should be rare).
        isolated_orgs({})
        from tools.dashboard.org_identity import resolve_org_identity

        slugs = [f"slug-{i:02d}" for i in range(16)]
        colors = {resolve_org_identity(s)["color"] for s in slugs}
        assert len(colors) >= 6


# ── empty-string override falls through ──────────────────────────────


class TestEmptyOverrideFallsThrough:
    def test_empty_string_color_falls_through_to_generated(self, isolated_orgs):
        # YAML writers sometimes leave an empty string when "unsetting" a
        # field. Treat it as no-value so the cascade still works.
        isolated_orgs({"acme": {"color": ""}})
        from tools.dashboard.org_identity import resolve_org_identity, _hash_color

        identity = resolve_org_identity("acme")
        assert identity["color"] == _hash_color("acme")


# ── session_org_slug — workspace → org mapping ───────────────────────


class TestSessionOrgSlug:
    def test_workspace_id_maps_to_graph_project(self, isolated_orgs):
        isolated_orgs({})
        from tools.dashboard.org_identity import session_org_slug

        # enterprise-ng is configured with graph_project=anchore
        assert session_org_slug({"project": "enterprise-ng"}) == "anchore"
        assert session_org_slug({"project": "autonomy"}) == "autonomy"

    def test_unknown_project_passes_through_as_slug(self, isolated_orgs):
        isolated_orgs({})
        from tools.dashboard.org_identity import session_org_slug

        # When the project field already IS an org slug (e.g. graph.db
        # sources store graph_project here), pass it through unchanged.
        assert session_org_slug({"project": "anchore"}) == "anchore"
        assert session_org_slug({"project": "personal"}) == "personal"

    def test_bracket_wrapped_project_is_stripped(self, isolated_orgs):
        isolated_orgs({})
        from tools.dashboard.org_identity import session_org_slug

        # Recent-sessions DAO wraps in brackets for the legacy display.
        assert session_org_slug({"project": "[autonomy]"}) == "autonomy"

    def test_missing_or_empty_project_maps_to_unknown(self, isolated_orgs):
        isolated_orgs({})
        from tools.dashboard.org_identity import session_org_slug, UNKNOWN_SLUG

        assert session_org_slug({}) == UNKNOWN_SLUG
        assert session_org_slug({"project": ""}) == UNKNOWN_SLUG
        assert session_org_slug({"project": None}) == UNKNOWN_SLUG


# ── resolve_session_org — convenience composition ────────────────────


class TestResolveSessionOrg:
    def test_attaches_full_identity_to_session(self, isolated_orgs):
        isolated_orgs({
            "anchore": {"name": "Anchore", "color": "#2D7DD2"},
        })
        from tools.dashboard.org_identity import resolve_session_org

        org = resolve_session_org({"project": "enterprise-ng"})
        assert org["slug"] == "anchore"
        assert org["name"] == "Anchore"
        assert org["color"] == "#2D7DD2"
        assert org["initial"] == "A"


# ── _initial — skip non-alphanumeric, '?' fallback ───────────────────


class TestInitial:
    def test_skips_leading_non_alphanumeric(self, isolated_orgs):
        # Names like "A-b-c" should yield the first ALPHANUMERIC char.
        from tools.dashboard.org_identity import _initial

        assert _initial("A-b-c") == "A"

    def test_returns_question_mark_when_only_punctuation(self, isolated_orgs):
        # Path-derived junk like "-workspace-repo" starts with "-".
        # Under the old rule, `_initial` would return "-" as the first
        # non-whitespace char. Skipping non-alphanumeric avoids that.
        from tools.dashboard.org_identity import _initial

        assert _initial("-workspace-repo") == "W"

    def test_empty_string_is_question_mark(self, isolated_orgs):
        from tools.dashboard.org_identity import _initial

        assert _initial("") == "?"

    def test_pure_punctuation_is_question_mark(self, isolated_orgs):
        from tools.dashboard.org_identity import _initial

        assert _initial("---") == "?"
        assert _initial("!!!") == "?"


# ── session_org_slug — path-derived junk maps to UNKNOWN_SLUG ────────


class TestPathDerivedProjectMapsToUnknown:
    def test_dash_prefixed_raw_maps_to_unknown(self, isolated_orgs):
        # Ingested-from-path sessions store ``project`` like
        # "-some-unknown-legacy" (the parent dir name with path
        # separators rewritten to dashes). These never identify a real
        # org; the resolver returns UNKNOWN_SLUG so the renderer paints
        # "?". Known autonomy path patterns are covered separately —
        # see TestAutonomyPathPatternsMapToAutonomy.
        isolated_orgs({})
        from tools.dashboard.org_identity import session_org_slug, UNKNOWN_SLUG

        assert session_org_slug({"project": "-some-unknown-legacy-junk"}) == UNKNOWN_SLUG
        assert session_org_slug({"project": "  -enterprise  "}) == UNKNOWN_SLUG
        # Bracket-wrapped path-derived junk also maps to unknown.
        assert session_org_slug({"project": "[-some-other-repo]"}) == UNKNOWN_SLUG


# ── session_org_slug — autonomy path patterns map to autonomy ────────


class TestAutonomyPathPatternsMapToAutonomy:
    """Live sessions don't always have a ``.session_meta.json`` written,
    so the ingester falls back to the parent-directory name (with
    separators rewritten to dashes). Those path-derived values for
    sessions living inside the autonomy repo must resolve to the
    ``autonomy`` org, not be dropped as unknown.
    """

    def test_workspace_repo_maps_to_autonomy(self, isolated_orgs):
        # Dashboard-container mount: /workspace/repo → "-workspace-repo".
        isolated_orgs({})
        from tools.dashboard.org_identity import session_org_slug

        assert session_org_slug({"project": "-workspace-repo"}) == "autonomy"

    def test_home_jeremy_workspace_autonomy_maps_to_autonomy(self, isolated_orgs):
        # Host session running in /home/jeremy/workspace/autonomy.
        isolated_orgs({})
        from tools.dashboard.org_identity import session_org_slug

        assert (
            session_org_slug({"project": "-home-jeremy-workspace-autonomy"})
            == "autonomy"
        )

    def test_any_workspace_autonomy_suffix_maps_to_autonomy(self, isolated_orgs):
        # Any operator's path ending in "/workspace/autonomy".
        isolated_orgs({})
        from tools.dashboard.org_identity import session_org_slug

        assert (
            session_org_slug({"project": "-home-alice-src-workspace-autonomy"})
            == "autonomy"
        )

    def test_bracket_wrapped_autonomy_path_maps_to_autonomy(self, isolated_orgs):
        # Recent-sessions DAO wraps the project in brackets.
        isolated_orgs({})
        from tools.dashboard.org_identity import session_org_slug

        assert session_org_slug({"project": "[-workspace-repo]"}) == "autonomy"

    def test_workspace_lookup_still_works(self, isolated_orgs):
        # Non-path-derived workspace ids still go through the config
        # lookup (enterprise-ng → anchore).
        isolated_orgs({})
        from tools.dashboard.org_identity import session_org_slug

        assert session_org_slug({"project": "enterprise-ng"}) == "anchore"


# ── resolved flag — identifies unresolved orgs for ? rendering ───────


class TestResolvedFlag:
    def test_known_org_is_resolved(self, isolated_orgs):
        isolated_orgs({"autonomy": {"name": "Autonomy"}})
        from tools.dashboard.org_identity import resolve_org_identity

        identity = resolve_org_identity("autonomy")
        assert identity["resolved"] is True

    def test_arbitrary_slug_is_resolved(self, isolated_orgs):
        # A slug we haven't seen but that isn't UNKNOWN_SLUG still counts as
        # resolved — the cascade renders generated color + initial.
        isolated_orgs({})
        from tools.dashboard.org_identity import resolve_org_identity

        assert resolve_org_identity("newco")["resolved"] is True

    def test_unknown_slug_is_not_resolved(self, isolated_orgs):
        isolated_orgs({})
        from tools.dashboard.org_identity import resolve_org_identity, UNRESOLVED_COLOR

        identity = resolve_org_identity(None)
        assert identity["resolved"] is False
        # Per acceptance: "?" on neutral gray.
        assert identity["initial"] == "?"
        assert identity["color"] == UNRESOLVED_COLOR

    def test_legacy_session_resolves_to_unresolved(self, isolated_orgs):
        # Path-derived project values that DON'T match a known autonomy
        # pattern flow through to unresolved. (Known autonomy patterns
        # like "-workspace-repo" map to the autonomy org — see
        # TestAutonomyPathPatternsMapToAutonomy.)
        isolated_orgs({})
        from tools.dashboard.org_identity import resolve_session_org

        org = resolve_session_org({"project": "-some-unknown-legacy-junk"})
        assert org["resolved"] is False
        assert org["initial"] == "?"
