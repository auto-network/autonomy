"""Idempotent first-run initialization (bead auto-q1fsp, H3).

Turns a fresh checkout on a clean machine into a working **empty**
deployment, from nothing:

* data directories (``data/``, ``data/orgs``, ``data/agent-runs``,
  ``data/session-traces``),
* per-org DBs: ``personal`` plus one operator-named first org,
* default Settings — org identity seeds plus the bootstrap
  public-surface allowlist (``autonomy.org.bootstrap-allowlist#1``,
  seeded from the committed curation YAML; ties to bead auto-mu1n1),
* dashboard-side operational DBs (dashboard / auth / dispatch /
  approval-requests / commit-workflow / identity sessions),
* a TLS keypair at ``data/tls.crt`` + ``data/tls.key`` (self-signed;
  ``start-dashboard.sh`` picks the pair up automatically). For a
  browser-trusted cert use Tailscale (``renew-tls-cert.sh``) or
  terminate TLS in a tunnel/reverse proxy with Let's Encrypt — see
  ``DEPLOY.md``.

Design: graph://dc310166-911. Every step is idempotent — pre-existing
files, DBs, org rows and Settings are left untouched, so running init
twice is a no-op the second time (``InitReport.changed`` is ``False``).
Nothing here assumes seeded content: zero pre-existing graph rows is
the expected state, not an error.

Library entry point is :func:`initialize`; the CLI wrapper lives in
``__main__.py`` (``python -m tools.init``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from tools.data_paths import resolve_store

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories the running system expects under <root>/data. Kept to the
# documented set (DEPLOY.md); services create deeper structure lazily.
DATA_SUBDIRS = ("orgs", "agent-runs", "session-traces")

ALLOWLIST_YAML = (
    REPO_ROOT / "tools" / "graph" / "curation" / "autonomy-bootstrap-allowlist.yaml"
)

# Step actions
CREATED = "created"
EXISTS = "exists"
SKIPPED = "skipped"
PENDING = "pending"   # accepted, awaiting work outside first-run (a join)
FAILED = "failed"


class InitConflict(ValueError):
    """Mutually exclusive first-run options were supplied together."""


@dataclass
class InitStep:
    name: str
    action: str  # created | exists | skipped
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "action": self.action, "detail": self.detail}


@dataclass
class InitReport:
    root: str
    steps: list[InitStep] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """True when any step actually created something this run."""
        return any(s.action == CREATED for s in self.steps)

    def add(self, name: str, action: str, detail: str = "") -> InitStep:
        step = InitStep(name=name, action=action, detail=detail)
        self.steps.append(step)
        return step

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "changed": self.changed,
            "steps": [s.to_dict() for s in self.steps],
        }


def initialize(
    root: Path | str | None = None,
    *,
    first_org: str | None = None,
    first_org_name: str | None = None,
    invite: str | None = None,
    join_transport=None,
    fleet_invite: str | None = None,
    fleet_client=None,
    tls: bool = True,
    tls_domain: str | None = None,
) -> InitReport:
    """Initialize an empty deployment under *root* (default: this checkout).

    Idempotent: every step detects pre-existing state and reports
    ``exists`` instead of touching it. ``first_org`` names the first
    shared org (falls back to ``AUTONOMY_FIRST_ORG`` env, then
    ``autonomy``); ``first_org_name`` sets its display name.

    ``invite`` (``AUTONOMY_INVITE``) selects the organization JOIN path.
    ``fleet_invite`` (``AUTONOMY_FLEET_INVITE``) selects the personal Fleet
    enrollment path. Either path means the node does not found a shared org;
    node founds no org of its own and instead claims membership in the
    inviting identity (auto-8v5ri / auto-b6fee). A node has exactly one
    bootstrap mode — passing more than one is a configuration error,
    because the two produce different identities and quietly picking one
    would strand state under the other.
    """
    _validate_bootstrap_choice(
        first_org=first_org,
        invite=invite,
        fleet_invite=fleet_invite,
    )
    root = Path(root).resolve() if root is not None else REPO_ROOT
    data = root / "data"
    return _initialize_data_root(
        data,
        report_root=root,
        first_org=first_org,
        first_org_name=first_org_name,
        invite=invite,
        join_transport=join_transport,
        fleet_invite=fleet_invite,
        fleet_client=fleet_client,
        tls=tls,
        tls_domain=tls_domain,
    )


def initialize_data_root(
    data: Path | str,
    *,
    first_org: str | None = None,
    first_org_name: str | None = None,
    invite: str | None = None,
    join_transport=None,
    fleet_invite: str | None = None,
    fleet_client=None,
    tls: bool = True,
    tls_domain: str | None = None,
) -> InitReport:
    """Initialize/migrate an already-mounted node data volume directly.

    Unlike :func:`initialize`, *data* is the volume itself rather than a
    checkout/deployment root whose ``data/`` child is the volume.
    """
    _validate_bootstrap_choice(
        first_org=first_org or os.environ.get("AUTONOMY_FIRST_ORG"),
        invite=invite,
        fleet_invite=fleet_invite,
    )
    data = Path(data).resolve()
    return _initialize_data_root(
        data,
        report_root=data,
        first_org=first_org,
        first_org_name=first_org_name,
        invite=invite,
        join_transport=join_transport,
        fleet_invite=fleet_invite,
        fleet_client=fleet_client,
        production_join_transport=True,
        tls=tls,
        tls_domain=tls_domain,
    )


def _initialize_data_root(
    data: Path,
    *,
    report_root: Path,
    first_org: str | None,
    first_org_name: str | None,
    invite: str | None,
    join_transport,
    fleet_invite: str | None,
    fleet_client,
    production_join_transport: bool = False,
    tls: bool,
    tls_domain: str | None,
) -> InitReport:
    report = InitReport(root=str(report_root))
    _init_data_dirs(data, report)
    fleet_invitation = None
    if invite:
        invitation = _init_join(data, report, invite=invite)
    elif fleet_invite:
        fleet_invitation = _init_fleet_join(
            data, report, invite=fleet_invite
        )
    else:
        _init_orgs(data, report, first_org=first_org, first_org_name=first_org_name)
    _init_operational_dbs(data, report)
    _seed_bootstrap_allowlist(data, report)
    if tls:
        _init_tls(data, report, domain=tls_domain)
    else:
        report.add("tls", SKIPPED, "disabled by caller (--no-tls)")
    if invite and join_transport is not None:
        # The ceremony runs last: it writes the personal identity into
        # stores the steps above just created, and it reaches the network.
        _run_join(report, invitation, join_transport)
    elif invite and production_join_transport:
        from tools.init.join import production_transport

        _run_join(report, invitation, production_transport(invitation))
    if fleet_invitation is not None:
        _run_fleet_join(report, fleet_invitation, client=fleet_client)
    return report


def _validate_bootstrap_choice(
    *, first_org: str | None, invite: str | None, fleet_invite: str | None
) -> None:
    selected = [
        name for name, value in (
            ("AUTONOMY_FIRST_ORG", first_org),
            ("AUTONOMY_INVITE", invite),
            ("AUTONOMY_FLEET_INVITE", fleet_invite),
        ) if value
    ]
    if len(selected) > 1:
        raise InitConflict(
            "a node has exactly one first-run bootstrap mode; conflicting "
            f"inputs: {', '.join(selected)}"
        )


def _run_join(report: InitReport, invitation, transport) -> None:
    """Run the join ceremony and fold its outcome into the report.

    A transport is injected rather than constructed here so first-run
    stays testable in-process and the production channel client (B4b /
    the B6 harness) is chosen by the caller.
    """
    from tools.init.join import (
        JoinError,
        join_existing_identity,
        join_org,
        persist_outcome,
        personal_identity_exists,
        read_personal_password,
    )

    try:
        password = read_personal_password()
        if personal_identity_exists():
            if password is None:
                raise JoinError(
                    "a personal identity exists but no one-time stdin password "
                    "was available to resume the join; complete this operation "
                    "through an approved interactive identity ceremony"
                )
            outcome = join_existing_identity(
                invitation, transport, password=password
            )
        else:
            outcome = join_org(
                invitation, transport, password=password
            )
        persist_outcome(outcome)
    except JoinError as exc:
        report.add("join", FAILED, str(exc))
        raise
    report.add(f"join:{outcome.state}", CREATED, outcome.detail)


def _run_fleet_join(report: InitReport, invitation, *, client=None) -> None:
    """Submit or recover one Fleet enrollment request for this installation.

    This is deliberately the pre-authorization half of enrollment. The retry
    record carries the installation's public random ``machine_id``, but the
    durable local identity row, root armor, derived machine key, and roster
    authority do not exist on the joining node until verified completion.
    """
    from tools.network import machine_boot
    from tools.network.fleet_enrollment_client import (
        FleetEnrollmentClient,
        FleetEnrollmentClientError,
    )

    # BOOTSTRAP-ONCE. init re-runs on EVERY container boot with the invite
    # still in the environment, but the ceremony must run at most once per
    # machine. Without these guards a restart AFTER a completed join re-ran the
    # ceremony against home, read the already-consumed request's reply as
    # terminal-negative, DELETED the saved join state (armor included), and a
    # subsequent pass minted a brand-new machine identity — a ghost 'new
    # machine awaiting approval' duplicating an already-enrolled node
    # (observed live on SJC 2026-09-05 20:23, request 3866d32c).
    if machine_boot.has_identity():
        report.add(
            "fleet-enrollment", EXISTS,
            "machine already enrolled; ceremony not re-run",
        )
        return
    enrollment_client = client or FleetEnrollmentClient()
    saved = enrollment_client.state_store.latest_any()
    if saved is not None and \
            enrollment_client.state_store.load_delivery(saved.request_id) is not None:
        # Approved and delivered — only the browser completion remains.
        # Touch nothing; re-resuming a consumed request risks state loss.
        report.add(
            "fleet-enrollment:approved", PENDING,
            "approved; unlock this Dashboard with the personal password "
            "to finish machine enrollment",
        )
        return
    try:
        recovery = asyncio.run(enrollment_client.start_or_recover(invitation))
        result = asyncio.run(enrollment_client.resume(recovery))
    except FleetEnrollmentClientError as exc:
        report.add("fleet-enrollment", FAILED, str(exc))
        raise
    if result.status == "declined":
        # A decline ends THIS request, but never silently destroys a join
        # that already delivered armor (guarded above). Deleting only the
        # undelivered retry record is safe: nothing irreplaceable is in it.
        enrollment_client.state_store.delete(recovery.request_id)
        report.add(
            "fleet-enrollment:declined",
            FAILED,
            "the parent Dashboard declined this machine request",
        )
        return
    if result.status == "expired":
        report.add(
            "fleet-enrollment:expired",
            FAILED,
            "the Fleet invitation expired before this machine was approved",
        )
        return
    if result.status == "approved":
        if (
            result.delivery is None
            or result.personal_root_armor is None
            or result.personal_root_created_at is None
            or result.personal_root_updated_at is None
        ):
            raise FleetEnrollmentClientError(
                "approved fleet enrollment returned incomplete delivery"
            )
        _store_fleet_personal_armor(
            result.personal_root_armor,
            expected_root_pub=invitation.personal_root_pub,
            source_created_at=result.personal_root_created_at,
            source_updated_at=result.personal_root_updated_at,
        )
        enrollment_client.state_store.save_delivery(
            recovery.request_id,
            result.delivery,
        )
        report.add(
            "fleet-enrollment:approved",
            PENDING,
            "approved; unlock this Dashboard with the personal password "
            "to finish machine enrollment",
        )
        return
    report.add(
        "fleet-enrollment:pending", PENDING,
        "compare this code on both Dashboards: "
        f"{recovery.verification_code}",
    )


def _store_fleet_personal_armor(
    armor: str, *, expected_root_pub: str,
    source_created_at: str, source_updated_at: str,
) -> None:
    """Store only the canonical encrypted root delivered after approval."""
    import time

    from tools.dashboard import identity_routes
    from tools.graph import settings_ops
    from tools.graph.schemas.personal_identity import (
        PERSONAL_IDENTITY_REVISION,
        PERSONAL_IDENTITY_SET_ID,
    )
    from tools.network.idkit.armor import armor_root_pub, canonicalize_armor

    canonical = canonicalize_armor(armor)
    root_pub = armor_root_pub(canonical)
    if root_pub != expected_root_pub:
        raise ValueError("delivered personal armor has a different root anchor")
    existing = identity_routes._personal_member()
    if existing is not None:
        if existing.payload.get("root_pub") != root_pub:
            raise ValueError(
                "this machine already carries a different personal identity"
            )
        return
    with settings_ops.identity_write_context():
        settings_ops.upsert_by_key(
            PERSONAL_IDENTITY_SET_ID,
            PERSONAL_IDENTITY_REVISION,
            identity_routes.PERSONAL_CANONICAL_LABEL,
            {
                "armored_private_key": canonical,
                "root_pub": root_pub,
                "display_name": "Fleet member",
                "created_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                ),
            },
            org=None,
            _source_created_at=source_created_at,
            _source_updated_at=source_updated_at,
        )


# ── Steps ────────────────────────────────────────────────────


def _init_data_dirs(data: Path, report: InitReport) -> None:
    for rel in ("",) + DATA_SUBDIRS:
        path = data / rel if rel else data
        name = f"dir:data/{rel}" if rel else "dir:data"
        if path.is_dir():
            report.add(name, EXISTS, str(path))
        else:
            path.mkdir(parents=True, exist_ok=True)
            report.add(name, CREATED, str(path))


def _init_orgs(
    data: Path,
    report: InitReport,
    *,
    first_org: str | None,
    first_org_name: str | None,
) -> None:
    from tools.graph import org_ops

    from tools.graph.db import _org_db_path

    orgs_root = resolve_store("orgs", root=data)
    slug = org_ops.resolve_first_org_slug(first_org)
    for org_slug in ((slug, "personal") if slug else ("personal",)):
        # Routed: "personal" is a local store living beside orgs/
        # (auto-35kmy); checking the legacy path would report it created
        # on every run and break second-run idempotence.
        org_path = _org_db_path(org_slug, orgs_root)
        report.add(
            f"org:{org_slug}",
            EXISTS if org_path.exists() else CREATED,
            str(org_path),
        )
    org_ops.ensure_bootstrap_orgs(
        root=orgs_root, first_org=slug, first_org_name=first_org_name,
    )

    if slug:
        _seed_shell_default_org(data, report, slug)


def _init_join(data: Path, report: InitReport, *, invite: str) -> None:
    """Prepare the JOIN path: personal identity store + a validated code.

    The invitation is decoded and fully validated HERE — before any
    network call — so a mistyped code fails at first-run with a clear
    report instead of a half-formed request. The org side is not created
    locally: membership arrives from the inviting org's ledger.
    """
    from tools.graph import org_ops
    from tools.network.invitation import InvitationError, decode_invitation

    from tools.graph.db import _local_store_db_path

    orgs_root = resolve_store("orgs", root=data)
    personal_path = _local_store_db_path("personal", orgs_root)
    existed = personal_path.exists()
    org_ops.ensure_bootstrap_orgs(root=orgs_root, first_org=None, personal_only=True)
    report.add(
        "org:personal", EXISTS if existed else CREATED,
        str(_local_store_db_path("personal", orgs_root)),
    )
    try:
        invitation = decode_invitation(invite)
    except InvitationError as exc:
        report.add("join", FAILED, str(exc))
        raise
    # Never the token: the report is printed and logged.
    report.add(
        "join",
        PENDING,
        f"org {invitation.org} invite {invitation.invite_ref[:12]}… "
        f"root {invitation.root_pub[:12]}…",
    )
    return invitation


def _init_fleet_join(data: Path, report: InitReport, *, invite: str):
    """Prepare a Fleet-linked install without creating a shared org.

    The copied installer value may be either the bare encoded invitation or
    the literal shell assignment rendered by Fleet Machines. Only public
    invitation metadata is reported; the rendezvous bearer stays out of logs.
    """
    from tools.graph import org_ops
    from tools.graph.db import _local_store_db_path
    from tools.network import fleet_invite

    orgs_root = resolve_store("orgs", root=data)
    personal_path = _local_store_db_path("personal", orgs_root)
    existed = personal_path.exists()
    org_ops.ensure_bootstrap_orgs(
        root=orgs_root, first_org=None, personal_only=True
    )
    report.add(
        "org:personal",
        EXISTS if existed else CREATED,
        str(personal_path),
    )
    code = invite.strip()
    prefix = "AUTONOMY_FLEET_INVITE="
    if code.startswith(prefix):
        code = code[len(prefix):]
    invitation = fleet_invite.decode(code)
    fleet_invite.verify(invitation)
    report.add(
        "fleet-invitation",
        PENDING,
        f"invite {invitation.invite_id[:12]}… "
        f"root {invitation.personal_root_pub[:12]}…",
    )
    return invitation


def _init_operational_dbs(data: Path, report: InitReport) -> None:
    """Dashboard-side sqlite stores, each via its own idempotent init_db."""
    from agents import dispatch_db
    from tools.dashboard.dao import (
        approval_requests,
        auth_db,
        commit_workflow_db,
        dashboard_db,
        identity_sessions,
        pending_joins,
    )

    # Manifest keys, not bare filenames: init must lay each store down
    # where its readers resolve it (env first), or rooting a deployment
    # splits the writer from every reader (auto-lr6gu).
    stores = (
        ("dashboard", dashboard_db.init_db),
        ("auth", auth_db.init_db),
        ("dispatch", dispatch_db.init_db),
        ("approval_requests", approval_requests.init_db),
        ("commit_workflow", commit_workflow_db.init_db),
        ("identity_sessions", identity_sessions.init_db),
        ("pending_joins", pending_joins.init_db),
    )
    for key, init_fn in stores:
        path = resolve_store(key, root=data)
        filename = path.name
        existed = path.exists()
        init_fn(path)
        report.add(filename, EXISTS if existed else CREATED, str(path))


def _seed_shell_default_org(data: Path, report: InitReport, slug: str) -> None:
    """Declare the shell's default org for this node (dashboard.shell.default-org).

    # org-scope: machine — the dashboard renders from this declaration;
    # no ambient scope exists. Written by explicit path under *data*, like
    # every root-scoped seed here: ambient resolution would split-brain a
    # writer handed a root from readers knowing only the env.
    """
    from uuid import uuid4

    from tools.graph import schemas
    from tools.graph.db import GraphDB, _local_store_db_path
    from tools.graph.org_ops import _now_iso
    from tools.graph.schemas.dashboard_shell import (
        SHELL_DEFAULT_ORG_KEY,
        SHELL_DEFAULT_ORG_REVISION,
        SHELL_DEFAULT_ORG_SET_ID,
    )

    payload = {"org": slug}
    schemas.validate_payload(
        SHELL_DEFAULT_ORG_SET_ID, SHELL_DEFAULT_ORG_REVISION, payload,
    )
    machine = _local_store_db_path("machine", resolve_store("orgs", root=data))
    name = "shell-default-org"
    db = GraphDB(machine, create=True)
    try:
        row = db.conn.execute(
            "SELECT id FROM settings WHERE set_id = ? AND key = ?",
            (SHELL_DEFAULT_ORG_SET_ID, SHELL_DEFAULT_ORG_KEY),
        ).fetchone()
        if row is not None:
            report.add(name, EXISTS, f"{slug} (machine.db)")
            return
        now = _now_iso()
        db.conn.execute(
            "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (str(uuid4()), SHELL_DEFAULT_ORG_SET_ID,
             SHELL_DEFAULT_ORG_REVISION, SHELL_DEFAULT_ORG_KEY,
             json.dumps(payload), now, now),
        )
        db.conn.commit()
    finally:
        db.close()
    report.add(name, CREATED, f"{slug} (machine.db)")


def _seed_bootstrap_allowlist(data: Path, report: InitReport) -> None:
    """Seed ``autonomy.org.bootstrap-allowlist#1`` into ``personal.db``.

    Records the upstream reference org's public surface (from the
    committed curation YAML) so an empty deployment knows what canonical
    content it subscribes to — without shipping any of that content
    (graph://dc310166-911, comment a13a28c1). Skipped when the YAML is
    absent; left untouched when the Setting already exists.
    """
    from tools.graph import schemas
    from tools.graph.curation import allowlist as allowlist_mod
    from tools.graph.db import GraphDB
    from tools.graph.org_ops import _now_iso, uuid7
    from tools.graph.schemas.bootstrap_allowlist import SCHEMA_REVISION, SET_ID

    name = "setting:bootstrap-allowlist"
    if not ALLOWLIST_YAML.exists():
        report.add(name, SKIPPED, f"allowlist YAML not found: {ALLOWLIST_YAML}")
        return

    try:
        allow = allowlist_mod.load(ALLOWLIST_YAML)
    except Exception as exc:
        report.add(name, SKIPPED, f"allowlist YAML unreadable: {exc}")
        return

    payload = {
        "version": allow.version,
        "canonical": list(allow.canonical),
        "published": list(allow.published),
        "source": str(ALLOWLIST_YAML.relative_to(REPO_ROOT)),
    }
    schemas.validate_payload(SET_ID, SCHEMA_REVISION, payload)

    from tools.graph.db import _local_store_db_path

    personal = _local_store_db_path("personal", resolve_store("orgs", root=data))
    if not personal.exists():
        report.add(name, SKIPPED, f"personal org DB missing: {personal}")
        return

    db = GraphDB(personal)
    try:
        row = db.conn.execute(
            "SELECT id FROM settings WHERE set_id = ? AND key = ? "
            "AND schema_revision = ?",
            (SET_ID, allow.org, SCHEMA_REVISION),
        ).fetchone()
        if row is not None:
            report.add(name, EXISTS, f"{SET_ID}#{SCHEMA_REVISION} key={allow.org}")
            return
        now = _now_iso()
        db.conn.execute(
            "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
            "created_at, updated_at, expires_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                uuid7(), SET_ID, SCHEMA_REVISION, allow.org,
                json.dumps(payload), now, now,
                schemas.cache_expires_at(SET_ID, SCHEMA_REVISION, now),
            ),
        )
        db.conn.commit()
        report.add(name, CREATED, f"{SET_ID}#{SCHEMA_REVISION} key={allow.org}")
    finally:
        db.close()


def _init_tls(data: Path, report: InitReport, *, domain: str | None) -> None:
    """Self-signed keypair at ``data/tls.crt`` + ``data/tls.key``.

    Good enough for LAN/tailnet HTTPS (browsers warn once). Never
    overwrites: an existing pair reports ``exists``; a half-pair is left
    alone for the operator to resolve. Missing/failing ``openssl``
    degrades to ``skipped`` — the dashboard then serves plain HTTP.
    """
    # Volume-contract rooted (auto-lr6gu): AUTONOMY_TLS_CERT/_KEY when
    # set, else under this init's data root. Previously derivable only
    # from init's own argument, so nothing else could relocate them.
    crt = resolve_store("tls_cert", root=data)
    key = resolve_store("tls_key", root=data)
    if crt.exists() and key.exists():
        report.add("tls", EXISTS, f"{crt} + {key}")
        return
    if crt.exists() or key.exists():
        half = crt if crt.exists() else key
        report.add(
            "tls", SKIPPED,
            f"refusing to complete half-pair (only {half.name} exists); "
            f"remove it or supply the missing file",
        )
        return

    cn = domain or os.environ.get("DASHBOARD_DOMAIN") or socket.gethostname()
    san = f"DNS:localhost,IP:127.0.0.1,DNS:{socket.gethostname()}"
    if domain or os.environ.get("DASHBOARD_DOMAIN"):
        san += f",DNS:{cn}"
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256",
        "-days", "825", "-nodes",
        "-keyout", str(key), "-out", str(crt),
        "-subj", f"/CN={cn}",
        "-addext", f"subjectAltName={san}",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        report.add("tls", SKIPPED, f"openssl unavailable ({exc}); dashboard will serve HTTP")
        return
    if proc.returncode != 0:
        # Never leave a half-pair behind on failure.
        for p in (crt, key):
            p.unlink(missing_ok=True)
        report.add(
            "tls", SKIPPED,
            f"openssl failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}",
        )
        return
    key.chmod(0o600)
    report.add("tls", CREATED, f"self-signed CN={cn} → {crt} + {key}")
