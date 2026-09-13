"""member_directory: the org member directory's three writers and the
joiner-side import (punch list item 31)."""
from types import SimpleNamespace

from tools.dashboard import member_directory as md


ICON = "data:image/webp;base64,UklGRg=="
PERSONAL_ATT = "0192a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"
ORG_ATT = "0192ffff-c3d4-7e5f-8a9b-0c1d2e3f4a5b"


def _capture(monkeypatch):
    writes = []
    monkeypatch.setattr(md.settings_ops, "upsert_by_key",
                        lambda set_id, rev, key, payload, *, org, state="raw", **kw:
                        writes.append((set_id, key, payload, org, state)) or "id")
    return writes


def test_founder_row_is_the_personal_profile_snapshot(monkeypatch):
    writes = _capture(monkeypatch)
    monkeypatch.setattr("tools.dashboard.personal_profile.get_effective_profile",
                        lambda: {"display_name": " Alice ", "biography": "Founder",
                                 "avatar_attachment_id": PERSONAL_ATT})
    copies = []
    monkeypatch.setattr(md, "org_avatar_attachment",
                        lambda slug, att: copies.append((slug, att)) or ORG_ATT)
    assert md.write_founder("org", "a" * 64) is True
    # The photo is copied into the ORG's attachment store; the row names
    # that copy, never the personal attachment and never an inline icon.
    assert copies == [("org", PERSONAL_ATT)]
    assert writes == [(md.MEMBER_PROFILE_SET_ID, "a" * 64,
                       {"display_name": "Alice", "byline": "Founder", "avatar": ORG_ATT, "color": ""},
                       "org", "published")]


def test_avatar_ref_accepts_only_a_canonical_attachment_id():
    assert md.avatar_ref(ORG_ATT) == ORG_ATT
    for bad in (ICON, "https://cdn.example/a.png", "/tmp/a.webp", ORG_ATT.upper(), "", None, 5):
        assert md.avatar_ref(bad) == ""


def test_no_personal_identity_writes_nothing(monkeypatch):
    writes = _capture(monkeypatch)
    monkeypatch.setattr("tools.dashboard.personal_profile.get_effective_profile", lambda: None)
    assert md.write_founder("org", "a" * 64) is False
    assert writes == []


def test_claim_projection_never_carries_a_photo_and_never_overwrites(monkeypatch):
    writes = _capture(monkeypatch)
    monkeypatch.setattr(md.settings_ops, "read_set_key", lambda *a, **k: None)
    assert md.project_claim("org", "b" * 64, {"display_name": "Bob", "biography": "Joining",
                                              "avatar_icon_data_uri": ICON}) is True
    assert writes[-1][2] == {"display_name": "Bob", "byline": "Joining", "avatar": "", "color": ""}
    monkeypatch.setattr(md.settings_ops, "read_set_key", lambda *a, **k: {"payload": {}})
    assert md.project_claim("org", "b" * 64, {"display_name": "Bob"}) is False
    assert len(writes) == 1


def test_rows_lists_named_members_with_their_key(monkeypatch):
    monkeypatch.setattr(md.settings_ops, "read_owned_set", lambda *a, **k: SimpleNamespace(members=[
        SimpleNamespace(key="a" * 64, payload={"display_name": "Alice", "avatar": ORG_ATT, "byline": "", "color": ""}),
        SimpleNamespace(key="z" * 64, payload={"display_name": ""}),
    ]))
    assert md.rows("org") == [{"persona_pub": "a" * 64, "display_name": "Alice",
                               "avatar": ORG_ATT, "byline": "", "color": ""}]
