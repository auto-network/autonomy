"""Dolt/MySQL DAO for bead data (read-only dashboard queries).

Connects directly to the Dolt SQL server on :3306 via pymysql, bypassing
the `bd` CLI subprocess.  This module never writes — all mutations still
go through `bd` (state machine, validation, audit trail).

Connection strategy: thread-local lazy connection with ping/reconnect.
The dashboard server calls these functions via asyncio.to_thread(), so
each worker thread gets its own pymysql connection.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import pymysql
import pymysql.cursors

from tools import data_paths

# ── Where the Dolt server is, PER ORG ───────────────────────────────────
#
# There is ONE source of truth for how to reach a bead tracker, and it is the
# per-org beads config the `bd` CLI already obeys — resolved through
# tools.data_paths, NEVER re-derived here. ``data_paths.org_beads_dir(org)``
# picks the org's provisioned dir (or None -> the shared DATA_ROOT/.beads,
# which is autonomy's home). That dir alone yields the credential
# (credentials.env -> the per-org SQL user ``beads_<org>``; operator ruling
# 2026-08-24: no shared root), the database (metadata.json ``dolt_database``:
# autonomy/shared -> "auto", a provisioned org -> its own name) and the
# host/port (config.yaml -> 172.17.0.1 in the host deployment). Every OTHER
# beads consumer already goes through
# ``data_paths.beads_client_env(org_beads_dir(org))`` — the bd-CLI reads, the
# mission bridge, the session launcher — so this DAO doing the same is what
# keeps the two from diverging.
#
# This is deliberately routed through data_paths.DATA_ROOT and NOT a hardcoded
# path: the old code defaulted BEADS_DIR to "/data" (a container convention),
# host to 127.0.0.1 and user to root. The dashboard runs NATIVELY on the host
# with none of those env vars set, so it resolved to a nonexistent
# /data/.beads, read no config, fell to 127.0.0.1 (where nothing listens —
# dolt binds only 172.17.0.1) and every DAO read degraded to empty. Resolving
# from data_paths.DATA_ROOT fixes that at the root, for every org.
#
# Precedence, highest first: an explicit DOLT_SQL_* env var (the Compose
# distribution sets DOLT_SQL_HOST=dolt — docker-compose.yml, DEPLOY.md) for
# operational overrides; then the per-org files; then a last-resort same-host
# default so an unprovisioned box degrades rather than crashes.


def _pick(*values, default):
    """First value actually provided (not None); else the default. ``or``
    would wrongly skip a deliberately-empty password, so test for None."""
    for v in values:
        if v is not None:
            return v
    return default


def _config_host_port(beads_dir: Path) -> tuple[str | None, int | None]:
    """(host, port) from the ``dolt:`` block of a beads dir's config.yaml.

    A minimal block parser (no YAML dependency), matching how
    agents/start-dolt.sh reads the same file.
    """
    host: str | None = None
    port: int | None = None
    try:
        with open(beads_dir / "config.yaml", encoding="utf-8") as fh:
            in_dolt = False
            for raw in fh:
                line = raw.rstrip("\n")
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                if not line[0].isspace():
                    in_dolt = line.strip().rstrip(":") == "dolt"
                    continue
                if in_dolt:
                    key, _, val = line.strip().partition(":")
                    val = val.strip()
                    if key == "host" and val:
                        host = val
                    elif key == "port" and val:
                        try:
                            port = int(val)
                        except ValueError:
                            pass
    except OSError:
        pass
    return host, port


def _dolt_database(beads_dir: Path) -> str | None:
    """The org's Dolt database name from its metadata.json, or None.

    Per-org databases (autonomy@74585ba): each org's metadata.json carries
    ``dolt_database`` (autonomy/shared -> "auto", anchore -> "anchore"). This
    is authoritative — DOLT_SQL_DATABASE is not set in this deployment.
    """
    try:
        meta = json.loads((beads_dir / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    db = meta.get("dolt_database")
    return db if isinstance(db, str) and db else None


def _credential(beads_dir: Path, name: str) -> str | None:
    """One key from a beads dir's credentials.env, or None."""
    try:
        with open(beads_dir / "credentials.env", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line.startswith("#"):
                    continue
                key, sep, val = line.partition("=")
                if sep and key == name:
                    return val
    except OSError:
        pass
    return None


def _beads_root() -> Path:
    """The beads state root. An explicit ``BEADS_DIR`` (a container mount)
    wins; otherwise ``data_paths.DATA_ROOT/.beads`` — the host-native default,
    which is what the dashboard uses because it sets no BEADS_DIR. This mirrors
    ``run_cli``'s rule exactly ("explicit BEADS_DIR still wins"), so the DAO
    and the bd CLI resolve to the same tree in every topology. The old bug was
    a hardcoded "/data" here, which does not exist on the host and sent every
    read to 127.0.0.1 (nothing listens there — dolt binds 172.17.0.1 only).
    """
    env = os.environ.get("BEADS_DIR")
    return Path(env) if env else data_paths.DATA_ROOT / ".beads"


def _conn_params(org: str | None) -> dict:
    """Resolve one org's Dolt connection from that org's OWN files — the same
    files the bd CLI reads (config.yaml host/port, credentials.env for the
    per-org ``beads_<org>`` user, metadata.json for the database). See the
    module comment: never re-derive a beads connection elsewhere; call this.
    ``org=None`` (or an org with no provisioned dir, e.g. autonomy) resolves to
    the shared tracker + ``auto``.
    """
    base = _beads_root()
    effective = base
    if org:
        candidate = base / "orgs" / str(org)
        if (candidate / "metadata.json").is_file():
            effective = candidate
    cfg_host, cfg_port = _config_host_port(effective)
    return {
        "host": _pick(os.environ.get("DOLT_SQL_HOST"), cfg_host, default="127.0.0.1"),
        "port": int(_pick(os.environ.get("DOLT_SQL_PORT"), cfg_port, default=3306)),
        "user": _pick(os.environ.get("DOLT_SQL_USER"),
                      _credential(effective, "BEADS_DOLT_SERVER_USER"), default="root"),
        "password": _pick(os.environ.get("DOLT_SQL_PASSWORD"),
                          _credential(effective, "BEADS_DOLT_PASSWORD"), default=""),
        "database": _pick(os.environ.get("DOLT_SQL_DATABASE"),
                          _dolt_database(effective), default="auto"),
    }


_local = threading.local()

_logger = logging.getLogger(__name__)
_unreachable_logged = False


def _degrade_when_unreachable(default_factory):
    """Return a safe empty default when the Dolt server is unreachable.

    A deployment without the beads toolchain (no dolt service — see
    DEPLOY.md) is a supported empty state, not an error: readers get
    zero beads instead of a 500 on every beads surface. Logged once per
    process, at warning, so a misconfigured host is still diagnosable
    (the exception carries the exact host:port it tried).
    """
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            global _unreachable_logged
            try:
                return fn(*args, **kwargs)
            except (pymysql.err.MySQLError, OSError) as exc:
                if not _unreachable_logged:
                    _unreachable_logged = True
                    _logger.warning(
                        "dolt unreachable (%s) — beads surfaces degrade to "
                        "empty until it comes back", exc,
                    )
                return default_factory()
        return wrapper
    return decorate


def _connect(org: str | None) -> pymysql.Connection:
    p = _conn_params(org)
    return pymysql.connect(
        host=p["host"],
        port=p["port"],
        user=p["user"],
        password=p["password"],
        database=p["database"],
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=5,
    )


def _get_conn(org: str | None = None) -> pymysql.Connection:
    """Thread-local pymysql connection PER ORG — each org has its own SQL user
    and database — reconnecting on error. Keyed by org so a read for one org
    never rides another org's connection.
    """
    key = org or ""
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = {}
        _local.conns = conns
    conn = conns.get(key)
    if conn is None:
        conn = _connect(org)
        conns[key] = conn
        return conn
    try:
        conn.ping(reconnect=True)
    except Exception:
        conn = _connect(org)
        conns[key] = conn
    return conn


def _rows(cur: pymysql.cursors.DictCursor) -> list[dict]:
    return list(cur.fetchall())


def _parse_labels(raw: str | None) -> list[str]:
    """Split a GROUP_CONCAT label string into a list."""
    if not raw:
        return []
    return raw.split(",")


def _coerce(row: dict) -> dict:
    """Convert datetime objects to ISO strings for JSON-safe output."""
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat() + "Z"
        elif k == "labels" and isinstance(v, str):
            out[k] = _parse_labels(v)
        else:
            out[k] = v
    return out


# ── Shared SQL fragments ───────────────────────────────────────────────

_BEAD_COLS = """
    i.id, i.title, i.status, i.priority, i.issue_type,
    i.description, i.created_at, i.updated_at,
    i.assignee, i.estimated_minutes, i.close_reason,
    GROUP_CONCAT(l.label ORDER BY l.label SEPARATOR ',') AS labels
"""


# ── Public API ─────────────────────────────────────────────────────────

@_degrade_when_unreachable(lambda: {
    "approved_waiting": [], "approved_waiting_total": 0,
    "approved_blocked": [],
})
def get_dispatch_beads(waiting_limit: int | None = None) -> dict:
    """Return beads grouped by dispatch role for the Dispatch page.

    Returns a dict with:
    - "approved_waiting": readiness:approved, open, all blocking deps
      satisfied (closed).  Active dispatches are driven by SQLite
      dispatch_runs (status=RUNNING), not Dolt labels.
    - "approved_waiting_total": full count of the above — with
      ``waiting_limit`` set, the LIST is truncated by SQL LIMIT (the
      dispatch page shows the top few plus the count; shipping a
      thousand-row payload every watcher tick helped no one) while the
      total stays exact via a COUNT query.
    - "approved_blocked": readiness:approved, open, at least one open
      non-parent-child dependency.

    Deps are resolved server-side via SQL — no N+1 bd subprocess calls.
    """
    conn = _get_conn()
    with conn.cursor() as cur:

        # Approved waiting: open, readiness:approved, no open blocking deps
        limit_sql = ""
        if waiting_limit is not None:
            limit_sql = f" LIMIT {int(waiting_limit)}"
        cur.execute(
            f"""
            SELECT {_BEAD_COLS}
            FROM issues i
            JOIN labels la ON la.issue_id = i.id AND la.label = %s
            LEFT JOIN labels l ON l.issue_id = i.id
            WHERE i.status = %s
              AND NOT EXISTS (
                  SELECT 1 FROM dependencies d
                  JOIN issues di ON di.id = d.depends_on_issue_id
                  WHERE d.issue_id = i.id
                    AND d.type != %s
                    AND di.status != %s
              )
            GROUP BY i.id
            ORDER BY i.priority ASC, i.updated_at DESC{limit_sql}
            """,
            ("readiness:approved", "open", "parent-child", "closed"),
        )
        approved_waiting = [_coerce(r) for r in _rows(cur)]

        if waiting_limit is None:
            approved_waiting_total = len(approved_waiting)
        else:
            cur.execute(
                """
                SELECT COUNT(DISTINCT i.id)
                FROM issues i
                JOIN labels la ON la.issue_id = i.id AND la.label = %s
                WHERE i.status = %s
                  AND NOT EXISTS (
                      SELECT 1 FROM dependencies d
                      JOIN issues di ON di.id = d.depends_on_issue_id
                      WHERE d.issue_id = i.id
                        AND d.type != %s
                        AND di.status != %s
                  )
                """,
                ("readiness:approved", "open", "parent-child", "closed"),
            )
            row = cur.fetchone()
            approved_waiting_total = int(list(row.values())[0] if isinstance(row, dict) else row[0])

        # Approved blocked: same as waiting but has at least one open dep.
        # Include the IDs of open blockers for the frontend to link to.
        cur.execute(
            """
            SELECT
                i.id, i.title, i.status, i.priority, i.issue_type,
                i.description, i.created_at, i.updated_at,
                i.assignee, i.estimated_minutes, i.close_reason,
                GROUP_CONCAT(DISTINCT l.label ORDER BY l.label SEPARATOR ',') AS labels,
                GROUP_CONCAT(DISTINCT di.id ORDER BY di.id SEPARATOR ',') AS open_blocker_ids,
                GROUP_CONCAT(DISTINCT di.title ORDER BY di.id SEPARATOR '\x1f') AS open_blocker_titles
            FROM issues i
            JOIN labels la ON la.issue_id = i.id AND la.label = %s
            JOIN dependencies d ON d.issue_id = i.id AND d.type != %s
            JOIN issues di ON di.id = d.depends_on_issue_id AND di.status != %s
            LEFT JOIN labels l ON l.issue_id = i.id
            WHERE i.status = %s
            GROUP BY i.id
            ORDER BY i.priority ASC, i.updated_at DESC
            """,
            ("readiness:approved", "parent-child", "closed", "open"),
        )
        approved_blocked_raw = _rows(cur)

    # Post-process blocked: split the unit-separator-delimited blocker titles
    approved_blocked = []
    for row in approved_blocked_raw:
        row = _coerce(row)
        ids = row.pop("open_blocker_ids", None) or ""
        titles = row.pop("open_blocker_titles", None) or ""
        id_list = ids.split(",") if ids else []
        title_list = titles.split("\x1f") if titles else []
        row["open_blockers"] = [
            {"id": bid, "title": t}
            for bid, t in zip(id_list, title_list)
        ]
        approved_blocked.append(row)

    return {
        "approved_waiting": approved_waiting,
        "approved_waiting_total": approved_waiting_total,
        "approved_blocked": approved_blocked,
    }


@_degrade_when_unreachable(dict)
def get_bead_title_priority(bead_ids: list[str]) -> dict[str, dict]:
    """Return a mapping of bead_id → {id, title, priority, labels} for the given IDs.

    Used to enrich SQLite dispatch_runs rows with Dolt bead metadata.
    Missing bead IDs are silently omitted from the result.
    """
    if not bead_ids:
        return {}
    conn = _get_conn()
    placeholders = ", ".join(["%s"] * len(bead_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT i.id, i.title, i.priority,
                   GROUP_CONCAT(l.label ORDER BY l.label SEPARATOR ',') AS labels
            FROM issues i
            LEFT JOIN labels l ON l.issue_id = i.id
            WHERE i.id IN ({placeholders})
            GROUP BY i.id
            """,
            tuple(bead_ids),
        )
        rows = _rows(cur)
    return {r["id"]: _coerce(r) for r in rows}


@_degrade_when_unreachable(lambda: None)
def get_bead(bead_id: str, org: str | None = None) -> dict | None:
    """Return a single bead with its labels, deps, and comments.

    ``org`` selects the tracker to read (per-org credential + database);
    None / an unprovisioned org reads the shared autonomy tracker. Bead IDs
    do not encode their org, so the caller supplies it — the route enforces
    that an org-bound caller may only name its own.

    Returns None if the bead does not exist.
    """
    conn = _get_conn(org)
    with conn.cursor() as cur:

        # Main bead row with all text fields
        cur.execute(
            """
            SELECT
                i.id, i.title, i.status, i.priority, i.issue_type,
                i.description, i.design, i.acceptance_criteria, i.notes,
                i.created_at, i.updated_at, i.closed_at,
                i.assignee, i.estimated_minutes, i.close_reason,
                i.created_by, i.owner,
                GROUP_CONCAT(l.label ORDER BY l.label SEPARATOR ',') AS labels
            FROM issues i
            LEFT JOIN labels l ON l.issue_id = i.id
            WHERE i.id = %s
            GROUP BY i.id
            """,
            (bead_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        bead = _coerce(row)

        # Dependencies with dep bead metadata
        cur.execute(
            """
            SELECT d.depends_on_issue_id AS id, d.type,
                   di.title, di.status, di.priority
            FROM dependencies d
            JOIN issues di ON di.id = d.depends_on_issue_id
            WHERE d.issue_id = %s
            ORDER BY d.type, d.depends_on_issue_id
            """,
            (bead_id,),
        )
        bead["deps"] = [_coerce(r) for r in _rows(cur)]

        # Comments oldest-first
        cur.execute(
            """
            SELECT id, author, text, created_at
            FROM comments
            WHERE issue_id = %s
            ORDER BY created_at ASC
            """,
            (bead_id,),
        )
        bead["comments"] = [_coerce(r) for r in _rows(cur)]

        # Direct children via parent-child dependencies.
        cur.execute(
            """
            SELECT
                ci.id, ci.title, ci.status, ci.priority, ci.issue_type,
                ci.description, ci.created_at, ci.updated_at,
                ci.assignee, ci.estimated_minutes, ci.close_reason,
                GROUP_CONCAT(l.label ORDER BY l.label SEPARATOR ',') AS labels
            FROM dependencies d
            JOIN issues ci ON ci.id = d.issue_id
            LEFT JOIN labels l ON l.issue_id = ci.id
            WHERE d.depends_on_issue_id = %s
              AND d.type = %s
            GROUP BY ci.id
            ORDER BY ci.priority ASC, ci.updated_at DESC
            """,
            (bead_id, "parent-child"),
        )
        bead["children"] = [_coerce(r) for r in _rows(cur)]

    return bead


@_degrade_when_unreachable(list)
def get_beads_by_label(label: str) -> list[dict]:
    """Return beads that have a specific label. Used for pinned beads strip."""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_BEAD_COLS}
            FROM issues i
            JOIN labels la ON la.issue_id = i.id AND la.label = %s
            LEFT JOIN labels l ON l.issue_id = i.id
            GROUP BY i.id
            ORDER BY i.priority ASC, i.updated_at DESC
            """,
            (label,),
        )
        return [_coerce(r) for r in _rows(cur)]


@_degrade_when_unreachable(list)
def get_open_beads(limit: int = 200) -> list[dict]:
    """Return the working set — all beads that are not closed.

    Ordered by priority ASC (lower number = higher priority), then
    updated_at DESC within the same priority.
    """
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_BEAD_COLS}
            FROM issues i
            LEFT JOIN labels l ON l.issue_id = i.id
            WHERE i.status != %s
            GROUP BY i.id
            ORDER BY i.priority ASC, i.updated_at DESC
            LIMIT %s
            """,
            ("closed", limit),
        )
        return [_coerce(r) for r in _rows(cur)]


@_degrade_when_unreachable(dict)
def get_bead_counts() -> dict[str, int]:
    """Return lightweight counts for nav badges and dashboard header.

    Returns:
        open_count            — beads with status='open'
        in_progress_count     — beads with status='in_progress'
        approved_count        — open beads with readiness:approved label
        approved_blocked_count — approved open beads with at least one open blocker
        total_open_count      — all non-closed beads
    """
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                SUM(CASE WHEN i.status = %s THEN 1 ELSE 0 END)
                    AS open_count,
                SUM(CASE WHEN i.status = %s THEN 1 ELSE 0 END)
                    AS in_progress_count,
                SUM(CASE WHEN EXISTS (
                    SELECT 1 FROM labels l
                    WHERE l.issue_id = i.id AND l.label = %s
                ) THEN 1 ELSE 0 END)
                    AS approved_count,
                SUM(CASE WHEN i.status = %s
                    AND EXISTS (
                        SELECT 1 FROM labels la
                        WHERE la.issue_id = i.id AND la.label = %s
                    )
                    AND EXISTS (
                        SELECT 1 FROM dependencies d
                        JOIN issues di ON di.id = d.depends_on_issue_id
                        WHERE d.issue_id = i.id
                          AND d.type != %s
                          AND di.status != %s
                    )
                    THEN 1 ELSE 0 END)
                    AS approved_blocked_count,
                COUNT(*)
                    AS total_open_count
            FROM issues i
            WHERE i.status != %s
            """,
            ("open", "in_progress", "readiness:approved",
             "open", "readiness:approved", "parent-child", "closed",
             "closed"),
        )
        row = cur.fetchone()

    if not row:
        return {
            "open_count": 0,
            "in_progress_count": 0,
            "approved_count": 0,
            "approved_blocked_count": 0,
            "total_open_count": 0,
        }
    return {k: int(v or 0) for k, v in row.items()}
