"""Writing a full payload to a key must pick the call the SET allows.

Three write functions implement one intent, and which is correct is a property
of the set rather than something a caller can know. ``upsert_by_key`` refuses
the two patterns it cannot serve, and the obvious repair — fall back to
``add_setting`` — is right for only one of them.

The vault case is the one worth a test rather than a comment. ``add_setting``
against a vault key that already exists does not raise: it mints a second base
for that key, which is the duplicate-base bug upserting was introduced to fix.
It resurfaces there in its least visible form, because vault rows are opaque
locators and the two bases cannot be told apart by reading them.
"""

from __future__ import annotations

import pytest

import tools.graph.settings_ops as settings_ops


@pytest.fixture
def calls(monkeypatch):
    """Record which write function the dispatch chose, without writing."""
    seen: list[tuple] = []

    def fake(name, ret):
        def f(*a, **k):
            seen.append((name, a, k))
            return ret
        return f

    monkeypatch.setattr(settings_ops, "add_setting", fake("add", "new-id"))
    monkeypatch.setattr(settings_ops, "upsert_by_key", fake("upsert", "up-id"))
    monkeypatch.setattr(settings_ops, "override_setting", fake("override", "ov-id"))
    monkeypatch.setattr(settings_ops, "_resolve_org_arg", lambda o: o)
    return seen


def _arrange(monkeypatch, *, pattern=None, tier=None, existing=None):
    monkeypatch.setattr(
        settings_ops, "_access_pattern_for", lambda *a, **k: pattern,
    )
    monkeypatch.setattr(
        settings_ops.schemas, "declared_vault_tier", lambda *a, **k: tier,
    )
    monkeypatch.setattr(
        settings_ops, "_existing_base_id", lambda *a, **k: existing,
    )


def _write():
    return settings_ops.write_by_key(
        "some.set", 1, "k", {"value": "v"}, org=None,
    )


def test_an_ordinary_set_still_upserts(monkeypatch, calls):
    """The 2026-08-03 fix must survive this change untouched."""
    _arrange(monkeypatch, existing="base-1")
    assert _write() == "up-id"
    assert [c[0] for c in calls] == ["upsert"]


def test_an_append_only_log_appends(monkeypatch, calls):
    _arrange(monkeypatch, pattern="append_only_log", existing="base-1")
    assert _write() == "new-id"
    assert [c[0] for c in calls] == ["add"]


def test_a_vault_sets_first_value_is_minted(monkeypatch, calls):
    """add_setting seals the initial revision; there is nothing to override."""
    _arrange(monkeypatch, tier="audited", existing=None)
    assert _write() == "new-id"
    assert [c[0] for c in calls] == ["add"]


def test_a_vault_set_that_already_has_the_key_replaces_its_row(monkeypatch, calls):
    """THE ONE THAT MATTERS (auto-z4582).

    A vaulted value is replaced whole, never stacked: a new row is added
    that ``replaces`` the existing one in the same transaction. Asserts the
    specific call, because a bare ``add_setting`` would quietly produce a
    second base for a key whose rows nobody can eyeball, and an override
    would stack.
    """
    _arrange(monkeypatch, tier="audited", existing="base-1")
    assert _write() == "new-id"
    assert [c[0] for c in calls] == ["add"]
    name, args, kwargs = calls[0]
    assert kwargs.get("replaces") == "base-1", "must replace the existing row"
    assert args[3] == {"value": "v"}, "a vault value is the COMPLETE payload"



def test_a_cold_vault_is_not_worked_around(monkeypatch):
    """The only fallback available would be storing the secret in the clear,
    so a missing sealer must propagate rather than be caught here."""
    _arrange(monkeypatch, tier="audited", existing=None)
    monkeypatch.setattr(settings_ops, "_resolve_org_arg", lambda o: o)

    def cold(*a, **k):
        raise settings_ops.VaultSealerMissing("no sealer in this process")

    monkeypatch.setattr(settings_ops, "add_setting", cold)
    with pytest.raises(settings_ops.VaultSealerMissing):
        _write()
