"""This machine's serving credential is a machine-homed row plus a machine-vault
key (graph://67d0aa5f-885 D4, D5); the supervisor's state reads them.

Replaces test_serve_cert_per_machine.py: the ownership question that file pinned
(whose row is mine, does my key file exist) is gone, because the machine store
holds only this machine's rows and never replicates, and the key is a vault
row rather than a file.
"""

from __future__ import annotations

import time

import pytest

from tools.dashboard import link_serving_supervisor as lss
from tools.dashboard.tests import _serving_vault_kit as vault_kit
from tools.graph import settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_BINDING_REVISION,
    NETWORK_BINDING_SET_ID,
)
from tools.network.idkit import KeyPair, Subject, issue_cert

ORG = "acme"
ORG_UUID = "44444444-4444-4444-8444-444444444444"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.setenv("AUTONOMY_KEYCACHE_MOUNT", str(tmp_path / "keycache"))
    (tmp_path / "keycache").mkdir()
    monkeypatch.setenv("AUTONOMY_NETWORK_KEY_DIR", str(tmp_path / "network"))
    GraphDB.create_org_db(ORG).close()
    private = vault_kit.publish_recipient(monkeypatch)
    yield {"tmp": tmp_path, "delegate_private": private}
    vault_kit.cold()
    GraphDB.close_all_pooled()


def _bind(root: KeyPair) -> None:
    now = int(time.time())
    settings_ops.add_setting(
        NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION, "auto.network",
        {"org_uuid": ORG_UUID, "root_pub": root.public_hex,
         "registry_url": "https://auto.network", "recovery_policy": {"mode": "none"},
         "binding_expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 86400))},
        org=ORG,
    )


def _persona_credential(ttl=30 * 86400):
    persona, delegate = KeyPair.generate(), KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(
        persona, delegate.public_hex, scope=("tunnel:serve",), org=ORG_UUID,
        subject=Subject("persona", persona.public_hex),
        not_before=now - 300, not_after=now + ttl,
    )
    return persona, delegate, cert


def test_the_set_is_machine_homed_and_plain():
    from tools.graph import schemas
    from tools.graph.schemas.machine_serve_cert import MACHINE_SERVE_CERT_SET_ID

    assert schemas.declared_home(MACHINE_SERVE_CERT_SET_ID) == "machine"
    assert schemas.declared_vault_tier(MACHINE_SERVE_CERT_SET_ID) is None


def test_no_binding_reads_missing(env):
    assert lss.serve_cert_state(ORG) == {"status": "missing"}


def test_a_bound_org_with_no_row_reads_missing(env):
    _bind(KeyPair.generate())
    assert lss.serve_cert_state(ORG) == {"status": "missing"}


def test_a_row_whose_key_is_not_vaulted_reads_key_unvaulted(env):
    root = KeyPair.generate(); _bind(root)
    persona, delegate, cert = _persona_credential()
    vault_kit.store_row(ORG_UUID, cert=cert.to_json().decode("ascii"),
                        child_pub=delegate.public_hex, not_after=cert.not_after,
                        persona_pub=persona.public_hex)
    assert lss.serve_cert_state(ORG)["status"] == "key-unvaulted"


def test_a_vaulted_credential_reads_ok_while_cold_and_names_its_work_base(env):
    root = KeyPair.generate(); _bind(root)
    persona, delegate, cert = _persona_credential()
    vault_kit.store_key(ORG_UUID, delegate.private_hex)
    vault_kit.store_row(ORG_UUID, cert=cert.to_json().decode("ascii"),
                        child_pub=delegate.public_hex, not_after=cert.not_after,
                        persona_pub=persona.public_hex)
    vault_kit.cold()
    state = lss.serve_cert_state(ORG)
    assert state["status"] == "ok", state
    assert state["child_pub"] == delegate.public_hex
    assert state["vault_key"] == f"serving-key.{ORG_UUID}.{delegate.public_hex}"
    assert state["work_base"].endswith(f"serve-{ORG_UUID}-{delegate.public_hex}")
    assert "key_path" not in state


def test_the_key_opens_only_when_the_vault_is_warm(env):
    root = KeyPair.generate(); _bind(root)
    persona, delegate, cert = _persona_credential()
    vault_kit.store_key(ORG_UUID, delegate.private_hex)
    vault_kit.store_row(ORG_UUID, cert=cert.to_json().decode("ascii"),
                        child_pub=delegate.public_hex, not_after=cert.not_after,
                        persona_pub=persona.public_hex)
    state = lss.serve_cert_state(ORG)
    vault_kit.cold()
    with pytest.raises(RuntimeError, match="cannot be opened"):
        lss.serving_key_hex(state)
    path, error = lss._release_serving_key(state)
    assert path is None and "cannot be opened" in error
    vault_kit.warm(env["delegate_private"])
    assert lss.serving_key_hex(state) == delegate.private_hex
    path, error = lss._release_serving_key(state)
    assert error is None and open(path).read() == delegate.private_hex
    assert path.startswith(str(env["tmp"] / "keycache" / "serving"))


def test_an_expired_row_reads_expired(env):
    root = KeyPair.generate(); _bind(root)
    persona, delegate, cert = _persona_credential(ttl=-10)
    vault_kit.store_key(ORG_UUID, delegate.private_hex)
    vault_kit.store_row(ORG_UUID, cert=cert.to_json().decode("ascii"),
                        child_pub=delegate.public_hex, not_after=cert.not_after,
                        persona_pub=persona.public_hex)
    assert lss.serve_cert_state(ORG)["status"] == "expired"


def test_the_deprecated_organization_homed_row_is_not_read(env):
    """A row in autonomy.network.serve-cert, however valid, is not this
    machine's credential any more (D5): no reader remains."""
    from tools.graph.schemas.network_identity import NETWORK_SERVE_CERT_SET_ID

    root = KeyPair.generate(); _bind(root)
    persona, delegate, cert = _persona_credential()
    settings_ops.add_setting(
        NETWORK_SERVE_CERT_SET_ID, 3, "some-machine",
        {"cert": cert.to_json().decode("ascii"), "key_path": "serve.key",
         "persona_pub": persona.public_hex, "not_after": cert.not_after},
        org=ORG,
    )
    assert lss.serve_cert_state(ORG) == {"status": "missing"}


def _root_signed_personal_credential(root: KeyPair, org_uuid: str, subject_pub: str):
    """The browser's personal mint (network-signon.mjs _mintServeCredential):
    root-signed, persona subject, plus the identity-neutral viewer cert."""
    delegate = KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(
        root, delegate.public_hex, scope=("tunnel:serve",), org=org_uuid,
        subject=Subject("persona", subject_pub), not_before=now - 300, not_after=now + 30 * 86400,
    )
    viewer = issue_cert(
        root, delegate.public_hex, scope=("tunnel:serve",), org=org_uuid,
        subject=Subject("operator", delegate.public_hex), not_before=cert.not_before,
        not_after=cert.not_after,
    )
    return delegate, cert, viewer


@pytest.mark.parametrize("names", ["root", "persona"])
def test_a_personal_delegate_naming_the_root_is_ok_but_flagged_for_remint(env, monkeypatch, names):
    """auto-8sdrr: personal Services are reserved under the ledger persona, so
    a personal delegate whose subject is still the root stays serving (fleet
    sync must not stop) but reports remint_required; one naming the persona
    does not."""
    from tools.network import fleet_runtime

    root, persona = KeyPair.generate(), KeyPair.generate().public_hex
    org_uuid = fleet_runtime.personal_org_uuid(root.public_hex)
    now = int(time.time())
    settings_ops.add_setting(
        NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION, "auto.network",
        {"org_uuid": org_uuid, "root_pub": root.public_hex,
         "registry_url": "https://auto.network", "recovery_policy": {"mode": "none"},
         "binding_expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 86400))},
        org="personal",
    )
    subject = root.public_hex if names == "root" else persona
    delegate, cert, viewer = _root_signed_personal_credential(root, org_uuid, subject)
    vault_kit.store_key(org_uuid, delegate.private_hex)
    vault_kit.store_row(org_uuid, cert=cert.to_json().decode("ascii"),
                        viewer_cert=viewer.to_json().decode("ascii"),
                        child_pub=delegate.public_hex, not_after=cert.not_after,
                        root_pub=root.public_hex)
    vault_kit.cold()
    monkeypatch.setattr(lss, "personal_persona_pub", lambda: persona)

    state = lss.serve_cert_state(None)
    assert state["status"] == "ok", state
    if names == "root":
        assert state["remint_required"] is True
        assert "personal persona" in state["remint_reason"]
    else:
        assert "remint_required" not in state

    # Unknown persona (no ledger genesis yet): nothing to re-mint for.
    monkeypatch.setattr(lss, "personal_persona_pub", lambda: None)
    assert "remint_required" not in lss.serve_cert_state(None)


def test_personal_persona_lookup_never_creates_the_database_and_caches_the_genesis(env, monkeypatch, tmp_path):
    """auto-8sdrr review: the genesis is immutable, read once, never folded;
    and a fresh identity with no personal.db must not gain an empty one."""
    import tools.network.ledger as ledger

    empty = tmp_path / "no-identity-yet"
    empty.mkdir()
    path = empty / "personal.db"
    monkeypatch.setattr(ledger, "org_ledger_db_path", lambda slug, root=None: path)
    assert not path.exists()
    lss._personal_genesis_cache.clear()
    assert lss._personal_genesis_id() is None
    assert not path.exists(), "the lookup must not create personal.db"

    lss._personal_genesis_cache[str(path)] = "ab" * 32
    calls = []
    monkeypatch.setattr(lss, "_log", lss._log)
    from tools.graph import org_ops
    monkeypatch.setattr(org_ops, "persona_pub_for_org",
                        lambda genesis: calls.append(genesis) or "cd" * 32)
    assert lss.personal_persona_pub() == "cd" * 32
    assert calls == ["ab" * 32]          # served from the cache, no ledger open
    lss._personal_genesis_cache.clear()
