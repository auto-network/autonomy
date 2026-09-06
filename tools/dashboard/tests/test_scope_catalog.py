"""The scope catalog: one table, two languages, no drift, no unenforced scope.

Roles design of record graph://d1b3db8f-879 (R-S4): people never read a raw
scope string in a headline view, and a role is built only from scopes
something enforces. Bead auto-ai7li.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from tools.dashboard import scope_catalog

REPO_ROOT = Path(__file__).resolve().parents[3]
JS_TWIN = REPO_ROOT / "tools/dashboard/static/js/scope-catalog.js"


def _js_table() -> list[dict]:
    source = JS_TWIN.read_text(encoding="utf-8")
    match = re.search(
        r"/\* CATALOG-BEGIN \*/\s*const CATALOG = (\[.*?\]);\s*/\* CATALOG-END \*/",
        source, re.S,
    )
    assert match, "scope-catalog.js must carry the table between the markers"
    return json.loads(match.group(1))


def test_python_and_javascript_tables_are_identical():
    assert _js_table() == scope_catalog.CATALOG


def test_no_unenforced_scope_is_offered():
    patterns = {entry["pattern"] for entry in scope_catalog.CATALOG}
    assert "org:read" not in patterns
    assert "content:write" not in patterns
    for entry in scope_catalog.CATALOG:
        assert entry["enforced_by"], entry["pattern"]


def test_exact_and_templated_scopes_resolve_to_plain_words():
    star = scope_catalog.describe_scope("*")
    assert star["label"] == "Everything" and star["unknown"] is False

    member_invite = scope_catalog.describe_scope("invite:member")
    assert member_invite["unknown"] is False
    assert member_invite["role"] == "member"
    assert member_invite["label"] == "Invite people as member"
    assert "admit people as member" in member_invite["sentence"]

    grant_any = scope_catalog.describe_scope("role:grant:*")
    assert grant_any["label"] == "Approve and grant any role"
    assert "role" not in grant_any  # the family, not a templated role

    grant_admin = scope_catalog.describe_scope("role:grant:admin")
    assert grant_admin["role"] == "admin"
    assert grant_admin["scope"] == "role:grant:admin"


def test_unknown_scopes_come_back_raw_never_hidden():
    for raw in ("x:y", "org:read", "", None, "invite:not a role"):
        described = scope_catalog.describe_scope(raw)
        assert described["unknown"] is True
        assert described["scope"] == raw


def test_describe_scope_set_preserves_order():
    out = scope_catalog.describe_scope_set(["link:publish", "zzz", "invite:member"])
    assert [entry["scope"] for entry in out] == ["link:publish", "zzz", "invite:member"]
    assert [entry["unknown"] for entry in out] == [False, True, False]
