"""Give an organization its own bead database the first time one is needed.

Authority here is possession of the Docker socket, not a password. ``docker
exec`` into the Dolt container reaches ``__dolt_local_user__@localhost``, a
built-in superuser that needs no credential and is not reachable over the
network. Verified 2026-08-30: a database and user created that way are
immediately visible to a network client, so the exec is a client of the
running server, not a second process opening its data directory.

So there is no admin password to generate, store, back up, rotate, or lose,
and no bootstrap step. The only secrets created are the per-org SQL users,
whose credentials land in ``/app/data`` — inside the one volume the deployment
already says to back up.

**The config.yaml host convention (auto-7iw4r).** A tracker dir is read from
two perspectives with one file: compose-network processes (dashboard,
dispatcher) and host processes (cron, host bd). The DAO order is env →
config.yaml → default, and docker-compose.yml already hands every compose
consumer ``DOLT_SQL_HOST=dolt`` in its environment — so ``config.yaml``
carries the HOST-reachable address (the Dolt container's published port
binding, discovered at provisioning time), and the compose DNS name never
lands in a file a host process will read. Writing ``host: dolt`` here was
exactly the 2026-09-01 blindhash defect: the first contract-correct backup
run failed its dump with "Unknown MySQL server host 'dolt'" from the host.
A Dolt container with no published binding gets no ``dolt:`` block at all —
consumers' environments and defaults decide, instead of a name that is a
lie in one of the two perspectives. Repair an existing row with
``python -m tools.beads_provision repair-config <slug>`` (run it inside the
node container: it needs the Docker socket and the data volume).
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import subprocess

from tools import data_paths

logger = logging.getLogger(__name__)


def _orgs_root():
    """Read DATA_ROOT at call time, not import time: a caller may reroot it
    (tests do, and a differently-rooted deployment would)."""
    return data_paths.DATA_ROOT / ".beads" / "orgs"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class BeadsProvisionError(RuntimeError):
    pass


def _docker(*args: str, timeout: int = 120) -> str:
    out = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout
    )
    if out.returncode != 0:
        raise BeadsProvisionError(
            f"docker {' '.join(args[:2])} failed: {out.stderr.strip()[:200]}"
        )
    return out.stdout.strip()


def _dolt_container() -> str:
    """The Dolt container of THIS node's compose project.

    Scoped by our own project label so a host running several nodes never
    provisions into a sibling's database.
    """
    from agents.mount_plan import _own_container_id

    cid = _own_container_id()
    if not cid:
        raise BeadsProvisionError(
            "not running in a container: provisioning reaches Dolt through the "
            "compose project's socket and has no host equivalent"
        )
    project = _docker(
        "inspect", "--format",
        '{{index .Config.Labels "com.docker.compose.project"}}', cid,
    )
    found = _docker(
        "ps", "-q",
        "--filter", f"label=com.docker.compose.project={project}",
        "--filter", "label=com.docker.compose.service=dolt",
    ).split()
    if len(found) != 1:
        raise BeadsProvisionError(
            f"expected exactly one dolt container in project {project!r}, "
            f"found {len(found)} — is the `beads` profile up?"
        )
    return found[0]


def _dolt_host_binding(container: str) -> "tuple[str, int] | None":
    """The Dolt server's HOST-reachable (ip, port), or None.

    Read from the container's published port bindings — the address a
    host process actually dials (172.17.0.1:3306 on this deployment).
    A binding published on all interfaces reports 0.0.0.0; loopback is
    the honest host-side name for that.
    """
    raw = _docker(
        "inspect", "--format", "{{json .NetworkSettings.Ports}}", container,
    )
    try:
        ports = json.loads(raw or "{}") or {}
    except ValueError:
        return None
    for binding in ports.get("3306/tcp") or []:
        host_ip = (binding or {}).get("HostIp") or ""
        host_port = (binding or {}).get("HostPort") or ""
        if not host_port:
            continue
        if host_ip in ("", "0.0.0.0", "::"):
            host_ip = "127.0.0.1"
        try:
            return host_ip, int(host_port)
        except ValueError:
            continue
    return None


def _config_yaml_text(slug: str, binding: "tuple[str, int] | None") -> str:
    """The tracker's config.yaml — host-reachable address, never the
    compose DNS name (module docstring: the config.yaml host convention)."""
    head = (
        f"# {slug} org tracker: its own database on the node's Dolt server.\n"
        "no-git-ops: true\n"
        "image: autonomy-session-platform\n"
    )
    if binding is None:
        return head + (
            "# No published Dolt port binding at provisioning time: no\n"
            "# host-reachable address exists, so none is recorded. Compose\n"
            "# consumers carry DOLT_SQL_HOST in their environment.\n"
        )
    host, port = binding
    return head + f"dolt:\n  host: {host}\n  port: {port}\n"


def repair_config(slug: str, orgs_root=None) -> str:
    """Rewrite an existing tracker's config.yaml to the current
    convention, discovering the host binding live. Returns the new text."""
    root = orgs_root if orgs_root is not None else _orgs_root()
    tracker = root / slug
    if not (tracker / "metadata.json").is_file():
        raise BeadsProvisionError(f"no provisioned tracker for {slug!r}")
    text = _config_yaml_text(slug, _dolt_host_binding(_dolt_container()))
    (tracker / "config.yaml").write_text(text)
    return text


def dolt_sql(*statements: str) -> None:
    """Run each statement as the container's local superuser."""
    container = _dolt_container()
    for statement in statements:
        _docker("exec", container, "dolt", "sql", "-q", statement, timeout=300)


def ensure_org_beads_dir(slug: str, orgs_root=None):
    """The org's bead tracker dir, provisioning it on first sight.

    ``orgs_root`` lets a caller that owns its own DATA_ROOT pass it, so the
    directory this writes and the directory that caller reads are never two
    different places.
    """
    root = orgs_root if orgs_root is not None else _orgs_root()
    final = root / slug
    if (final / "metadata.json").is_file():
        return final
    if not SLUG_RE.match(slug):
        # The slug is interpolated into SQL as an identifier and used as a
        # directory name; both need it to be what it claims to be.
        raise BeadsProvisionError(f"unsafe org slug: {slug!r}")

    user = f"beads_{slug}"
    # token_urlsafe yields only [A-Za-z0-9_-], so it needs no SQL escaping.
    password = secrets.token_urlsafe(24)
    dolt_sql(
        f"CREATE DATABASE IF NOT EXISTS `{slug}`",
        f"CREATE USER IF NOT EXISTS `{user}`@'%' IDENTIFIED BY '{password}'",
        f"GRANT ALL ON `{slug}`.* TO `{user}`@'%'",
    )

    # Staged, then published by one rename. A crash anywhere before it leaves a
    # .tmp dir that nothing reads and no tracker dir, so the next call simply
    # redoes the whole thing. That is why there is no lock and no recovery path.
    tmp = root / f".tmp-{slug}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "metadata.json").write_text(json.dumps({
        "database": "dolt",
        "backend": "dolt",
        "dolt_mode": "server",
        "dolt_database": slug,
    }, indent=2) + "\n")
    # Two-phase config: `bd migrate` below runs INSIDE the compose
    # network, where the service DNS name is the reachable address — so
    # the staged dir migrates against `dolt`, and the PUBLISHED file
    # carries the host-reachable binding per the module docstring's
    # convention. The compose-DNS transient never survives the rename.
    (tmp / "config.yaml").write_text(
        f"# staged for schema migration — rewritten before publication\n"
        f"no-git-ops: true\n"
        f"dolt:\n  host: dolt\n  port: 3306\n"
    )
    creds = tmp / "credentials.env"
    creds.write_text(
        f"BEADS_DOLT_SERVER_USER={user}\nBEADS_DOLT_PASSWORD={password}\n"
    )
    creds.chmod(0o600)

    subprocess.run(
        ["bd", "migrate", "schema"],
        env={**os.environ, "BEADS_DIR": str(tmp),
             "BEADS_DOLT_SERVER_USER": user, "BEADS_DOLT_PASSWORD": password},
        check=True, capture_output=True, timeout=300,
    )
    dolt_sql(
        f"REPLACE INTO `{slug}`.config (`key`, `value`) "
        f"VALUES ('issue_prefix', '{slug}')"
    )

    (tmp / "config.yaml").write_text(
        _config_yaml_text(slug, _dolt_host_binding(_dolt_container()))
    )
    os.rename(tmp, final)
    return final


def beads_dir_for_write(org: str | None):
    """The tracker dir a WRITE for *org* must go to, provisioning on first sight.

    Returns None for an unscoped write, which belongs in the shared tracker.

    On a host process there is no compose project and no socket path to a Dolt
    container, so an unprovisioned org falls back to the shared tracker with a
    warning. That branch exists only for the legacy host install and goes away
    with it; on a node, an org that cannot be provisioned raises rather than
    filing a bead into another org's database.
    """
    if not org:
        return None
    final = _orgs_root() / str(org)
    if (final / "metadata.json").is_file():
        return final
    from agents.mount_plan import _own_container_id
    if not _own_container_id():
        logger.warning(
            "beads: org %r has no tracker and this is a host process — writing to "
            "the shared tracker. A node provisions instead.", org,
        )
        return None
    return ensure_org_beads_dir(str(org))


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="tools.beads_provision",
        description="Org bead-tracker provisioning maintenance",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    repair = sub.add_parser(
        "repair-config",
        help="Rewrite an existing tracker's config.yaml to the "
             "host-reachable convention (run inside the node container)",
    )
    repair.add_argument("slug")
    args = parser.parse_args(argv)
    if args.cmd == "repair-config":
        try:
            text = repair_config(args.slug)
        except BeadsProvisionError as exc:
            print(f"repair-config: {exc}", file=__import__("sys").stderr)
            return 1
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
