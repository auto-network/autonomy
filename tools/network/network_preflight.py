"""Network preflight — pick a Compose subnet that won't eat the operator's LAN.

Built 2026-08-30 after bringing up the Compose stack on this machine
blackholed the operator's home network for hours. ``docker-compose.yml``
declared no top-level ``networks:`` block, so the daemon allocated the project
network from its built-in pools: ``172.17.0.0/12`` carved into /16s, then
``192.168.0.0/16`` carved into /20s. With ``172.17``–``172.23`` held by other
bridges and ``172.24.0.0/20`` by WSL's own ``eth0``, allocation fell through to
the second pool and took ``192.168.0.0/20`` — which contains ``192.168.1.0/24``,
the most common home LAN there is. Traffic to the router routed into the bridge
instead, and nothing named the cause.

This is a READ-ONLY diagnostic, the same shape as ``fleet_doctor``: it reads
live state (the host routing table, the subnets Docker already holds, the
daemon's configured pools) and reports findings for an agent to act on. It
creates no network, needs no sudo, and never restarts the daemon.

Run it:
    python3 -m tools.network.network_preflight
    python3 -m tools.network.network_preflight --json
    python3 -m tools.network.network_preflight --env        # AUTONOMY_SUBNET=<cidr>
    python3 -m tools.network.network_preflight --print-subnet

From those reads it reports: the machine's own networks; the pools in effect
and whether they are configured or the built-in defaults; every subnet Docker
has already allocated; whether any pool in effect overlaps a host route; and a
subnet the operator can safely pin.

The chosen subnet is picked by enumerating ``172.16.0.0/12`` in /24 blocks,
discarding any that overlap something the host currently routes, and taking the
first that survives. Deterministic on the same inputs, and it needs no model of
Docker's allocator. It deliberately does NOT try to predict which block Docker
would pick next — that would require modelling pool order, skip-on-conflict, and
the block carve, and the observed behaviour is not yet explained (allocation
skipped ``172.25``–``172.31``, which were free, and jumped to the second pool
anyway). Picking a block disjoint from every current route is a strictly weaker,
verifiable claim: a set difference over CIDRs, re-checkable by re-reading the
routing table after the stack is up.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import subprocess
import sys
from typing import Iterable

# The address space this tool pins into, and the block size it carves. RFC 1918
# private space that Docker's FIRST built-in pool (172.17.0.0/12 in /16s) draws
# from too — but we hand out /24s, and we avoid everything already routed, so a
# chosen block is disjoint from any bridge the daemon has already created. A /24
# also caps the blast radius of any future collision at 256 addresses.
DEFAULT_SPACE = "172.16.0.0/12"
DEFAULT_PREFIX = 24

# Docker's built-in default address pools, in force whenever the daemon has no
# configured ``default-address-pools`` (docker info reports them as null). These
# are the exact pools that produced the 192.168.0.0/20 grab described above.
BUILTIN_POOLS = (
    {"base": "172.17.0.0/12", "size": 16},
    {"base": "192.168.0.0/16", "size": 20},
)


# ── text rendering (mirrors fleet_doctor) ────────────────────────────────

_QUIET = False  # set True for --json: report is still collected, only printing is suppressed


def _section(title: str) -> None:
    if not _QUIET:
        print(f"\n== {title} ==")


def _line(label: str, value, *, warn: bool = False, fail: bool = False) -> None:
    if _QUIET:
        return
    mark = "FAIL" if fail else ("WARN" if warn else "ok  ")
    print(f"  [{mark}] {label}: {value}")


def _detail(text: str) -> None:
    if not _QUIET:
        print(text)


# ── pure CIDR logic (fixture-testable, no host access) ───────────────────

def _ipv4_networks(cidrs: Iterable[str]) -> list[ipaddress.IPv4Network]:
    """Parse an iterable of CIDR/host strings into deduplicated IPv4 networks.

    Non-IPv4 entries (IPv6, junk) are dropped rather than raised on: this is fed
    real routing-table text, and an IPv6 route can never overlap a v4 candidate
    anyway (ipaddress refuses cross-version .overlaps()). Host bits are tolerated
    (strict=False) so ``192.168.1.5/24`` is read as the network ``192.168.1.0/24``.
    """
    out: dict[str, ipaddress.IPv4Network] = {}
    for raw in cidrs:
        if not raw:
            continue
        text = raw.strip()
        if "/" not in text:
            text += "/32"
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError:
            continue
        if isinstance(net, ipaddress.IPv4Network):
            out[str(net)] = net
    return list(out.values())


def choose_subnet(
    avoid_cidrs: Iterable[str],
    *,
    space: str = DEFAULT_SPACE,
    prefix: int = DEFAULT_PREFIX,
) -> str | None:
    """First /``prefix`` block of ``space`` that overlaps nothing in ``avoid_cidrs``.

    Enumerates the blocks in ascending address order (ipaddress.subnets() is
    ordered), discards any that overlap a current route/allocation, and returns
    the first survivor as a CIDR string. Deterministic on the same inputs;
    returns None only in the impossible-in-practice case that every block in
    ``space`` is taken.
    """
    avoid = _ipv4_networks(avoid_cidrs)
    container = ipaddress.ip_network(space, strict=False)
    for block in container.subnets(new_prefix=prefix):
        if not any(block.overlaps(a) for a in avoid):
            return str(block)
    return None


def overlapping_pairs(
    pools: Iterable[dict],
    cidrs: Iterable[str],
) -> list[tuple[str, str]]:
    """(pool_base, host_cidr) pairs where a pool in effect overlaps a host route.

    A pool is ``{"base": "192.168.0.0/16", "size": 20}``; only its base network
    matters for overlap. This is the check that would have caught the incident:
    the built-in ``192.168.0.0/16`` pool overlaps a ``192.168.1.0/24`` LAN, so
    any block the daemon carves from it can land on the operator's own network.
    """
    hosts = _ipv4_networks(cidrs)
    hits: list[tuple[str, str]] = []
    for pool in pools:
        base = pool.get("base")
        if not base:
            continue
        try:
            base_net = ipaddress.ip_network(base, strict=False)
        except ValueError:
            continue
        if not isinstance(base_net, ipaddress.IPv4Network):
            continue
        for host in hosts:
            if base_net.overlaps(host):
                hits.append((str(base_net), str(host)))
    return hits


def pools_in_effect(configured: object) -> tuple[list[dict], bool]:
    """Resolve the address pools actually in force, and whether they're built-in.

    ``configured`` is whatever ``docker info`` reported for DefaultAddressPools:
    ``None`` (the JSON ``null``) or ``[]`` means no pools are configured, so the
    daemon's built-in defaults are in force. Returns (pools, is_builtin).
    """
    if not configured:
        return [dict(p) for p in BUILTIN_POOLS], True
    normalized: list[dict] = []
    for entry in configured:  # type: ignore[union-attr]
        if not isinstance(entry, dict):
            continue
        base = entry.get("Base") or entry.get("base")
        size = entry.get("Size", entry.get("size"))
        if base:
            normalized.append({"base": base, "size": size})
    if not normalized:
        return [dict(p) for p in BUILTIN_POOLS], True
    return normalized, False


# ── parsers for live command output (fixture-testable) ───────────────────

_ROUTE_KEYWORDS = {"unreachable", "blackhole", "prohibit", "throw"}


def parse_ip_route(text: str) -> list[str]:
    """Destination CIDRs from ``ip route`` output.

    Each line's destination is its first field, except ``default`` (skipped) and
    the reject keywords (unreachable/blackhole/...), where the destination is the
    second field. Bare host IPs become /32. Non-parseable destinations are
    dropped.
    """
    out: list[str] = []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        head = parts[0]
        if head == "default":
            continue
        if head in _ROUTE_KEYWORDS:
            if len(parts) < 2:
                continue
            dest = parts[1]
        else:
            dest = head
        if "/" not in dest:
            dest += "/32"
        try:
            ipaddress.ip_network(dest, strict=False)
        except ValueError:
            continue
        out.append(dest)
    return out


def _is_docker_iface(name: str) -> bool:
    """True for a Docker-managed bridge/veth interface, whose address is Docker's
    own allocation rather than a real network the operator is on."""
    return name == "docker0" or name.startswith("br-") or name.startswith("veth")


def parse_ip_addr(text: str) -> list[dict]:
    """Interface networks from ``ip -o addr`` output.

    Each entry is ``{"iface", "cidr", "docker"}`` — ``docker`` flags a Docker-
    managed bridge so callers can tell the operator's real LAN from Docker's own
    bridge addresses (which self-overlap the networks Docker allocated).
    """
    out: list[dict] = []
    for line in text.splitlines():
        parts = line.split()
        # Format: "<idx>: <iface>    inet <cidr> ..."  (idx may be absent)
        iface = None
        for i, tok in enumerate(parts):
            if tok in ("inet", "inet6") and i + 1 < len(parts):
                cidr = parts[i + 1]
                try:
                    ipaddress.ip_network(cidr, strict=False)
                except ValueError:
                    continue
                out.append({
                    "iface": iface or "?",
                    "cidr": cidr,
                    "docker": _is_docker_iface(iface or ""),
                })
                break
            if i == 1 and ":" not in tok:
                # "2: eth0" → parts = ["2:", "eth0", ...]; iface is field 1.
                iface = tok
            elif i == 0 and tok.endswith(":") and not tok[:-1].isdigit():
                # Some outputs omit the leading index: "lo    inet ..."
                iface = tok[:-1]
        else:
            continue
    return out


def parse_docker_networks(inspect_json: str) -> list[dict]:
    """(name, subnets) for each Docker network from ``docker network inspect``
    JSON. A network with no IPAM subnet (host/none drivers) contributes an empty
    subnet list."""
    try:
        data = json.loads(inspect_json)
    except (ValueError, TypeError):
        return []
    out: list[dict] = []
    for net in data:
        if not isinstance(net, dict):
            continue
        subnets: list[str] = []
        ipam = net.get("IPAM") or {}
        for cfg in ipam.get("Config") or []:
            subnet = (cfg or {}).get("Subnet")
            if subnet:
                subnets.append(subnet)
        out.append({"name": net.get("Name", "?"), "subnets": subnets})
    return out


def parse_docker_pools(info_json: str) -> object:
    """Parse ``docker info --format '{{json .DefaultAddressPools}}'`` output.

    Returns the decoded value: a list of pool dicts, or None when the daemon has
    no configured pools (the built-in defaults are then in force). Unparseable
    output is treated as None."""
    try:
        return json.loads(info_json)
    except (ValueError, TypeError):
        return None


# ── live reads (read-only, best-effort) ──────────────────────────────────

def _run(argv: list[str]) -> str | None:
    """Run a read-only command, return stdout, or None if it is missing/fails.
    This tool must degrade rather than crash when ``ip`` or ``docker`` is absent."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def read_host_cidrs() -> tuple[list[str], list[dict]]:
    """The host's routed CIDRs and its interface networks, from ``ip``.

    Returns (routes, interfaces). ``routes`` is every destination the host routes
    (which already includes each Docker bridge's own subnet); ``interfaces`` is
    the per-interface address list with the docker/real distinction.
    """
    routes: list[str] = []
    route_text = _run(["ip", "route"])
    if route_text is not None:
        routes = parse_ip_route(route_text)
    interfaces: list[dict] = []
    addr_text = _run(["ip", "-o", "addr"])
    if addr_text is not None:
        interfaces = parse_ip_addr(addr_text)
    return routes, interfaces


def read_docker_networks() -> list[dict] | None:
    """Subnets Docker already holds, or None if the daemon is unreachable."""
    names = _run(["docker", "network", "ls", "--format", "{{.Name}}"])
    if names is None:
        return None
    net_names = [n for n in names.splitlines() if n.strip()]
    if not net_names:
        return []
    inspect = _run(["docker", "network", "inspect", *net_names])
    if inspect is None:
        return []
    return parse_docker_networks(inspect)


def read_docker_pools() -> tuple[object, bool]:
    """The daemon's configured pools, and whether Docker was reachable at all.

    Returns (configured, reachable). ``configured`` is the raw DefaultAddressPools
    value (None → built-in defaults in force). ``reachable`` is False when
    ``docker info`` could not be run, so callers can distinguish "no pools
    configured" from "no daemon".
    """
    info = _run(["docker", "info", "--format", "{{json .DefaultAddressPools}}"])
    if info is None:
        return None, False
    return parse_docker_pools(info), True


# ── report assembly ──────────────────────────────────────────────────────

def build_report() -> dict:
    """Collect every read into one report dict and choose a subnet.

    Structured so ``main`` renders text from it and ``--json`` dumps it verbatim.
    Never mutates anything on the host.
    """
    routes, interfaces = read_host_cidrs()
    docker_nets = read_docker_networks()
    configured_pools, docker_reachable = read_docker_pools()
    pools, pools_builtin = pools_in_effect(configured_pools)

    # The operator's own networks: interface addresses that are NOT a Docker
    # bridge. This is what a pool must not overlap — the LAN the incident ate.
    operator_cidrs = [i["cidr"] for i in interfaces if not i["docker"]]

    docker_subnets: list[str] = []
    if docker_nets:
        for net in docker_nets:
            docker_subnets.extend(net["subnets"])

    # Avoid everything currently in use when choosing: real routes, the
    # operator's interface networks, and every subnet Docker already holds.
    avoid = list(routes) + operator_cidrs + docker_subnets
    chosen = choose_subnet(avoid)

    pool_overlaps = overlapping_pairs(pools, operator_cidrs)

    # A Docker network whose subnet overlaps a real operator network — the
    # already-happened collision this tool exists to surface (and, run after the
    # stack is up, the verify check that the pinned network stayed clear).
    collisions: list[dict] = []
    if docker_nets:
        op_nets = _ipv4_networks(operator_cidrs)
        for net in docker_nets:
            for subnet in net["subnets"]:
                sub_nets = _ipv4_networks([subnet])
                if not sub_nets:
                    continue
                sub = sub_nets[0]
                for op in op_nets:
                    if sub.overlaps(op):
                        collisions.append({
                            "network": net["name"],
                            "subnet": subnet,
                            "operator_network": str(op),
                        })

    return {
        "host_routes": routes,
        "interfaces": interfaces,
        "operator_networks": operator_cidrs,
        "docker_reachable": docker_reachable,
        "docker_networks": docker_nets,
        "docker_subnets": docker_subnets,
        "pools_in_effect": pools,
        "pools_are_builtin": pools_builtin,
        "pool_overlaps_host": [
            {"pool": p, "host_network": h} for p, h in pool_overlaps
        ],
        "network_collisions": collisions,
        "chosen_subnet": chosen,
    }


def _render(report: dict) -> None:
    _section("Host networks (this machine)")
    if not report["interfaces"]:
        _line("interfaces", "none read (is `ip` available?)", warn=True)
    for iface in report["interfaces"]:
        tag = " (docker bridge)" if iface["docker"] else ""
        _line(f"{iface['iface']}{tag}", iface["cidr"])
    if report["host_routes"]:
        _detail(f"        routed destinations: {', '.join(report['host_routes'])}")

    _section("Docker address pools in effect")
    if not report["docker_reachable"]:
        _line("docker daemon", "not reachable — reporting the built-in defaults "
              "the daemon WOULD use", warn=True)
    origin = "built-in defaults" if report["pools_are_builtin"] else "configured (daemon.json)"
    _line("pools origin", origin, warn=report["pools_are_builtin"])
    for pool in report["pools_in_effect"]:
        _line("pool", f"{pool['base']} carved into /{pool['size']} blocks")

    _section("Subnets already allocated by Docker")
    if report["docker_networks"] is None:
        _line("docker networks", "daemon not reachable — cannot enumerate", warn=True)
    elif not report["docker_subnets"]:
        _line("docker networks", "none hold an IPAM subnet yet")
    else:
        for net in report["docker_networks"]:
            if net["subnets"]:
                _line(net["name"], ", ".join(net["subnets"]))

    _section("Pool / host-route overlap")
    if report["pool_overlaps_host"]:
        for hit in report["pool_overlaps_host"]:
            _line(
                "pool overlaps a host network",
                f"pool {hit['pool']} contains host network {hit['host_network']} — "
                "a block the daemon carves from this pool can land on the "
                "operator's own network. Pin AUTONOMY_SUBNET below.",
                fail=True,
            )
    else:
        _line("pool overlap", "no pool in effect overlaps a host network")

    if report["network_collisions"]:
        _section("Existing network COLLISIONS with the operator's LAN")
        for c in report["network_collisions"]:
            _line(
                c["network"],
                f"subnet {c['subnet']} overlaps host network {c['operator_network']} "
                "— this network is on top of a real network the host uses; recreate "
                "it on a pinned subnet",
                fail=True,
            )

    _section("Chosen subnet (safe to pin)")
    if report["chosen_subnet"]:
        _line("AUTONOMY_SUBNET", report["chosen_subnet"])
        _detail(
            "        record this and pin it before `docker compose up`:\n"
            f"            echo 'AUTONOMY_SUBNET={report['chosen_subnet']}' >> .env"
        )
    else:
        _line("AUTONOMY_SUBNET", f"no free /{DEFAULT_PREFIX} block found in "
              f"{DEFAULT_SPACE} — every block is already routed", fail=True)


def main(argv: list[str] | None = None) -> int:
    global _QUIET
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true",
                        help="emit the collected report as JSON instead of text")
    parser.add_argument("--env", action="store_true",
                        help="print only `AUTONOMY_SUBNET=<cidr>` for the project .env")
    parser.add_argument("--print-subnet", action="store_true",
                        help="print only the chosen subnet CIDR")
    args = parser.parse_args(argv)

    _QUIET = args.json or args.env or args.print_subnet
    report = build_report()

    if args.print_subnet:
        if not report["chosen_subnet"]:
            print("no free subnet found", file=sys.stderr)
            return 1
        print(report["chosen_subnet"])
        return 0
    if args.env:
        if not report["chosen_subnet"]:
            print("no free subnet found", file=sys.stderr)
            return 1
        print(f"AUTONOMY_SUBNET={report['chosen_subnet']}")
        return 0
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print("Network preflight — pick a Compose subnet that won't eat the LAN")
    print("=" * 64)
    _render(report)
    print("\n" + "=" * 64)
    # A pool overlapping a host route, or an existing collision, is the failure
    # mode this tool exists to catch — exit nonzero so a script can gate on it.
    return 1 if report["network_collisions"] else 0


if __name__ == "__main__":
    sys.exit(main())
