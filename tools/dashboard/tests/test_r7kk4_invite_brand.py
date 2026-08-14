"""auto-r7kk4 — the invite/join context carries the org's OWN identity
(name/description/icon/color), org-served over the E2E channel. The icon MUST
be a bounded data: URI or absent — NEVER a remote URL (a remote favicon fetch on
the invite page would leak the visitor's IP/UA to the favicon host, and fails
the invite page's data:-only img-src CSP)."""
from __future__ import annotations

from tools.dashboard import link_serving


def test_remote_favicon_is_never_emitted_as_icon(monkeypatch):
    monkeypatch.setattr(
        link_serving, "_resolve_org_brand",
        lambda org: {"name": "Anchore", "color": "#6c63ff", "initial": "A",
                     "favicon_url": "https://tracker.example/f.png"},
    )
    monkeypatch.setattr(
        "tools.dashboard.org_identity.resolve_org_identity",
        lambda org: {"byline": "sovereign by default"},
    )
    out = link_serving._org_brand_for_invite("anchore")
    assert out["org_name"] == "Anchore"
    assert out["org_color"] == "#6c63ff"
    assert out["org_description"] == "sovereign by default"
    assert "org_icon" not in out  # a remote favicon must NEVER become the icon


def test_bounded_icon_becomes_data_uri(monkeypatch):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    monkeypatch.setattr(
        link_serving, "_resolve_org_brand",
        lambda org: {"name": "Anchore", "color": "#6c63ff", "initial": "A",
                     "favicon": {"mime": "image/png", "bytes": png}},
    )
    monkeypatch.setattr(
        "tools.dashboard.org_identity.resolve_org_identity", lambda org: {"byline": ""},
    )
    out = link_serving._org_brand_for_invite("anchore")
    assert out["org_icon"].startswith("data:image/png;base64,")
    assert "://" not in out["org_icon"]  # no remote scheme, ever
    assert "org_description" not in out  # empty byline omitted


def test_no_identity_yields_none(monkeypatch):
    monkeypatch.setattr(link_serving, "_resolve_org_brand", lambda org: None)
    assert link_serving._org_brand_for_invite("anchore") is None
