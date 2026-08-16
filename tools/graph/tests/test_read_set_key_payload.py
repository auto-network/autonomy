"""``read_set_key`` answers with the value, and identifies the base.

The row it returns identifies the BASE — which is what a caller targeting an
override, a promote or an exclude needs, and why it returns a row rather than
a payload. Its payload is the RESOLVED one: base plus every override that
applies.

Those two have to come from different places, and when they did not, the
function returned a well-formed payload with declared defaults applied that
silently omitted every override. Nothing about the shape gave that away — it
looked exactly like a resolved payload, so the first consumer to read a field
from it read a stale value and had no way to notice.
"""
from __future__ import annotations

import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB

SET_ID = "autonomy.workspace"


@pytest.fixture
def acme(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    GraphDB.create_org_db("acme").close()
    yield
    GraphDB.close_all_pooled()


def _base(**payload) -> str:
    return settings_ops.add_setting(
        SET_ID, 1, "w", {"name": "w", "image": "img", **payload}, org="acme")


def _resolved():
    return settings_ops.read_set_key(SET_ID, "w", org="acme", peers=[])


def test_an_override_is_applied(acme):
    base = _base()
    settings_ops.override_setting(base, {"image": "img2"}, org="acme")

    assert _resolved()["payload"]["image"] == "img2"


def test_overrides_apply_in_order(acme):
    base = _base()
    settings_ops.override_setting(base, {"image": "second"}, org="acme")
    settings_ops.override_setting(base, {"image": "third"}, org="acme")

    assert _resolved()["payload"]["image"] == "third"


def test_it_agrees_with_read_set(acme):
    """The two read paths must not answer differently about one key."""
    base = _base(working_dir="/a")
    settings_ops.override_setting(base, {"working_dir": "/b"}, org="acme")

    member = next(m for m in settings_ops.read_set(
        SET_ID, org="acme", peers=[]).members if m.key == "w")

    assert _resolved()["payload"] == member.payload


def test_the_row_still_identifies_the_base(acme):
    """Most callers want the id, to target an override or a promote."""
    base = _base()
    settings_ops.override_setting(base, {"image": "img2"}, org="acme")

    assert _resolved()["id"] == base


def test_declared_defaults_are_still_applied(acme):
    _base()
    assert _resolved()["payload"]["network_host"] is False


def test_a_key_with_no_row_resolves_to_nothing(acme):
    _base()
    assert settings_ops.read_set_key(
        SET_ID, "absent", org="acme", peers=[]) is None
