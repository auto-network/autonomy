"""CLI integration for ``graph link publish|revoke|list`` (C3 acceptance).

The CLI's single HTTP seam (``link_cmd._api_request``) is routed into a real
approvals app (TestClient) whose test fixture plays the operator: as soon as
the CLI posts an approval, the fixture fetches the enrichment, click-signs
the staged registry request with a session key (what the C2 browser signer
will do), and posts the decision. The registry is the real B1 app over
ASGITransport, so the URL the CLI prints comes from an actually-issued
grant. Covers: publish → URL printed; decline → clean message + exit 1;
revoke → grant gone from ``graph link list``.
"""

from __future__ import annotations

import argparse
import io
import time
import urllib.error

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import approvals_routes, link_approvals
from tools.dashboard.dao import approval_requests as ar
from tools.graph import link_cmd, settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_BINDING_SET_ID,
    NETWORK_BINDING_REVISION,
)
from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.ledger import LedgerStore, org_ledger_db_path
from tools.network.ledger.found import found_org_ledger
from tools.network.registry.app import create_app as create_registry_app
from tools.network.registry.signing import sign_request

ORG = "netorg"
ORG_UUID = "22222222-2222-4222-8222-222222222222"
TARGET = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
REGISTRY_URL = "http://registry.test"
PUBLIC_LINK_URL = "https://relay.auto.network"
NOTE_TARGET = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class OperatorFixture:
    """Auto-resolves approvals the way the operator's browser would."""

    def __init__(
        self, client, session_key, session_cert, persona_pub, approve=True,
    ):
        self.client = client
        self.session_key = session_key
        self.session_cert = session_cert
        self.persona_pub = persona_pub
        self.approve = approve

    def decide(self, rid: str) -> None:
        if not self.approve:
            self.client.post(f"/api/approvals/{rid}/decision",
                             json={"approved": False})
            return
        enriched = self.client.get(f"/api/approvals/{rid}").json()
        rr = enriched["registry_request"]
        envelope = sign_request(
            self.session_key, rr["method"], rr["path"], rr["payload"],
            ts=int(time.time()), cert=self.session_cert,
        )
        self.client.post(f"/api/approvals/{rid}/decision",
                         json={"approved": True, "envelope": envelope})


@pytest.fixture
def operator_env(tmp_path, monkeypatch):
    """Approvals app + registry + settings + CLI transport, fully wired."""
    from tools.graph.db import GraphDB

    root = KeyPair.generate()
    personal_root = KeyPair.generate()
    orgs_dir = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    GraphDB.create_org_db(
        ORG, root=orgs_dir, org_id=ORG_UUID,
    ).close()
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        founded = found_org_ledger(
            store,
            org_id=ORG_UUID,
            org_root=root,
            personal_root_seed=bytes.fromhex(personal_root.private_hex),
            now=int(time.time() * 1000),
        )

    session_key = KeyPair.generate()
    now = int(time.time())
    session_cert = issue_cert(
        root, session_key.public_hex,
        scope=("link:publish", "link:revoke", "viewer:identify"),
        org=ORG_UUID,
        subject=Subject("operator", founded.founder_persona_pub),
        not_before=now - 3600, not_after=now + 30 * 86400,
    )

    registry_app = create_registry_app(":memory:", base_url=PUBLIC_LINK_URL,
                                       secure_cookies=False)
    rc = TestClient(registry_app)
    envelope = sign_request(
        root, "POST", "/v1/orgs",
        {"org_uuid": ORG_UUID, "root_pub": root.public_hex,
         "recovery_policy": "none"},
        ts=now,
    )
    assert rc.post("/v1/orgs", json=envelope).status_code == 201

    monkeypatch.setattr(ar, "DB_PATH", tmp_path / "approvals.db")
    GraphDB.close_all_pooled()
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "graph.db"))
    settings_ops.add_setting(
        NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION, "registry.test",
        {
            "org_uuid": ORG_UUID,
            "root_pub": root.public_hex,
            "registry_url": REGISTRY_URL,
            "recovery_policy": {"mode": "none"},
            "binding_expires_at": "2030-01-01T00:00:00Z",
        },
        org=ORG,
    )
    monkeypatch.setattr(
        link_approvals, "_registry_client",
        lambda base_url: httpx.AsyncClient(
            transport=httpx.ASGITransport(app=registry_app), base_url=base_url),
    )
    # `graph link list` reads Settings host-direct against the tmp GRAPH_DB.
    from tools.graph import client as graph_client
    monkeypatch.setattr(graph_client, "_FORCE_HOST_DIRECT", True)

    with TestClient(Starlette(routes=approvals_routes.ROUTES)) as client:
        operator = OperatorFixture(
            client,
            session_key,
            session_cert,
            founded.founder_persona_pub,
        )

        def fake_api(method, path, *, body=None, timeout=None):
            resp = client.request(method, path, json=body)
            if resp.status_code >= 400:
                raise urllib.error.HTTPError(
                    path, resp.status_code, "error", {}, io.BytesIO(resp.content))
            data = resp.json() if resp.content else {}
            if method == "POST" and path == "/api/approvals":
                operator.decide(data["id"])   # the operator acts immediately
            return data

        monkeypatch.setattr(link_cmd, "_api_request", fake_api)
        yield operator
    GraphDB.close_all_pooled()


def _publish_args(**over):
    base = dict(target=TARGET, target_type="file", ttl="1h",
                label="binder", org=ORG)
    base.update(over)
    return argparse.Namespace(**base)


def test_uuid_prefix_expands_note(monkeypatch):
    calls = []

    def fake_api(method, path, **kwargs):
        calls.append((method, path))
        return {"id": NOTE_TARGET, "type": "note"}

    monkeypatch.setattr(link_cmd, "_api_request", fake_api)
    assert link_cmd._resolve_uuid_target(NOTE_TARGET[:12], "note") == NOTE_TARGET
    assert calls == [("GET", "/api/graph/source/" + NOTE_TARGET[:12])]


def test_uuid_prefix_expands_file(monkeypatch):
    calls = []

    def fake_api(method, path, **kwargs):
        calls.append((method, path))
        return {"id": NOTE_TARGET, "type": "attachment"}

    monkeypatch.setattr(link_cmd, "_api_request", fake_api)
    assert link_cmd._resolve_uuid_target(NOTE_TARGET[:12], "file") == NOTE_TARGET
    assert calls == [("GET", "/api/graph/" + NOTE_TARGET[:12])]


def test_full_uuid_does_not_require_graph_lookup(monkeypatch):
    monkeypatch.setattr(link_cmd, "_api_request", lambda *a, **k: pytest.fail("unexpected lookup"))
    assert link_cmd._resolve_uuid_target(NOTE_TARGET, "note") == NOTE_TARGET


MISSION_TARGET = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


def test_resolve_mission_target_confirms_existence(monkeypatch, capsys):
    calls = []

    def fake_api(method, path, **kwargs):
        calls.append((method, path))
        return {"mission": {"mission_id": MISSION_TARGET, "name": "OSS Insights"}}

    monkeypatch.setattr(link_cmd, "_api_request", fake_api)
    assert link_cmd._resolve_mission_target(MISSION_TARGET) == MISSION_TARGET
    assert calls == [("GET", "/api/missions/" + MISSION_TARGET)]
    assert "OSS Insights" in capsys.readouterr().out


def test_resolve_mission_target_rejects_non_uuid(monkeypatch):
    monkeypatch.setattr(link_cmd, "_api_request", lambda *a, **k: pytest.fail("unexpected lookup"))
    with pytest.raises(SystemExit):
        link_cmd._resolve_mission_target("not-a-uuid-at-all")


def test_resolve_mission_target_fails_clean_on_404(monkeypatch):
    def fake_api(method, path, **kwargs):
        raise urllib.error.HTTPError(path, 404, "not found", {}, io.BytesIO(b""))

    monkeypatch.setattr(link_cmd, "_api_request", fake_api)
    with pytest.raises(SystemExit):
        link_cmd._resolve_mission_target(MISSION_TARGET)


PARTICIPANT_ID = "guest:eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"


def test_resolve_mission_participant_confirms_existence(monkeypatch, capsys):
    calls = []

    def fake_api(method, path, **kwargs):
        calls.append((method, path))
        return {"visitor": {"participant_id": PARTICIPANT_ID, "display_name": "Priya (data partner)"}}

    monkeypatch.setattr(link_cmd, "_api_request", fake_api)
    assert link_cmd._resolve_mission_participant(PARTICIPANT_ID) == PARTICIPANT_ID
    assert calls == [("GET", "/api/visitor-tokens/" + PARTICIPANT_ID)]
    assert "Priya" in capsys.readouterr().out


def test_resolve_mission_participant_fails_clean_on_404(monkeypatch):
    def fake_api(method, path, **kwargs):
        raise urllib.error.HTTPError(path, 404, "not found", {}, io.BytesIO(b""))

    monkeypatch.setattr(link_cmd, "_api_request", fake_api)
    with pytest.raises(SystemExit):
        link_cmd._resolve_mission_participant(PARTICIPANT_ID)


def test_publish_mission_requires_participant_flag(monkeypatch):
    monkeypatch.setattr(link_cmd, "_resolve_mission_target", lambda t: MISSION_TARGET)
    monkeypatch.setattr(
        link_cmd, "_resolve_mission_participant",
        lambda p: pytest.fail("should never reach participant resolution -- no --participant given"),
    )
    with pytest.raises(SystemExit):
        link_cmd.cmd_link_publish(_publish_args(target=MISSION_TARGET, target_type="mission"))


def test_publish_non_mission_rejects_participant_flag(monkeypatch):
    monkeypatch.setattr(link_cmd, "_resolve_uuid_target", lambda t, ty: t)
    with pytest.raises(SystemExit):
        link_cmd.cmd_link_publish(_publish_args(
            target=NOTE_TARGET, target_type="note", participant=PARTICIPANT_ID,
        ))


def test_publish_mission_binds_resolved_participant_into_meta(monkeypatch):
    monkeypatch.setattr(link_cmd, "_resolve_mission_target", lambda t: MISSION_TARGET)
    monkeypatch.setattr(link_cmd, "_resolve_mission_participant", lambda p: p)
    captured = {}

    def fake_post_approval(kind, request):
        captured.update(request)
        return "approval-id"

    monkeypatch.setattr(link_cmd, "_post_approval", fake_post_approval)
    monkeypatch.setattr(
        link_cmd, "_await_decision",
        lambda approval_id, verb: {"url": "https://relay.auto.network/l/deadbeef"},
    )
    link_cmd.cmd_link_publish(_publish_args(
        target=MISSION_TARGET, target_type="mission", participant=PARTICIPANT_ID,
    ))
    assert captured["meta"]["participant_id"] == PARTICIPANT_ID


def test_mission_target_type_does_not_fall_through_to_uuid_resolver(monkeypatch):
    """Regression: mission must not silently hit _resolve_uuid_target's
    note/file-shaped type check, which knows nothing about mission_id."""
    monkeypatch.setattr(
        link_cmd, "_resolve_mission_target",
        lambda target_id: MISSION_TARGET,
    )
    monkeypatch.setattr(
        link_cmd, "_resolve_uuid_target",
        lambda *a, **k: pytest.fail("mission target hit the uuid resolver, not its own"),
    )
    monkeypatch.setattr(
        link_cmd, "_post_approval", lambda kind, request: "approval-id",
    )
    # cmd_link_publish always calls _await_decision right after
    # _post_approval -- a real, network-polling function (up to 55s per
    # attempt). This test only cares about the target-resolution dispatch
    # above it, so mock this too rather than let it hang against nothing.
    monkeypatch.setattr(
        link_cmd, "_await_decision",
        lambda approval_id, verb: {"url": "https://relay.auto.network/l/deadbeef"},
    )
    # A mission grant now requires a resolved participant (auto-tp1v9) --
    # mocked here since this test is about the target-resolution dispatch
    # above it, not participant resolution (covered separately below).
    monkeypatch.setattr(
        link_cmd, "_resolve_mission_participant",
        lambda participant_id: participant_id,
    )
    link_cmd.cmd_link_publish(_publish_args(
        target=MISSION_TARGET, target_type="mission", participant="guest:test-1",
    ))


def test_publish_prints_url(operator_env, capsys):
    link_cmd.cmd_link_publish(_publish_args())
    out = capsys.readouterr().out
    assert "✓ share-link published: " + PUBLIC_LINK_URL + "/l/" in out
    assert "token: " in out


def test_publish_then_list_then_revoke(operator_env, capsys):
    link_cmd.cmd_link_publish(_publish_args())
    out = capsys.readouterr().out
    token = out.split("token: ")[1].strip().split()[0]

    link_cmd.cmd_link_list(argparse.Namespace(org=ORG))
    listed = capsys.readouterr().out
    assert token in listed and "binder" in listed and "ttl=3600s" in listed
    assert f"operator:{operator_env.persona_pub}" in listed  # I6 visible

    link_cmd.cmd_link_revoke(argparse.Namespace(target=token, org=ORG))
    assert "✓ share-link revoked" in capsys.readouterr().out

    link_cmd.cmd_link_list(argparse.Namespace(org=ORG))
    assert "no share-link grants cached" in capsys.readouterr().out


def test_decline_is_a_clean_message(operator_env, capsys):
    operator_env.approve = False
    with pytest.raises(SystemExit) as exc:
        link_cmd.cmd_link_publish(_publish_args())
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "operator declined" in err
    assert "Traceback" not in err


def test_unshown_present_deck_errors_with_guidance(operator_env, capsys):
    """Q5 trivial path: a deck that isn't in Design Studio can't be
    published; the CLI says so and points at graph ui-design."""
    with pytest.raises(SystemExit) as exc:
        link_cmd.cmd_link_publish(
            _publish_args(target="feedbeef", target_type="present"))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "not in Design Studio" in err
    assert "graph ui-design" in err


def test_bad_ttl_rejected_before_posting(operator_env, capsys):
    with pytest.raises(SystemExit):
        link_cmd.cmd_link_publish(_publish_args(ttl="soon"))
    assert "not a duration" in capsys.readouterr().err


def test_revoke_wants_a_real_token(operator_env, capsys):
    with pytest.raises(SystemExit):
        link_cmd.cmd_link_revoke(argparse.Namespace(target="oops", org=ORG))
    assert "not a grant token" in capsys.readouterr().err


def test_link_list_hides_peer_published_grant(tmp_path, monkeypatch, capsys):
    """`graph link list` shows only THIS org's own grants — a peer-published
    grant row must never appear (owning-scope read, P2). Uses real per-org
    DBs so the peer's canonical grant IS peer-visible; owning scope must
    still exclude it."""
    from tools.graph.db import GraphDB
    from tools.graph import client as graph_client
    from tools.graph.schemas.network_identity import NETWORK_LINK_GRANT_SET_ID

    GraphDB.close_all_pooled()
    orgs_dir = tmp_path / "orgs"
    GraphDB.create_org_db(ORG, root=orgs_dir).close()
    GraphDB.create_org_db("peerorg", root=orgs_dir).close()
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.setattr(graph_client, "_FORCE_HOST_DIRECT", True)

    peer_token = "d" * 32
    settings_ops.add_setting(
        NETWORK_LINK_GRANT_SET_ID, 1, peer_token,
        {"token": peer_token, "target_uuid": TARGET, "target_type": "note",
         "meta": {}, "subject": {"kind": "operator", "id": "peer"},
         "issued_at": "2026-01-01T00:00:00Z"},
        org="peerorg", state="canonical",
    )

    # ORG owns no grants; a composed read would surface peerorg's canonical
    # grant, owning-scope must not.
    link_cmd.cmd_link_list(argparse.Namespace(org=ORG))
    out = capsys.readouterr().out
    assert peer_token not in out
    assert "no share-link grants cached" in out
    GraphDB.close_all_pooled()
