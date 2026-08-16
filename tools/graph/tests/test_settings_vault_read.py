"""Vaulted Settings resolution through the local and real HTTP boundaries.

The test holder is intentionally only an accessor. It supplies held secrets
plus a real persisted ``KeyControlStore`` and ``ContentStore``; production's
ramfs cache can fill the same seam later. ``settings_ops.read_set`` remains
the only decrypting implementation and calls ``storagekit.objects.read_object``.

View-state map (the headless §21 contract):

* enter ``graph set read autonomy.test.vault-read early --org autonomy``;
  display the plaintext JSON payload, with no locator or encryption marker;
* send ``GET /api/graph/settings/autonomy.test.vault-read`` with
  ``X-Graph-Org: autonomy``; display the same resolved member payloads;
* when one object cannot open, display that member with ``payload: null`` and
  a named ``error`` while every resolvable sibling remains in ``members``.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route

from tools.graph import ops, schemas, settings_ops
from tools.graph.client import HttpClient
from tools.graph.db import GraphDB
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS
from tools.network.idkit.canonical import canonical_json
from tools.network.storagekit.keycontrol import KeyControlStore
from tools.network.storagekit.store import ContentStore
from tools.network.storagekit.tests.conftest import World
from tools.vault import policy_class as policy_class_mod
from tools.vault.factors import open_password_seed
from tools.vault.storage_object import Holdings, parse_locator, seal_revision
from tools.vault.testkit import make_test_identity

AUDITED_SET = "autonomy.test.vault-read"
SECURED_SET = "autonomy.test.vault-read-secured"
PLAIN_SET = "autonomy.test.vault-read-plain"
SECRET = "sk-read-set-plaintext"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def _live_settings_server():
    from tools.dashboard import server as dashboard_server

    app = Starlette(routes=[
        Route(
            "/api/graph/settings/{set_id}",
            dashboard_server.api_graph_settings_list,
            methods=["GET"],
        ),
    ])
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and thread.is_alive() and time.time() < deadline:
        time.sleep(0.02)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("autonomy").close()
    yield orgs / "autonomy.db"
    GraphDB.close_all_pooled()


@pytest.fixture(autouse=True)
def _isolated_process_seams():
    schemas_snapshot = dict(SCHEMAS)
    upconverters_snapshot = dict(UPCONVERTERS)
    settings_ops.set_vault_sealer(None)
    settings_ops.set_vault_key_holder(None)
    try:
        yield
    finally:
        settings_ops.set_vault_sealer(None)
        settings_ops.set_vault_key_holder(None)
        SCHEMAS.clear()
        SCHEMAS.update(schemas_snapshot)
        UPCONVERTERS.clear()
        UPCONVERTERS.update(upconverters_snapshot)


@pytest.fixture
def vault_schemas():
    @schemas.vaulted("audited")
    class AuditedV1(schemas.SettingSchema):
        set_id = AUDITED_SET
        schema_revision = 1

    @schemas.vaulted("secured")
    class SecuredV1(schemas.SettingSchema):
        set_id = SECURED_SET
        schema_revision = 1

    class PlainV1(schemas.SettingSchema):
        set_id = PLAIN_SET
        schema_revision = 1

    schemas.register_schema(AUDITED_SET, 1, AuditedV1)
    schemas.register_schema(SECURED_SET, 1, SecuredV1)
    schemas.register_schema(PLAIN_SET, 1, PlainV1)


class PersistedVault:
    """Real object/key-control stores behind a replaceable holder accessor."""

    def __init__(self, root):
        self.world = World(member_count=3)
        self.author = self.world.member(0)
        self.initial, self.initial_secret = self.world.mint_initial_state(self.author)
        self.content = ContentStore(root / "content")
        self.content_reader = _ReopeningContentStore(root / "content")
        self.key_control = KeyControlStore(root / "key-control.db")
        self.key_control.accept_state(
            self.initial, self.world.fold, self.world.ancestry,
        )
        self.locators: dict[str, str] = {}

        identity = make_test_identity()
        self.policy_class = policy_class_mod.create_class(
            "password", [identity.published], created_at="2026-08-16T00:00:00Z",
        )
        self.policy_seeds = {
            identity.factor_id: open_password_seed(identity.armor, identity.password),
        }
        self._contexts: dict[str, settings_ops.VaultReadContext] = {}

    def _writer_holdings(self) -> Holdings:
        return Holdings(
            secrets=self.world.held(self.author),
            descriptors=self.world.stores.kc.states,
            bridges=self.world.stores.kc.bridges,
        )

    def sealer(self, *, set_id, key, setting_id, payload, tier, **_):
        kwargs = {}
        if tier == "secured":
            kwargs.update(
                policy_class=self.policy_class,
                opener_seeds=self.policy_seeds,
            )
        sealed = seal_revision(
            author=self.author,
            frontier=self.world.fold(),
            set_id=set_id,
            key=key,
            setting_id=setting_id,
            payload=payload,
            holdings=self._writer_holdings(),
            ancestry=self.world.ancestry,
            content_store=self.content,
            tier=tier,
            **kwargs,
        )
        self.locators[key] = sealed.locator
        return sealed.locator

    def context(self, secrets, *, key_control=None) -> settings_ops.VaultReadContext:
        store = key_control or self.key_control
        return settings_ops.VaultReadContext(
            holdings=Holdings(
                secrets=dict(secrets),
                descriptors=store.states,
                bridges=list(store.accepted_bridges()),
            ),
            content_store=self.content_reader,
        )

    def set_default_context(self, secrets) -> None:
        self._contexts["*"] = self.context(secrets)

    def set_context(self, key, context) -> None:
        self._contexts[key] = context

    def holder(self, *, key, **_) -> settings_ops.VaultReadContext:
        return self._contexts.get(key, self._contexts["*"])

    def advance(self, root):
        leaving = self.world.member(1)
        self.world.grant(self.author, leaving, self.initial)
        self.world.remove(leaving)
        self.current, self.current_secret = self.world.advance(
            self.author, self.initial,
        )
        bridges = tuple(self.world.stores.kc.bridges)
        self.key_control.accept_state(
            self.current,
            self.world.fold,
            self.world.ancestry,
            bridges=bridges,
            parent_descriptors={self.initial.state_id: self.initial},
        )

        incomplete = KeyControlStore(root / "key-control-no-bridge.db")
        incomplete.accept_state(self.initial, self.world.fold, self.world.ancestry)
        incomplete.accept_state(
            self.current,
            self.world.fold,
            self.world.ancestry,
            bridges=(),
            parent_descriptors={self.initial.state_id: self.initial},
        )
        self.incomplete_key_control = incomplete
        self.set_default_context({self.current.state_id: self.current_secret})

    def close(self):
        incomplete = getattr(self, "incomplete_key_control", None)
        if incomplete is not None:
            incomplete.close()
        self.key_control.close()
        self.content.close()


class _ReopeningContentStore:
    """Thread-safe accessor over the same real persisted ContentStore."""

    def __init__(self, root):
        self.root = root

    def get_object(self, object_id, revision_id):
        with ContentStore(self.root) as store:
            return store.get_object(object_id, revision_id)


@pytest.fixture
def vault(tmp_path, graph_db_env, vault_schemas):
    live = PersistedVault(tmp_path / "vault")
    settings_ops.set_vault_sealer(live.sealer)
    try:
        yield live
    finally:
        live.close()


def _add(set_id: str, key: str, payload: dict) -> str:
    return ops.add_setting(set_id, 1, key, payload, org="autonomy")


def _failure(member) -> settings_ops.SettingReadError:
    assert isinstance(member.payload, settings_ops.SettingReadError)
    return member.payload


def test_local_http_and_graph_set_read_are_identical_for_an_earlier_generation(
    vault, tmp_path
):
    _add(AUDITED_SET, "early", {"access_token": SECRET, "source": "earlier"})
    vault.advance(tmp_path / "vault")
    settings_ops.set_vault_key_holder(vault.holder)

    local = ops.read_set(AUDITED_SET, org="autonomy", peers=[]).to_dict()["early"]
    assert local.payload == {"access_token": SECRET, "source": "earlier"}
    assert "autonomy.vault.v1" not in json.dumps(local.to_dict())

    with _live_settings_server() as url:
        remote = HttpClient(url).read_set(
            AUDITED_SET, org="autonomy", peers=[],
        ).to_dict()["early"]
        assert remote.payload == local.payload
        assert remote.to_dict() == local.to_dict()

        graph = shutil.which("graph")
        assert graph is not None
        env = {**os.environ, "GRAPH_API": url, "GRAPH_ORG": "autonomy"}
        result = subprocess.run(
            [graph, "set", "read", AUDITED_SET, "early", "--org", "autonomy"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == local.payload


def test_missing_holder_is_a_named_member_error(vault):
    _add(AUDITED_SET, "cold", {"access_token": SECRET})
    settings_ops.set_vault_key_holder(None)
    member = ops.read_set(AUDITED_SET, org="autonomy", peers=[]).members[0]
    failure = _failure(member)
    assert failure.code == "vault_key_holder_missing"
    assert failure.error == "VaultKeyHolderMissing"
    assert SECRET not in json.dumps(member.to_dict())
    assert member.to_dict()["payload"] is None


def test_four_open_failures_are_distinct_and_do_not_hide_other_members(vault, tmp_path):
    _add(AUDITED_SET, "missing-bridge", {"value": "old"})
    vault.advance(tmp_path / "vault")
    _add(AUDITED_SET, "good", {"value": "visible"})
    _add(AUDITED_SET, "no-key", {"value": "unavailable"})
    _add(AUDITED_SET, "tampered", {"value": "must-not-leak"})
    _add(AUDITED_SET, "unknown-suite", {"value": "must-not-leak-either"})

    current = {vault.current.state_id: vault.current_secret}
    vault.set_context("no-key", vault.context({}))
    vault.set_context(
        "missing-bridge",
        vault.context(current, key_control=vault.incomplete_key_control),
    )

    tampered = parse_locator(vault.locators["tampered"])
    tampered_header, tampered_body = vault.content.get_object(
        tampered["object_id"], tampered["revision_id"],
    )
    tampered_path = vault.content._blob_path(tampered_header.ciphertext_hash)
    tampered_path.write_bytes(bytes([tampered_body[0] ^ 1]) + tampered_body[1:])

    unknown = parse_locator(vault.locators["unknown-suite"])
    header, _ = vault.content.get_object(unknown["object_id"], unknown["revision_id"])
    header_wire = json.loads(header.to_json())
    header_wire["body_suite_id"] = "unknown-body-suite"
    with vault.content._db:
        vault.content._db.execute(
            "UPDATE content_objects SET header_json=? WHERE object_id=? AND revision_id=?",
            (canonical_json(header_wire), unknown["object_id"], unknown["revision_id"]),
        )

    settings_ops.set_vault_key_holder(vault.holder)
    members = ops.read_set(AUDITED_SET, org="autonomy", peers=[]).to_dict()

    assert members["good"].payload == {"value": "visible"}
    assert _failure(members["no-key"]).code == "vault_key_unavailable"
    assert _failure(members["missing-bridge"]).code == "vault_bridge_missing"
    assert _failure(members["tampered"]).code == "vault_decryption_failed"
    assert _failure(members["unknown-suite"]).code == "vault_unknown_suite"
    assert set(members) == {
        "good", "missing-bridge", "no-key", "tampered", "unknown-suite",
    }
    wire = ops.read_set(AUDITED_SET, org="autonomy", peers=[]).as_payload()
    assert "autonomy.vault.v1" not in json.dumps(wire)
    assert "must-not-leak" not in json.dumps(wire)

    # The member-level error variant survives the actual dashboard/HttpClient
    # boundary as the same Python type and the same stable error code.
    with _live_settings_server() as url:
        remote = HttpClient(url).read_set(
            AUDITED_SET, org="autonomy", peers=[],
        ).to_dict()
    assert remote["good"].payload == members["good"].payload
    for key in ("missing-bridge", "no-key", "tampered", "unknown-suite"):
        assert isinstance(remote[key].payload, settings_ops.SettingReadError)
        assert remote[key].payload == members[key].payload


def test_secured_member_stops_at_the_still_sealed_content_key(vault):
    _add(SECURED_SET, "secured", {"access_token": SECRET})
    vault.set_default_context({vault.initial.state_id: vault.initial_secret})
    settings_ops.set_vault_key_holder(vault.holder)

    payload = ops.read_set(SECURED_SET, org="autonomy", peers=[]).members[0].payload
    assert set(payload) == {"v", "sealed_cek", "body_suite_id", "nonce", "ciphertext"}
    assert SECRET not in json.dumps(payload)


def test_non_vault_member_is_byte_identical_and_never_calls_the_holder(vault):
    _add(PLAIN_SET, "plain", {"enabled": True, "nested": {"n": 1}})

    def forbidden(**_):
        raise AssertionError("ordinary setting reached the vault holder")

    settings_ops.set_vault_key_holder(forbidden)
    member = ops.read_set(PLAIN_SET, org="autonomy", peers=[]).members[0]
    assert member.payload == {"enabled": True, "nested": {"n": 1}}
    encoded = json.dumps(member.to_dict(), sort_keys=True, separators=(",", ":"))
    assert '"error"' not in encoded
    assert '"payload":{"enabled":true,"nested":{"n":1}}' in encoded


def test_settings_ops_contains_no_parallel_key_derivation():
    with open(settings_ops.__file__, encoding="utf-8") as source:
        text = source.read()
    for forbidden in ("WRAP_INFO_LABEL", "EDGE_INFO_LABEL", "HKDFExpand"):
        assert forbidden not in text
    assert "storage_objects.read_object(" in text
