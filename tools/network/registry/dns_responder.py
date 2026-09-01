"""Authoritative DNS responder for serve.auto.network (auto-g1jxw).

The DNS server IS the registry: answers derive from registry state, not
a zone file. This module is the pure query→response function; transport,
rate limiting, and state fetch live in ``dns_server.py`` (its own
process on the relay host — one system, two crash domains).

The resolver-behavior matrix this encodes (each item is a test):
EDNS0 OPT echoed with our payload size and client-size-bounded
truncation; QNAME-minimization NS probes at empty non-terminals answer
NOERROR/NODATA + SOA, never REFUSED; AAAA and any unimplemented qtype
for an existing name answer NODATA + SOA, never an error; negative
answers carry the SOA with the MINIMUM (60 s) as the negative-cache TTL
— the namespace grows as hosts register, so negatives must die fast;
qname case is echoed (0x20 tolerance); RA is always 0; ANY and zone
transfers are REFUSED; CHAOS TXT id.server reports the node id (the
per-PoP identifier future anycast catchment measurement needs).

Answer policy today: every in-zone A answers the relay IP (60 s TTL) —
the ``answer_a`` seam on :class:`ZoneState` is where tunnel-aware
per-hostname answers (the lease table) plug in later. DNSSEC is
explicitly unsigned; answers are computed at query time, so signing, if
ever, is online signing by construction.
"""

from __future__ import annotations

import struct
import time

ZONE = "serve.auto.network"
NS_NAMES = ("ns1.auto.network.", "ns2.auto.network.")
SOA_MNAME = "ns1.auto.network."
SOA_RNAME = "hostmaster.auto.network."

DATA_TTL = 60
NS_TTL = 3600
SOA_MINIMUM = 60
SOA_REFRESH, SOA_RETRY, SOA_EXPIRE = 300, 60, 604800
#: What WE can receive over UDP (advertised in our OPT).
UDP_PAYLOAD = 1232
#: Ceiling on how large a UDP reply we send even to a generous client.
MAX_UDP_REPLY = 4096
CLASSIC_UDP_LIMIT = 512

_TYPE_A, _TYPE_NS, _TYPE_SOA, _TYPE_TXT = 1, 2, 6, 16
_TYPE_AAAA, _TYPE_OPT = 28, 41
_TYPE_IXFR, _TYPE_AXFR, _TYPE_ANY = 251, 252, 255
_CLASS_IN, _CLASS_CH = 1, 3
_RCODE_FORMERR, _RCODE_NOTIMP, _RCODE_REFUSED = 1, 4, 5

_CHALLENGE_PREFIX = "_acme-challenge."


class ZoneState:
    """Everything the responder answers from — injected, never global."""

    def __init__(self, *, relay_ip: str, node_id: str = "",
                 txt_lookup=None, answer_a=None):
        self.relay_ip = relay_ip
        self.node_id = node_id
        #: fqdn (lowercase, trailing dot) -> list of TXT strings
        self.txt_lookup = txt_lookup or (lambda name: [])
        #: qname (lowercase, no trailing dot) -> IPv4 str | None.
        #: None → the constant relay IP. The tunnel-aware seam.
        self.answer_a = answer_a


def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("ascii")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def _rr(owner: bytes, rtype: int, rclass: int, ttl: int,
        rdata: bytes) -> bytes:
    return owner + struct.pack(">HHIH", rtype, rclass, ttl,
                               len(rdata)) + rdata


_QPTR = b"\xc0\x0c"  # compression pointer to the question name at offset 12


def _soa_rdata(now: int) -> bytes:
    serial = max(1, now // 60)
    return (
        _encode_name(SOA_MNAME)
        + _encode_name(SOA_RNAME)
        + struct.pack(">IIIII", serial, SOA_REFRESH, SOA_RETRY,
                      SOA_EXPIRE, SOA_MINIMUM)
    )


def _txt_rdata(value: str) -> bytes:
    raw = value.encode("utf-8")
    return bytes([len(raw)]) + raw


def _parse_question(raw: bytes) -> tuple[bytes, str, int, int, int] | None:
    """(qname_wire, qname_lower, qtype, qclass, end_offset) or None."""
    off, labels = 12, []
    while True:
        if off >= len(raw):
            return None
        length = raw[off]
        if length & 0xC0:
            return None  # compression in a question: refuse to guess
        if length == 0:
            off += 1
            break
        if off + 1 + length > len(raw):
            return None
        labels.append(raw[off + 1:off + 1 + length])
        off += 1 + length
    if off + 4 > len(raw):
        return None
    qtype, qclass = struct.unpack(">HH", raw[off:off + 4])
    qname_wire = raw[12:off]
    qname_lower = b".".join(labels).decode("ascii", "replace").lower()
    return qname_wire, qname_lower, qtype, qclass, off + 4


def _client_opt_payload(raw: bytes, off: int, counts) -> int | None:
    """Scan the remaining records for a root-name OPT; returns payload."""
    qd_extra, an, ns, ar = counts
    # Skip any unexpected extra questions defensively.
    total = an + ns + ar
    for _ in range(total):
        if off >= len(raw):
            return None
        # owner name (possibly compressed)
        while True:
            if off >= len(raw):
                return None
            length = raw[off]
            if length & 0xC0 == 0xC0:
                off += 2
                break
            off += 1
            if length == 0:
                break
            off += length
        if off + 10 > len(raw):
            return None
        rtype, rclass, _ttl, rdlen = struct.unpack(
            ">HHIH", raw[off:off + 10])
        off += 10 + rdlen
        if rtype == _TYPE_OPT:
            return rclass
    return None


def handle_query(raw: bytes, state: ZoneState, *, tcp: bool = False,
                 now_fn=time.time) -> bytes | None:
    if len(raw) < 12:
        return None
    qid, flags, qdcount, ancount, nscount, arcount = struct.unpack(
        ">HHHHHH", raw[:12])
    if flags >> 15 & 1:
        return None  # a response, not a query
    opcode = flags >> 11 & 0xF
    rd = flags >> 8 & 1

    def reply(rcode: int, *, aa: bool = False, tc: bool = False,
              question: bytes = b"", answers: list[bytes] = (),
              authority: list[bytes] = (), opt: bool = False) -> bytes:
        rflags = (1 << 15) | (opcode << 11) | (int(aa) << 10) \
            | (int(tc) << 9) | (rd << 8) | rcode
        additional = [_rr(b"\x00", _TYPE_OPT, UDP_PAYLOAD, 0, b"")] \
            if opt else []
        msg = struct.pack(
            ">HHHHHH", qid, rflags, 1 if question else 0,
            len(answers), len(authority), len(additional),
        )
        return msg + question + b"".join(answers) + b"".join(authority) \
            + b"".join(additional)

    if qdcount != 1:
        return reply(_RCODE_FORMERR)
    parsed = _parse_question(raw)
    if parsed is None:
        return reply(_RCODE_FORMERR)
    qname_wire, qname, qtype, qclass, off = parsed
    question = qname_wire + struct.pack(">HH", qtype, qclass)
    client_payload = _client_opt_payload(
        raw, off, (0, ancount, nscount, arcount))
    opt = client_payload is not None

    def finish(rcode, *, aa=False, answers=(), authority=()):
        full = reply(rcode, aa=aa, question=question,
                     answers=list(answers), authority=list(authority),
                     opt=opt)
        if tcp:
            return full
        limit = CLASSIC_UDP_LIMIT if not opt else max(
            CLASSIC_UDP_LIMIT, min(client_payload, MAX_UDP_REPLY))
        if len(full) <= limit:
            return full
        return reply(rcode, aa=aa, tc=True, question=question, opt=opt)

    if opcode != 0:
        return finish(_RCODE_NOTIMP)

    # CHAOS id.server — the per-PoP identifier.
    if qclass == _CLASS_CH:
        if qtype == _TYPE_TXT and qname in ("id.server", "hostname.bind") \
                and state.node_id:
            answer = _rr(_QPTR, _TYPE_TXT, _CLASS_CH, 0,
                         _txt_rdata(state.node_id))
            return finish(0, aa=True, answers=[answer])
        return finish(_RCODE_REFUSED)
    if qclass != _CLASS_IN:
        return finish(_RCODE_REFUSED)

    in_zone = qname == ZONE or qname.endswith("." + ZONE)
    if not in_zone:
        return finish(_RCODE_REFUSED)
    if qtype in (_TYPE_ANY, _TYPE_AXFR, _TYPE_IXFR):
        return finish(_RCODE_REFUSED)

    now = int(now_fn())
    soa_authority = _rr(_encode_name(ZONE + "."), _TYPE_SOA, _CLASS_IN,
                        SOA_MINIMUM, _soa_rdata(now))

    def nodata():
        return finish(0, aa=True, authority=[soa_authority])

    if qtype == _TYPE_A:
        ip = None
        if state.answer_a is not None:
            ip = state.answer_a(qname)
        if ip is None:
            ip = state.relay_ip
        rdata = bytes(int(part) for part in ip.split("."))
        return finish(0, aa=True, answers=[
            _rr(_QPTR, _TYPE_A, _CLASS_IN, DATA_TTL, rdata)])

    if qtype == _TYPE_NS:
        if qname != ZONE:
            return nodata()  # QNAME-minimization probe: NODATA, not REFUSED
        answers = [
            _rr(_QPTR, _TYPE_NS, _CLASS_IN, NS_TTL, _encode_name(ns))
            for ns in NS_NAMES
        ]
        return finish(0, aa=True, answers=answers)

    if qtype == _TYPE_SOA:
        if qname != ZONE:
            return nodata()
        return finish(0, aa=True, answers=[
            _rr(_QPTR, _TYPE_SOA, _CLASS_IN, NS_TTL, _soa_rdata(now))])

    if qtype == _TYPE_TXT:
        if qname.startswith(_CHALLENGE_PREFIX):
            values = state.txt_lookup(qname + ".")
            if values:
                answers = [
                    _rr(_QPTR, _TYPE_TXT, _CLASS_IN, DATA_TTL,
                        _txt_rdata(value))
                    for value in values
                ]
                return finish(0, aa=True, answers=answers)
        return nodata()

    # AAAA and every other qtype for an existing name: NODATA, never an
    # error — resolvers penalize servers that SERVFAIL on AAAA.
    return nodata()
