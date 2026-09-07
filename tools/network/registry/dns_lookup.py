"""Minimal stdlib DNS lookups for zone-binding verification (no dnspython).

Asks public recursive resolvers for NS and TXT records over UDP, retrying
over TCP when the answer is truncated. Enough to answer two questions: which
name servers does the parent zone delegate ``<zone>`` to, and what TXT
strings sit at ``_autonomy.<parent>``. Never used on the query path.
"""
from __future__ import annotations

import random
import socket
import struct

PUBLIC_RESOLVERS = ("1.1.1.1", "8.8.8.8", "9.9.9.9")
TYPE_NS, TYPE_TXT = 2, 16
_TIMEOUT = 3.0


class LookupError_(Exception):
    """No resolver answered usefully."""


def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("idna") if label else b""
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def _decode_name(msg: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    jumped = False
    end = offset
    hops = 0
    while True:
        length = msg[offset]
        if length & 0xC0 == 0xC0:
            pointer = struct.unpack(">H", msg[offset:offset + 2])[0] & 0x3FFF
            if not jumped:
                end = offset + 2
            jumped = True
            offset = pointer
            hops += 1
            if hops > 64:
                raise ValueError("compression loop")
            continue
        offset += 1
        if length == 0:
            if not jumped:
                end = offset
            break
        labels.append(msg[offset:offset + length].decode("ascii", "replace"))
        offset += length
    return ".".join(labels).lower(), end


def _build_query(name: str, qtype: int, *, recursive: bool = True) -> tuple[bytes, int]:
    qid = random.randint(0, 0xFFFF)
    header = struct.pack(">HHHHHH", qid, 0x0100 if recursive else 0x0000, 1, 0, 0, 0)
    return header + _encode_name(name) + struct.pack(">HH", qtype, 1), qid


def _parse(msg: bytes, qid: int, qtype: int) -> list[str]:
    rid, flags, qd, an, ns, _ar = struct.unpack(">HHHHHH", msg[:12])
    if rid != qid:
        raise ValueError("id mismatch")
    if flags & 0x0200:
        raise ValueError("truncated")
    rcode = flags & 0x0F
    if rcode == 3:
        return []  # NXDOMAIN
    if rcode != 0:
        raise ValueError(f"rcode {rcode}")
    offset = 12
    for _ in range(qd):
        _, offset = _decode_name(msg, offset)
        offset += 4
    values: list[str] = []
    # A parent's non-recursive answer for a delegated name is a REFERRAL: the
    # NS set sits in the authority section with an empty answer section.
    for _section, count in (("answer", an), ("authority", ns)):
        for _ in range(count):
            _, offset = _decode_name(msg, offset)
            rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", msg[offset:offset + 10])
            offset += 10
            rdata = msg[offset:offset + rdlen]
            if rtype == qtype == TYPE_NS:
                values.append(_decode_name(msg, offset)[0])
            elif rtype == qtype == TYPE_TXT:
                pos, parts = 0, []
                while pos < len(rdata):
                    n = rdata[pos]
                    parts.append(rdata[pos + 1:pos + 1 + n].decode("utf-8", "replace"))
                    pos += 1 + n
                values.append("".join(parts))
            offset += rdlen
        if values and _section == "answer":
            break
    return values


def _exchange_udp(server: str, query: bytes) -> bytes:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(_TIMEOUT)
        sock.sendto(query, (server, 53))
        return sock.recv(4096)


def _exchange_tcp(server: str, query: bytes) -> bytes:
    with socket.create_connection((server, 53), timeout=_TIMEOUT) as sock:
        sock.sendall(struct.pack(">H", len(query)) + query)
        head = sock.recv(2)
        (length,) = struct.unpack(">H", head)
        data = b""
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                break
            data += chunk
        return data


def query(name: str, rtype: str, *, servers=PUBLIC_RESOLVERS, recursive: bool = True) -> list[str]:
    """Return NS target names (lowercase, no trailing dot) or TXT strings.

    Raises :class:`LookupError_` when no resolver answers; an authoritative
    empty answer (NODATA/NXDOMAIN) is ``[]``.
    """
    qtype = {"NS": TYPE_NS, "TXT": TYPE_TXT}[rtype.upper()]
    qname = name.rstrip(".").lower()
    last: Exception | None = None
    for server in servers:
        try:
            raw, qid = _build_query(qname, qtype, recursive=recursive)
            try:
                return _parse(_exchange_udp(server, raw), qid, qtype)
            except ValueError as exc:
                if "truncated" not in str(exc):
                    raise
                return _parse(_exchange_tcp(server, raw), qid, qtype)
        except (OSError, ValueError) as exc:
            last = exc
            continue
    raise LookupError_(f"no resolver answered {rtype} {qname}: {last}")


def delegation_ns(zone: str, *, resolvers=PUBLIC_RESOLVERS) -> list[str]:
    """The NS names the PARENT delegates ``zone`` to, read from the parent's
    own authoritative servers (a referral). Asking a recursive resolver for
    the zone's NS would go to the child servers, which may not answer yet.
    """
    labels = zone.rstrip(".").lower().split(".")
    if len(labels) < 3:
        raise LookupError_("a delegated zone needs at least three labels")
    parent = ".".join(labels[1:])
    parent_ns = query(parent, "NS", servers=resolvers)
    if not parent_ns:
        raise LookupError_(f"no NS for parent {parent}")
    addresses: list[str] = []
    for ns in parent_ns:
        try:
            addresses.append(socket.gethostbyname(ns))
        except OSError:
            continue
    if not addresses:
        raise LookupError_(f"parent name servers of {parent} did not resolve")
    return query(zone, "NS", servers=tuple(addresses), recursive=False)
