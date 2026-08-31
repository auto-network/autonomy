"""The profile tray's sync flag + certificate quiet-note, from the server side
(bead auto-sdrsa). Drives the real GET /api/identity/unlock-state through a
TestClient, with the serving-scope probes stubbed — so the flag logic (armed,
current, never-set-up) is exercised, not mocked away."""

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import identity_routes


@pytest.fixture
def client():
    with TestClient(Starlette(routes=identity_routes.ROUTES),
                    base_url="https://localhost:8080") as c:
        yield c


def _stub_serving(monkeypatch, *, scopes, cert_status, replies, disk="c0ffee",
                  designated=True, managed=True):
    """Stub the serving-scope reads get_unlock_state makes.

    scopes: list of org scopes (None == personal); cert_status: {scope: status};
    replies: {scope: connector-status dict} — a scope absent from replies has no
    reachable connector. designated: whether this machine is the fleet's
    designated tunnel server (a managed fleet with allowed=False means it is
    NOT, so holding no serving credential is expected, not a fault). managed:
    whether a fleet roster exists at all — False is a legacy single-node install
    with no other machines, where peer sync does not apply."""
    import types
    from tools.dashboard import link_serving_supervisor as sup
    from tools.network import build_version
    from tools.network import fleet_tunnel_server

    monkeypatch.setattr(
        fleet_tunnel_server, "state",
        lambda: types.SimpleNamespace(managed=bool(managed),
                                      allowed=bool(designated)),
    )
    monkeypatch.setattr(sup, "_discover_startup_orgs", lambda: list(scopes))
    monkeypatch.setattr(
        sup, "serve_cert_state",
        lambda org, **k: {"status": cert_status.get(org, "missing")},
    )

    def control(org, op, args, **k):
        if org in replies:
            return replies[org]
        raise sup.TunnelUnavailable("no connector")

    monkeypatch.setattr(sup, "control", control)
    monkeypatch.setattr(build_version, "disk_head", lambda: disk)
    # Keep the tunnel probe from touching a real supervisor singleton.
    monkeypatch.setattr(sup, "get_supervisor",
                        lambda: type("S", (), {"serving": lambda self: True})())


def test_sync_dim_when_every_connector_armed_and_current(client, monkeypatch):
    _stub_serving(
        monkeypatch,
        scopes=["anchore", "autonomy"],
        cert_status={"anchore": "ok", "autonomy": "ok"},
        replies={
            "anchore": {"fleet_runtime_configured": True, "process_commit": "c0ffee"},
            "autonomy": {"fleet_runtime_configured": True, "process_commit": "c0ffee"},
        },
    )
    sync = client.get("/api/identity/unlock-state").json()["sync"]
    assert sync["needs"] is False
    assert sync["value"] == "Serving"
    assert sync["scopes"] == []


def test_sync_lights_when_a_connector_is_unarmed(client, monkeypatch):
    _stub_serving(
        monkeypatch,
        scopes=["anchore", "autonomy", "dynbench"],
        cert_status={"anchore": "ok", "autonomy": "ok", "dynbench": "ok"},
        replies={
            "anchore": {"fleet_runtime_configured": False, "process_commit": "c0ffee",
                        "locked_refusals": 764, "locked_refusal_since": 1_700_000_000},
            "autonomy": {"fleet_runtime_configured": False, "process_commit": "c0ffee",
                         "locked_refusals": 382, "locked_refusal_since": 1_700_000_500},
            "dynbench": {"fleet_runtime_configured": True, "process_commit": "c0ffee"},
        },
    )
    sync = client.get("/api/identity/unlock-state").json()["sync"]
    assert sync["needs"] is True
    assert sync["value"] == "Locked"
    assert sync["unarmed"] == ["anchore", "autonomy"]
    assert sync["count"] == 764 + 382
    assert "can't sync" in sync["detail"]
    assert "1146 requests" in sync["detail"]


def test_sync_lights_when_a_connector_is_stale(client, monkeypatch):
    _stub_serving(
        monkeypatch,
        scopes=["anchore"],
        cert_status={"anchore": "ok"},
        replies={
            # armed, but running an older commit than what's on disk
            "anchore": {"fleet_runtime_configured": True, "process_commit": "0ld"},
        },
        disk="c0ffee",
    )
    sync = client.get("/api/identity/unlock-state").json()["sync"]
    assert sync["needs"] is True
    assert sync["value"] == "Stale"
    assert sync["stale"] == ["anchore"]
    assert "older code" in sync["detail"]


def test_unreachable_serving_connector_counts_as_unarmed(client, monkeypatch):
    _stub_serving(
        monkeypatch,
        scopes=["anchore"],
        cert_status={"anchore": "ok"},
        replies={},   # cert provisioned, but no connector answers
    )
    sync = client.get("/api/identity/unlock-state").json()["sync"]
    assert sync["needs"] is True
    assert "anchore" in sync["unarmed"]


def test_never_set_up_scope_is_a_quiet_note_not_a_lit_flag(client, monkeypatch):
    _stub_serving(
        monkeypatch,
        scopes=["anchore", "blindhash"],
        cert_status={"anchore": "ok", "blindhash": "missing"},
        replies={
            "anchore": {"fleet_runtime_configured": True, "process_commit": "c0ffee"},
        },
    )
    flags = client.get("/api/identity/unlock-state").json()
    # blindhash never lights either the sync or the certificate flag
    assert flags["sync"]["needs"] is False
    assert flags["certificates"]["needs"] is False
    assert "blindhash" not in flags["sync"]["scopes"]
    # ...but it is surfaced as a quiet note on the certificate balloon
    assert "blindhash" in flags["certificates"].get("note", "")


def test_non_designated_tunnel_server_does_not_light_sync(client, monkeypatch):
    # A machine that is not the fleet's designated tunnel server holds no serving
    # credential by design. Its unarmed connector must NOT be reported as a fault,
    # and no root-unlock remedy is offered — unlocking cannot arm a non-server.
    _stub_serving(
        monkeypatch,
        scopes=[None],
        cert_status={None: "ok"},
        replies={None: {"fleet_runtime_configured": False, "process_commit": "c0ffee"}},
        designated=False,
    )
    sync = client.get("/api/identity/unlock-state").json()["sync"]
    assert sync["needs"] is False
    assert sync["unarmed"] == []
    assert sync["scopes"] == []
    assert "serving credential" not in sync["detail"] or "expected" in sync["detail"]
    assert "unlock with your root" not in sync["detail"].lower()
    assert "expected" in sync["detail"]


def test_single_node_install_does_not_light_sync(client, monkeypatch):
    # A legacy single-node install has no fleet roster (managed=False) and thus
    # no OTHER machines. Even with every serving connector unarmed, peer sync is
    # moot: the flag must stay quiet and never offer the root-unlock remedy for a
    # credential no peer is waiting on. This is the operator-reported case — three
    # orgs read as "hold no serving credential" on a machine with no peers.
    _stub_serving(
        monkeypatch,
        scopes=["anchore", "autonomy", "dynbench"],
        cert_status={"anchore": "ok", "autonomy": "ok", "dynbench": "ok"},
        replies={
            "anchore": {"fleet_runtime_configured": False, "process_commit": "c0ffee"},
            "autonomy": {"fleet_runtime_configured": False, "process_commit": "c0ffee"},
            "dynbench": {"fleet_runtime_configured": False, "process_commit": "c0ffee"},
        },
        managed=False,
    )
    sync = client.get("/api/identity/unlock-state").json()["sync"]
    assert sync["needs"] is False
    assert sync["unarmed"] == []
    assert sync["scopes"] == []
    assert "hold no serving credential" not in sync["detail"]
    assert "unlock with your root" not in sync["detail"].lower()
    assert "only machine" in sync["detail"]


def test_scopeless_and_personal_scope_reported_once(client, monkeypatch):
    # The scopeless (org=None) and "personal" scopes resolve to the SAME database,
    # so an unarmed credential must not read "personal and personal ...".
    _stub_serving(
        monkeypatch,
        scopes=[None, "personal"],
        cert_status={None: "ok", "personal": "ok"},
        replies={
            None: {"fleet_runtime_configured": False, "process_commit": "c0ffee"},
            "personal": {"fleet_runtime_configured": False, "process_commit": "c0ffee"},
        },
    )
    sync = client.get("/api/identity/unlock-state").json()["sync"]
    assert sync["needs"] is True
    assert sync["unarmed"] == ["personal"]
    assert sync["scopes"] == ["personal"]
    assert "personal and personal" not in sync["detail"]
    assert "personal holds no serving credential" in sync["detail"]


def test_certificate_lights_for_a_lapsed_serving_scope(client, monkeypatch):
    _stub_serving(
        monkeypatch,
        scopes=["anchore"],
        cert_status={"anchore": "expired"},
        replies={
            "anchore": {"fleet_runtime_configured": True, "process_commit": "c0ffee"},
        },
    )
    cert = client.get("/api/identity/unlock-state").json()["certificates"]
    assert cert["needs"] is True
    assert cert["scopes"] == ["anchore"]
    assert "lapsed" in cert["detail"]
