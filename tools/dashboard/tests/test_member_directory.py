"""member_directory: the org member directory's three writers and the
joiner-side import (punch list item 31)."""
from types import SimpleNamespace

from tools.dashboard import member_directory as md


ICON = "data:image/webp;base64,UklGRg=="


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
                                 "avatar_icon_data_uri": ICON})
    assert md.write_founder("org", "a" * 64) is True
    assert writes == [(md.MEMBER_PROFILE_SET_ID, "a" * 64,
                       {"display_name": "Alice", "byline": "Founder", "avatar": ICON, "color": ""},
                       "org", "published")]


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


def test_import_rows_skips_self_and_malformed_and_bounds_avatar(monkeypatch):
    writes = _capture(monkeypatch)
    n = md.import_rows("org", [
        {"persona_pub": "a" * 64, "display_name": "Alice", "avatar": ICON},
        {"persona_pub": "b" * 64, "display_name": "Bob"},            # self, skipped
        {"persona_pub": "c" * 64, "display_name": ""},               # nameless
        {"persona_pub": "short", "display_name": "X"},               # bad key
        {"persona_pub": "d" * 64, "display_name": "Dee", "avatar": "data:image/png;base64," + "A" * 30000},
        "junk",
    ], skip="b" * 64)
    assert n == 2
    assert writes[0][1] == "a" * 64 and writes[0][2]["avatar"] == ICON
    assert writes[1][1] == "d" * 64 and writes[1][2]["avatar"] == ""


def test_rows_lists_named_members_with_their_key(monkeypatch):
    monkeypatch.setattr(md.settings_ops, "read_owned_set", lambda *a, **k: SimpleNamespace(members=[
        SimpleNamespace(key="a" * 64, payload={"display_name": "Alice", "avatar": ICON, "byline": "", "color": ""}),
        SimpleNamespace(key="z" * 64, payload={"display_name": ""}),
    ]))
    assert md.rows("org") == [{"persona_pub": "a" * 64, "display_name": "Alice",
                               "avatar": ICON, "byline": "", "color": ""}]
