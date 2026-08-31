"""CLI secured reads use the approval delivery path, never export a CEK."""

from __future__ import annotations

import argparse
from tools.graph import set_cmd


def _args():
    return argparse.Namespace(
        id_parts=["autonomy.vault.secured", "mac.ssh"],
        org="autonomy",
        chain=False,
    )


def test_secured_read_requests_vault_open_and_prints_only_ramfs_path(
    monkeypatch, capsys,
):
    calls = []

    class Client:
        def read_set(self, set_id, *, org):
            raise AssertionError("personal secured reads must not enumerate the set")

        def request_vault_open(self, set_id, key, *, org, ttl_seconds=0):
            calls.append((set_id, key, org, ttl_seconds))
            return {
                "delivery": "session-ramfs",
                "path": "/run/secrets/vault-open-release-1.json",
            }

    monkeypatch.setattr(set_cmd, "get_client", lambda: Client())
    set_cmd.cmd_set_read(_args())

    assert capsys.readouterr().out == "/run/secrets/vault-open-release-1.json\n"
    assert calls == [("autonomy.vault.secured", "mac.ssh", "autonomy", 0)]


def test_secured_read_tolerates_a_revision_suffix(monkeypatch, capsys):
    """`graph set read 'autonomy.vault.secured#1' <key>` — the #1 the docs
    and `set add` use must NOT make the secured dispatch miss (it would fall
    through to a failing plain read). The suffix is stripped for the read."""
    calls = []

    class Client:
        def read_set(self, set_id, *, org):
            raise AssertionError("must dispatch to vault-open, not enumerate")

        def request_vault_open(self, set_id, key, *, org, ttl_seconds=0):
            calls.append((set_id, key, org, ttl_seconds))
            return {"delivery": "session-ramfs",
                    "path": "/run/secrets/fleet-ssh-key"}

    args = argparse.Namespace(
        id_parts=["autonomy.vault.secured#1", "fleet-ssh-key"],
        org="blindhash", chain=False,
    )
    monkeypatch.setattr(set_cmd, "get_client", lambda: Client())
    set_cmd.cmd_set_read(args)
    assert capsys.readouterr().out == "/run/secrets/fleet-ssh-key\n"
    # dispatched with the BARE set id, suffix stripped
    assert calls == [("autonomy.vault.secured", "fleet-ssh-key", "blindhash", 0)]


def test_secured_read_passes_ttl_and_defaults_to_lifespan(monkeypatch, capsys):
    """--ttl flows to request_vault_open; default is 0 (full container
    lifespan — the credential is destroyed only when the container stops)."""
    seen = []

    class Client:
        def request_vault_open(self, set_id, key, *, org, ttl_seconds=0):
            seen.append(ttl_seconds)
            return {"delivery": "session-ramfs", "path": "/run/secrets/k"}

    monkeypatch.setattr(set_cmd, "get_client", lambda: Client())

    base = dict(id_parts=["autonomy.vault.secured", "k"], org="blindhash",
                chain=False)
    set_cmd.cmd_set_read(argparse.Namespace(**base, ttl=0))
    set_cmd.cmd_set_read(argparse.Namespace(**base, ttl=120))
    # a caller with no --ttl attribute at all still defaults to lifespan
    set_cmd.cmd_set_read(argparse.Namespace(**base))
    assert seen == [0, 120, 0]
