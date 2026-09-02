"""CLI vault removes route through the server's derivation seam by bare name."""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from tools.graph import set_cmd


def _args(set_id="autonomy.vault.secured", key="pg.test", org="autonomy"):
    return argparse.Namespace(id_parts=[set_id, key], org=org)


class _VaultClient:
    def __init__(self, receipt=None, error=None):
        self.calls = []
        self._receipt = receipt or {}
        self._error = error

    def read_set(self, set_id, *, org):
        raise AssertionError("a vault remove must not enumerate the set")

    def remove_vault_credential(self, set_id, name, *, org):
        self.calls.append((set_id, name, org))
        if self._error is not None:
            raise self._error
        return self._receipt


def test_vault_remove_dispatches_bare_name_to_the_seam(monkeypatch, capsys):
    client = _VaultClient(receipt={"ok": True, "key": "autonomy:pg.test"})
    monkeypatch.setattr(set_cmd, "get_client", lambda: client)
    set_cmd.cmd_set_remove(_args())
    assert client.calls == [("autonomy.vault.secured", "pg.test", "autonomy")]
    out = capsys.readouterr().out
    assert "Removed vault credential: autonomy:pg.test" in out


def test_vault_remove_tolerates_a_revision_suffix(monkeypatch):
    client = _VaultClient(receipt={"ok": True, "key": "autonomy:pg.test"})
    monkeypatch.setattr(set_cmd, "get_client", lambda: client)
    set_cmd.cmd_set_remove(_args(set_id="autonomy.vault.secured#1"))
    assert client.calls == [("autonomy.vault.secured", "pg.test", "autonomy")]


def test_vault_remove_covers_the_audited_set(monkeypatch):
    client = _VaultClient(receipt={"ok": True, "key": "pg.test"})
    monkeypatch.setattr(set_cmd, "get_client", lambda: client)
    set_cmd.cmd_set_remove(_args(set_id="autonomy.vault.audited"))
    assert client.calls == [("autonomy.vault.audited", "pg.test", "autonomy")]


def test_vault_remove_surfaces_the_server_refusal(monkeypatch, capsys):
    message = (
        "vault_remove organization writeback accepts a simple unprefixed "
        "credential name; the server derives the organization namespace"
    )
    client = _VaultClient(error=ValueError(message))
    monkeypatch.setattr(set_cmd, "get_client", lambda: client)
    with pytest.raises(SystemExit) as excinfo:
        set_cmd.cmd_set_remove(_args(key="autonomy:pg.test"))
    assert excinfo.value.code == 1
    assert message in capsys.readouterr().err


def test_direct_mode_client_falls_through_to_the_generic_path(
    monkeypatch, capsys,
):
    """A client without the seam keeps the caller's explicit local routing."""

    class DirectClient:
        def __init__(self):
            self.read_calls = []

        def read_set(self, set_id, *, org):
            self.read_calls.append((set_id, org))
            return SimpleNamespace(members=[])

    client = DirectClient()
    monkeypatch.setattr(set_cmd, "get_client", lambda: client)
    with pytest.raises(SystemExit):
        set_cmd.cmd_set_remove(_args())
    assert client.read_calls, "generic resolution should have enumerated"


def test_scope_phrase_never_renders_the_caller_org_sentinel():
    from tools.graph import ops

    phrase = set_cmd._scope_phrase(ops.CALLER_ORG)
    assert "CALLER_ORG" not in phrase
    assert phrase == " in the caller's organization scope"
    assert set_cmd._scope_phrase(None) == ""
    assert set_cmd._scope_phrase("acme") == " in org 'acme'"


def test_missing_member_error_names_the_caller_scope(monkeypatch, capsys):
    """No ``--org`` → the miss message says what it means, not a sentinel repr."""

    class DirectClient:
        def read_set(self, set_id, *, org):
            return SimpleNamespace(members=[])

    monkeypatch.setattr(set_cmd, "get_client", lambda: DirectClient())
    args = argparse.Namespace(id_parts=["dashboard.feature_flags", "nope"], org=None)
    with pytest.raises(SystemExit):
        set_cmd.cmd_set_remove(args)
    err = capsys.readouterr().err
    assert "<settings_ops.CALLER_ORG>" not in err
    assert "in the caller's organization scope" in err
