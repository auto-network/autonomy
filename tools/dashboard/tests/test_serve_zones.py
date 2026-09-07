"""Custom domains in the dashboard: organization-owned delegated zones.

Contract matched: the registry's serve.zone.claim/release control ops, the
zone reservation id ``uuid5(ns, "zone:<zone>\\0<app>")``, the signed ``zone``
on serve.dns01.*, and ``<app>.<zone>`` hosts (bead auto-lf2k6).
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from tools.dashboard import (
    acme_dns01,
    service_certificate as certs,
    service_certificate_manager as manager,
    service_gateway,
    service_publication as sp,
)
from tools.dashboard.acme_dns01_hook import Dns01HookServer
from tools.graph.schemas import namespace_reservation as nr
from tools.graph.schemas import serve_zone as sz
from tools.graph.schemas.registry import SchemaValidationError
from tools.graph.schemas.service_certificate import ServiceCertificateV1
from tools.network.registry import relay

ZONE = "autonomy.taplink.net"
PERSONA = "persona-77827e972ba4c37d4215"


# --- schemas ---------------------------------------------------------------

def test_zone_value_is_normalized_and_bounded_like_the_registry():
    assert sz.validate_zone_value(" Autonomy.TapLink.net. ") == ZONE
    for bad in ("taplink.net", "x.serve.auto.network", "y.auto.network", "a..b.c", "-a.b.c", 7):
        with pytest.raises(SchemaValidationError):
            sz.validate_zone_value(bad)
        with pytest.raises(relay.ZoneValidationError):
            relay.validate_org_zone(bad)


def test_reservation_schema_accepts_zone_rows_without_persona_label():
    base = {
        "persona_pub": "ab" * 32, "app_label": "themes", "state": "active",
        "created_at": "2026-09-07T00:00:00.000Z", "updated_at": "2026-09-07T00:00:00.000Z",
    }
    nr.NamespaceReservationV1.validate({**base, "zone": ZONE})
    with pytest.raises(SchemaValidationError):  # both identities
        nr.NamespaceReservationV1.validate({**base, "zone": ZONE, "persona_label": PERSONA})
    with pytest.raises(SchemaValidationError):  # neither
        nr.NamespaceReservationV1.validate(base)
    with pytest.raises(SchemaValidationError):  # not normalized
        nr.NamespaceReservationV1.validate({**base, "zone": "Autonomy.taplink.net"})


def test_serve_zone_schema_key_and_payload():
    sz.ServeZoneV1.validate_member_key(ZONE)
    with pytest.raises(SchemaValidationError):
        sz.ServeZoneV1.validate_member_key("Autonomy.taplink.net")
    good = {"binding_kind": "parent-txt", "state": "active", "verified_at": 1,
            "claimed_at": "x", "updated_at": "x"}
    sz.ServeZoneV1.validate(good)
    with pytest.raises(SchemaValidationError):
        sz.ServeZoneV1.validate({**good, "binding_kind": "magic"})


def test_certificate_schema_accepts_zone_identity():
    ServiceCertificateV1.validate_member_key(f"autonomy:{ZONE}")
    ServiceCertificateV1.validate_member_key(f"autonomy:{PERSONA}")
    payload = {
        "org": "autonomy", "zone": ZONE, "apex": ZONE, "sans": [f"*.{ZONE}", ZONE],
        "not_before": 1, "not_after": 2, "serial": "ab", "vault_key": "k",
        "staging": False, "activated_at": 1,
    }
    ServiceCertificateV1.validate(payload)
    with pytest.raises(SchemaValidationError):
        ServiceCertificateV1.validate({**payload, "persona_label": PERSONA})
    with pytest.raises(SchemaValidationError):
        ServiceCertificateV1.validate({**payload, "apex": f"{ZONE}.serve.auto.network"})


# --- reservation identity and hostnames --------------------------------------

def test_zone_reservation_key_matches_the_relay_byte_for_byte():
    assert sp.zone_reservation_key(ZONE, "themes") == relay.zone_reservation_id(ZONE, "themes")
    assert sp.zone_reservation_key(ZONE, "themes") != sp.reservation_key("ab" * 32, "themes")
    assert uuid.UUID(sp.zone_reservation_key(ZONE, "themes")).version == 5


def test_hostname_and_identity_derive_from_the_stored_payload():
    zone_row = {"app_label": "themes", "zone": ZONE}
    persona_row = {"app_label": "themes", "persona_label": PERSONA}
    assert sp.reservation_hostname_from_payload(zone_row) == f"themes.{ZONE}"
    assert sp.reservation_hostname_from_payload(persona_row) == f"themes.{PERSONA}.serve.auto.network"
    assert sp.certificate_identity_for_payload(zone_row) == ZONE
    assert sp.certificate_identity_for_payload(persona_row) == PERSONA
    assert sp.certificate_identity_for_payload({"app_label": "x"}) is None
    projection = sp.reservation_projection("k", {**zone_row, "state": "active", "created_at": "a", "updated_at": "b"})
    assert projection["origin"] == f"https://themes.{ZONE}"
    assert projection["zone"] == ZONE and projection["persona_label"] is None


def test_reserve_origin_under_a_zone_requires_the_claimed_zone(monkeypatch):
    monkeypatch.setattr(sp, "_persona_for_org", lambda org: ("ab" * 32, "Jeremy"))
    monkeypatch.setattr(sp, "_member_by_key", lambda org, key: None)
    monkeypatch.setattr(sp, "active_zone", lambda org, zone: None)
    with pytest.raises(sp.ServicePublicationError) as refused:
        sp.reserve_origin("autonomy", "themes", ZONE)
    assert refused.value.code == "zone_not_claimed" and refused.value.status_code == 409

    written = {}
    monkeypatch.setattr(sp, "active_zone", lambda org, zone: {"zone": zone, "state": "active"})
    monkeypatch.setattr(
        sp.settings_ops, "upsert_by_key",
        lambda set_id, rev, key, payload, **kw: written.update({"key": key, "payload": payload}),
    )
    projection, created = sp.reserve_origin("autonomy", "themes", "Autonomy.TapLink.net.")
    assert created and written["key"] == relay.zone_reservation_id(ZONE, "themes")
    assert written["payload"]["zone"] == ZONE and "persona_label" not in written["payload"]
    assert projection["origin"] == f"https://themes.{ZONE}"


# --- zone claim / release through the serving tunnel ------------------------

def test_claim_zone_records_only_the_registry_verdict(monkeypatch):
    calls = []
    written = {}

    def control(org, op, args):
        calls.append((org, op, args))
        return {"ok": True, "zone": ZONE, "state": "active", "verified_at": 1757000000}

    monkeypatch.setattr(sp, "_zone_members", lambda org: [])
    monkeypatch.setattr(
        sp.settings_ops, "upsert_by_key",
        lambda set_id, rev, key, payload, **kw: written.update({"set": set_id, "key": key, "payload": payload}),
    )
    projection, created = sp.claim_zone("autonomy", "Autonomy.taplink.net", "parent-txt", control=control)
    assert calls == [("autonomy", "serve.zone.claim", {"zone": ZONE, "binding_kind": "parent-txt"})]
    assert created and written["set"] == sz.SERVE_ZONE_SET_ID and written["key"] == ZONE
    assert written["payload"]["state"] == "active" and written["payload"]["verified_at"] == 1757000000
    assert projection["zone"] == ZONE


@pytest.mark.parametrize(
    ("reply", "code", "status"),
    [
        ({"ok": False, "error": "zone-unverified: parent does not delegate x to ns1.auto.network, ns2.auto.network"}, "zone_unverified", 409),
        ({"ok": False, "error": "zone-owned-elsewhere"}, "zone_owned_elsewhere", 409),
        ({"ok": False, "error": "zone-invalid: zone must be outside auto.network"}, "zone_invalid", 400),
        ({"ok": False, "error": "not-authorized"}, "zone_not_authorized", 403),
    ],
)
def test_claim_zone_maps_registry_refusals_and_writes_nothing(monkeypatch, reply, code, status):
    monkeypatch.setattr(sp, "_zone_members", lambda org: [])
    monkeypatch.setattr(sp.settings_ops, "upsert_by_key", lambda *a, **k: pytest.fail("wrote on refusal"))
    with pytest.raises(sp.ServicePublicationError) as refused:
        sp.claim_zone("autonomy", ZONE, "parent-txt", control=lambda org, op, args: reply)
    assert (refused.value.code, refused.value.status_code) == (code, status)
    if ":" in reply["error"]:
        assert refused.value.detail == reply["error"].split(":", 1)[1].strip()


def test_claim_zone_without_a_tunnel_is_a_503_not_a_crash(monkeypatch):
    def control(org, op, args):
        raise RuntimeError("no serving delegate is provisioned for this org")
    monkeypatch.setattr(sp, "_zone_members", lambda org: [])
    with pytest.raises(sp.ServicePublicationError) as refused:
        sp.claim_zone("autonomy", ZONE, "parent-txt", control=control)
    assert refused.value.code == "serving_unavailable" and refused.value.status_code == 503
    with pytest.raises(ValueError):
        sp.claim_zone("autonomy", ZONE, "magic", control=control)


def test_release_zone_refuses_while_services_publish_under_it(monkeypatch):
    class _Zone:
        key = ZONE
        payload = {"binding_kind": "parent-txt", "state": "active", "verified_at": 1, "claimed_at": "a", "updated_at": "a"}

    class _Res:
        payload = {"zone": ZONE, "app_label": "themes", "state": "active"}

    monkeypatch.setattr(sp, "_zone_members", lambda org: [_Zone()])
    monkeypatch.setattr(sp, "_reservation_members", lambda org: [_Res()])
    with pytest.raises(sp.ServicePublicationError) as refused:
        sp.release_zone("autonomy", ZONE, control=lambda *a: pytest.fail("released in use"))
    assert refused.value.code == "zone_in_use"

    written = {}
    calls = []
    monkeypatch.setattr(sp, "_reservation_members", lambda org: [])
    monkeypatch.setattr(sp.settings_ops, "upsert_by_key", lambda s, r, key, payload, **kw: written.update(payload))

    def control(org, op, args):
        calls.append((op, args))
        return {"ok": True, "zone": ZONE, "state": "revoked"}
    projection = sp.release_zone("autonomy", ZONE, control=control)
    assert calls == [("serve.zone.release", {"zone": ZONE})]
    assert written["state"] == "revoked" and projection["state"] == "revoked"


def test_active_zone_reads_only_active_rows(monkeypatch):
    class _Active:
        key = ZONE
        payload = {"binding_kind": "parent-txt", "state": "active", "verified_at": 1, "claimed_at": "a", "updated_at": "a"}

    class _Revoked:
        key = "old.example.com"
        payload = {**_Active.payload, "state": "revoked"}
    monkeypatch.setattr(sp, "_zone_members", lambda org: [_Active(), _Revoked()])
    assert sp.active_zone("autonomy", ZONE)["zone"] == ZONE
    assert sp.active_zone("autonomy", "old.example.com") is None
    assert [z["zone"] for z in sp.list_zones("autonomy")] == [ZONE, "old.example.com"]


# --- gateway hostnames -----------------------------------------------------

def test_gateway_accepts_zone_hosts_and_still_rejects_the_old_bad_shapes():
    assert service_gateway._valid_service_hostname(f"themes.{ZONE}")
    assert service_gateway._valid_service_hostname(f"port-8000.{PERSONA}.serve.auto.network")
    for bad in ("app.example.com", "*.serve.auto.network", "app.foo.auto.network",
                f"themes.{ZONE}\nreverse_proxy attacker:80", "", None):
        assert not service_gateway._valid_service_hostname(bad)
    assert service_gateway._validate_unavailable_hostname(f"themes.{ZONE}") == f"themes.{ZONE}"


def test_gateway_hostname_for_a_zone_reservation_comes_from_the_row(monkeypatch):
    class _Member:
        payload = {"app_label": "themes", "zone": ZONE, "state": "active"}
    monkeypatch.setattr(sp, "_reservation_for_target", lambda org, key, serving=False: _Member())
    assert service_gateway.reservation_hostname("autonomy", "any") == f"themes.{ZONE}"


# --- certificates: identity, apex, signed zone on DNS-01 ---------------------

def test_certificate_identity_helpers_cover_zone_and_persona():
    assert certs.apex_for_identity(ZONE) == ZONE
    assert certs.apex_for_identity(PERSONA) == f"{PERSONA}.serve.auto.network"
    assert certs._identity_fields(ZONE) == {"zone": ZONE}
    assert certs._identity_fields(PERSONA) == {"persona_label": PERSONA}
    assert certs.certificate_identity({"zone": ZONE}) == ZONE
    assert certs.certificate_identity({"persona_label": PERSONA}) == PERSONA
    assert certs.certificate_key("autonomy", ZONE) == f"autonomy:{ZONE}"
    assert certs._zone_kwargs_for_apex(ZONE) == {"zone": ZONE}
    assert certs._zone_kwargs_for_apex(f"{PERSONA}.serve.auto.network") == {}
    assert certs.certificate_name("autonomy", ZONE) != certs.certificate_name("autonomy", PERSONA)


def test_bundle_payload_and_reader_carry_the_zone_identity(tmp_path):
    cert = tmp_path / "c.pem"; key = tmp_path / "k.pem"
    cert.write_text("CERT"); key.write_text("KEY")
    bundle = json.loads(certs._bundle_payload(cert, key, {"org": "autonomy", "zone": ZONE, "serial": "ab"})["value"])
    assert set(bundle) == {"fullchain_pem", "private_key_pem", "org", "zone", "serial"}
    persona = json.loads(certs._bundle_payload(cert, key, {"org": "autonomy", "persona_label": PERSONA, "serial": "ab"})["value"])
    assert set(persona) == {"fullchain_pem", "private_key_pem", "org", "persona_label", "serial"}


def test_dns01_client_signs_the_zone_into_present_and_cleanup(monkeypatch):
    sent = []

    class _Authority:
        class cert:
            @staticmethod
            def to_json():
                return b'"cert"'

        class key:
            @staticmethod
            def sign_hex(message):
                return message.hex()

    def control(org, op, args):
        sent.append((op, args))
        return {"ok": True, "serving": True, "accepted_caps": [acme_dns01.CAPABILITY],
                "name": f"_acme-challenge.{ZONE}", "expires_at": 160}
    client = acme_dns01.Dns01Client("autonomy", _Authority(), control=control, now=lambda: 100)
    result = client.present("order", "value", ttl=30, lifetime=60, zone=ZONE)
    assert result == {"name": f"_acme-challenge.{ZONE}", "expires_at": 160}
    present = next(args for op, args in sent if op == "serve.dns01.present")
    assert present["zone"] == ZONE
    # The zone is inside the signed core: the signature covers it.
    core = {"op": "serve.dns01.present", **{k: v for k, v in present.items() if k not in {"cert", "sig"}}}
    assert present["sig"] == (acme_dns01.SIGNING_DOMAIN + acme_dns01.canonical_json(core)).hex()
    client.cleanup("order", "value", zone=ZONE)
    cleanup = next(args for op, args in sent if op == "serve.dns01.cleanup")
    assert cleanup["zone"] == ZONE
    client.present("order", "value")
    assert "zone" not in [args for op, args in sent if op == "serve.dns01.present"][-1]


def test_preflight_presents_the_canary_at_the_zone(monkeypatch):
    seen = {}

    class _Client:
        def present(self, order, value, **kw):
            seen["present"] = kw
            return {"name": f"_acme-challenge.{ZONE}", "expires_at": 1}

        def cleanup(self, order, value, **kw):
            seen["cleanup"] = kw
    certs._dns01_preflight(_Client(), ZONE, wait=lambda name, value: None)
    assert seen["present"]["zone"] == ZONE and seen["cleanup"] == {"zone": ZONE}

    class _Wrong(_Client):
        def present(self, order, value, **kw):
            return {"name": f"_acme-challenge.{PERSONA}.serve.auto.network", "expires_at": 1}
    with pytest.raises(certs.ServiceCertificateError, match="No ACME order was placed"):
        certs._dns01_preflight(_Wrong(), ZONE, wait=lambda name, value: None)


def test_hook_server_forwards_its_bound_zone_to_the_client(tmp_path):
    seen = []

    class _Client:
        def present(self, order, value, **kw):
            seen.append(("present", kw))
            return {"name": f"_acme-challenge.{ZONE}", "expires_at": 1}

        def cleanup(self, order, value, **kw):
            seen.append(("cleanup", kw))

    async def run():
        path = tmp_path / "hook.sock"
        async with Dns01HookServer(_Client(), "order", path, wait_ready=lambda n, v: None, zone=ZONE):
            for action in ("present", "cleanup"):
                reader, writer = await asyncio.open_unix_connection(str(path))
                writer.write((json.dumps({"action": action, "value": "v"}) + "\n").encode())
                await writer.drain()
                reply = json.loads((await reader.readline()).decode())
                assert reply["ok"] is True, reply
                writer.close()
    asyncio.run(run())
    assert seen == [("present", {"zone": ZONE}), ("cleanup", {"zone": ZONE})]


def test_obtain_refuses_an_identity_that_is_neither_label_nor_zone():
    with pytest.raises(certs.ServiceCertificateError, match="invalid certificate identity"):
        asyncio.run(certs.obtain("autonomy", "not a label"))


def test_manager_desires_one_certificate_per_zone_with_a_live_target(monkeypatch):
    class _Ref:
        slug = "autonomy"
        type = "shared"
    monkeypatch.setattr(manager.service_publication, "list_service_targets",
                        lambda org: [{"reservation_id": "z1"}, {"reservation_id": "z2"}, {"reservation_id": "p1"}])
    monkeypatch.setattr(manager.service_publication, "list_reservations", lambda org: [
        {"reservation_id": "z1", "zone": ZONE, "persona_label": None, "state": "active"},
        {"reservation_id": "z2", "zone": ZONE, "persona_label": None, "state": "paused"},
        {"reservation_id": "p1", "persona_label": PERSONA, "zone": None, "state": "active"},
        {"reservation_id": "untargeted", "zone": "other.example.com", "state": "active"},
    ])
    import tools.graph.org_ops as org_ops
    monkeypatch.setattr(org_ops, "list_orgs", lambda: [_Ref()])
    assert manager.desired_personas() == {("autonomy", ZONE), ("autonomy", PERSONA)}


# --- HTTP surface -----------------------------------------------------------

def test_reservation_post_accepts_a_zone_and_zone_routes_exist(monkeypatch):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from tools.dashboard import network_routes

    monkeypatch.setattr(network_routes, "require_global_api_authority", lambda request: None)
    monkeypatch.setattr(network_routes, "organization_scope_from_request", lambda request: "autonomy")
    seen = {}

    def reserve(org, app_label, zone=None):
        seen["reserve"] = (org, app_label, zone)
        return {"reservation_id": "r", "origin": f"https://{app_label}.{zone}"}, True
    monkeypatch.setattr(sp, "reserve_origin", reserve)
    monkeypatch.setattr(sp, "claim_zone", lambda org, zone, kind: ({"zone": zone, "binding_kind": kind}, True))
    monkeypatch.setattr(sp, "release_zone", lambda org, zone: {"zone": zone, "state": "revoked"})
    monkeypatch.setattr(sp, "list_zones", lambda org: [{"zone": ZONE, "state": "active"}])

    app = Starlette(routes=[
        Route("/api/network/service-reservations", network_routes.post_service_reservation, methods=["POST"]),
        Route("/api/network/serve-zones", network_routes.get_serve_zones, methods=["GET"]),
        Route("/api/network/serve-zones", network_routes.post_serve_zone, methods=["POST"]),
        Route("/api/network/serve-zones/{zone}", network_routes.delete_serve_zone, methods=["DELETE"]),
    ])
    with TestClient(app) as client:
        r = client.post("/api/network/service-reservations", json={"app_label": "themes", "zone": "Autonomy.taplink.net"})
        assert r.status_code == 201 and seen["reserve"] == ("autonomy", "themes", ZONE)
        r = client.post("/api/network/service-reservations", json={"app_label": "themes", "zone": "taplink.net"})
        assert r.status_code == 400 and r.json()["error"] == "zone_invalid" and r.json()["detail"]
        r = client.post("/api/network/service-reservations", json={"app_label": "themes", "other": 1})
        assert r.status_code == 400 and r.json()["error"] == "unknown_fields"
        assert client.get("/api/network/serve-zones").json() == {"zones": [{"zone": ZONE, "state": "active"}]}
        r = client.post("/api/network/serve-zones", json={"zone": ZONE})
        assert r.status_code == 201 and r.json()["zone"] == {"zone": ZONE, "binding_kind": "ns-token"}
        r = client.delete(f"/api/network/serve-zones/{ZONE}")
        assert r.status_code == 200 and r.json()["zone"]["state"] == "revoked"


def test_reservation_publishers_resolve_display_names_from_member_profiles(monkeypatch):
    from tools.graph.schemas.org_member_profile import MEMBER_PROFILE_SET_ID

    class _Row:
        def __init__(self, key, payload):
            self.key, self.payload = key, payload

    class _Set:
        def __init__(self, members):
            self.members = members

    def read_owned_set(set_id, org=None, **kw):
        assert set_id == MEMBER_PROFILE_SET_ID
        return _Set([_Row("ab" * 32, {"display_name": "Jeremy"})])
    monkeypatch.setattr(sp.settings_ops, "read_owned_set", read_owned_set)
    monkeypatch.setattr(sp, "_reservation_members", lambda org: [
        _Row("r1", {"persona_pub": "ab" * 32, "app_label": "themes"}),
        _Row("r2", {"persona_pub": "cd" * 32, "app_label": "docs"}),
    ])
    publishers = sp.reservation_publishers("autonomy")
    assert publishers["r1"] == {"persona_pub": "ab" * 32, "display_name": "Jeremy"}
    assert publishers["r2"]["display_name"] == "member cdcdcdcd"
