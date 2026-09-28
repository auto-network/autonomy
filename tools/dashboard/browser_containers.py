"""Docker operations for browser lease containers (auto-czoc0).

One container per lease, from ``autonomy-browser:local``, on the Docker
network ``autonomy-browser``, which only the dashboard and lease containers
join (no session container), and with no published ports. Each container is
capped (memory with no swap, CPUs, processes, a 1 GiB /dev/shm), runs with
restart policy ``no`` and ``--rm``, and keeps Chrome's sandbox through the
vendored seccomp profile. Design: graph://c330323d-986.

Secrets reach ``docker`` through its environment (``-e NAME`` without a
value), never on its command line.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from tools.data_paths import REPO_ROOT

IMAGE = "autonomy-browser:local"
NETWORK = "autonomy-browser"
AGENT_PORT = 7300
SHM_SIZE = "1g"
SECCOMP_PROFILE = REPO_ROOT / "tools" / "browser_broker" / "image" / "seccomp-chrome.json"
LABEL = "autonomy.browser"
DOCKER_TIMEOUT_S = 30

logger = logging.getLogger(__name__)


class DockerUnavailable(RuntimeError):
    pass


class NameConflict(RuntimeError):
    """A container with this name exists: the persistent profile is in use."""


def _docker(*args: str, env: Optional[dict] = None, timeout: float = DOCKER_TIMEOUT_S,
            check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(["docker", *args], capture_output=True, text=True,
                              timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DockerUnavailable(f"docker {args[0]}: {exc}") from exc
    if check and proc.returncode != 0:
        if "is already in use" in proc.stderr and "Conflict" in proc.stderr:
            raise NameConflict(proc.stderr.strip())
        if "Cannot connect to the Docker daemon" in proc.stderr:
            raise DockerUnavailable(proc.stderr.strip())
        raise RuntimeError(f"docker {args[0]} failed: {proc.stderr.strip()[:500]}")
    return proc


def labels(*, lease_hash: str, session: str, org: str, workspace: str, profile: str,
           adapter: str, expires_at: float) -> dict[str, str]:
    """The labels the reconciler rebuilds its view from ("Lease identity")."""
    return {
        f"{LABEL}.lease": lease_hash,
        f"{LABEL}.session": session,
        f"{LABEL}.org": org,
        f"{LABEL}.workspace": workspace,
        f"{LABEL}.profile": profile,
        f"{LABEL}.adapter": adapter,
        f"{LABEL}.expires_at": str(int(expires_at)),
    }


@dataclass(frozen=True)
class Caps:
    memory_mb: int
    cpus: int
    pids: int


def create_args(*, name: str, lease_labels: dict[str, str], caps: Caps,
                mount_argv: list[str], timezone: Optional[str]) -> list[str]:
    """``docker create`` arguments for one lease container (no secrets)."""
    argv = ["create", "--name", name, "--network", NETWORK, "--restart", "no", "--rm",
            "--memory", f"{caps.memory_mb}m", "--memory-swap", f"{caps.memory_mb}m",
            "--cpus", str(caps.cpus), "--pids-limit", str(caps.pids), "--shm-size", SHM_SIZE,
            "--security-opt", f"seccomp={SECCOMP_PROFILE}",
            "--security-opt", "no-new-privileges",
            # No raw sockets even after an escape: the egress exemption is by
            # source address, which raw sockets could forge (measured: Chrome
            # does not need NET_RAW; --cap-drop ALL breaks its zygote).
            "--cap-drop", "NET_RAW",
            "-e", "BROWSER_LEASE_SECRET", "-e", "BROWSER_VNC_PASSWORD",
            "-e", "BROWSER_LEASE_EXPIRES_AT"]
    if timezone:
        argv += ["-e", f"TZ={timezone}"]
    for key, value in sorted(lease_labels.items()):
        argv += ["--label", f"{key}={value}"]
    return argv + mount_argv + [IMAGE]


def ensure_network() -> None:
    """Create ``autonomy-browser`` if missing and attach this dashboard's
    container to it (a host-process dashboard reaches the bridge directly)."""
    if _docker("network", "inspect", NETWORK, check=False).returncode != 0:
        proc = _docker("network", "create", "--driver", "bridge",
                       "--label", f"{LABEL}.network=1", NETWORK, check=False)
        if proc.returncode != 0 and "already exists" not in proc.stderr:
            raise RuntimeError(f"cannot create {NETWORK}: {proc.stderr.strip()}")
    from agents.mount_plan import _own_container_id

    own = _own_container_id()
    if own:
        # --gw-priority -1 keeps the dashboard's default route (its egress) on
        # its own network; without it the attach moves the default route to the
        # lease bridge (measured). A daemon older than Docker 28 (API 1.48)
        # ignores the flag silently (measured on Docker 26), so say so here.
        api = _docker("version", "--format", "{{.Server.APIVersion}}", check=False).stdout.strip()
        try:
            old_daemon = tuple(int(x) for x in api.split(".")) < (1, 48)
        except ValueError:
            old_daemon = False
        if old_daemon:
            logger.warning("docker API %s ignores --gw-priority: the dashboard's default route "
                           "moves to %s, so its egress leaves from its address there", api, NETWORK)
        proc = _docker("network", "connect", "--gw-priority", "-1", NETWORK, own, check=False)
        if proc.returncode != 0 and "gw-priority" in proc.stderr:
            proc = _docker("network", "connect", NETWORK, own, check=False)
        if proc.returncode != 0 and "already exists" not in proc.stderr \
                and "already attached" not in proc.stderr:
            raise RuntimeError(f"cannot attach the dashboard to {NETWORK}: {proc.stderr.strip()}")


class IsolationUnavailable(RuntimeError):
    """The lease -> dashboard refusal could not be put in place."""


def _isolation_rule(subnet: str) -> list[str]:
    # New connections from the lease network to anything in the dashboard's
    # network namespace are dropped; replies to connections the dashboard
    # opens (ESTABLISHED) still pass, so dashboard -> lease keeps working.
    return ["INPUT", "-s", subnet, "-m", "conntrack", "--ctstate", "NEW", "-j", "DROP"]


def _gateway_rule(gateway: str) -> list[str]:
    # The network gateway is the host: docker-proxy's source for the published
    # port and the host's own route in. Lease containers never hold it.
    return ["INPUT", "-s", gateway, "-j", "ACCEPT"]


def isolate_dashboard() -> None:
    """Refuse, at the network level, every connection a lease starts toward the
    dashboard (the dashboard dials leases, never the reverse).

    The dashboard runs unprivileged, so a short-lived helper sharing its
    network namespace installs one rule there (idempotent). The rule lives as
    long as the dashboard container; each worker activation re-checks it.
    """
    from agents.mount_plan import _own_container_id

    own = _own_container_id()
    if not own:
        raise IsolationUnavailable("the dashboard is not a container; lease isolation needs one")
    subnet, _, rest = _docker("network", "inspect", "--format",
                              "{{(index .IPAM.Config 0).Subnet}} {{(index .IPAM.Config 0).Gateway}} {{.EnableIPv6}}",
                              NETWORK).stdout.strip().partition(" ")
    gateway, _, ipv6 = rest.partition(" ")
    if not subnet:
        raise IsolationUnavailable(f"{NETWORK} has no subnet")
    if not gateway:
        raise IsolationUnavailable(f"{NETWORK} has no gateway")
    if ipv6.strip() == "true":
        # The refusal is IPv4 iptables; never protect only half of a network.
        raise IsolationUnavailable(f"{NETWORK} has IPv6 enabled; recreate it IPv4-only")
    helper = ["run", "--rm", "--network", f"container:{own}", "--cap-drop", "ALL",
              "--cap-add", "NET_ADMIN", "--user", "0", "--entrypoint", "iptables", IMAGE, "-w"]
    rule = _isolation_rule(subnet)
    if _docker(*helper, "-C", *rule, check=False).returncode != 0:
        proc = _docker(*helper, "-I", *rule, check=False)
        if proc.returncode != 0 or _docker(*helper, "-C", *rule, check=False).returncode != 0:
            raise IsolationUnavailable(f"cannot install the lease refusal rule: {proc.stderr.strip()[:300]}")
    # The host reaches the dashboard through this same network: docker-proxy
    # forwards the published port from the network's gateway address, so the
    # refusal must not cover it (2026-09-28: it did, and every localhost:8080
    # connection on the host was dropped). The gateway exemption sits above
    # the refusal; re-inserting keeps it first after any later insert.
    allow = _gateway_rule(gateway)
    _docker(*helper, "-D", *allow, check=False)
    proc = _docker(*helper, "-I", allow[0], "1", *allow[1:], check=False)
    if proc.returncode != 0 or _docker(*helper, "-C", *allow, check=False).returncode != 0:
        raise IsolationUnavailable(f"cannot exempt the host gateway from the refusal: {proc.stderr.strip()[:300]}")


#: Destinations a lease may never open a connection to: private, carrier-grade
#: NAT, link-local (cloud metadata) and loopback ranges (auto-i5okc). Public
#: internet egress still leaves from the node's own address.
EGRESS_BLOCKED = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
                  "169.254.0.0/16", "127.0.0.0/8")


def _bridge_name() -> str:
    proc = _docker("network", "inspect", "--format",
                   '{{.Id}} {{index .Options "com.docker.network.bridge.name"}}', NETWORK)
    net_id, _, custom = proc.stdout.strip().partition(" ")
    custom = custom.strip()
    if custom == "<no value>":  # Go's template output for a missing option
        custom = ""
    name = custom or f"br-{net_id[:12]}"
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", name) or (not custom and len(net_id) < 12):
        raise IsolationUnavailable(f"cannot name the bridge of {NETWORK}: {proc.stdout.strip()!r}")
    return name


#: Our own chains. The node's INPUT and DOCKER-USER chains are shared with
#: tailscaled and Docker, which rewrite them; we only ever add one jump rule
#: to each and never delete from them by position.
FWD_CHAIN = "AUTONOMY-BROWSER-FWD"
IN_CHAIN = "AUTONOMY-BROWSER-IN"


def egress_rules(bridge: str, dashboard_ip: str) -> dict[str, list[list[str]]]:
    """The rules of our two chains, in order (jumps into them match ``-i <bridge>``).

    FWD: new connections leaving the lease bridge for a blocked range are
    dropped; ``! -o <bridge>`` leaves traffic within the bridge (dashboard <->
    lease) alone. IN: new connections from the lease bridge to the node itself
    (its published ports, its LAN address, the gateway) are dropped. The
    dashboard's own address on the bridge is exempt from both: on Docker < 28
    its default route, and so its egress to the LAN and the NAS, leaves
    through this bridge."""
    exempt = ["!", "-s", f"{dashboard_ip}/32"]
    new = ["-m", "conntrack", "--ctstate", "NEW", "-j", "DROP"]
    return {
        FWD_CHAIN: [["!", "-o", bridge, *exempt, "-d", cidr, *new] for cidr in EGRESS_BLOCKED],
        IN_CHAIN: [[*exempt, *new]],
    }


def jump_rules(bridge: str) -> dict[str, list[str]]:
    """The one rule each shared chain gets."""
    return {"DOCKER-USER": ["-i", bridge, "-j", FWD_CHAIN], "INPUT": ["-i", bridge, "-j", IN_CHAIN]}


def _dashboard_ip_on_lease_network(own: str) -> str:
    return _docker("inspect", "--format",
                   "{{(index .NetworkSettings.Networks \"%s\").IPAddress}}" % NETWORK,
                   own).stdout.strip()


def restrict_egress() -> None:
    """Install and verify the lease egress policy on the node (auto-i5okc).

    A short-lived helper on the host network with NET_ADMIN keeps our two
    chains exactly equal to :func:`egress_rules` and makes sure the shared
    INPUT and DOCKER-USER chains jump to them for the lease bridge. Raises
    IsolationUnavailable when the policy cannot be verified; leases are then
    refused."""
    import shlex
    from agents.mount_plan import _own_container_id

    backend = _docker("info", "--format", "{{.FirewallBackend.Driver}}", check=False).stdout.strip()
    if backend and backend != "iptables":
        raise IsolationUnavailable(f"docker firewall backend {backend!r} has no DOCKER-USER chain")
    own = _own_container_id()
    if not own:
        raise IsolationUnavailable("the dashboard is not a container; lease egress policy needs one")
    dashboard_ip = _dashboard_ip_on_lease_network(own)
    if not dashboard_ip:
        raise IsolationUnavailable(f"the dashboard has no address on {NETWORK}")
    helper = ["run", "--rm", "--network", "host", "--cap-drop", "ALL", "--cap-add", "NET_ADMIN",
              "--user", "0", "--entrypoint", "iptables", IMAGE, "-w"]

    def ipt(*args):
        return _docker(*helper, *args, check=False)

    bridge = _bridge_name()
    for chain, wanted in egress_rules(bridge, dashboard_ip).items():
        if ipt("-S", chain).returncode != 0 and ipt("-N", chain).returncode != 0:
            raise IsolationUnavailable(f"cannot create chain {chain}")
        current = [line[len(f"-A {chain} "):] for line in ipt("-S", chain).stdout.splitlines()
                   if line.startswith(f"-A {chain} ")]
        if [_normalize(c) for c in current] != [frozenset(w) for w in wanted]:
            # Our own chain: put the wanted rules first, then trim the old ones
            # from the end, so there is never a moment without the policy.
            for position, rule in enumerate(wanted, start=1):
                if ipt("-I", chain, str(position), *rule).returncode != 0:
                    raise IsolationUnavailable(f"cannot write {chain}")
            for position in range(len(wanted) + len(current), len(wanted), -1):
                ipt("-D", chain, str(position))
        verified = [line[len(f"-A {chain} "):] for line in ipt("-S", chain).stdout.splitlines()
                    if line.startswith(f"-A {chain} ")]
        if [_normalize(v) for v in verified] != [frozenset(w) for w in wanted]:
            raise IsolationUnavailable(f"{chain} does not hold the lease egress policy")
    for shared, jump in jump_rules(bridge).items():
        target = jump[-1]
        # A jump for an old bridge (a recreated network) is removed by its exact
        # specification, never by position.
        for line in ipt("-S", shared).stdout.splitlines():
            if line.startswith(f"-A {shared} ") and line.endswith(f"-j {target}"):
                spec = line[len(f"-A {shared} "):]
                if _normalize(spec) != frozenset(jump):
                    ipt("-D", shared, *shlex.split(spec))
        if ipt("-C", shared, *jump).returncode != 0:
            ipt("-I", shared, "1", *jump)
            if ipt("-C", shared, *jump).returncode != 0:
                raise IsolationUnavailable(f"cannot add the {target} jump to {shared}")


def _normalize(spec: str) -> frozenset:
    # iptables -S prints matches in its own order and quoting; compare as a set
    # of tokens. A line that does not parse never equals a wanted rule.
    import shlex
    try:
        return frozenset(shlex.split(spec))
    except ValueError:
        return frozenset({"<unparseable>", spec})


def profile_mount_argv(org: str, workspace: str, name: str) -> list[str]:
    from agents.mount_plan import MountPlan, discover_topology, mount_args
    from tools.browser_broker.profiles import profile_mount

    plan = MountPlan()
    plan.set(profile_mount(org, workspace, name))
    return mount_args(plan, discover_topology())


def prepare_profile_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def profile_integrity(path: Path) -> Optional[str]:
    """None if the profile is sound (or new), else a diagnostic.

    Only called while this lease holds the profile's container name, so Chrome
    is not running on it. Chrome starting on it is the lease's own health.
    """
    local_state = path / "Local State"
    if local_state.exists():
        try:
            json.loads(local_state.read_text())
        except (OSError, ValueError) as exc:
            return f"Local State does not parse: {exc.__class__.__name__}"
    cookies = path / "Default" / "Cookies"
    if cookies.exists():
        try:
            conn = sqlite3.connect(f"file:{cookies}?mode=ro", uri=True)
            try:
                result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return f"Cookies database unreadable: {exc.__class__.__name__}"
        if result != "ok":
            return f"Cookies integrity_check: {result[:200]}"
    return None


def create_and_start(*, argv: list[str], secret: str, vnc_password: str,
                     expires_at: float, name: str,
                     before_start=None) -> str:
    """Create the container (claiming its name), run *before_start* (which may
    return a diagnostic to abort), start it, and return its address on the
    lease network."""
    env = {**os.environ, "BROWSER_LEASE_SECRET": secret, "BROWSER_VNC_PASSWORD": vnc_password,
           "BROWSER_LEASE_EXPIRES_AT": str(int(expires_at))}
    _docker(*argv, env=env)
    try:
        if before_start is not None:
            diagnostic = before_start()
            if diagnostic:
                raise ProfileDamaged(diagnostic)
        _docker("start", name)
        return address(name)
    except Exception:
        _docker("rm", "-f", name, check=False)
        raise


class ProfileDamaged(RuntimeError):
    pass


def address(name: str) -> str:
    proc = _docker("inspect", "--format",
                   "{{(index .NetworkSettings.Networks \"%s\").IPAddress}}" % NETWORK, name)
    return proc.stdout.strip()


def _missing(proc: subprocess.CompletedProcess) -> bool:
    # Docker's wording varies by version and case: 29.x prints
    # "error: no such object: <name>" (measured on Home), older daemons
    # "Error: No such object" / "No such container".
    err = proc.stderr.lower()
    return "no such object" in err or "no such container" in err


def stop(name: str, *, lease_hash: str, grace_s: int = 5, removal_timeout_s: float = 20.0) -> None:
    """Stop and remove a lease container, returning only once it no longer exists.

    ``docker stop`` returns when the container exits, but ``--rm`` removes it
    asynchronously, so without the wait a lease could be recorded gone while
    its persistent profile's name is still held (measured on the node: the
    next request for the profile got 409). Everything after the first lookup
    uses the container's ID, never its name: once the name frees, a new lease
    for the same profile may take it, and must not be touched. The first lookup
    also reads the container's lease label: a container carrying another lease
    (ours already went, and a retry found the name reused) is left alone.
    Idempotent. A
    docker error other than "no such container" raises DockerUnavailable, so
    the lease stays releasing and is retried."""
    import time

    found = _docker("inspect", "--format",
                    '{{.Id}} {{index .Config.Labels "%s.lease"}}' % LABEL, name, check=False)
    if found.returncode != 0:
        if _missing(found):
            return
        raise DockerUnavailable(f"docker inspect {name}: {found.stderr.strip()[:200]}")
    container_id, _, labelled = found.stdout.strip().partition(" ")
    if labelled.strip() != lease_hash:
        return  # the name now belongs to another lease; ours is already gone
    _docker("stop", "-t", str(grace_s), container_id, check=False, timeout=grace_s + 20)
    _docker("rm", "-f", container_id, check=False)
    deadline = time.monotonic() + removal_timeout_s
    while True:
        probe = _docker("inspect", "--format", "{{.Id}}", container_id, check=False)
        if probe.returncode != 0:
            if _missing(probe):
                return
            raise DockerUnavailable(f"docker inspect {container_id[:12]}: {probe.stderr.strip()[:200]}")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"{name} ({container_id[:12]}) was not removed within "
                               f"{removal_timeout_s:.0f} s")
        time.sleep(0.2)
        _docker("rm", "-f", container_id, check=False)


@dataclass(frozen=True)
class LeaseContainer:
    name: str
    lease_hash: str
    state: str
    labels: dict


def list_containers() -> list[LeaseContainer]:
    proc = _docker("ps", "-a", "--no-trunc", "--filter", f"label={LABEL}.lease",
                   "--format", "{{json .}}")
    out = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        pairs = dict(p.split("=", 1) for p in (item.get("Labels") or "").split(",") if "=" in p)
        out.append(LeaseContainer(name=item["Names"], lease_hash=pairs.get(f"{LABEL}.lease", ""),
                                  state=item.get("State", ""), labels=pairs))
    return out


def stats(name: str) -> dict:
    proc = _docker("stats", "--no-stream", "--format", "{{json .}}", name, check=False, timeout=10)
    try:
        item = json.loads(proc.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return {"cpu": None, "mem_mb": None}
    cpu = item.get("CPUPerc", "").rstrip("%")
    mem = (item.get("MemUsage", "").split("/")[0]).strip()
    return {"cpu": _float(cpu), "mem_mb": _mem_mb(mem)}


def _float(value: str) -> Optional[float]:
    try:
        return float(value)
    except ValueError:
        return None


def _mem_mb(text: str) -> Optional[float]:
    units = {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024, "B": 1 / 1024 / 1024}
    for unit, factor in units.items():
        if text.endswith(unit) and _float(text[: -len(unit)]) is not None:
            return round(_float(text[: -len(unit)]) * factor, 1)
    return None


def free_gib(path: Path) -> float:
    return shutil.disk_usage(path).free / (1 << 30)


def agent_request(address_: str, secret: str, method: str, path: str,
                  body: Optional[dict] = None, timeout: float = 5.0) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"http://{address_}:{AGENT_PORT}{path}", method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"X-Lease-Secret": secret, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        except ValueError:
            return exc.code, {}
