"""auto-r7kk4 — the invite/join context carries ONE bounded, provenance-clean
organization and sponsor presentation, assembled strictly from three
organization-OWNED sources: the ``autonomy.org`` identity row, the live invite
event's ``sponsor_pub``, and the ``autonomy.org.member-profile`` directory.

Focused unit coverage of the three serving helpers. The end-to-end context
walk (real ledger + real Settings) lives in
``test_join_channel_integration.py``.

Rules pinned here (graph://4f9e881c-a9 §§4,6,9):

* org presentation reads the org's OWN ``autonomy.org`` row directly (never the
  identity cascade, which would substitute a generated slug/UUID name); it is
  valid ONLY with a non-empty name AND a valid ``#rrggbb`` color;
* ``org_icon`` is the row's bounded ``data:image/webp;base64`` URI or ABSENT —
  never a remote URL, path, or the legacy ``favicon`` field;
* the sponsor avatar is produced ONLY from a same-org JPEG/PNG/WebP graph
  attachment <= 64 KiB, encoded inline — never a URL/path/stored-data-URI, and
  never a wrong-org/missing/oversized/malformed-MIME blob;
* a missing/incomplete sponsor profile is honest: ``sponsor_pub`` only, no
  fabricated name.
"""
from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from tools.dashboard import link_serving

WEBP_ICON = "data:image/webp;base64," + base64.b64encode(b"RIFF....WEBP-bytes").decode()
SPONSOR = "a" * 64  # canonical 64-lowercase-hex persona key


def _members(*rows):
    """A fake ``read_owned_set(...)`` result: ``.members`` of key/payload rows."""
    return SimpleNamespace(
        members=[SimpleNamespace(key=k, payload=p) for k, p in rows]
    )


def _patch_owned(monkeypatch, mapping):
    """Route ``read_owned_set(set_id, ...)`` to the per-set rows in *mapping*."""
    def fake(set_id, *, org=None, target_revision=None, **kw):
        return mapping.get(set_id, SimpleNamespace(members=[]))
    monkeypatch.setattr(link_serving.settings_ops, "read_owned_set", fake)


# ── _org_brand_for_invite ─────────────────────────────────────────────


def test_org_brand_reads_the_owned_row_directly(monkeypatch):
    _patch_owned(monkeypatch, {
        "autonomy.org": _members(("anchore", {
            "name": "Anchore", "color": "#6c63ff",
            "byline": "sovereign by default", "icon_data_uri": WEBP_ICON,
        })),
    })
    out = link_serving._org_brand_for_invite("anchore")
    assert out == {
        "org_name": "Anchore",
        "org_color": "#6c63ff",
        "org_description": "sovereign by default",
        "org_icon": WEBP_ICON,
    }


def test_org_brand_omits_empty_byline_and_absent_icon(monkeypatch):
    _patch_owned(monkeypatch, {
        "autonomy.org": _members(("anchore", {
            "name": "Anchore", "color": "#6c63ff", "byline": "",
        })),
    })
    out = link_serving._org_brand_for_invite("anchore")
    assert out == {"org_name": "Anchore", "org_color": "#6c63ff"}


def test_org_brand_none_when_row_absent_no_generated_fallback(monkeypatch):
    # The identity cascade would synthesize a name from the slug; the owned
    # read finds no row, so brand is unavailable — never a generated UUID label.
    _patch_owned(monkeypatch, {"autonomy.org": _members(("other", {"name": "X", "color": "#111111"}))})
    assert link_serving._org_brand_for_invite("anchore") is None


def test_org_brand_none_when_name_missing_or_color_malformed(monkeypatch):
    _patch_owned(monkeypatch, {"autonomy.org": _members(("anchore", {"color": "#6c63ff"}))})
    assert link_serving._org_brand_for_invite("anchore") is None
    _patch_owned(monkeypatch, {"autonomy.org": _members(("anchore", {"name": "Anchore", "color": "purple"}))})
    assert link_serving._org_brand_for_invite("anchore") is None
    _patch_owned(monkeypatch, {"autonomy.org": _members(("anchore", {"name": "Anchore"}))})
    assert link_serving._org_brand_for_invite("anchore") is None


def test_org_brand_none_when_org_is_empty():
    assert link_serving._org_brand_for_invite("") is None
    assert link_serving._org_brand_for_invite(None) is None


def test_org_brand_read_failure_degrades_to_none(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(link_serving.settings_ops, "read_owned_set", boom)
    assert link_serving._org_brand_for_invite("anchore") is None


# ── _valid_org_icon: the icon is never a remote URL / legacy favicon ──


@pytest.mark.parametrize("bad", [
    "https://tracker.example/f.png",            # remote URL
    "/static/orgs/anchore.png",                 # path
    "data:image/png;base64," + base64.b64encode(b"png").decode(),  # wrong mime
    "data:image/webp;base64,%%%not-base64%%%",  # undecodable
    "data:image/webp;base64,",                  # empty payload
    123, None,
])
def test_org_icon_rejected(bad):
    assert link_serving._valid_org_icon(bad) is False


def test_org_icon_rejected_over_uri_char_bound():
    from tools.graph.schemas.org import ORG_ICON_DATA_URI_MAX_CHARS
    big = "data:image/webp;base64," + "A" * ORG_ICON_DATA_URI_MAX_CHARS
    assert len(big) > ORG_ICON_DATA_URI_MAX_CHARS
    assert link_serving._valid_org_icon(big) is False


def test_org_icon_rejected_over_decoded_byte_bound():
    over = link_serving._ORG_ICON_MAX_DECODED_BYTES + 1
    payload = base64.b64encode(b"\x00" * over).decode()
    assert link_serving._valid_org_icon("data:image/webp;base64," + payload) is False


def test_org_icon_accepts_bounded_webp():
    assert link_serving._valid_org_icon(WEBP_ICON) is True


# ── _sponsor_profile_for_invite ──────────────────────────────────────


def test_sponsor_profile_complete(monkeypatch, tmp_path):
    blob = tmp_path / "a.webp"
    blob.write_bytes(b"webp-bytes")
    _patch_owned(monkeypatch, {
        "autonomy.org.member-profile": _members((SPONSOR, {
            "display_name": "Ada", "byline": "founder", "avatar": "a0dfac16-c45e-4b87-90c8-af6400991dbd",
        })),
    })
    monkeypatch.setattr(
        "tools.graph.ops.get_attachment",
        lambda ref, *, org=None, peers=None: {
            "mime_type": "image/webp", "file_path": str(blob),
        },
    )
    out = link_serving._sponsor_profile_for_invite("anchore", SPONSOR)
    assert out["sponsor_pub"] == SPONSOR
    assert out["sponsor_name"] == "Ada"
    assert out["sponsor_byline"] == "founder"
    assert out["sponsor_avatar"] == "data:image/webp;base64," + base64.b64encode(b"webp-bytes").decode()


def test_sponsor_profile_missing_row_is_pub_only(monkeypatch):
    _patch_owned(monkeypatch, {"autonomy.org.member-profile": _members()})
    assert link_serving._sponsor_profile_for_invite("anchore", SPONSOR) == {"sponsor_pub": SPONSOR}


def test_sponsor_profile_wrong_persona_row_is_pub_only(monkeypatch):
    # A row keyed by a DIFFERENT persona must not leak into this sponsor.
    _patch_owned(monkeypatch, {
        "autonomy.org.member-profile": _members(("b" * 64, {"display_name": "Someone Else"})),
    })
    assert link_serving._sponsor_profile_for_invite("anchore", SPONSOR) == {"sponsor_pub": SPONSOR}


def test_sponsor_profile_rejects_noncanonical_pub(monkeypatch):
    _patch_owned(monkeypatch, {"autonomy.org.member-profile": _members()})
    assert link_serving._sponsor_profile_for_invite("anchore", "A" * 64) is None  # uppercase
    assert link_serving._sponsor_profile_for_invite("anchore", "a" * 63) is None  # short
    assert link_serving._sponsor_profile_for_invite("anchore", "zz" + "a" * 62) is None  # non-hex
    assert link_serving._sponsor_profile_for_invite("anchore", None) is None


def test_sponsor_profile_lookup_failure_is_pub_only(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(link_serving.settings_ops, "read_owned_set", boom)
    assert link_serving._sponsor_profile_for_invite("anchore", SPONSOR) == {"sponsor_pub": SPONSOR}


def test_sponsor_profile_name_and_byline_bounded(monkeypatch):
    _patch_owned(monkeypatch, {
        "autonomy.org.member-profile": _members((SPONSOR, {
            "display_name": "N" * 500, "byline": "B" * 500,
        })),
    })
    out = link_serving._sponsor_profile_for_invite("anchore", SPONSOR)
    assert len(out["sponsor_name"]) == link_serving._SPONSOR_TEXT_MAX
    assert len(out["sponsor_byline"]) == link_serving._SPONSOR_TEXT_MAX


# ── _sponsor_avatar_data_uri: only owned bounded raster bytes ─────────


def _avatar_attachment(monkeypatch, att):
    monkeypatch.setattr(
        "tools.graph.ops.get_attachment",
        lambda ref, *, org=None, peers=None: att,
    )


def test_avatar_from_owned_png(monkeypatch, tmp_path):
    blob = tmp_path / "a.png"
    blob.write_bytes(b"\x89PNG-bytes")
    _avatar_attachment(monkeypatch, {"mime_type": "image/png", "file_path": str(blob)})
    out = link_serving._sponsor_avatar_data_uri("anchore", "a0dfac16-c45e-4b87-90c8-af6400991dbd")
    assert out == "data:image/png;base64," + base64.b64encode(b"\x89PNG-bytes").decode()


@pytest.mark.parametrize("ref", [
    "https://cdn.example/a.png",                 # absolute URL
    "/uploads/a.png",                            # path
    "data:image/png;base64,AAAA",                # stored data URI
    "", None, 42,
])
def test_avatar_rejects_non_attachment_refs(monkeypatch, ref):
    # None of these are attachment-id shaped, so get_attachment is never reached.
    _avatar_attachment(monkeypatch, {"mime_type": "image/png", "file_path": "/dev/null"})
    assert link_serving._sponsor_avatar_data_uri("anchore", ref) is None


def test_avatar_missing_blob_is_omitted(monkeypatch):
    _avatar_attachment(monkeypatch, None)  # wrong-org / missing → get_attachment None
    assert link_serving._sponsor_avatar_data_uri("anchore", "a0dfac16-c45e-4b87-90c8-af6400991dbd") is None


def test_avatar_malformed_mime_is_omitted(monkeypatch, tmp_path):
    blob = tmp_path / "a.gif"
    blob.write_bytes(b"GIF89a")
    _avatar_attachment(monkeypatch, {"mime_type": "image/gif", "file_path": str(blob)})
    assert link_serving._sponsor_avatar_data_uri("anchore", "a0dfac16-c45e-4b87-90c8-af6400991dbd") is None


def test_avatar_oversized_is_omitted(monkeypatch, tmp_path):
    blob = tmp_path / "big.webp"
    blob.write_bytes(b"\x00" * (link_serving._SPONSOR_AVATAR_MAX_BYTES + 1))
    _avatar_attachment(monkeypatch, {"mime_type": "image/webp", "file_path": str(blob)})
    assert link_serving._sponsor_avatar_data_uri("anchore", "a0dfac16-c45e-4b87-90c8-af6400991dbd") is None


def test_avatar_empty_blob_is_omitted(monkeypatch, tmp_path):
    blob = tmp_path / "empty.jpg"
    blob.write_bytes(b"")
    _avatar_attachment(monkeypatch, {"mime_type": "image/jpeg", "file_path": str(blob)})
    assert link_serving._sponsor_avatar_data_uri("anchore", "a0dfac16-c45e-4b87-90c8-af6400991dbd") is None


def test_avatar_boundary_exactly_at_cap(monkeypatch, tmp_path):
    blob = tmp_path / "cap.jpeg"
    blob.write_bytes(b"\x00" * link_serving._SPONSOR_AVATAR_MAX_BYTES)
    _avatar_attachment(monkeypatch, {"mime_type": "image/jpeg", "file_path": str(blob)})
    out = link_serving._sponsor_avatar_data_uri("anchore", "a0dfac16-c45e-4b87-90c8-af6400991dbd")
    assert out is not None and out.startswith("data:image/jpeg;base64,")
