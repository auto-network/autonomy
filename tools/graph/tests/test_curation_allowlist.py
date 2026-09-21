"""Tests for the curation allowlist loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.graph.curation import allowlist as al


def _write(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


def test_loads_bundled_autonomy_allowlist():
    loaded = al.load(al.DEFAULT_AUTONOMY_PATH)
    assert loaded.org == "autonomy"
    assert loaded.version == 1
    # Both tiers non-empty after the auto-gzvji bootstrap promotion (commit
    # 261429f intentionally downgraded 14 entries canonical → published to
    # unblock auto-hbgow while their pending comments are integrated, so
    # the lower bound here is set well below the post-downgrade count to
    # avoid future drift on either side).
    assert len(loaded.canonical) >= 5
    assert len(loaded.published) >= 5
    # No prefix may appear in both tiers.
    assert set(loaded.canonical).isdisjoint(set(loaded.published))


def test_tiers_iterates_in_order(tmp_path):
    p = _write(tmp_path / "a.yaml", "org: x\nversion: 1\ncanonical: [aaa, bbb]\npublished: [ccc]\n")
    loaded = al.load(p)
    entries = list(loaded.tiers())
    assert [e.prefix for e in entries] == ["aaa", "bbb", "ccc"]
    assert [e.target_state for e in entries] == ["canonical", "canonical", "published"]


def test_rejects_missing_org(tmp_path):
    p = _write(tmp_path / "a.yaml", "version: 1\n")
    with pytest.raises(al.AllowlistError):
        al.load(p)


def test_rejects_overlap(tmp_path):
    p = _write(tmp_path / "a.yaml",
               "org: x\nversion: 1\ncanonical: [aaa]\npublished: [aaa]\n")
    with pytest.raises(al.AllowlistError, match="both canonical and published"):
        al.load(p)


def test_rejects_non_list_tier(tmp_path):
    p = _write(tmp_path / "a.yaml",
               "org: x\nversion: 1\ncanonical: aaa\n")
    with pytest.raises(al.AllowlistError, match="must be a list"):
        al.load(p)


def test_rejects_bad_version(tmp_path):
    p = _write(tmp_path / "a.yaml", "org: x\nversion: -1\ncanonical: []\n")
    with pytest.raises(al.AllowlistError, match="positive int"):
        al.load(p)


def test_missing_file_raises(tmp_path):
    with pytest.raises(al.AllowlistError, match="not found"):
        al.load(tmp_path / "nope.yaml")


def test_empty_tier_keys_ok(tmp_path):
    p = _write(tmp_path / "a.yaml", "org: x\nversion: 1\n")
    loaded = al.load(p)
    assert loaded.canonical == []
    assert loaded.published == []


# ── the org:follow rendezvous block (design of record §10.1) ─────


_FOLLOW_YAML = (
    "org: x\nversion: 1\n"
    "follow:\n"
    "  org_uuid: '11111111-1111-4111-8111-111111111111'\n"
    "  rendezvous: 'https://relay.auto.network/l/abc'\n"
    "  link_pub: '{}'\n".format("a" * 64)
)


def test_loads_follow_block(tmp_path):
    loaded = al.load(_write(tmp_path / "a.yaml", _FOLLOW_YAML))
    assert loaded.follow == {
        "org_uuid": "11111111-1111-4111-8111-111111111111",
        "rendezvous": "https://relay.auto.network/l/abc",
        "link_pub": "a" * 64,
    }


def test_bundled_autonomy_allowlist_carries_a_follow_block():
    loaded = al.load(al.DEFAULT_AUTONOMY_PATH)
    assert loaded.follow is not None
    # The org uuid is committed now; the link values arrive only when the
    # operator publishes the follow link (record v5 §10.1), so their
    # absence is the expected state of the bundled file until then.
    assert loaded.follow["org_uuid"] == "2d4b90cb-1e89-452b-82cb-68ca44fd8e52"
    assert "0000" not in loaded.follow["org_uuid"]


def test_follow_absent_is_none(tmp_path):
    loaded = al.load(_write(tmp_path / "a.yaml", "org: x\nversion: 1\n"))
    assert loaded.follow is None


def test_follow_missing_required_key_raises(tmp_path):
    body = "org: x\nversion: 1\nfollow:\n  rendezvous: 'r'\n  link_pub: 'k'\n"
    with pytest.raises(al.AllowlistError, match="missing required key"):
        al.load(_write(tmp_path / "a.yaml", body))


def test_follow_block_loads_with_only_the_org_uuid(tmp_path):
    body = "org: x\nversion: 1\nfollow:\n  org_uuid: 'u'\n"
    loaded = al.load(_write(tmp_path / "a.yaml", body))
    assert loaded.follow == {"org_uuid": "u"}


def test_follow_unknown_key_raises(tmp_path):
    body = (
        "org: x\nversion: 1\nfollow:\n"
        "  org_uuid: 'u'\n  rendezvous: 'r'\n  link_pub: 'p'\n  extra: 'no'\n"
    )
    with pytest.raises(al.AllowlistError, match="unknown key"):
        al.load(_write(tmp_path / "a.yaml", body))


def test_follow_non_mapping_raises(tmp_path):
    body = "org: x\nversion: 1\nfollow: not-a-map\n"
    with pytest.raises(al.AllowlistError, match="must be a mapping"):
        al.load(_write(tmp_path / "a.yaml", body))


def test_follow_empty_value_raises(tmp_path):
    body = (
        "org: x\nversion: 1\nfollow:\n"
        "  org_uuid: ''\n  rendezvous: 'r'\n  link_pub: 'p'\n"
    )
    with pytest.raises(al.AllowlistError, match="non-empty string"):
        al.load(_write(tmp_path / "a.yaml", body))
