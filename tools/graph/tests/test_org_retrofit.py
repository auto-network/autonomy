"""retrofit_found_ledgers: found every keyed and keyless org (auto-6l3f8)."""

from __future__ import annotations

import io
import os
import types

import pytest

from tools.graph import org_cmd, org_ops, settings_ops
from tools.graph.db import GraphDB
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
from tools.network.ledger import HLC, LedgerStore, make_event, org_ledger_db_path

PASSWORD = "week-glacier-thirty-nine"
T0 = 1_800_000_000_000


@pytest.fixture
def env(tmp_path, monkeypatch):
    GraphDB.close_all_pooled()
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    # NO GRAPH_DB pin: the retrofit needs the production layout — a real
    # personal.db plus genuinely separate per-org settings stores (the
    # pin would collapse every org's Settings into one file).
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("AUTONOMY_PERSONAL_PASSWORD", raising=False)
    GraphDB.create_org_db("personal", type_="personal", root=orgs).close()
    personal_root = KeyPair.generate()
    with settings_ops.identity_write_context():
        settings_ops.upsert_by_key(
            PERSONAL_IDENTITY_SET_ID, 1, "default",
            {
                "armored_private_key": encrypt_root_key(
                    personal_root, PASSWORD, iterations=10_000
                ),
                "root_pub": personal_root.public_hex,
                "display_name": "Owner",
                "created_at": "2026-07-26T00:00:00Z",
            },
            org=None,
        )
    # The pre-existing "autonomy"-style org: keyed with a LEGACY password
    # armor of an independent root, orgs row present, no ledger.
    GraphDB.create_org_db("autonomy", root=orgs).close()
    legacy_root = KeyPair.generate()
    settings_ops.add_setting(
        NETWORK_ORG_KEY_SET_ID, 1, "default",
        {
            "armored_private_key": encrypt_root_key(
                legacy_root, PASSWORD, iterations=10_000
            ),
            "root_pub": legacy_root.public_hex,
        },
        org="autonomy", state="canonical",
    )
    # A legacy KEYLESS org: orgs row, no key Setting, no ledger.
    GraphDB.create_org_db("keyless", root=orgs).close()
    yield types.SimpleNamespace(
        orgs=orgs,
        personal_root=personal_root,
        personal_seed=bytes.fromhex(personal_root.private_hex),
        legacy_root=legacy_root,
    )
    GraphDB.close_all_pooled()


def _outcome(report, slug):
    return next(e for e in report.outcomes if e["slug"] == slug)


class TestRetrofitFoundLedgers:
    def test_keyed_org_founds_under_its_stored_root(self, env):
        report = org_ops.retrofit_found_ledgers(PASSWORD)
        entry = _outcome(report, "autonomy")
        assert entry["outcome"] == "founded"

        org = org_ops.get_org("autonomy")
        with LedgerStore(org_ledger_db_path("autonomy")) as store:
            assert len(store) == 4
            genesis = store.ledger.genesis
            assert genesis.payload["org"] == org.id  # the orgs.id UUID
            assert genesis.payload["org"] != "autonomy"  # never the slug
            assert genesis.payload["root_pub"] == env.legacy_root.public_hex
            state = store.fold()
            founder = derive_persona(env.personal_seed, genesis.event_id)
            assert state.members[founder.public_hex].roles == ("owner",)
            # RESOLUTION 2: credential-free founding.
            for event in store.events():
                if event.type == "member.claim":
                    assert "kem_credential" not in event.payload

    def test_second_run_is_idempotent(self, env):
        org_ops.retrofit_found_ledgers(PASSWORD)
        report = org_ops.retrofit_found_ledgers(PASSWORD)
        for slug in ("autonomy", "keyless"):
            assert _outcome(report, slug)["outcome"] == "skipped_already_founded"
        with LedgerStore(org_ledger_db_path("autonomy")) as store:
            assert len(store) == 4

    def test_keyless_org_gets_a_sealed_root_then_founds(self, env):
        report = org_ops.retrofit_found_ledgers(PASSWORD)
        assert _outcome(report, "keyless")["outcome"] == "keyed_and_founded"

        member = next(
            m
            for m in settings_ops.read_owned_set(
                NETWORK_ORG_KEY_SET_ID, org="keyless"
            ).members
            if isinstance(m.payload, dict)
        )
        payload = member.payload
        recipient_priv, _ = derive_encapsulation_keypair(
            env.personal_seed, ORG_ROOT_ARMOR_PURPOSE
        )
        seed = seal_open(
            bytes.fromhex(payload["sealed_root_key"]),
            recipient_priv,
            ORG_ROOT_ARMOR_PURPOSE,
        )
        minted = KeyPair.from_private_hex(seed.hex())
        assert minted.public_hex == payload["root_pub"]
        # Independent of the personal root, and openable ONLY by the owner.
        assert minted.public_hex != env.personal_root.public_hex
        other_priv, _ = derive_encapsulation_keypair(
            bytes(range(32)), ORG_ROOT_ARMOR_PURPOSE
        )
        with pytest.raises(SealingError):
            seal_open(
                bytes.fromhex(payload["sealed_root_key"]), other_priv,
                ORG_ROOT_ARMOR_PURPOSE,
            )

        with LedgerStore(org_ledger_db_path("keyless")) as store:
            assert len(store) == 4
            assert store.ledger.genesis.payload["root_pub"] == minted.public_hex
            state = store.fold()
            assert any("owner" in m.roles for m in state.members.values())

    def test_wrong_password_founds_nothing(self, env):
        with pytest.raises(ArmorPassphraseError):
            org_ops.retrofit_found_ledgers("not-the-password")
        for slug in ("autonomy", "keyless"):
            with LedgerStore(org_ledger_db_path(slug)) as store:
                assert len(store) == 0

    def test_interrupted_founding_is_completed_not_refounded(self, env):
        org = org_ops.get_org("autonomy")
        with LedgerStore(org_ledger_db_path("autonomy")) as store:
            genesis_id = store.append(
                make_event(
                    env.legacy_root,
                    {
                        "type": "genesis",
                        "org": org.id,
                        "root_pub": env.legacy_root.public_hex,
                    },
                    [],
                    HLC(T0, 0),
                )
            )
        report = org_ops.retrofit_found_ledgers(PASSWORD)
        entry = _outcome(report, "autonomy")
        assert entry["outcome"] == "founded"
        assert entry["genesis_id"] == genesis_id  # SAME genesis: identity kept
        with LedgerStore(org_ledger_db_path("autonomy")) as store:
            assert len(store) == 4
            assert store.fold().genesis_id == genesis_id

    def test_foreign_genesis_aborts_that_org_only(self, env):
        org = org_ops.get_org("autonomy")
        imposter = KeyPair.generate()
        with LedgerStore(org_ledger_db_path("autonomy")) as store:
            store.append(
                make_event(
                    imposter,
                    {
                        "type": "genesis",
                        "org": org.id,
                        "root_pub": imposter.public_hex,
                    },
                    [],
                    HLC(T0, 0),
                )
            )
        report = org_ops.retrofit_found_ledgers(PASSWORD)
        assert _outcome(report, "autonomy")["outcome"].startswith("error:")
        with LedgerStore(org_ledger_db_path("autonomy")) as store:
            assert len(store) == 1  # never completed onto unknown provenance
        # The rest of the run continued.
        assert _outcome(report, "keyless")["outcome"] == "keyed_and_founded"

    def test_cli_drives_the_retrofit(self, env, monkeypatch, capsys):
        import argparse

        parser = argparse.ArgumentParser()
        org_cmd.attach_org_subparser(parser.add_subparsers(dest="cmd"))
        args = parser.parse_args(["org", "retrofit-ledgers", "--password-stdin"])
        monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
        args.func(args)
        out = capsys.readouterr().out
        assert "autonomy: founded" in out
        assert "keyless: keyed_and_founded" in out
