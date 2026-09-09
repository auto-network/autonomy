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
                  may_serve=True, tunnel_serving=True):
    """Stub the serving-scope reads get_unlock_state makes.

    scopes: list of org scopes (None == personal); cert_status: {scope: status};
    replies: {scope: connector-status dict} — a scope absent from replies has no
    reachable connector. may_serve: whether this machine is permitted to serve
    at all. Since auto-clune.7 every authorized machine is, so False means the
    machine is mid-join, holds no personal root, or is absent from the active
    roster — the only cases where holding no serving credential is expected."""
    import types
    from tools.dashboard import link_serving_supervisor as sup
    from tools.dashboard import service_certificate_manager
    from tools.network import build_version
    from tools.network import fleet_tunnel_server

    # The serving predicate, not the singular-ownership election: reading
    # state().allowed as "may this machine serve" is the defect that made a
    # dead connector render as expected.
    monkeypatch.setattr(
        fleet_tunnel_server, "tunnel_serving_permitted",
        lambda: (bool(may_serve), "stubbed"),
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
    monkeypatch.setattr(service_certificate_manager, "certificate_states", lambda: [])
    # Keep the tunnel probe from touching a real supervisor singleton. The
    # serving() value is a test parameter: hardcoding True here is what let the
    # missing-serving()-method bug (permanent false-green) slip past every test.
    monkeypatch.setattr(
        sup, "get_supervisor",
        lambda: type("S", (), {"serving": lambda self: tunnel_serving})())


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


def test_sync_lights_when_the_personal_connector_is_unarmed(client, monkeypatch):
    # Only the PERSONAL tunnel serves Fleet sync. When ITS connector is unarmed
    # (fleet_runtime_configured=False) sync pulls refuse, so the tray lights.
    # Org connectors alongside it (armed or not) never contribute.
    _stub_serving(
        monkeypatch,
        scopes=[None, "anchore"],
        cert_status={None: "ok", "anchore": "ok"},
        replies={
            None: {"fleet_runtime_configured": False, "process_commit": "c0ffee",
                   "locked_refusals": 764, "locked_refusal_since": 1_700_000_000},
            "anchore": {"fleet_runtime_configured": True, "process_commit": "c0ffee"},
        },
    )
    sync = client.get("/api/identity/unlock-state").json()["sync"]
    assert sync["needs"] is True
    assert sync["value"] == "Locked"
    assert sync["unarmed"] == ["personal"]
    assert sync["count"] == 764
    assert "can't sync" in sync["detail"]
    assert "764 requests" in sync["detail"]


def test_org_connectors_never_light_sync(client, monkeypatch):
    # The operator-reported false alarm: anchore/autonomy/dynbench databases do
    # not sync over the personal engine, so their connectors report
    # fleet_runtime_configured=False BY DESIGN — they never serve Fleet sync and
    # were never provisioned to. The tray must not read that as a fault ("hold
    # no serving credential"). With the personal tunnel absent from the serving
    # set, sync stays quiet no matter what the org connectors report.
    _stub_serving(
        monkeypatch,
        scopes=["anchore", "autonomy", "dynbench"],
        cert_status={"anchore": "ok", "autonomy": "ok", "dynbench": "ok"},
        replies={
            "anchore": {"fleet_runtime_configured": False, "process_commit": "c0ffee"},
            "autonomy": {"fleet_runtime_configured": False, "process_commit": "c0ffee"},
            "dynbench": {"fleet_runtime_configured": False, "process_commit": "c0ffee"},
        },
    )
    sync = client.get("/api/identity/unlock-state").json()["sync"]
    assert sync["needs"] is False
    assert sync["unarmed"] == []
    assert sync["scopes"] == []
    assert "hold no serving credential" not in sync["detail"]
    assert "unlock with your root" not in sync["detail"].lower()


def test_stale_connector_code_does_NOT_light_the_user_sync_flag(client, monkeypatch):
    # Operator-directed 2026-09-05: "connector git commit != disk" is a
    # developer deploy-hygiene probe (fleet_doctor's STALE-CODE), not a user
    # sync fault — it fires on ANY code change, including a frontend edit that
    # cannot affect sync. An ARMED connector running an older commit is serving
    # fine; the user Sync flag must NOT light on it (real schema/version
    # incompatibility is the compatibility digest's job, not a commit compare).
    _stub_serving(
        monkeypatch,
        scopes=[None],
        cert_status={None: "ok"},
        replies={
            # armed (has its credential), but running an older commit than disk
            None: {"fleet_runtime_configured": True, "process_commit": "0ld"},
        },
        disk="c0ffee",
    )
    sync = client.get("/api/identity/unlock-state").json()["sync"]
    assert sync["needs"] is False
    assert sync["value"] != "Stale"
    assert sync["stale"] == []
    assert "older code" not in sync["detail"]


def test_unreachable_personal_connector_counts_as_unarmed(client, monkeypatch):
    _stub_serving(
        monkeypatch,
        scopes=[None],
        cert_status={None: "ok"},
        replies={},   # cert provisioned, but no connector answers
    )
    sync = client.get("/api/identity/unlock-state").json()["sync"]
    assert sync["needs"] is True
    assert "personal" in sync["unarmed"]


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


def test_a_machine_not_permitted_to_serve_does_not_light_sync(client, monkeypatch):
    # A machine that may not serve — mid-join, no personal root, or absent from
    # the active roster — holds no serving credential by design. Its unarmed
    # connector must NOT be reported as a fault, and no root-unlock remedy is
    # offered, because unlocking cannot make it eligible.
    _stub_serving(
        monkeypatch,
        scopes=[None],
        cert_status={None: "ok"},
        replies={None: {"fleet_runtime_configured": False, "process_commit": "c0ffee"}},
        may_serve=False,
    )
    sync = client.get("/api/identity/unlock-state").json()["sync"]
    assert sync["needs"] is False
    assert sync["unarmed"] == []
    assert sync["scopes"] == []
    assert "serving credential" not in sync["detail"] or "expected" in sync["detail"]
    assert "unlock with your root" not in sync["detail"].lower()
    assert "expected" in sync["detail"]


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
    # The dedup guard lives on the FIELDS (a single 'personal', not two).
    assert sync["unarmed"] == ["personal"]
    assert sync["scopes"] == ["personal"]
    # The user message names no scope at all now (2026-09-05: the 'personal'
    # label is jargon), so it trivially cannot read "personal and personal".
    assert "personal" not in sync["detail"]
    assert "fleet connection has no serving credential" in sync["detail"]


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
    assert "expired" in cert["detail"]


def test_existing_certificate_flag_includes_missing_service_tls(client, monkeypatch):
    _stub_serving(
        monkeypatch,
        scopes=["autonomy"],
        cert_status={"autonomy": "ok"},
        replies={"autonomy": {"process_commit": "c0ffee"}},
    )
    from tools.dashboard import service_certificate_manager
    monkeypatch.setattr(
        service_certificate_manager,
        "certificate_states",
        lambda: [{
            "org": "autonomy",
            "persona_label": "persona-missing",
            "state": "missing",
            "reason": "No persona Service TLS certificate has been issued yet.",
        }],
    )
    cert = client.get("/api/identity/unlock-state").json()["certificates"]
    assert cert["needs"] is True
    assert cert["value"] == "Missing"
    assert cert["scopes"] == ["autonomy / persona-missing"]
    assert "has been issued" in cert["detail"]


@pytest.mark.parametrize(
    ("state", "value", "reason"),
    [
        ("issuing", "Issuing", "Certificate issuance or renewal is in progress."),
        ("current", "Current", "The persona Service TLS certificate is current."),
        ("renewal_due", "Renewal due", "The certificate is inside its renewal window."),
        ("expired", "Expired", "The persona Service TLS certificate has expired."),
        ("issuance_failed", "Issuance failed", "ACME authorization was refused."),
    ],
)
def test_certificate_flag_projects_each_manager_state(
    client, monkeypatch, state, value, reason
):
    _stub_serving(monkeypatch, scopes=[], cert_status={}, replies={})
    from tools.dashboard import service_certificate_manager
    monkeypatch.setattr(
        service_certificate_manager,
        "certificate_states",
        lambda: [{
            "org": "anchore",
            "persona_label": "persona-state",
            "state": state,
            "reason": reason,
        }],
    )

    cert = client.get("/api/identity/unlock-state").json()["certificates"]

    assert cert["value"] == value
    assert cert["state"] == state
    assert reason in cert["detail"]
    assert cert["needs"] is (state != "current")


def test_tunnel_lights_down_when_a_serving_machine_is_not_serving(
    client, monkeypatch
):
    """The false-green regression: a serving machine whose connector
    is dead must show the tunnel tile RED ('Down', needs=True) — not the old
    swallowed 'Tunnel state is unavailable' dim tile. This is the exact state
    the operator hit: home serving nothing, UI must say so."""
    _stub_serving(
        monkeypatch,
        scopes=[None],
        cert_status={None: "ok"},
        replies={},          # personal connector unreachable
        may_serve=True,
        tunnel_serving=False,  # ServingSupervisor.serving() -> False (dead)
    )
    tunnel = client.get("/api/identity/unlock-state").json()["tunnel"]
    assert tunnel["needs"] is True
    assert tunnel["value"] == "Down"
    assert "unavailable" not in tunnel["detail"].lower()


def test_tunnel_up_when_a_serving_machine_is_serving(client, monkeypatch):
    _stub_serving(
        monkeypatch,
        scopes=[None],
        cert_status={None: "ok"},
        replies={None: {"fleet_runtime_configured": True, "process_commit": "c0ffee"}},
        may_serve=True,
        tunnel_serving=True,
    )
    tunnel = client.get("/api/identity/unlock-state").json()["tunnel"]
    assert tunnel["needs"] is False
    assert tunnel["value"] == "Up"


def test_tunnel_quiet_on_a_machine_not_permitted_to_serve(client, monkeypatch):
    """No false RED either: a machine that may not serve runs no tunnel of its
    own, so a missing one is expected — quiet, never lit (the operator's
    no-false-alarm rule, applied to the tunnel tile too).

    The inverse is the point: on a machine that MAY serve, a missing tunnel is
    a fault. Every authorized machine may, since auto-clune.7."""
    _stub_serving(
        monkeypatch,
        scopes=[None],
        cert_status={None: "ok"},
        replies={},
        may_serve=False,
        tunnel_serving=False,
    )
    tunnel = client.get("/api/identity/unlock-state").json()["tunnel"]
    assert tunnel["needs"] is False
    assert tunnel["value"] == ""
    assert "not permitted to serve" in tunnel["detail"]
