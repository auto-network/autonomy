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
    # Canonical is a strictly larger set than published; both non-empty.
    assert len(loaded.canonical) >= 20
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
