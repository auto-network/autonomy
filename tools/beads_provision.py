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
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess

from tools.data_paths import DATA_ROOT

ORGS = DATA_ROOT / ".beads" / "orgs"
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


def dolt_sql(*statements: str) -> None:
    """Run each statement as the container's local superuser."""
    container = _dolt_container()
    for statement in statements:
        _docker("exec", container, "dolt", "sql", "-q", statement, timeout=300)


def ensure_org_beads_dir(slug: str):
    """The org's bead tracker dir, provisioning it on first sight."""
    final = ORGS / slug
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
    tmp = ORGS / f".tmp-{slug}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "metadata.json").write_text(json.dumps({
        "database": "dolt",
        "backend": "dolt",
        "dolt_mode": "server",
        "dolt_database": slug,
    }, indent=2) + "\n")
    (tmp / "config.yaml").write_text(
        f"# {slug} org tracker: its own database on the node's Dolt server.\n"
        "no-git-ops: true\n"
        "image: autonomy-session-platform\n"
        "dolt:\n"
        "  host: dolt\n"
        "  port: 3306\n"
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

    os.rename(tmp, final)
    return final
