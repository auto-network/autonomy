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

        def request_vault_open(self, set_id, key, *, org):
            calls.append((set_id, key, org))
            return {
                "delivery": "session-ramfs",
                "path": "/run/secrets/vault-open-release-1.json",
            }

    monkeypatch.setattr(set_cmd, "get_client", lambda: Client())
    set_cmd.cmd_set_read(_args())

    assert capsys.readouterr().out == "/run/secrets/vault-open-release-1.json\n"
    assert calls == [("autonomy.vault.secured", "mac.ssh", "autonomy")]
