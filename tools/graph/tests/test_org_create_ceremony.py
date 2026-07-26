"""create_org_with_identity: the atomic founding ceremony (auto-nixfv)."""

from __future__ import annotations

import io
import json
import time
import types

import pytest

from tools.graph import org_cmd, org_ops, settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_ORG_KEY_SET_ID,
    ORG_ROOT_ARMOR_PURPOSE,
)
from tools.graph.schemas.personal_identity import PERSONAL_IDENTITY_SET_ID
from tools.network.idkit import KeyPair, derive_persona
from tools.network.idkit.armor import ArmorPassphraseError, encrypt_root_key
from tools.network.idkit.errors import SealingError
from tools.network.idkit.sealing import derive_encapsulation_keypair
from tools.network.idkit.sealing import open as seal_open
from tools.network.ledger import LedgerStore, org_ledger_db_path

PASSWORD = "week-glacier-thirty-nine"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "graph.db"))
    monkeypatch.delenv("AUTONOMY_PERSONAL_PASSWORD", raising=False)
    personal_root = KeyPair.generate()
    with settings_ops.identity_write_context():
        settings_ops.upsert_by_key(
            PERSONAL_IDENTITY_SET_ID, 1, "default",
            {
                "armored_private_key": encrypt_root_key(
                    personal_root, PASSWORD, iterations=10_000
                ),
                "root_pub": personal_root.public_hex,
                "display_name": "Test Owner",
                "created_at": "2026-07-26T00:00:00Z",
            },
            org=None,
        )
    yield types.SimpleNamespace(
        orgs=orgs,
        personal_root=personal_root,
        personal_seed=bytes.fromhex(personal_root.private_hex),
    )
    GraphDB.close_all_pooled()


def test_ceremony_founds_a_four_event_ledger(env):
    result = org_ops.create_org_with_identity("acme", PASSWORD)
    assert len(result.event_ids) == 4
    with LedgerStore(org_ledger_db_path("acme")) as store:
        assert len(store) == 4
        assert tuple(sorted(result.event_ids)) == tuple(sorted(store.ledger.all_ids()))
        state = store.fold()
        member = state.members[result.founder_persona_pub]
        assert member.roles == ("owner",)
        assert state.authority(result.founder_persona_pub) == frozenset({"*"})
        assert state.bare_roles == {}

        # genesis.org is the stable orgs.id UUID label, never the slug (D21).
        genesis = store.get(result.genesis_id)
        assert genesis.payload["org"] == result.org.id
        assert genesis.payload["org"] != "acme"
        assert genesis.payload["root_pub"] == result.root_pub

        # The founder persona is derived from the actual genesis id.
        assert (
            derive_persona(env.personal_seed, result.genesis_id).public_hex
            == result.founder_persona_pub
        )

        # RESOLUTION 2: the founding claim carries no kem_credential.
        claim = store.get(result.event_ids[3])
        assert "kem_credential" not in claim.payload


def test_org_key_revision_2_seals_to_the_owner(env):
    result = org_ops.create_org_with_identity("acme", PASSWORD)
    member = next(
        m
        for m in settings_ops.read_owned_set(NETWORK_ORG_KEY_SET_ID, org="acme").members
        if isinstance(m.payload, dict)
    )
    payload = member.payload
    assert payload["root_pub"] == result.root_pub
    assert payload["seal_purpose"] == ORG_ROOT_ARMOR_PURPOSE

    recipient_priv, recipient_pub = derive_encapsulation_keypair(
        env.personal_seed, ORG_ROOT_ARMOR_PURPOSE
    )
    assert payload["owner_kem_pub"] == recipient_pub
    seed = seal_open(
        bytes.fromhex(payload["sealed_root_key"]), recipient_priv, ORG_ROOT_ARMOR_PURPOSE
    )
    assert KeyPair.from_private_hex(seed.hex()).public_hex == result.root_pub

    # Fail closed: a different personal seed, or a different purpose.
    other_priv, _ = derive_encapsulation_keypair(
        bytes(range(32)), ORG_ROOT_ARMOR_PURPOSE
    )
    with pytest.raises(SealingError):
        seal_open(
            bytes.fromhex(payload["sealed_root_key"]), other_priv, ORG_ROOT_ARMOR_PURPOSE
        )
    with pytest.raises(SealingError):
        seal_open(
            bytes.fromhex(payload["sealed_root_key"]), recipient_priv, "other/purpose"
        )


def test_org_root_is_independent_of_the_personal_root(env):
    a = org_ops.create_org_with_identity("acme", PASSWORD)
    b = org_ops.create_org_with_identity("globex", PASSWORD)
    assert a.root_pub != b.root_pub  # random mint, not a derivation
    assert a.root_pub != env.personal_root.public_hex
    assert a.org.id != b.org.id


def test_wrong_password_creates_nothing(env):
    with pytest.raises(ArmorPassphraseError):
        org_ops.create_org_with_identity("acme", "not-the-password")
    assert all(o.slug != "acme" for o in org_ops.list_orgs())
    assert not (env.orgs / "acme.db").exists()


def test_fault_after_genesis_leaves_nothing(env, monkeypatch):
    import tools.network.idkit.sealing as sealing_mod

    def blow_up(*args, **kwargs):
        raise RuntimeError("injected fault after founding")

    with monkeypatch.context() as patched:
        patched.setattr(sealing_mod, "seal", blow_up)
        with pytest.raises(RuntimeError):
            org_ops.create_org_with_identity("acme", PASSWORD)
    assert all(o.slug != "acme" for o in org_ops.list_orgs())
    assert not (env.orgs / "acme.db").exists()

    result = org_ops.create_org_with_identity("acme", PASSWORD)  # clean re-run
    assert result.org.slug == "acme"


def test_cli_create_runs_the_ceremony(env, monkeypatch, capsys):
    import argparse

    parser = argparse.ArgumentParser()
    org_cmd.attach_org_subparser(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(["org", "create", "acme", "--password-stdin"])
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    args.func(args)
    out = capsys.readouterr().out
    payload = json.loads(out[: out.rindex("}") + 1])
    assert payload["org"]["slug"] == "acme"
    assert len(payload["identity"]["event_ids"]) == 4
    with LedgerStore(org_ledger_db_path("acme")) as store:
        assert len(store) == 4


def test_cli_requires_a_password(env, monkeypatch, capsys):
    import argparse

    parser = argparse.ArgumentParser()
    org_cmd.attach_org_subparser(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(["org", "create", "acme"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)
    assert excinfo.value.code == 2
    assert "personal-identity password" in capsys.readouterr().err
    assert all(o.slug != "acme" for o in org_ops.list_orgs())
