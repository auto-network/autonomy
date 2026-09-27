"""The onboarding label question's check (F6): local, deterministic, one
sentence per refusal."""

from __future__ import annotations

import pytest

from tools.dashboard import remote_access as ra


def test_a_distinct_well_formed_slug_passes():
    result = ra.check_label("acme", "boat-lore", existing=["oss-metrics"], bound="")
    assert result == ra.LabelCheck(True, "boat-lore")
    assert result.as_dict() == {"ok": True, "label": "boat-lore", "code": "", "against": "",
                                "reason": "", "bound": False}


def test_input_is_trimmed_and_lowercased():
    assert ra.check_label("acme", "  BoatLore ", existing=[], bound="").label == "boatlore"


def test_a_persona_with_its_label_bound_accepts_only_that_label(monkeypatch):
    """The registry binds one slug per persona forever (reviewer): the bound
    slug is the only valid answer and the screen can skip the question."""
    from tools.dashboard import service_publication

    monkeypatch.setattr(service_publication, "_persona_for_org", lambda org: ("ab" * 32, "Jeremy"))
    monkeypatch.setattr(service_publication, "bound_persona_label",
                        lambda org, persona: "jeremy-0123456789abcdef0123")
    assert ra.bound_slug("acme") == "jeremy"
    same = ra.check_label("acme", "jeremy")
    assert same == ra.LabelCheck(True, "jeremy", bound=True)
    other = ra.check_label("acme", "boat-lore")
    assert (other.ok, other.code, other.against, other.bound) == (False, "already_bound", "jeremy", True)
    assert other.reason == 'Your label is already "jeremy" and cannot change.'
    # No binding known: the ordinary rules apply.
    monkeypatch.setattr(service_publication, "bound_persona_label", lambda org, persona: None)
    assert ra.bound_slug("acme") is None
    assert ra.check_label("acme", "boat-lore", existing=[]).ok is True


@pytest.mark.parametrize("candidate", ["", "-boat", "boat-", "Boat!", "x" * 43, "xn--80ak6aa92e", None, 7])
def test_malformed_slugs_are_refused_with_the_shape_rule(candidate):
    result = ra.check_label("acme", candidate, existing=[], bound="")
    assert result.ok is False and result.code == "malformed"
    assert "lowercase letters, digits and hyphens" in result.reason or result.reason == "The label must be text."


@pytest.mark.parametrize("candidate, code, against", [
    ("aut0n0my", "platform_name", "autonomy"),
    ("my-autonomy", "platform_name", "autonomy"),
    ("adrnin", "platform_name", "admin"),
    ("vvww", "platform_name", "www"),
])
def test_platform_and_reserved_look_alikes_name_what_they_read_as(candidate, code, against):
    result = ra.check_label("acme", candidate, existing=[], bound="")
    assert (result.ok, result.code, result.against) == (False, code, against)
    assert against in result.reason


def test_the_operators_own_labels_are_protected(monkeypatch):
    from tools.dashboard import service_publication

    rows = {
        "personal": [
            {"app_label": "docs", "persona_label": "jeremy-0123456789abcdef0123", "state": "active"},
            {"app_label": "old", "persona_label": "retired-0123456789abcdef0123", "state": "released"},
        ],
        "acme": [{"app_label": "collector", "persona_label": "persona-0f1e2d3c4b5a69788796", "state": "paused"}],
    }
    monkeypatch.setattr(service_publication, "list_reservations", lambda org: rows[org])
    monkeypatch.setattr(ra, "bound_slug", lambda org: None)   # a persona not yet bound

    assert ra.existing_labels("acme") == ["docs", "jeremy", "collector", "persona"]
    result = ra.check_label("acme", "jererny")
    assert (result.ok, result.code, result.against) == (False, "existing_label", "jeremy")
    assert "existing label" in result.reason
    # A released label no longer protects anything; the exact own label passes.
    assert ra.check_label("acme", "retired").ok is True
    assert ra.check_label("acme", "jeremy").ok is True


def test_route_is_registered_under_remote_access():
    from tools.dashboard import network_routes

    paths = {route.path for route in network_routes.ROUTES}
    assert "/api/network/remote-access/label/check" in paths
