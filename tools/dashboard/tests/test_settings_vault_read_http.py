"""One vault setting, resolved twice: in process, and over real HTTP.

The choice between an in-process and an HTTP client is made in one place, and
both reach the same ``read_set`` — which executes inside the dashboard process
either way, because that is where the generation keys are. So the two must not
merely both work; they must agree, member for member and field for field.

This drives a real uvicorn server over a real socket with the real
``HttpClient`` — not a TestClient and not a stubbed transport — because what
is under test is precisely whether the result survives serialization.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn

from tools.graph import ops, schemas, settings_ops
from tools.graph.client import HttpClient
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS
from tools.graph.tests.vault_read_harness import VaultWorld, clear_seams
from tools.vault.storage_object import is_vault_locator, parse_locator

ORG = "autonomy"
SECRET = "sk-live-must-never-cross-the-wire"
VAULT_SET = "autonomy.test.vault-http"
PLAIN_SET = "autonomy.test.vault-http-plain"


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB

    orgs = tmp_path / "orgs"
    orgs.mkdir(exist_ok=True)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db(ORG).close()
    yield orgs / f"{ORG}.db"


@pytest.fixture(autouse=True)
def _isolate_schema_registry():
    schemas_snap = dict(SCHEMAS)
    upcon_snap = dict(UPCONVERTERS)
    try:
        yield
    finally:
        SCHEMAS.clear()
        SCHEMAS.update(schemas_snap)
        UPCONVERTERS.clear()
        UPCONVERTERS.update(upcon_snap)


@pytest.fixture(autouse=True)
def _clear_seams():
    clear_seams()
    try:
        yield
    finally:
        clear_seams()


@pytest.fixture
def vault_schema():
    @schemas.vaulted("audited")
    class VaultedV1(schemas.SettingSchema):
        set_id = VAULT_SET
        schema_revision = 1

    schemas.register_schema(VAULT_SET, 1, VaultedV1)
    return VaultedV1


@pytest.fixture
def plain_schema():
    class PlainV1(schemas.SettingSchema):
        set_id = PLAIN_SET
        schema_revision = 1

    schemas.register_schema(PLAIN_SET, 1, PlainV1)
    return PlainV1


@pytest.fixture
def vault(tmp_path):
    live = VaultWorld(tmp_path / "vault").register()
    try:
        yield live
    finally:
        live.close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def _live_server(app):
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and thread.is_alive() and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "the dashboard did not come up"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=20)


@pytest.fixture
def remote(test_app):
    """A real ``HttpClient`` against a real server — the client the CLI gets
    when ``GRAPH_API`` is set, over a socket, with nothing stubbed."""
    with _live_server(test_app) as base_url:
        yield HttpClient(base_url)


# ── the two clients agree ─────────────────────────────────────────────────


def test_a_vault_secret_resolves_identically_in_process_and_over_http(
    graph_db_env, vault_schema, vault, remote
):
    payload = {"access_token": SECRET, "refresh_token": "rt-also-secret"}
    setting_id = ops.add_setting(VAULT_SET, 1, "default", payload, org=ORG)
    assert is_vault_locator(
        json.loads(_row_payload(graph_db_env, setting_id))
    ), "the row holds a locator, so the plaintext below came from step six"

    local = settings_ops.read_set(VAULT_SET, org=ORG).to_dict()["default"]
    over_http = remote.read_set(VAULT_SET, org=ORG).to_dict()["default"]

    assert local.payload == payload
    assert over_http.payload == payload
    assert over_http.to_dict() == local.to_dict()
    # And nothing in what came back says it was ever encrypted.
    assert "vault_error" not in over_http.to_dict()
    assert "sealed_content_key" not in over_http.to_dict()


def test_the_wire_carries_the_value_and_never_the_locator(
    graph_db_env, vault_schema, vault, remote
):
    """The response body itself, before any client-side reconstruction."""
    ops.add_setting(VAULT_SET, 1, "default", {"access_token": SECRET}, org=ORG)
    body = remote._request(
        "GET", f"/api/graph/settings/{VAULT_SET}", headers={"X-Graph-Org": ORG},
    )
    member = body["members"][0]
    assert member["payload"] == {"access_token": SECRET}
    assert not is_vault_locator(member["payload"])
    assert "autonomy.vault.v1." not in json.dumps(body)


def test_a_refusal_reaches_the_http_caller_as_the_same_named_error(
    graph_db_env, vault_schema, vault, remote
):
    """A refusal that did not survive the wire would reach a remote caller as
    a null payload with no reason — indistinguishable from a setting whose
    value is null."""
    ops.add_setting(VAULT_SET, 1, "default", {"access_token": SECRET}, org=ORG)
    settings_ops.set_vault_key_holder(None)

    local = settings_ops.read_set(VAULT_SET, org=ORG).to_dict()["default"]
    over_http = remote.read_set(VAULT_SET, org=ORG).to_dict()["default"]

    assert local.vault_error.reason == settings_ops.VAULT_NO_KEY_HOLDER
    assert over_http.vault_error == local.vault_error
    assert over_http.payload is None
    assert SECRET not in json.dumps(over_http.to_dict())


def test_one_unopenable_secret_still_answers_for_the_rest_over_http(
    graph_db_env, vault_schema, vault, remote
):
    for key in ("alpha", "beta", "gamma"):
        ops.add_setting(VAULT_SET, 1, key, {"t": key}, org=ORG)
    beta = settings_ops.read_set(VAULT_SET, org=ORG).to_dict()["beta"]
    vault.tamper(
        parse_locator(json.loads(_row_payload(graph_db_env, beta.id)))["object_id"],
        "decryption_failed",
    )

    resolved = remote.read_set(VAULT_SET, org=ORG).to_dict()
    assert sorted(resolved) == ["alpha", "beta", "gamma"]
    assert resolved["alpha"].payload == {"t": "alpha"}
    assert resolved["gamma"].payload == {"t": "gamma"}
    assert resolved["beta"].vault_error.reason == settings_ops.VAULT_DECRYPTION_FAILED


def test_an_ordinary_set_crosses_the_wire_exactly_as_it_did_before(
    graph_db_env, plain_schema, vault, remote
):
    payload = {"a": 1, "nested": {"b": [1, 2, 3]}}
    ops.add_setting(PLAIN_SET, 1, "default", payload, org=ORG)

    body = remote._request(
        "GET", f"/api/graph/settings/{PLAIN_SET}", headers={"X-Graph-Org": ORG},
    )
    assert list(body["members"][0]) == [
        "id", "set_id", "stored_revision", "key", "payload", "state",
        "supersedes", "excludes", "deprecated", "successor_id", "created_at",
        "updated_at", "target_revision", "org", "upconverted",
    ]
    assert body["members"][0]["payload"] == payload

    local = settings_ops.read_set(PLAIN_SET, org=ORG).to_dict()["default"]
    over_http = remote.read_set(PLAIN_SET, org=ORG).to_dict()["default"]
    assert over_http.to_dict() == local.to_dict()
    assert vault.holder_calls == []


# ── the named CLI request ─────────────────────────────────────────────────


class _Args:
    def __init__(self, **kw):
        self.id_parts = kw.pop("id_parts")
        self.org = kw.pop("org", None)
        self.chain = False


def _graph_set_read(set_id: str, key: str, base_url: str, monkeypatch, capsys):
    """``graph set read <set_id> <key>``, routed through the dashboard.

    The command with ``GRAPH_API`` set is the container's whole read path:
    ``get_client()`` returns the HTTP client and the resolution happens in the
    server process, which is where the keys are.
    """
    from tools.graph import set_cmd

    monkeypatch.setenv("GRAPH_API", base_url)
    set_cmd.cmd_set_read(_Args(id_parts=[set_id, key], org=ORG))
    return capsys.readouterr()


def test_graph_set_read_prints_the_secret_through_the_dashboard(
    graph_db_env, vault_schema, vault, test_app, monkeypatch, capsys
):
    payload = {"access_token": SECRET}
    ops.add_setting(VAULT_SET, 1, "default", payload, org=ORG)

    with _live_server(test_app) as base_url:
        out = _graph_set_read(VAULT_SET, "default", base_url, monkeypatch, capsys)
    assert json.loads(out.out) == payload


def test_graph_set_read_refuses_rather_than_printing_a_null_value(
    graph_db_env, vault_schema, vault, test_app, monkeypatch, capsys
):
    """A refusal printed as ``null`` would read as "this setting's value is
    null" — absence wearing the shape of an answer."""
    ops.add_setting(VAULT_SET, 1, "default", {"access_token": SECRET}, org=ORG)
    settings_ops.set_vault_key_holder(None)

    with _live_server(test_app) as base_url:
        with pytest.raises(SystemExit) as exit_info:
            _graph_set_read(VAULT_SET, "default", base_url, monkeypatch, capsys)
    out = capsys.readouterr()
    assert exit_info.value.code == 1
    assert out.out.strip() == ""
    assert settings_ops.VAULT_NO_KEY_HOLDER in out.err
    assert SECRET not in out.err


def _row_payload(db_path, setting_id: str) -> str:
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT payload FROM settings WHERE id = ?", (setting_id,)
        ).fetchone()
        assert row is not None, f"no settings row {setting_id}"
        return row[0]
    finally:
        conn.close()
