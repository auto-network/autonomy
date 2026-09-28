"""Egress proxy for browser leases (auto-8c2df; design graph://c330323d-986 v7).

The browser network ``autonomy-browser`` is a Docker *internal* network: a
lease container has no route anywhere. Chrome reaches the internet only
through this HTTP forward proxy (``--proxy-server``), which runs in the
Compose service ``autonomy-browser-egress`` on the browser network and on the
separate egress network ``autonomy-browser-egress``, which nothing else joins.

Policy, in userland (there are no host firewall rules):

- requests are accepted only from the browser network's subnet;
- the proxy resolves each destination itself and connects only to an address
  it checked, so a DNS answer cannot swap in a private address afterwards;
- any address that is not globally routable is refused: private ranges,
  100.64/10 (CGNAT, the tailnet), link-local (cloud metadata), loopback,
  multicast and the IPv6 local ranges; IPv4 inside NAT64, 6to4 and Teredo
  addresses is judged as that IPv4 address;
- CONNECT goes to port 443 and plain requests to 80 only (plus
  EGRESS_EXTRA_PORTS), so a lease is not a general relay from the node's IP;
- at most 256 open connections per client.

A refusal is logged with the client address, the host and the reason, never
page content.
"""

from __future__ import annotations

import asyncio
import fcntl
import ipaddress
import logging
import os
import socket
import struct
import sys
from typing import Optional

logger = logging.getLogger("browser-egress")

PORT = 3128
HEADER_LIMIT = 16 * 1024
CONNECT_TIMEOUT_S = 10.0
HEADER_TIMEOUT_S = 30.0


# ── policy (pure; unit-tested) ─────────────────────────────────────────


def address_allowed(address: str) -> bool:
    """Only globally routable unicast addresses may be reached."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        # IPv4 carried inside IPv6 is judged as the IPv4 address it reaches.
        if ip in _NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        else:
            ip = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None) or ip
    return ip.is_global and not ip.is_multicast


_NAT64 = ipaddress.ip_network("64:ff9b::/96")
#: CONNECT goes to 443 and plain requests to 80, so a lease is not a general
#: TCP relay from the node's address (SMTP, SSH, ...). EGRESS_EXTRA_PORTS
#: (comma-separated, empty by default) adds ports for both.
CONNECT_PORTS = {443}
PLAIN_PORTS = {80}
MAX_CONNECTIONS_PER_CLIENT = 256


def port_allowed(method: str, port: int, extra: frozenset = frozenset()) -> bool:
    allowed = CONNECT_PORTS if method == "CONNECT" else PLAIN_PORTS
    return port in allowed or port in extra


def extra_ports(value: str) -> frozenset:
    return frozenset(int(p) for p in value.split(",") if p.strip().isdigit() and 0 < int(p) < 65536)


def parse_target(method: str, target: str) -> tuple[str, int, str]:
    """``(host, port, origin-form path)`` for CONNECT or an absolute http URL."""
    if method == "CONNECT":
        host, _, port = target.rpartition(":")
        host = host.strip("[]")
        if not host or not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError("CONNECT needs host:port")
        return host, int(port), ""
    from urllib.parse import urlsplit

    parts = urlsplit(target)
    if parts.scheme.lower() != "http" or not parts.hostname or parts.username or parts.password:
        raise ValueError("plain requests must use an absolute http:// URL")
    try:
        port = parts.port or 80
    except ValueError:
        raise ValueError("bad port") from None
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return parts.hostname, port, path


async def resolve_checked(host: str, port: int) -> tuple[Optional[str], str]:
    """The first allowed address for *host*, or ``(None, reason)``."""
    try:
        # IPv4 only: the egress network has no IPv6, so a global IPv6 answer
        # sorted first would fail a dual-stack site that IPv4 serves.
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except OSError:
        return None, "unresolvable"
    addresses = [info[4][0] for info in infos]
    for address in addresses:
        if address_allowed(address):
            return address, ""
    return None, "private-destination" if addresses else "unresolvable"


# ── serving ────────────────────────────────────────────────────────────


class Proxy:
    def __init__(self, allowed_clients: ipaddress._BaseNetwork, extra: frozenset = frozenset()):
        self.allowed_clients = allowed_clients
        self.extra = extra
        self.open: dict[str, int] = {}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = (writer.get_extra_info("peername") or ("?",))[0]
        if ipaddress.ip_address(peer) not in self.allowed_clients:
            writer.close()
            return
        if self.open.get(peer, 0) >= MAX_CONNECTIONS_PER_CLIENT:
            await self._refuse(writer, peer, "?", "too-many-connections", 503)
            writer.close()
            return
        self.open[peer] = self.open.get(peer, 0) + 1
        try:
            await self._serve(reader, writer, peer)
        finally:
            self.open[peer] -= 1
            if not self.open[peer]:
                del self.open[peer]

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer: str) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEADER_TIMEOUT_S)
            if len(head) > HEADER_LIMIT:
                return await self._refuse(writer, peer, "?", "headers-too-large", 431)
            lines = head.decode("latin-1").split("\r\n")
            method, target, version = (lines[0].split(" ") + ["", "", ""])[:3]
            try:
                host, port, path = parse_target(method.upper(), target)
            except ValueError as exc:
                return await self._refuse(writer, peer, target[:80], str(exc), 400)
            if not port_allowed(method.upper(), port, self.extra):
                return await self._refuse(writer, peer, f"{host}:{port}", "port", 403)
            address, reason = await resolve_checked(host, port)
            if address is None:
                return await self._refuse(writer, peer, host, reason, 403)
            try:
                up_reader, up_writer = await asyncio.wait_for(
                    asyncio.open_connection(address, port), CONNECT_TIMEOUT_S)
            except (OSError, asyncio.TimeoutError):
                return await self._refuse(writer, peer, host, "connect-failed", 502)
            if method.upper() == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                headers = [line for line in lines[1:] if line and not
                           line.lower().startswith(("proxy-", "connection:", "keep-alive:"))]
                request = f"{method} {path} {version or 'HTTP/1.1'}\r\n" + \
                    "".join(f"{h}\r\n" for h in headers) + "Connection: close\r\n\r\n"
                up_writer.write(request.encode("latin-1"))
                await up_writer.drain()
            await _pipe_both(reader, writer, up_reader, up_writer)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError,
                ConnectionError):
            pass
        finally:
            writer.close()

    async def _refuse(self, writer, peer: str, host: str, reason: str, status: int) -> None:
        logger.warning("egress refused client=%s host=%s reason=%s", peer, host, reason)
        writer.write(f"HTTP/1.1 {status} Refused\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                     .encode())
        await writer.drain()


IDLE_TIMEOUT_S = 300.0


async def _pipe_both(client_reader, client_writer, up_reader, up_writer) -> None:
    async def pipe(src, dst):
        # A stalled peer must not hold a task and two descriptors forever.
        try:
            while data := await asyncio.wait_for(src.read(65536), IDLE_TIMEOUT_S):
                dst.write(data)
                await dst.drain()
        except (ConnectionError, asyncio.CancelledError, asyncio.TimeoutError):
            pass
        finally:
            dst.close()

    await asyncio.gather(pipe(client_reader, up_writer), pipe(up_reader, client_writer))


# ── startup ────────────────────────────────────────────────────────────


def interface_addresses() -> list[str]:
    """IPv4 addresses of this container's interfaces (SIOCGIFADDR)."""
    out = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _, name in socket.if_nameindex():
            try:
                raw = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack("256s", name[:15].encode()))
                out.append(socket.inet_ntoa(raw[20:24]))
            except OSError:
                continue
    return out


def address_in(subnet: ipaddress._BaseNetwork, addresses: list[str]) -> Optional[str]:
    return next((a for a in addresses if ipaddress.ip_address(a) in subnet), None)


async def serve(subnet: ipaddress._BaseNetwork) -> None:
    listen = address_in(subnet, interface_addresses())
    if listen is None:
        raise SystemExit(f"browser-egress: no interface on the browser network {subnet}")
    extra = extra_ports(os.environ.get("EGRESS_EXTRA_PORTS", ""))
    server = await asyncio.start_server(Proxy(subnet, extra).handle, listen, PORT, limit=HEADER_LIMIT)
    logger.info("browser-egress: listening on %s:%d for %s", listen, PORT, subnet)
    async with server:
        await server.serve_forever()


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    subnet = ipaddress.ip_network(os.environ["BROWSER_SUBNET"])
    asyncio.run(serve(subnet))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
