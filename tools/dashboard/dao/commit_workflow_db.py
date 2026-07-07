"""Commit workflow operational store.

Stores append-only commit workflow events plus rebuildable projection tables.
This is instance data for Worktrees/lifecycle tracking, not graph Settings.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[3]
DB_PATH = Path(os.environ.get("COMMIT_WORKFLOW_DB", str(REPO_ROOT / "data" / "commit_workflow.db")))
MIN_SQLITE_VERSION = (3, 31, 0)

NON_TERMINAL_STATUSES = frozenset({
    "draft",
    "proposed",
    "needs_revision",
    "awaiting_approval",
    "approved",
    "committed",
    "awaiting_signature",
    "signed",
    "awaiting_publish",
    "published",
    "review_linked",
    "watching",
    "partially_landed",
    "duplicate_active",
    "blocked",
    "failed_retryable",
})

CLOSED_TERMINAL_STATUSES = frozenset({
    "landed",
    "superseded",
    "abandoned",
    "rejected",
    "failed_terminal",
    "expired",
})

REOPEN_TERMINAL_STATUSES = frozenset({"reverted"})
TERMINAL_STATUSES = CLOSED_TERMINAL_STATUSES | REOPEN_TERMINAL_STATUSES
ALL_STATUSES = NON_TERMINAL_STATUSES | TERMINAL_STATUSES

_STATUS_SQL = ", ".join(f"'{status}'" for status in sorted(ALL_STATUSES))
_TERMINAL_SQL = ", ".join(f"'{status}'" for status in sorted(TERMINAL_STATUSES))


CREATE_TABLES = f"""\
CREATE TABLE IF NOT EXISTS commit_workflow_events (
    seq                   INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id              TEXT NOT NULL UNIQUE,
    workflow_id           TEXT NOT NULL,
    event_type            TEXT NOT NULL,
    status_after          TEXT CHECK (
        status_after IS NULL OR status_after IN ({_STATUS_SQL})
    ),
    occurred_at           REAL NOT NULL,
    actor_type            TEXT NOT NULL,
    actor_id              TEXT,
    session_name          TEXT,
    repo_slug             TEXT NOT NULL,
    branch                TEXT,
    commit_shas_json      TEXT NOT NULL DEFAULT '[]',
    commit_roles_json     TEXT NOT NULL DEFAULT '{{}}',
    content_fingerprint   TEXT,
    provider              TEXT,
    provider_review_id    TEXT,
    payload_json          TEXT NOT NULL DEFAULT '{{}}'
);

CREATE TABLE IF NOT EXISTS commit_workflow_states (
    workflow_id           TEXT PRIMARY KEY,
    repo_slug             TEXT NOT NULL,
    session_name          TEXT,
    branch                TEXT,
    status                TEXT NOT NULL CHECK (status IN ({_STATUS_SQL})),
    terminal              INTEGER GENERATED ALWAYS AS (
        CASE
            WHEN status IN ({_TERMINAL_SQL})
            THEN 1
            ELSE 0
        END
    ) STORED,
    content_fingerprint   TEXT,
    target_branch         TEXT,
    provider              TEXT,
    provider_review_id    TEXT,
    last_event_id         TEXT NOT NULL,
    created_at            REAL NOT NULL,
    updated_at            REAL NOT NULL,
    state_json            TEXT NOT NULL DEFAULT '{{}}',
    FOREIGN KEY(last_event_id) REFERENCES commit_workflow_events(event_id)
);

CREATE TABLE IF NOT EXISTS commit_workflow_commits (
    workflow_id           TEXT NOT NULL,
    repo_slug             TEXT NOT NULL,
    commit_sha            TEXT NOT NULL,
    position              INTEGER NOT NULL,
    role                  TEXT NOT NULL DEFAULT 'workflow_commit',
    created_at            REAL NOT NULL,
    PRIMARY KEY (workflow_id, commit_sha),
    FOREIGN KEY(workflow_id) REFERENCES commit_workflow_states(workflow_id)
);

CREATE TABLE IF NOT EXISTS commit_workflow_reviews (
    workflow_id           TEXT NOT NULL,
    repo_slug             TEXT NOT NULL,
    provider              TEXT NOT NULL,
    provider_review_id    TEXT NOT NULL,
    review_url            TEXT,
    base_sha              TEXT,
    head_sha              TEXT,
    state                 TEXT,
    checks_state          TEXT,
    last_seen_at          REAL,
    payload_json          TEXT NOT NULL DEFAULT '{{}}',
    PRIMARY KEY (workflow_id, provider, provider_review_id),
    FOREIGN KEY(workflow_id) REFERENCES commit_workflow_states(workflow_id)
);

CREATE TABLE IF NOT EXISTS commit_workflow_approvals (
    approval_id           TEXT PRIMARY KEY,
    workflow_id           TEXT NOT NULL,
    repo_slug             TEXT NOT NULL,
    approval_type         TEXT NOT NULL,
    status                TEXT NOT NULL,
    requested_by_session  TEXT,
    operator_id           TEXT,
    requested_at          REAL NOT NULL,
    decided_at            REAL,
    payload_json          TEXT NOT NULL DEFAULT '{{}}',
    FOREIGN KEY(workflow_id) REFERENCES commit_workflow_states(workflow_id)
);

CREATE TABLE IF NOT EXISTS commit_signing_requests (
    signing_request_id        TEXT PRIMARY KEY,
    workflow_id               TEXT NOT NULL,
    repo_slug                 TEXT NOT NULL,
    status                    TEXT NOT NULL,
    signing_method            TEXT NOT NULL,
    trusted_object_store_ref  TEXT NOT NULL,
    canonical_payload_hash    TEXT NOT NULL,
    device_id                 TEXT,
    batch_group_id            TEXT,
    position_in_batch         INTEGER,
    batch_size                INTEGER,
    encrypted_key_ref         TEXT,
    operator_id               TEXT,
    requested_at              REAL NOT NULL,
    completed_at              REAL,
    signature_ref             TEXT,
    payload_json              TEXT NOT NULL DEFAULT '{{}}',
    FOREIGN KEY(workflow_id) REFERENCES commit_workflow_states(workflow_id)
);

CREATE TABLE IF NOT EXISTS commit_workflow_idempotency (
    idempotency_id        TEXT PRIMARY KEY,
    actor_type            TEXT NOT NULL,
    actor_id              TEXT NOT NULL,
    scope_key             TEXT NOT NULL,
    operation             TEXT NOT NULL,
    workflow_id           TEXT,
    idempotency_key_hash  TEXT NOT NULL,
    request_fingerprint   TEXT NOT NULL,
    status                TEXT NOT NULL CHECK (status IN ('in_flight', 'completed', 'failed_retryable', 'failed_terminal')),
    response_json         TEXT,
    event_ids_json        TEXT NOT NULL DEFAULT '[]',
    side_effect_ref       TEXT,
    created_at            REAL NOT NULL,
    updated_at            REAL NOT NULL,
    expires_at            REAL NOT NULL,
    UNIQUE(actor_type, actor_id, scope_key, operation, idempotency_key_hash)
);
"""

CREATE_INDEXES = """\
CREATE INDEX IF NOT EXISTS idx_cwe_workflow_time
    ON commit_workflow_events(workflow_id, occurred_at, seq);

CREATE INDEX IF NOT EXISTS idx_cwe_repo_time
    ON commit_workflow_events(repo_slug, occurred_at, seq);

CREATE INDEX IF NOT EXISTS idx_cwe_fingerprint
    ON commit_workflow_events(content_fingerprint);

CREATE INDEX IF NOT EXISTS idx_cws_repo_status
    ON commit_workflow_states(repo_slug, status, terminal);

CREATE INDEX IF NOT EXISTS idx_cws_session
    ON commit_workflow_states(session_name);

CREATE INDEX IF NOT EXISTS idx_cws_fingerprint
    ON commit_workflow_states(content_fingerprint);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cws_active_fingerprint
    ON commit_workflow_states(repo_slug, content_fingerprint)
    WHERE terminal = 0
      AND status <> 'duplicate_active'
      AND content_fingerprint IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_cwc_commit_sha
    ON commit_workflow_commits(commit_sha);

CREATE INDEX IF NOT EXISTS idx_cwc_repo_commit_sha
    ON commit_workflow_commits(repo_slug, commit_sha);

CREATE INDEX IF NOT EXISTS idx_cwr_repo_review
    ON commit_workflow_reviews(repo_slug, provider, provider_review_id);

CREATE INDEX IF NOT EXISTS idx_cwr_last_seen
    ON commit_workflow_reviews(last_seen_at);

CREATE INDEX IF NOT EXISTS idx_cwa_workflow
    ON commit_workflow_approvals(workflow_id, status);

CREATE INDEX IF NOT EXISTS idx_cwa_operator
    ON commit_workflow_approvals(operator_id, decided_at);

CREATE INDEX IF NOT EXISTS idx_csr_workflow_status
    ON commit_signing_requests(workflow_id, status);

CREATE INDEX IF NOT EXISTS idx_csr_payload_hash
    ON commit_signing_requests(canonical_payload_hash);

CREATE INDEX IF NOT EXISTS idx_cwi_workflow
    ON commit_workflow_idempotency(workflow_id, operation);

CREATE INDEX IF NOT EXISTS idx_cwi_expiry
    ON commit_workflow_idempotency(expires_at);
"""

CREATE_TRIGGERS = """\
CREATE TRIGGER IF NOT EXISTS trg_cwe_no_update
BEFORE UPDATE ON commit_workflow_events
BEGIN
    SELECT RAISE(ABORT, 'commit_workflow_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_cwe_no_delete
BEFORE DELETE ON commit_workflow_events
BEGIN
    SELECT RAISE(ABORT, 'commit_workflow_events is append-only');
END;
"""


@dataclass(frozen=True)
class CommitDisposition:
    sha: str
    outstanding: bool
    source: str
    workflow_id: str | None = None
    status: str | None = None
    reason: str = ""


def _db_path(db_path: Path | str | None = None) -> Path:
    return Path(db_path) if db_path is not None else DB_PATH


def _check_sqlite_version() -> None:
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        got = ".".join(str(part) for part in sqlite3.sqlite_version_info)
        need = ".".join(str(part) for part in MIN_SQLITE_VERSION)
        raise RuntimeError(
            f"commit workflow store requires SQLite >= {need} for generated columns; got {got}"
        )


def _get_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    _check_sqlite_version()
    path = _db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA recursive_triggers=ON")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    conn = _get_conn(db_path)
    try:
        conn.executescript(CREATE_TABLES)
        conn.executescript(CREATE_INDEXES)
        conn.executescript(CREATE_TRIGGERS)
        _migrate_commit_signing_request_batch_columns(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate_commit_signing_request_batch_columns(conn: sqlite3.Connection) -> None:
    cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(commit_signing_requests)").fetchall()
    }
    if "batch_group_id" not in cols:
        conn.execute("ALTER TABLE commit_signing_requests ADD COLUMN batch_group_id TEXT")
    if "position_in_batch" not in cols:
        conn.execute("ALTER TABLE commit_signing_requests ADD COLUMN position_in_batch INTEGER")
    if "batch_size" not in cols:
        conn.execute("ALTER TABLE commit_signing_requests ADD COLUMN batch_size INTEGER")


def append_event(
    *,
    event_id: str,
    workflow_id: str,
    event_type: str,
    status_after: str | None,
    repo_slug: str,
    commit_shas: Iterable[str] = (),
    commit_roles: dict[str, tuple[str, int]] | None = None,
    occurred_at: float | None = None,
    actor_type: str = "dashboard",
    actor_id: str | None = None,
    session_name: str | None = None,
    branch: str | None = None,
    content_fingerprint: str | None = None,
    provider: str | None = None,
    provider_review_id: str | None = None,
    payload: dict | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Append one event and update the current-state projection.

    This intentionally uses plain INSERT. Do not change it to OR REPLACE/IGNORE:
    the event table is append-only audit history.
    """
    shas = [str(sha) for sha in commit_shas if str(sha)]
    roles: dict[str, tuple[str, int]] = {}
    if commit_roles is not None:
        for sha, value in commit_roles.items():
            if str(sha) not in shas:
                raise ValueError("commit_roles keys must match commit_shas values when provided")
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise ValueError("commit_roles values must be (role, position) pairs")
            role, position = value
            if not isinstance(position, int) or isinstance(position, bool):
                raise ValueError("commit_roles positions must be integers")
            roles[str(sha)] = (str(role), int(position))
    if status_after is not None and status_after not in ALL_STATUSES:
        raise ValueError(f"invalid commit workflow status: {status_after}")
    conn = _get_conn(db_path)
    try:
        init_schema_on_connection(conn)
        conn.execute(
            """\
            INSERT INTO commit_workflow_events (
                event_id, workflow_id, event_type, status_after, occurred_at,
                actor_type, actor_id, session_name, repo_slug, branch,
                commit_shas_json, commit_roles_json, content_fingerprint, provider,
                provider_review_id, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                workflow_id,
                event_type,
                status_after,
                float(occurred_at if occurred_at is not None else time.time()),
                actor_type,
                actor_id,
                session_name,
                repo_slug,
                branch,
                json.dumps(shas),
                json.dumps(roles, sort_keys=True),
                content_fingerprint,
                provider,
                provider_review_id,
                json.dumps(payload or {}, sort_keys=True),
            ),
        )
        _rebuild_projection_for_workflow(conn, workflow_id)
        conn.commit()
    finally:
        conn.close()


def rebuild_projection(db_path: Path | str | None = None) -> None:
    conn = _get_conn(db_path)
    try:
        init_schema_on_connection(conn)
        workflow_ids = [
            row["workflow_id"]
            for row in conn.execute(
                "SELECT DISTINCT workflow_id FROM commit_workflow_events ORDER BY workflow_id"
            ).fetchall()
        ]
        conn.execute("DELETE FROM commit_workflow_commits")
        conn.execute("DELETE FROM commit_workflow_states")
        for workflow_id in workflow_ids:
            _rebuild_projection_for_workflow(conn, workflow_id)
        conn.commit()
    finally:
        conn.close()


def _rebuild_projection_for_workflow(conn: sqlite3.Connection, workflow_id: str) -> None:
    rows = conn.execute(
        """
        SELECT * FROM commit_workflow_events
        WHERE workflow_id = ?
        ORDER BY occurred_at, seq
        """,
        (workflow_id,),
    ).fetchall()
    if not rows:
        return
    latest = rows[-1]
    status = latest["status_after"]
    if status is None:
        return
    now = time.time()
    existing = conn.execute(
        "SELECT created_at FROM commit_workflow_states WHERE workflow_id = ?",
        (workflow_id,),
    ).fetchone()
    created_at = float(existing["created_at"]) if existing else now
    conn.execute(
        """\
        INSERT INTO commit_workflow_states (
            workflow_id, repo_slug, session_name, branch, status,
            content_fingerprint, provider, provider_review_id, last_event_id,
            created_at, updated_at, state_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(workflow_id) DO UPDATE SET
            repo_slug = excluded.repo_slug,
            session_name = excluded.session_name,
            branch = excluded.branch,
            status = excluded.status,
            content_fingerprint = excluded.content_fingerprint,
            provider = excluded.provider,
            provider_review_id = excluded.provider_review_id,
            last_event_id = excluded.last_event_id,
            updated_at = excluded.updated_at,
            state_json = excluded.state_json
        """,
        (
            workflow_id,
            latest["repo_slug"],
            latest["session_name"],
            latest["branch"],
            status,
            latest["content_fingerprint"],
            latest["provider"],
            latest["provider_review_id"],
            latest["event_id"],
            created_at,
            float(latest["occurred_at"]),
            latest["payload_json"] or "{}",
        ),
    )
    conn.execute(
        "DELETE FROM commit_workflow_commits WHERE workflow_id = ?",
        (workflow_id,),
    )
    shas, roles = _latest_non_empty_commit_data(rows)
    for idx, sha in enumerate(shas):
        override = roles.get(sha)
        if override is None:
            role = "workflow_commit"
            # Default projection remains 1-indexed unless an explicit override is stored.
            position = idx + 1
        else:
            # Explicit overrides preserve the caller-provided chain slot, so two SHAs
            # can share the same position when rewrite source/result land together.
            role, position = override
        conn.execute(
            """\
            INSERT INTO commit_workflow_commits (
                workflow_id, repo_slug, commit_sha, position, role, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (workflow_id, latest["repo_slug"], sha, position, role, float(latest["occurred_at"])),
        )


def _loads_shas(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _latest_non_empty_commit_shas(rows: list[sqlite3.Row]) -> list[str]:
    """Return the newest non-empty commit set in a workflow event stream."""
    shas, _roles = _latest_non_empty_commit_data(rows)
    return shas


def _latest_non_empty_commit_data(rows: list[sqlite3.Row]) -> tuple[list[str], dict[str, tuple[str, int]]]:
    """Return the newest non-empty commit set and any persisted roles."""
    for row in reversed(rows):
        shas = _loads_shas(row["commit_shas_json"])
        if shas:
            roles = _loads_commit_roles(row["commit_roles_json"], shas)
            return shas, roles
    return [], []


def _loads_commit_roles(raw: str | None, shas: list[str]) -> dict[str, tuple[str, int]]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(value, dict):
        out: dict[str, tuple[str, int]] = {}
        for sha, entry in value.items():
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            role, position = entry
            if not isinstance(position, int) or isinstance(position, bool):
                continue
            out[str(sha)] = (str(role), int(position))
        return out
    if isinstance(value, list):
        out: dict[str, tuple[str, int]] = {}
        for sha, entry in zip(shas, value):
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            role, position = entry
            if not isinstance(position, int) or isinstance(position, bool):
                continue
            out[str(sha)] = (str(role), int(position))
        return out
    return {}


def resolve_worktree_outstanding(
    *,
    repo_slug: str,
    scanned_shas: Iterable[str],
    git_merged_shas: set[str] | None = None,
    db_path: Path | str | None = None,
) -> list[CommitDisposition]:
    """Resolve displayed outstanding state for a Worktrees git-scan result.

    Workflow state decides only the displayed outstanding set. Git topology
    flags such as ff_eligible/rebase_required are computed separately from the
    physical branch.
    """
    shas = [str(sha) for sha in scanned_shas if str(sha)]
    if not shas:
        return []
    merged = git_merged_shas or set()
    status_by_sha = _workflow_status_by_sha(repo_slug=repo_slug, shas=shas, db_path=db_path)
    out: list[CommitDisposition] = []
    for sha in shas:
        chosen = status_by_sha.get(sha)
        if chosen is None:
            if sha in merged:
                out.append(CommitDisposition(
                    sha=sha,
                    outstanding=False,
                    source="git_merged",
                    reason="git fallback says merged",
                ))
            else:
                out.append(CommitDisposition(
                    sha=sha,
                    outstanding=True,
                    source="git_derivation",
                    reason="no workflow evidence; using git fallback",
                ))
            continue
        workflow_id, status = chosen
        if status in CLOSED_TERMINAL_STATUSES:
            out.append(CommitDisposition(
                sha=sha,
                outstanding=False,
                source=f"workflow:{status}",
                workflow_id=workflow_id,
                status=status,
                reason="suppressed by terminal workflow evidence",
            ))
        elif status in REOPEN_TERMINAL_STATUSES:
            reopened = sha not in merged
            out.append(CommitDisposition(
                sha=sha,
                outstanding=reopened,
                source=f"workflow:{status}",
                workflow_id=workflow_id,
                status=status,
                reason="reverted and absent from target" if reopened else "reverted but back on target",
            ))
        else:
            out.append(CommitDisposition(
                sha=sha,
                outstanding=True,
                source=f"workflow:{status}",
                workflow_id=workflow_id,
                status=status,
                reason="non-terminal workflow evidence",
            ))
    return out


def _workflow_status_by_sha(
    *,
    repo_slug: str,
    shas: list[str],
    db_path: Path | str | None = None,
) -> dict[str, tuple[str, str]]:
    placeholders = ",".join("?" for _ in shas)
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            f"""\
            SELECT c.commit_sha, s.workflow_id, s.status
            FROM commit_workflow_commits c
            JOIN commit_workflow_states s ON s.workflow_id = c.workflow_id
            WHERE c.repo_slug = ?
              AND c.commit_sha IN ({placeholders})
            """,
            (repo_slug, *shas),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return {}
    finally:
        conn.close()
    chosen: dict[str, tuple[str, str]] = {}
    for row in rows:
        sha = row["commit_sha"]
        candidate = (row["workflow_id"], row["status"])
        current = chosen.get(sha)
        if current is None or _status_rank(candidate[1]) > _status_rank(current[1]):
            chosen[sha] = candidate
    return chosen


def init_schema_on_connection(conn: sqlite3.Connection) -> None:
    """Ensure schema exists on an already configured connection."""
    conn.executescript(CREATE_TABLES)
    conn.executescript(CREATE_INDEXES)
    conn.executescript(CREATE_TRIGGERS)
    conn.commit()


def _status_rank(status: str) -> int:
    if status in CLOSED_TERMINAL_STATUSES:
        return 3
    if status in REOPEN_TERMINAL_STATUSES:
        return 2
    return 1


IDEMPOTENCY_NAMESPACE = "commit_workflow"
IDEMPOTENCY_WORKFLOW_RETENTION_SECONDS = 7 * 24 * 60 * 60
IDEMPOTENCY_PUBLISH_RETENTION_SECONDS = 30 * 24 * 60 * 60
_IDEMPOTENCY_FINGERPRINT_IGNORED_KEYS = frozenset({
    "authorization",
    "bearer_token",
    "correlation_id",
    "idempotency_key",
    "access_token",
    "refresh_token",
})


def hash_idempotency_key(namespace: str, raw_key: str) -> str:
    """Return the stored idempotency key hash.

    The raw key never leaves the request path; the DB stores only a
    namespace-scoped SHA-256 hex digest.
    """
    payload = f"{namespace}\0{raw_key}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def scope_key(repo_slug: str, workflow_id: str) -> str:
    return f"{repo_slug}:{workflow_id}"


def _fingerprint_sanitize(value):
    if isinstance(value, dict):
        return {
            key: _fingerprint_sanitize(item)
            for key, item in sorted(value.items())
            if str(key) not in _IDEMPOTENCY_FINGERPRINT_IGNORED_KEYS
        }
    if isinstance(value, list):
        return [_fingerprint_sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [_fingerprint_sanitize(item) for item in value]
    return value


def request_fingerprint(operation: str, resolved_request_fields: dict) -> str:
    """Return a canonical JSON fingerprint for the resolved request fields."""
    payload = {
        "operation": operation,
        "request": _fingerprint_sanitize(resolved_request_fields),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _idempotency_retention_seconds(operation: str) -> int:
    if operation == "publish":
        return IDEMPOTENCY_PUBLISH_RETENTION_SECONDS
    return IDEMPOTENCY_WORKFLOW_RETENTION_SECONDS


def reserve_idempotency(
    conn: sqlite3.Connection,
    *,
    actor_type: str,
    actor_id: str,
    scope_key: str,
    operation: str,
    raw_idempotency_key: str,
    request_fields: dict,
    workflow_id: str | None = None,
    namespace: str = IDEMPOTENCY_NAMESPACE,
    now: float | None = None,
    idempotency_id: str | None = None,
) -> dict[str, object]:
    """Insert an ``in_flight`` idempotency row and return its stored shape."""
    created_at = float(now if now is not None else time.time())
    key_hash = hash_idempotency_key(namespace, raw_idempotency_key)
    record = {
        "idempotency_id": idempotency_id or uuid.uuid4().hex,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "scope_key": scope_key,
        "operation": operation,
        "workflow_id": workflow_id,
        "idempotency_key_hash": key_hash,
        "request_fingerprint": request_fingerprint(operation, request_fields),
        "status": "in_flight",
        "response_json": None,
        "event_ids_json": "[]",
        "side_effect_ref": None,
        "created_at": created_at,
        "updated_at": created_at,
        "expires_at": created_at + _idempotency_retention_seconds(operation),
    }
    conn.execute(
        """\
        DELETE FROM commit_workflow_idempotency
        WHERE actor_type = ?
          AND actor_id = ?
          AND scope_key = ?
          AND operation = ?
          AND idempotency_key_hash = ?
          AND expires_at <= ?
        """,
        (
            record["actor_type"],
            record["actor_id"],
            record["scope_key"],
            record["operation"],
            record["idempotency_key_hash"],
            created_at,
        ),
    )
    conn.execute(
        """\
        INSERT INTO commit_workflow_idempotency (
            idempotency_id, actor_type, actor_id, scope_key, operation,
            workflow_id, idempotency_key_hash, request_fingerprint, status,
            response_json, event_ids_json, side_effect_ref,
            created_at, updated_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record["idempotency_id"],
            record["actor_type"],
            record["actor_id"],
            record["scope_key"],
            record["operation"],
            record["workflow_id"],
            record["idempotency_key_hash"],
            record["request_fingerprint"],
            record["status"],
            record["response_json"],
            record["event_ids_json"],
            record["side_effect_ref"],
            record["created_at"],
            record["updated_at"],
            record["expires_at"],
        ),
    )
    return record


def finalize_idempotency(
    conn: sqlite3.Connection,
    *,
    idempotency_id: str,
    status: str,
    response_json: dict | None = None,
    event_ids: Iterable[str] = (),
    side_effect_ref: str | None = None,
    updated_at: float | None = None,
    expires_at: float | None = None,
) -> None:
    now = float(updated_at if updated_at is not None else time.time())
    event_ids_json = json.dumps([str(item) for item in event_ids], sort_keys=True)
    payload = json.dumps(response_json or {}, sort_keys=True) if response_json is not None else None
    params: list[object] = [status, payload, event_ids_json, side_effect_ref, now]
    sql = (
        "UPDATE commit_workflow_idempotency SET "
        "status = ?, response_json = ?, event_ids_json = ?, side_effect_ref = ?, updated_at = ?"
    )
    if expires_at is not None:
        sql += ", expires_at = ?"
        params.append(float(expires_at))
    sql += " WHERE idempotency_id = ?"
    params.append(idempotency_id)
    conn.execute(sql, params)


def lookup_idempotency(
    conn: sqlite3.Connection,
    *,
    actor_type: str,
    actor_id: str,
    scope_key: str,
    operation: str,
    raw_idempotency_key: str,
    request_fields: dict,
    namespace: str = IDEMPOTENCY_NAMESPACE,
    now: float | None = None,
) -> dict[str, object]:
    """Return the current idempotency state for a request key."""
    key_hash = hash_idempotency_key(namespace, raw_idempotency_key)
    fp = request_fingerprint(operation, request_fields)
    current_time = float(now if now is not None else time.time())
    row = conn.execute(
        """\
        SELECT *
        FROM commit_workflow_idempotency
        WHERE actor_type = ?
          AND actor_id = ?
          AND scope_key = ?
          AND operation = ?
          AND idempotency_key_hash = ?
        """,
        (actor_type, actor_id, scope_key, operation, key_hash),
    ).fetchone()
    if row is None or float(row["expires_at"]) <= current_time:
        return {"kind": "fresh_reserve"}
    if row["request_fingerprint"] != fp:
        return {"kind": "conflict", "row": dict(row)}
    status = row["status"]
    if status == "in_flight":
        return {"kind": "in_flight", "row": dict(row)}
    if status == "completed":
        response_json = json.loads(row["response_json"] or "{}")
        event_ids = json.loads(row["event_ids_json"] or "[]")
        return {
            "kind": "completed_replay",
            "row": dict(row),
            "response_json": response_json,
            "event_ids": event_ids,
            "side_effect_ref": row["side_effect_ref"],
        }
    if status == "failed_retryable":
        return {"kind": "failed_retryable", "row": dict(row)}
    return {"kind": "failed_terminal", "row": dict(row)}


# ── operator review-and-sign read surface ────────────────────────────
#
# These are READ-ONLY projections for the operator-facing dashboard page.
# They are consumed by the dashboard operator (same-origin, single-operator
# trust model — the same posture as the bead and dispatch read endpoints),
# NOT by the agent capability path, which authenticates by bearer token.
# Nothing here mutates state.


def _decode_payload_column(record: dict[str, Any], column: str, into: str) -> None:
    """Parse a JSON text column into ``into`` on ``record``, dropping the raw
    column. A malformed/absent value degrades to an empty dict rather than
    raising — a display projection must never fail on one bad row."""
    raw = record.pop(column, None)
    try:
        record[into] = json.loads(raw or "{}")
    except Exception:
        record[into] = {}


def list_operator_signing_queue(
    *,
    repo_slug: str | None = None,
    limit: int = 100,
    db_path: Path | str | None = None,
) -> list[dict]:
    """Signing requests awaiting the operator, oldest-first (FIFO queue).

    Returns each ``pending`` signing request joined to its workflow's current
    state, so the operator page can render repo, branch, and a message preview
    without a second lookup. Workflows in a terminal status are excluded — a
    signing request whose workflow was abandoned, superseded, or otherwise
    closed is not actionable and must not sit in the queue. A chain rewrite
    contributes one row per link; ``batch_group_id`` / ``position_in_batch`` /
    ``batch_size`` are included so the page can group a chain into one item.
    """
    conn = _get_conn(db_path)
    try:
        where = ["sr.status = 'pending'", "st.terminal = 0"]
        params: list[Any] = []
        if repo_slug is not None:
            where.append("sr.repo_slug = ?")
            params.append(repo_slug)
        params.append(int(limit))
        rows = conn.execute(
            f"""
            SELECT
                sr.signing_request_id, sr.workflow_id, sr.repo_slug,
                sr.status AS signing_status, sr.signing_method,
                sr.canonical_payload_hash, sr.trusted_object_store_ref,
                sr.requested_at, sr.operator_id,
                sr.batch_group_id, sr.position_in_batch, sr.batch_size,
                sr.payload_json,
                st.status AS workflow_status, st.branch, st.target_branch,
                st.session_name, st.updated_at AS workflow_updated_at
            FROM commit_signing_requests sr
            JOIN commit_workflow_states st ON sr.workflow_id = st.workflow_id
            WHERE {' AND '.join(where)}
            ORDER BY sr.requested_at ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        queue = []
        for row in rows:
            record = dict(row)
            _decode_payload_column(record, "payload_json", "payload")
            queue.append(record)
        return queue
    finally:
        conn.close()


def get_operator_workflow_detail(
    *,
    workflow_id: str,
    db_path: Path | str | None = None,
) -> dict | None:
    """Full detail for one workflow for the operator detail view.

    Returns the current workflow-state row (with ``state_json`` decoded into
    ``state``) plus every signing request against the workflow (each with its
    ``payload_json`` decoded into ``payload``), oldest-first. Returns ``None``
    if the workflow does not exist. Read-only.
    """
    conn = _get_conn(db_path)
    try:
        state_row = conn.execute(
            "SELECT * FROM commit_workflow_states WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if state_row is None:
            return None
        workflow = dict(state_row)
        _decode_payload_column(workflow, "state_json", "state")
        signing_rows = conn.execute(
            "SELECT * FROM commit_signing_requests WHERE workflow_id = ? "
            "ORDER BY requested_at ASC",
            (workflow_id,),
        ).fetchall()
        signing_requests = []
        for row in signing_rows:
            record = dict(row)
            _decode_payload_column(record, "payload_json", "payload")
            signing_requests.append(record)
        return {"workflow": workflow, "signing_requests": signing_requests}
    finally:
        conn.close()
