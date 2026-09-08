"""Machine-local configuration of the direct fleet-sync tier.

Reads and writes the ``autonomy.machine.fleet-direct`` row (see the schema
module for why it exists). The environment variable
``AUTONOMY_FLEET_ADVERTISE_ADDRS`` that the advertise list originally came
from is still honored and unioned in, so an existing deployment keeps
working; the Settings row is the durable, operator-visible form.

Multi-homed machines (operator ruling 2026-09-06): a machine "wouldn't
necessarily know" which of its interfaces a peer can reach, so with
``advertise_auto`` (the default once a port is set) it advertises every
detected non-loopback IPv4 address at the listen port -- tailnet first,
then private LAN, then the rest -- and the peer tries the candidates in
order. Announcing an address grants nothing; the roster handshake does.

Containers: a dashboard running inside a compose container sees only the
container's bridge address (live 2026-09-06: auto-detect found 172.16.0.2
on both home and SJC), so the host's tailnet address must be given
explicitly in ``advertise_addrs`` and the compose port mapping must bind
it. Auto-detect still adds the bridge, which is harmless: a peer that
cannot reach it fails that candidate in 3s and tries the next.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
import subprocess
from dataclasses import dataclass, field

from tools.graph import settings_ops
from tools.graph.schemas.fleet_direct import (
    FLEET_DIRECT_KEY,
    FLEET_DIRECT_REVISION,
    FLEET_DIRECT_SET_ID,
    MAX_ADVERTISE_ADDRS,
    FleetDirectV1,
)

DEFAULT_LISTEN_HOST = "127.0.0.1"
ADVERTISE_ENV = "AUTONOMY_FLEET_ADVERTISE_ADDRS"

#: Tailscale's CGNAT range; a machine with an address here is on a tailnet.
TAILNET = ipaddress.ip_network("100.64.0.0/10")
#: Tailscale MagicDNS resolver: connecting a UDP socket toward it reveals
#: the local tailnet address on any host that routes the tailnet, without
#: sending a packet.
TAILNET_PROBE = ("100.100.100.100", 53)
DEFAULT_ROUTE_PROBE = ("8.8.8.8", 53)


@dataclass(frozen=True)
class FleetDirectConfig:
    listen_host: str = DEFAULT_LISTEN_HOST
    listen_port: int = 0
    advertise_addrs: tuple[str, ...] = field(default_factory=tuple)
    advertise_auto: bool = True
    #: Which process binds the listener. The connector subprocess by default:
    #: a direct serve builds and streams gigabytes, and doing that on the
    #: dashboard's event loop made the operator's UI unreachable for minutes
    #: (2026-09-06). The dashboard process only ever installs.
    serve_in: str = "connector"
    #: Whether this machine's dashboard pulls over direct at all.
    pull_direct: bool = True

    @property
    def enabled(self) -> bool:
        """A fixed port on a non-loopback bind is what peers can dial."""
        return self.listen_port > 0 and self.listen_host not in (
            "127.0.0.1", "localhost", "::1",
        )


def _env_advertise_addrs() -> list[str]:
    raw = os.environ.get(ADVERTISE_ENV, "")
    return [u.strip() for u in raw.split(",") if u.strip()]


# ── interface detection ───────────────────────────────────────────────


def _probe_local_address(target) -> str | None:
    """The local IPv4 the kernel would route toward *target* (no packet sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(target)
            return sock.getsockname()[0]
    except OSError:
        return None


def _tailscale_addresses() -> list[str]:
    binary = shutil.which("tailscale")
    if not binary:
        return []
    try:
        out = subprocess.run(
            [binary, "ip", "-4"], capture_output=True, text=True, timeout=3.0,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


#: Interface-name prefixes whose addresses no peer off this host can dial.
_UNDIALABLE_IFNAME_PREFIXES = ("docker", "br-", "veth", "virbr", "lxc", "lxd", "cni", "flannel")


def _ip_command_addresses() -> list[str]:
    binary = shutil.which("ip")
    if not binary:
        return []
    try:
        out = subprocess.run(
            [binary, "-j", "-4", "addr", "show"],
            capture_output=True, text=True, timeout=3.0,
        )
        links = json.loads(out.stdout) if out.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    found = []
    for link in links if isinstance(links, list) else []:
        if not isinstance(link, dict):
            continue
        # Container bridges and virtual NICs (docker0, br-*, veth*, virbr*)
        # are addresses only this host's containers can reach; announcing
        # them made every peer burn a connect attempt on ws://172.16.0.2
        # (auto-8dw0w, 2026-09-07).
        ifname = str(link.get("ifname") or "")
        if ifname.startswith(_UNDIALABLE_IFNAME_PREFIXES):
            continue
        for info in (link.get("addr_info") or []):
            local = info.get("local") if isinstance(info, dict) else None
            if isinstance(local, str):
                found.append(local)
    return found


def _hostname_addresses() -> list[str]:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    return [info[4][0] for info in infos]


def _detect_ipv4_addresses() -> list[str]:
    """Every candidate local IPv4, best sources first, duplicates kept
    (ranking dedups). Overridable in tests."""
    found: list[str] = []
    found += _tailscale_addresses()
    for probe in (TAILNET_PROBE, DEFAULT_ROUTE_PROBE):
        addr = _probe_local_address(probe)
        if addr:
            found.append(addr)
    found += _ip_command_addresses()
    found += _hostname_addresses()
    return found


PATH_CLASSES = ("tailnet", "private", "public", "loopback", "named", "relay", "turn")


def path_class(url_or_addr: str | None) -> str | None:
    """Classify a dialed/announced address for the tier-used readout.

    ``ws://100.x:9410`` -> tailnet, RFC1918 -> private, other IPs ->
    public, 127/::1 -> loopback, a DNS name -> named (a relay's host is
    classified by its caller as "relay"). None for garbage."""
    if not isinstance(url_or_addr, str) or not url_or_addr:
        return None
    host = url_or_addr
    if "://" in host:
        from urllib.parse import urlsplit

        try:
            host = urlsplit(host).hostname or ""
        except ValueError:
            return None
    if not host:
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "named"
    if ip.is_loopback:
        return "loopback"
    if ip.version == 4 and ip in TAILNET:
        return "tailnet"
    if ip.is_private:
        return "private"
    return "public"


def _rank(addr: str) -> int:
    ip = ipaddress.ip_address(addr)
    if ip in TAILNET:
        return 0
    if ip.is_private:
        return 1
    return 2


def candidate_addresses(port: int, *, detect=None) -> list[str]:
    """``ws://<ip>:<port>`` for each usable detected IPv4, tailnet first."""
    if port <= 0:
        return []
    raw = (detect or _detect_ipv4_addresses)()
    usable: list[str] = []
    for addr in raw:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.version != 4 or ip.is_loopback or ip.is_link_local \
                or ip.is_unspecified or ip.is_multicast:
            continue
        text = str(ip)
        if text not in usable:
            usable.append(text)
    usable.sort(key=_rank)
    return [f"ws://{ip}:{port}" for ip in usable]


# ── the row ───────────────────────────────────────────────────────────


def load(*, org: str = "machine", detect=None) -> FleetDirectConfig:
    """The effective direct-tier configuration (row + auto candidates + env).

    Never raises: a missing or unreadable row yields the defaults, so the
    sync runtime always activates; a malformed row is treated as absent.
    """
    payload: dict = {}
    try:
        members = settings_ops.read_owned_set(
            FLEET_DIRECT_SET_ID,
            org=org,
            target_revision=FLEET_DIRECT_REVISION,
        ).to_dict()
        member = members.get(FLEET_DIRECT_KEY)
        if member is not None:
            FleetDirectV1.validate(member.payload)
            payload = dict(member.payload)
    except Exception:
        payload = {}
    listen_host = payload.get("listen_host") or DEFAULT_LISTEN_HOST
    listen_port = int(payload.get("listen_port") or 0)
    auto = payload.get("advertise_auto")
    advertise_auto = True if auto is None else bool(auto)
    serve_in = payload.get("serve_in") or "connector"
    pull = payload.get("pull_direct")
    pull_direct = True if pull is None else bool(pull)
    addrs: list[str] = []
    sources = list(payload.get("advertise_addrs") or [])
    if advertise_auto and listen_port > 0 and listen_host != DEFAULT_LISTEN_HOST:
        sources += candidate_addresses(listen_port, detect=detect)
    sources += _env_advertise_addrs()
    for addr in sources:
        if addr not in addrs:
            addrs.append(addr)
    return FleetDirectConfig(
        listen_host=listen_host,
        listen_port=listen_port,
        advertise_addrs=tuple(addrs[:MAX_ADVERTISE_ADDRS]),
        advertise_auto=advertise_auto,
        serve_in=serve_in,
        pull_direct=pull_direct,
    )


def store(config: FleetDirectConfig, *, org: str = "machine") -> None:
    payload = {
        "listen_host": config.listen_host,
        "listen_port": int(config.listen_port),
        "advertise_addrs": list(config.advertise_addrs),
        "advertise_auto": bool(config.advertise_auto),
        "serve_in": config.serve_in,
        "pull_direct": bool(config.pull_direct),
    }
    FleetDirectV1.validate(payload)
    settings_ops.upsert_by_key(
        FLEET_DIRECT_SET_ID,
        FLEET_DIRECT_REVISION,
        FLEET_DIRECT_KEY,
        payload,
        org=org,
    )


def listener_bind(config: FleetDirectConfig, role: str) -> tuple[str, int]:
    """(host, port) the process in *role* ('connector'/'dashboard') should
    bind: the configured bind when it hosts the listener, else the
    historical loopback-ephemeral one nobody can dial."""
    if config.enabled and config.serve_in == role:
        return config.listen_host, config.listen_port
    return DEFAULT_LISTEN_HOST, 0


def advertise_addrs(*, org: str = "machine") -> list[str]:
    """Fresh read of the advertised URLs, for announce-time getters."""
    return list(load(org=org).advertise_addrs)
