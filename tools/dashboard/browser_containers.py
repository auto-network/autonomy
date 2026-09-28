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
    subnet, _, ipv6 = _docker("network", "inspect", "--format",
                              "{{(index .IPAM.Config 0).Subnet}} {{.EnableIPv6}}",
                              NETWORK).stdout.strip().partition(" ")
    if not subnet:
        raise IsolationUnavailable(f"{NETWORK} has no subnet")
    if ipv6.strip() == "true":
        # The refusal is IPv4 iptables; never protect only half of a network.
        raise IsolationUnavailable(f"{NETWORK} has IPv6 enabled; recreate it IPv4-only")
    helper = ["run", "--rm", "--network", f"container:{own}", "--cap-drop", "ALL",
              "--cap-add", "NET_ADMIN", "--user", "0", "--entrypoint", "iptables", IMAGE, "-w"]
    rule = _isolation_rule(subnet)
    if _docker(*helper, "-C", *rule, check=False).returncode == 0:
        return
    proc = _docker(*helper, "-I", *rule, check=False)
    if proc.returncode != 0 or _docker(*helper, "-C", *rule, check=False).returncode != 0:
        raise IsolationUnavailable(f"cannot install the lease refusal rule: {proc.stderr.strip()[:300]}")


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


def stop(name: str, grace_s: int = 5, removal_timeout_s: float = 20.0) -> None:
    """Stop and remove a lease container, returning only once its name is free.

    ``docker stop`` returns when the container exits, but ``--rm`` removes it
    asynchronously, so without the wait a lease could be recorded gone while
    its persistent profile's name is still held (measured on the node: the
    next request for the profile got 409). Idempotent."""
    import time

    _docker("stop", "-t", str(grace_s), name, check=False, timeout=grace_s + 20)
    _docker("rm", "-f", name, check=False)
    deadline = time.monotonic() + removal_timeout_s
    while _docker("inspect", "--format", "{{.Id}}", name, check=False).returncode == 0:
        if time.monotonic() >= deadline:
            raise RuntimeError(f"{name} was not removed within {removal_timeout_s:.0f} s")
        time.sleep(0.2)
        _docker("rm", "-f", name, check=False)


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
