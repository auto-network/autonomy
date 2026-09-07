"""Organization-owned delegated zones (custom domains, auto-h6hte).

A zone such as ``autonomy.taplink.net`` is claimed by an org tunnel, verified
against the PARENT zone (delegation to our name servers + a binding record),
served authoritatively by the responder, and hosts services DIRECTLY under
it: ``themes.autonomy.taplink.net``, one wildcard per zone, challenge at
``_acme-challenge.autonomy.taplink.net``. No persona label layer.
"""
from __future__ import annotations

import pytest

from tools.network.idkit import canonical_json
from tools.network.registry import relay as relay_mod
from tools.network.registry.dns_challenges import ChallengeError, validate_challenge_name
from tools.network.registry.dns_responder import DATA_TTL, NS_TTL, ZoneState, match_zone
from tools.network.registry.relay import DNS01_DOMAIN
from tools.network.registry.store import RegistryStore

from .conftest import ORG, register
from .test_dns01_ops import _ctrl, _dns01_cert, _tunnel
from .test_dns_responder import RELAY_IP, ask
from tools.network.idkit import KeyPair

BASE = "serve.auto.network"
ORG_ZONE = "autonomy.taplink.net"
OTHER_ORG = "11111111-2222-3333-4444-555555555555"


# ---------------------------------------------------------------- responder

def _state(zones, challenges=None):
    challenges = challenges or {}
    return ZoneState(relay_ip=RELAY_IP, node_id="n1",
                     txt_lookup=lambda name: challenges.get(name, []),
                     zones=lambda: tuple(zones))


def test_match_zone_prefers_the_longest_suffix():
    zones = (BASE, ORG_ZONE, "taplink.net")
    assert match_zone("themes.autonomy.taplink.net", zones) == ORG_ZONE
    assert match_zone("autonomy.taplink.net", zones) == ORG_ZONE
    assert match_zone("api.taplink.net", zones) == "taplink.net"
    assert match_zone("app.worker-aa.serve.auto.network", zones) == BASE
    assert match_zone("evil.example", zones) is None


def test_second_zone_answers_apex_a_ns_soa_and_service_a():
    state = _state([BASE, ORG_ZONE])
    r = ask(state, ORG_ZONE, "A")
    assert r["rcode"] == 0 and r["aa"] == 1 and r["answers"][0][3] == RELAY_IP
    r = ask(state, f"themes.{ORG_ZONE}", "A")
    assert r["answers"][0][3] == RELAY_IP and r["answers"][0][2] == DATA_TTL
    r = ask(state, ORG_ZONE, "NS")
    assert sorted(rd for (_, _, _, rd) in r["answers"]) == ["ns1.auto.network.", "ns2.auto.network."]
    assert all(ttl == NS_TTL for (_, _, ttl, _) in r["answers"])
    r = ask(state, ORG_ZONE, "SOA")
    assert r["answers"][0][3]["mname"] == "ns1.auto.network."
    # A non-apex NS probe inside the org zone is NODATA with the ORG zone's SOA
    r = ask(state, f"themes.{ORG_ZONE}", "NS")
    assert r["rcode"] == 0 and r["answers"] == []
    assert r["authority"][0][0] == ORG_ZONE + "."


def test_zone_direct_challenge_txt_and_base_zone_untouched():
    state = _state([BASE, ORG_ZONE], {f"_acme-challenge.{ORG_ZONE}.": ["zone-tok"],
                                      f"_acme-challenge.worker-aa.{BASE}.": ["persona-tok"]})
    r = ask(state, f"_acme-challenge.{ORG_ZONE}", "TXT")
    assert [v for (_, _, _, txt) in r["answers"] for v in txt] == ["zone-tok"]
    r = ask(state, f"_acme-challenge.worker-aa.{BASE}", "TXT")
    assert [v for (_, _, _, txt) in r["answers"] for v in txt] == ["persona-tok"]


def test_unknown_suffix_is_refused_even_next_to_an_org_zone():
    state = _state([BASE, ORG_ZONE])
    assert ask(state, "api.taplink.net", "A")["rcode"] == 5  # REFUSED: parent is not ours
    assert ask(state, "evil.example", "A")["rcode"] == 5


# --------------------------------------------------------------- challenges

def test_zone_direct_challenge_name_needs_an_active_org_zone():
    assert validate_challenge_name(f"_acme-challenge.{ORG_ZONE}", org_zones=[ORG_ZONE]) == ""
    with pytest.raises(ChallengeError):
        validate_challenge_name(f"_acme-challenge.{ORG_ZONE}")  # not active: base grammar refuses
    with pytest.raises(ChallengeError):
        validate_challenge_name(f"_acme-challenge.themes.{ORG_ZONE}", org_zones=[ORG_ZONE])  # one wildcard per zone


# ---------------------------------------------------------------- store

def test_store_zone_lifecycle_and_ownership():
    store = RegistryStore(":memory:")
    row = store.upsert_serve_zone(ORG_ZONE, org=ORG, binding_kind="parent-txt",
                                  binding_value=f"autonomy-org={ORG}", state="active", now=100, verified_at=100)
    assert row["state"] == "active" and store.active_serve_zones() == {ORG_ZONE: ORG}
    with pytest.raises(ValueError):
        store.upsert_serve_zone(ORG_ZONE, org=OTHER_ORG, binding_kind="parent-txt",
                                binding_value="x", state="active", now=101)
    store.set_serve_zone_state(ORG_ZONE, "revoked", now=102)
    assert store.active_serve_zones() == {}
    assert store.list_serve_zones(ORG)[0]["state"] == "revoked"


# ---------------------------------------------------------------- zone claim

def _lookup(delegation, txt):
    def lookup(kind, name):
        return delegation if kind == "delegation" else txt
    return lookup


def test_verify_zone_binding_parent_txt_and_ns_token():
    ok = relay_mod.verify_zone_binding(
        ORG_ZONE, ORG, "parent-txt",
        lookup=_lookup(["ns2.auto.network", "ns1.auto.network"], [f"autonomy-org={ORG}"]))
    assert ok == f"autonomy-org={ORG}"
    with pytest.raises(relay_mod.ZoneValidationError, match="does not delegate"):
        relay_mod.verify_zone_binding(ORG_ZONE, ORG, "parent-txt",
                                      lookup=_lookup(["ns1.other.example"], [f"autonomy-org={ORG}"]))
    with pytest.raises(relay_mod.ZoneValidationError, match="no TXT"):
        relay_mod.verify_zone_binding(ORG_ZONE, ORG, "parent-txt",
                                      lookup=_lookup(["ns1.auto.network", "ns2.auto.network"], [f"autonomy-org={OTHER_ORG}"]))
    assert relay_mod.verify_zone_binding(
        ORG_ZONE, ORG, "ns-token", lookup=_lookup([f"{ORG}.ns.auto.network"], [])) == f"{ORG}.ns.auto.network"


def test_token_binding_is_the_delegation_itself():
    """One set of records: the parent's NS names carry the org id under the
    token hosts; no TXT. Every NS must carry THIS org — a foreign server or
    another org's token in the set is not our zone."""
    pair = list(relay_mod.zone_token_names(ORG))
    assert pair == [f"{ORG}.ns1.auto.network", f"{ORG}.ns2.auto.network"]
    assert relay_mod.verify_zone_binding(
        ORG_ZONE, ORG, "ns-token", lookup=_lookup([n.upper() + "." for n in pair], [])
    ) == ",".join(pair)
    for bad in ([pair[0], "ns2.auto.network"], [f"{OTHER_ORG}.ns1.auto.network"], [pair[0], f"{OTHER_ORG}.ns2.auto.network"], []):
        with pytest.raises(relay_mod.ZoneValidationError, match="does not delegate"):
            relay_mod.verify_zone_binding(ORG_ZONE, ORG, "ns-token", lookup=_lookup(bad, []))


def test_token_zone_feed_and_responder_answer_the_token_name_servers(app, client, clock, root, monkeypatch):
    register(client, clock, root, org_uuid=ORG)
    import tools.network.registry.dns_lookup as dl
    from tools.network.registry import dns_responder as r
    from tools.network.registry.dns_lookup import _build_query
    import struct
    monkeypatch.setattr(dl, "delegation_ns", lambda zone: list(relay_mod.zone_token_names(ORG)))
    with _tunnel(client, clock, root) as ws:
        reply = _claim(ws, kind="ns-token")
        assert reply["ok"] is True and reply["state"] == "active", reply
        feed = client.get("/v1/dns/zone-state").json()
        assert feed["zones"] == [ORG_ZONE]
        assert feed["zone_ns"] == {ORG_ZONE: [n + "." for n in relay_mod.zone_token_names(ORG)]}
    # The responder, fed that state, answers the token NS set (and SOA MNAME)
    # for the token zone and the shared ns1/ns2 set for the base zone.
    ns = {z: tuple(v) for z, v in feed["zone_ns"].items()}
    state = r.ZoneState(relay_ip="5.161.17.217", zones=lambda: ["serve.auto.network", *feed["zones"]], zone_ns=ns.get)
    for zone, expect_token in ((ORG_ZONE, True), ("serve.auto.network", False)):
        for qtype in (2, 6):
            out = r.handle_query(_build_query(zone, qtype, recursive=False)[0], state)
            assert out[3] & 0xF == 0 and struct.unpack(">H", out[6:8])[0] >= 1
            assert (ORG.encode() in out) is expect_token


def test_validate_org_zone_bounds():
    assert relay_mod.validate_org_zone("Autonomy.TapLink.net.") == ORG_ZONE
    for bad in ("taplink.net", "serve.auto.network", "x.serve.auto.network", "foo.auto.network", "bad_label.example.com"):
        with pytest.raises(relay_mod.ZoneValidationError):
            relay_mod.validate_org_zone(bad)


def _claim(ws, zone=ORG_ZONE, kind="parent-txt"):
    return _ctrl(ws, "serve.zone.claim", {"zone": zone, "binding_kind": kind})


def test_zone_claim_activates_only_when_the_parent_binds_this_org(app, client, clock, root, monkeypatch):
    register(client, clock, root, org_uuid=ORG)
    import tools.network.registry.dns_lookup as dl
    monkeypatch.setattr(dl, "delegation_ns", lambda zone: ["ns1.auto.network", "ns2.auto.network"])
    monkeypatch.setattr(dl, "query", lambda name, rtype, **kw: [f"autonomy-org={OTHER_ORG}"])
    with _tunnel(client, clock, root) as ws:
        reply = _claim(ws)
        assert reply["ok"] is False and reply["error"].startswith("zone-unverified")
        assert app.state.store.get_serve_zone(ORG_ZONE)["state"] == "pending"
        monkeypatch.setattr(dl, "query", lambda name, rtype, **kw: [f"autonomy-org={ORG}"])
        reply = _claim(ws)
        assert reply["ok"] is True and reply["state"] == "active", reply
        assert app.state.store.active_serve_zones() == {ORG_ZONE: ORG}
        assert client.get("/v1/dns/zone-state").json()["zones"] == [ORG_ZONE]
        reply = _ctrl(ws, "serve.zone.release", {"zone": ORG_ZONE})
        assert reply["ok"] is True and reply["state"] == "revoked"
        assert client.get("/v1/dns/zone-state").json()["zones"] == []


# ------------------------------------------------------- hosts under a zone

def _activate(app, org=ORG):
    app.state.store.upsert_serve_zone(ORG_ZONE, org=org, binding_kind="parent-txt",
                                      binding_value=f"autonomy-org={org}", state="active",
                                      now=int(app.state.now_fn()), verified_at=int(app.state.now_fn()))


def test_host_register_directly_under_an_active_org_zone(app, client, clock, root):
    register(client, clock, root, org_uuid=ORG)
    _activate(app)
    host = f"themes.{ORG_ZONE}"
    reservation = relay_mod.zone_reservation_id(ORG_ZONE, "themes")
    with _tunnel(client, clock, root) as ws:
        reply = _ctrl(ws, "host-register", {"reservation": reservation, "host": host})
        assert reply["ok"] is True, reply
        # a persona-derived reservation id for the same host is refused
        bad = _ctrl(ws, "host-register", {"reservation": "00000000-0000-5000-8000-000000000000", "host": host})
        assert bad["ok"] is False and bad["error"] == "label-invalid"
        # a reserved app label is refused
        bad = _ctrl(ws, "host-register", {"reservation": relay_mod.zone_reservation_id(ORG_ZONE, "www"), "host": f"www.{ORG_ZONE}"})
        assert bad["ok"] is False and bad["error"] == "label-invalid"


def test_host_register_under_a_zone_owned_by_another_org_is_refused(app, client, clock, root):
    register(client, clock, root, org_uuid=ORG)
    _activate(app, org=OTHER_ORG)
    with _tunnel(client, clock, root) as ws:
        reply = _ctrl(ws, "host-register", {"reservation": relay_mod.zone_reservation_id(ORG_ZONE, "themes"),
                                            "host": f"themes.{ORG_ZONE}"})
        assert reply["ok"] is False and reply["error"] == "label-invalid"


# --------------------------------------------------------------- dns01 zone

def _present_zone_args(root, clock, zone, *, order="order-z", value="tok-z", ttl=120, expiry_in=600):
    key = KeyPair.generate()
    cert = _dns01_cert(root, key)
    ts = clock.now
    core = {"op": "serve.dns01.present", "order": order, "value": value,
            "ttl": ttl, "expiry": ts + expiry_in, "ts": ts, "zone": zone}
    return {"order": order, "value": value, "ttl": ttl, "expiry": ts + expiry_in, "ts": ts,
            "zone": zone, "cert": cert.to_json().decode("ascii"),
            "sig": key.sign_hex(DNS01_DOMAIN + canonical_json(core))}


def test_dns01_present_with_zone_publishes_at_the_zone_apex(app, client, clock, root):
    register(client, clock, root, org_uuid=ORG)
    _activate(app)
    with _tunnel(client, clock, root) as ws:
        reply = _ctrl(ws, "serve.dns01.present", _present_zone_args(root, clock, ORG_ZONE))
        assert reply["ok"] is True, reply
        assert reply["name"] == f"_acme-challenge.{ORG_ZONE}"
        live = app.state.store.live_serve_challenges(now=int(app.state.now_fn()))
        assert live[f"_acme-challenge.{ORG_ZONE}."]["values"] == ["tok-z"]
        # an unowned zone is refused uniformly, and the signature covers the zone
        assert _ctrl(ws, "serve.dns01.present", _present_zone_args(root, clock, "other.example.com"))["ok"] is False
        forged = _present_zone_args(root, clock, ORG_ZONE)
        forged["zone"] = ORG_ZONE.upper()
        assert _ctrl(ws, "serve.dns01.present", forged)["ok"] is False
