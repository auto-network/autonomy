"""Tests for tools.dashboard.org_identity — the three-level fallback cascade.

Spec: graph://497cdc20-d43.

Acceptance from auto-jl9dc:
  (a) full override: configured slug returns its overrides
  (b) partial override: only color overridden — keeps generated name/favicon
  (c) no override: pure generated identity
  (d) palette stability: same slug always gets same colour
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture
def isolated_orgs(tmp_path, monkeypatch):
    """Reload project_config + org_identity against a tmp projects.yaml.

    Each test gets a clean cache and can mutate the YAML by writing a new
    file before calling resolve_org_identity().
    """
    config_path = tmp_path / "projects.yaml"

    def write(orgs: dict | None) -> None:
        data = {
            "projects": {
                "autonomy": {
                    "image": "autonomy-agent:dashboard",
                    "graph_project": "autonomy",
                },
                "enterprise-ng": {
                    "image": "autonomy-agent:enterprise-ng",
                    "graph_project": "anchore",
                },
            },
        }
        if orgs is not None:
            data["orgs"] = orgs
        config_path.write_text(yaml.safe_dump(data))

    write({})  # baseline so the import succeeds

    from agents import project_config
    monkeypatch.setattr(project_config, "DEFAULT_CONFIG_PATH", config_path)
    project_config.clear_cache()

    yield write

    project_config.clear_cache()


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
        # "-workspace-repo" (the parent dir name with path separators
        # rewritten to dashes). These never identify a real org; the
        # resolver must return UNKNOWN_SLUG so the renderer paints "?".
        isolated_orgs({})
        from tools.dashboard.org_identity import session_org_slug, UNKNOWN_SLUG

        assert session_org_slug({"project": "-workspace-repo"}) == UNKNOWN_SLUG
        assert session_org_slug({"project": "  -enterprise  "}) == UNKNOWN_SLUG
        # Bracket-wrapped path-derived junk also maps to unknown.
        assert session_org_slug({"project": "[-workspace-repo]"}) == UNKNOWN_SLUG


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
        # Path-derived project values should flow through to unresolved.
        isolated_orgs({})
        from tools.dashboard.org_identity import resolve_session_org

        org = resolve_session_org({"project": "-workspace-repo"})
        assert org["resolved"] is False
        assert org["initial"] == "?"
