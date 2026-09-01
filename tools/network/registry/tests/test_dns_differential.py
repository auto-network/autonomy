"""Differential compatibility tier for the serve.auto.network responder.

The primary suite (test_dns_responder.py) proves the responder against a
mini-codec we also wrote — two implementations, one author, shared
misconceptions possible. This tier re-drives the resolver-behavior
matrix through dnspython's battle-tested wire codec: queries built by
dnspython, replies parsed by dnspython's strict parser (compression,
counts, rdata formats all validated by software we did not write).

Skips when dnspython is unavailable; the estate runbook's Zonemaster
undelegated gate and the multi-resolver public verify are the next
independence tiers up.
"""

from __future__ import annotations

import pytest

dns = pytest.importorskip("dns")
import dns.edns  # noqa: E402
import dns.flags  # noqa: E402
import dns.message  # noqa: E402
import dns.name  # noqa: E402
import dns.rcode  # noqa: E402
import dns.rdataclass  # noqa: E402
import dns.rdatatype  # noqa: E402

from tools.network.registry.dns_responder import (  # noqa: E402
    DATA_TTL,
    NS_TTL,
    SOA_MINIMUM,
    UDP_PAYLOAD,
    ZoneState,
    handle_query,
)

ZONE = "serve.auto.network"
RELAY_IP = "203.0.113.7"
CHALLENGE = f"_acme-challenge.worker-aa.{ZONE}"


@pytest.fixture
def state():
    return ZoneState(
        relay_ip=RELAY_IP,
        node_id="registry-ash-1",
        txt_lookup=lambda name: (
            ["apex-tok", "wild-tok"] if name == CHALLENGE + "." else []
        ),
        txt_ttl=lambda name: 45,
    )


def ask(state, qname, rdtype, *, rdclass="IN", edns=False, payload=1232,
        want_dnssec=False, tcp=False):
    query = dns.message.make_query(
        qname, rdtype, rdclass=rdclass,
        use_edns=0 if edns else None, payload=payload,
        want_dnssec=want_dnssec,
    )
    raw = handle_query(query.to_wire(), state, tcp=tcp)
    assert raw is not None
    reply = dns.message.from_wire(raw)  # dnspython's STRICT parser
    assert reply.id == query.id
    assert reply.flags & dns.flags.QR
    assert not reply.flags & dns.flags.RA
    return reply


def _rrset(reply, section, name, rdtype):
    return reply.find_rrset(
        section, dns.name.from_text(name),
        dns.rdataclass.IN, dns.rdatatype.from_text(rdtype),
    )


def test_apex_and_wildcard_a(state):
    for qname in (ZONE, f"app.worker-aa.{ZONE}", f"deep.x.{ZONE}"):
        reply = ask(state, qname, "A")
        assert reply.rcode() == dns.rcode.NOERROR
        assert reply.flags & dns.flags.AA
        rrset = _rrset(reply, reply.answer, qname, "A")
        assert rrset.ttl == DATA_TTL
        assert [r.address for r in rrset] == [RELAY_IP]


def test_mixed_case_qname_round_trips(state):
    reply = ask(state, f"PrObE.{ZONE.upper()}", "A")
    assert reply.rcode() == dns.rcode.NOERROR
    assert len(reply.answer) == 1


def test_ns_and_soa_at_apex(state):
    reply = ask(state, ZONE, "NS")
    rrset = _rrset(reply, reply.answer, ZONE, "NS")
    assert rrset.ttl == NS_TTL
    assert sorted(str(r.target) for r in rrset) == [
        "ns1.auto.network.", "ns2.auto.network."]
    soa_reply = ask(state, ZONE, "SOA")
    soa = _rrset(soa_reply, soa_reply.answer, ZONE, "SOA")
    rdata = list(soa)[0]
    assert str(rdata.mname) == "ns1.auto.network."
    assert rdata.minimum == SOA_MINIMUM


def _assert_nodata(reply):
    assert reply.rcode() == dns.rcode.NOERROR
    assert reply.answer == []
    soa = _rrset(reply, reply.authority, ZONE, "SOA")
    assert soa.ttl == SOA_MINIMUM


def test_aaaa_and_qname_minimization_probes_are_nodata(state):
    _assert_nodata(ask(state, ZONE, "AAAA"))
    _assert_nodata(ask(state, f"x.{ZONE}", "AAAA"))
    _assert_nodata(ask(state, f"worker-aa.{ZONE}", "NS"))
    _assert_nodata(ask(state, f"_acme-challenge.ghost.{ZONE}", "TXT"))


def test_challenge_txt_values_and_per_name_ttl(state):
    reply = ask(state, CHALLENGE, "TXT")
    rrset = _rrset(reply, reply.answer, CHALLENGE, "TXT")
    assert rrset.ttl == 45
    values = sorted(
        b"".join(r.strings).decode() for r in rrset)
    assert values == ["apex-tok", "wild-tok"]


def test_chaos_id_server(state):
    query = dns.message.make_query("id.server", "TXT", rdclass="CH")
    reply = dns.message.from_wire(handle_query(query.to_wire(), state))
    assert reply.rcode() == dns.rcode.NOERROR
    rrset = reply.find_rrset(
        reply.answer, dns.name.from_text("id.server"),
        dns.rdataclass.CH, dns.rdatatype.TXT)
    assert b"".join(list(rrset)[0].strings) == b"registry-ash-1"


def test_out_of_zone_any_and_axfr_are_refused(state):
    assert ask(state, "example.com", "A").rcode() == dns.rcode.REFUSED
    assert ask(state, "auto.network", "A").rcode() == dns.rcode.REFUSED
    assert ask(state, ZONE, "ANY").rcode() == dns.rcode.REFUSED
    query = dns.message.make_query(ZONE, "AXFR")
    reply = dns.message.from_wire(
        handle_query(query.to_wire(), state, tcp=True))
    assert reply.rcode() == dns.rcode.REFUSED


def test_edns_echo_do_bit_and_truncation(state):
    reply = ask(state, ZONE, "A", edns=True, payload=4096)
    assert reply.edns == 0
    assert reply.payload == UDP_PAYLOAD

    # DO bit (a validating resolver's query): unsigned zone answers
    # cleanly with no RRSIGs and no error.
    reply = ask(state, ZONE, "A", edns=True, want_dnssec=True)
    assert reply.rcode() == dns.rcode.NOERROR
    assert len(reply.answer) == 1

    big = ZoneState(
        relay_ip=RELAY_IP, node_id="n",
        txt_lookup=lambda name: [f"tok-{i}-" + "x" * 240 for i in range(8)],
    )
    plain = dns.message.from_wire(handle_query(
        dns.message.make_query(CHALLENGE, "TXT").to_wire(), big))
    assert plain.flags & dns.flags.TC
    assert plain.answer == []
    over_tcp = dns.message.from_wire(handle_query(
        dns.message.make_query(CHALLENGE, "TXT").to_wire(), big, tcp=True))
    assert not over_tcp.flags & dns.flags.TC
    rrset = over_tcp.find_rrset(
        over_tcp.answer, dns.name.from_text(CHALLENGE),
        dns.rdataclass.IN, dns.rdatatype.TXT)
    assert len(rrset) == 8


def test_edns_version_1_gets_badvers(state):
    """dnspython computes the extended rcode for us: version-1 EDNS must
    answer BADVERS (the Zonemaster-caught RFC 6891 case)."""
    query = dns.message.make_query(ZONE, "A", use_edns=1, payload=1232)
    reply = dns.message.from_wire(handle_query(query.to_wire(), state))
    assert reply.rcode() == dns.rcode.BADVERS
    assert reply.edns == 0               # we answer with version 0
    assert reply.answer == []
