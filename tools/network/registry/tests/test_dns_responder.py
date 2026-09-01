"""auto-g1jxw: the registry-embedded authoritative responder for
serve.auto.network — the resolver-behavior matrix as L1.

Modern resolvers punish servers that mishandle EDNS0, QNAME
minimization, AAAA, or negative answers; each behavior here is a test,
not a hope. The test file carries its own independent mini wire codec
(build queries, parse responses incl. compression pointers) so the
responder's encoding is checked against a second implementation.
"""

from __future__ import annotations

import struct

import pytest

from tools.network.registry.dns_responder import (
    DATA_TTL,
    NS_TTL,
    SOA_MINIMUM,
    UDP_PAYLOAD,
    ZoneState,
    handle_query,
)

ZONE = "serve.auto.network"
RELAY_IP = "203.0.113.7"

QTYPE = {"A": 1, "NS": 2, "SOA": 6, "TXT": 16, "AAAA": 28, "AXFR": 252,
         "ANY": 255, "OPT": 41}
CLASS_IN, CLASS_CH = 1, 3


# -- independent mini codec -------------------------------------------------


def _name(qname: str) -> bytes:
    out = b""
    for label in qname.rstrip(".").split("."):
        raw = label.encode()
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def build_query(qname: str, qtype: str, *, qid=0x1234, rd=1,
                qclass=CLASS_IN, edns_payload: int | None = None,
                qr=0) -> bytes:
    flags = (qr << 15) | (rd << 8)
    arcount = 1 if edns_payload else 0
    msg = struct.pack(">HHHHHH", qid, flags, 1, 0, 0, arcount)
    msg += _name(qname) + struct.pack(">HH", QTYPE[qtype], qclass)
    if edns_payload:
        msg += b"\x00" + struct.pack(">HHIH", QTYPE["OPT"], edns_payload,
                                     0, 0)
    return msg


def _read_name(raw: bytes, off: int) -> tuple[str, int]:
    labels, jumped, guard = [], False, 0
    end = off
    while True:
        guard += 1
        assert guard < 64, "name loop"
        length = raw[off]
        if length & 0xC0 == 0xC0:
            pointer = struct.unpack(">H", raw[off:off + 2])[0] & 0x3FFF
            if not jumped:
                end = off + 2
            off, jumped = pointer, True
            continue
        if length == 0:
            if not jumped:
                end = off + 1
            return ".".join(labels) + ".", end
        off += 1
        labels.append(raw[off:off + length].decode())
        off += length


def parse_response(raw: bytes) -> dict:
    (qid, flags, qd, an, ns, ar) = struct.unpack(">HHHHHH", raw[:12])
    out = {
        "id": qid,
        "qr": flags >> 15 & 1, "aa": flags >> 10 & 1,
        "tc": flags >> 9 & 1, "rd": flags >> 8 & 1, "ra": flags >> 7 & 1,
        "rcode": flags & 0xF,
        "question": None, "answers": [], "authority": [],
        "additional": [], "opt_payload": None,
    }
    off = 12
    for _ in range(qd):
        qname, off = _read_name(raw, off)
        qtype, qclass = struct.unpack(">HH", raw[off:off + 4])
        off += 4
        out["question"] = (qname, qtype, qclass)
    for section, count in (("answers", an), ("authority", ns),
                           ("additional", ar)):
        for _ in range(count):
            name, off = _read_name(raw, off)
            rtype, rclass, ttl, rdlen = struct.unpack(
                ">HHIH", raw[off:off + 10])
            off += 10
            rdata_raw = raw[off:off + rdlen]
            if rtype == QTYPE["OPT"]:
                out["opt_payload"] = rclass
                off += rdlen
                continue
            if rtype == QTYPE["A"]:
                rdata = ".".join(str(b) for b in rdata_raw)
            elif rtype in (QTYPE["NS"],):
                rdata, _ = _read_name(raw, off)
            elif rtype == QTYPE["SOA"]:
                mname, o2 = _read_name(raw, off)
                rname, o2 = _read_name(raw, o2)
                serial, refresh, retry, expire, minimum = struct.unpack(
                    ">IIIII", raw[o2:o2 + 20])
                rdata = {"mname": mname, "rname": rname, "serial": serial,
                         "minimum": minimum}
            elif rtype == QTYPE["TXT"]:
                strings, p = [], 0
                while p < len(rdata_raw):
                    ln = rdata_raw[p]
                    strings.append(rdata_raw[p + 1:p + 1 + ln].decode())
                    p += 1 + ln
                rdata = strings
            else:
                rdata = rdata_raw
            off += rdlen
            out[section].append((name, rtype, ttl, rdata))
    return out


# -- fixtures ---------------------------------------------------------------


@pytest.fixture
def state():
    challenges = {
        f"_acme-challenge.worker-aa.{ZONE}.": ["apex-tok", "wild-tok"],
    }
    return ZoneState(
        relay_ip=RELAY_IP,
        node_id="registry-ash-1",
        txt_lookup=lambda name: challenges.get(name, []),
    )


def ask(state, qname, qtype, **kw):
    reply = handle_query(build_query(qname, qtype, **kw), state)
    assert reply is not None
    parsed = parse_response(reply)
    assert parsed["id"] == kw.get("qid", 0x1234)
    assert parsed["qr"] == 1
    assert parsed["ra"] == 0          # never claim recursion
    return parsed


# -- positive answers -------------------------------------------------------


def test_apex_a_answer(state):
    r = ask(state, ZONE, "A")
    assert r["rcode"] == 0 and r["aa"] == 1
    assert r["answers"] == [(ZONE + ".", 1, DATA_TTL, RELAY_IP)]


def test_any_in_zone_name_answers_relay_ip(state):
    r = ask(state, f"app.worker-aa.{ZONE}", "A")
    assert r["answers"][0][3] == RELAY_IP
    assert r["answers"][0][2] == DATA_TTL


def test_qname_case_is_echoed(state):
    r = ask(state, f"PrObE.{ZONE.upper()}", "A")
    assert r["question"][0] == f"PrObE.{ZONE.upper()}."
    assert r["answers"][0][3] == RELAY_IP


def test_apex_ns_answer_uses_out_of_zone_names(state):
    r = ask(state, ZONE, "NS")
    names = sorted(rd for (_, _, _, rd) in r["answers"])
    assert names == ["ns1.auto.network.", "ns2.auto.network."]
    assert all(ttl == NS_TTL for (_, _, ttl, _) in r["answers"])
    assert r["additional"] == []      # out-of-zone: no glue to add
    # Compression: answer owner names point back at the question.
    reply = handle_query(build_query(ZONE, "NS"), state)
    assert b"\xc0\x0c" in reply


def test_apex_soa(state):
    r = ask(state, ZONE, "SOA")
    (_, _, ttl, soa) = r["answers"][0]
    assert soa["mname"] == "ns1.auto.network."
    assert soa["minimum"] == SOA_MINIMUM == 60
    assert soa["serial"] > 0


def test_challenge_txt_multi_value(state):
    r = ask(state, f"_acme-challenge.worker-aa.{ZONE}", "TXT")
    values = sorted(v for (_, _, _, txt) in r["answers"] for v in txt)
    assert values == ["apex-tok", "wild-tok"]
    assert all(ttl == DATA_TTL for (_, _, ttl, _) in r["answers"])


def test_chaos_id_server_reports_node_id(state):
    r = ask(state, "id.server", "TXT", qclass=CLASS_CH)
    assert r["answers"][0][3] == ["registry-ash-1"]


# -- negative answers (the resolver-behavior matrix) -----------------------


def _assert_nodata_with_soa(r):
    assert r["rcode"] == 0
    assert r["answers"] == []
    assert len(r["authority"]) == 1
    (name, rtype, ttl, soa) = r["authority"][0]
    assert rtype == QTYPE["SOA"] and name == ZONE + "."
    assert ttl == SOA_MINIMUM        # negative-cache TTL (RFC 2308)


def test_aaaa_is_nodata_never_an_error(state):
    _assert_nodata_with_soa(ask(state, ZONE, "AAAA"))
    _assert_nodata_with_soa(ask(state, f"x.{ZONE}", "AAAA"))


def test_qname_minimization_ns_probe_is_nodata(state):
    """Resolvers doing RFC 9156 minimization ask NS at empty
    non-terminals; REFUSED here would break the whole subtree."""
    _assert_nodata_with_soa(ask(state, f"worker-aa.{ZONE}", "NS"))


def test_unknown_challenge_txt_is_nodata(state):
    _assert_nodata_with_soa(
        ask(state, f"_acme-challenge.ghost.{ZONE}", "TXT"))


# -- refusals ---------------------------------------------------------------


def test_out_of_zone_is_refused(state):
    assert ask(state, "example.com", "A")["rcode"] == 5       # REFUSED
    assert ask(state, "auto.network", "A")["rcode"] == 5      # parent!


@pytest.mark.parametrize("qtype", ["ANY", "AXFR"])
def test_any_and_transfers_are_refused(state, qtype):
    assert ask(state, ZONE, qtype)["rcode"] == 5


# -- EDNS0 / truncation / transport ----------------------------------------


def test_edns_opt_is_echoed_with_our_payload_size(state):
    r = ask(state, ZONE, "A", edns_payload=4096)
    assert r["opt_payload"] == UDP_PAYLOAD


def _big_state():
    values = [f"tok-{i}-" + "x" * 240 for i in range(8)]
    return ZoneState(
        relay_ip=RELAY_IP, node_id="n",
        txt_lookup=lambda name: values,
    )


def test_oversize_udp_truncates_and_tcp_does_not():
    state = _big_state()
    name = f"_acme-challenge.worker-aa.{ZONE}"
    plain = parse_response(handle_query(build_query(name, "TXT"), state))
    assert plain["tc"] == 1 and plain["answers"] == []   # classic 512 limit
    edns = parse_response(handle_query(
        build_query(name, "TXT", edns_payload=4096), state))
    assert edns["tc"] == 0 and len(edns["answers"]) == 8
    tcp = parse_response(handle_query(
        build_query(name, "TXT"), state, tcp=True))
    assert tcp["tc"] == 0 and len(tcp["answers"]) == 8


# -- garbage ----------------------------------------------------------------


def test_malformed_and_non_queries_are_dropped(state):
    assert handle_query(b"\x00\x01\x02", state) is None
    assert handle_query(build_query(ZONE, "A", qr=1), state) is None


def test_challenge_txt_honors_per_name_ttl():
    state = ZoneState(
        relay_ip=RELAY_IP, node_id="n",
        txt_lookup=lambda name: ["tok"],
        txt_ttl=lambda name: 45,
    )
    r = ask(state, f"_acme-challenge.worker-aa.{ZONE}", "TXT")
    assert r["answers"][0][2] == 45
