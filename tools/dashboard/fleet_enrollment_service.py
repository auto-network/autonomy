"""Machine-local state and wire handling for a ``fleet:join`` grant.

The RelayKit grant is transport, not authority.  This module binds that grant
to one browser-signed fleet invitation, records machine-neutral requests, and
issues a one-time resume credential over the encrypted channel that created a
request.  Only its hash is durable.  Approval and root delivery are layered on
this state machine by the origin Dashboard; no personal-root secret enters
this module.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path

from tools.graph.db import _org_db_path
from tools.network import fleet_enroll, fleet_invite


FLEET_JOIN_TARGET_TYPE = "fleet:join"
FLEET_CHANNEL_BINDING_DOMAIN = fleet_enroll.FLEET_CHANNEL_BINDING_DOMAIN
FLEET_JOIN_OPS = ("fleet.request", "fleet.resume")
MAX_PENDING_REQUESTS_PER_INVITE = 100

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class FleetEnrollmentChannelError(ValueError):
    """A fleet invitation channel request is invalid or unauthorized."""


@dataclass(frozen=True)
class PendingEnrollment:
    request_id: str
    target_uuid: str
    request: fleet_enroll.EnrollmentRequest
    channel_binding: str
    verification_code: str
    status: str
    created_at: int
    updated_at: int
    source_approval_id: str | None = None
    last_error_code: str | None = None
    approval: fleet_enroll.EnrollmentApproval | None = None
    roster_entry: object | None = None


@dataclass(frozen=True)
class StoredFleetInvitation:
    target_uuid: str
    invite: fleet_invite.FleetInvite
    active: bool
    created_at: int


def channel_binding(resume_token: str) -> str:
    try:
        return fleet_enroll.resume_channel_binding(resume_token)
    except ValueError as exc:
        raise FleetEnrollmentChannelError(str(exc)) from exc


class FleetEnrollmentStore:
    """SQLite state in ``machine.db``; none of it joins personal sync."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else _org_db_path("machine")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS fleet_enrollment_invites (
                    target_uuid TEXT PRIMARY KEY,
                    grant_token TEXT NOT NULL UNIQUE,
                    invite_json TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fleet_enrollment_pending (
                    request_id TEXT PRIMARY KEY,
                    target_uuid TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    channel_binding TEXT NOT NULL,
                    verification_code TEXT NOT NULL,
                    status TEXT NOT NULL,
                    approval_json TEXT,
                    roster_entry_json TEXT,
                    source_approval_id TEXT,
                    last_error_code TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY(target_uuid)
                        REFERENCES fleet_enrollment_invites(target_uuid)
                );
                CREATE INDEX IF NOT EXISTS fleet_enrollment_pending_target
                    ON fleet_enrollment_pending(target_uuid, status);
                """
            )
            columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(fleet_enrollment_pending)"
                )
            }
            if "approval_json" not in columns:
                conn.execute(
                    "ALTER TABLE fleet_enrollment_pending "
                    "ADD COLUMN approval_json TEXT"
                )
            if "roster_entry_json" not in columns:
                conn.execute(
                    "ALTER TABLE fleet_enrollment_pending "
                    "ADD COLUMN roster_entry_json TEXT"
                )
            if "source_approval_id" not in columns:
                conn.execute(
                    "ALTER TABLE fleet_enrollment_pending "
                    "ADD COLUMN source_approval_id TEXT"
                )
            if "last_error_code" not in columns:
                conn.execute(
                    "ALTER TABLE fleet_enrollment_pending "
                    "ADD COLUMN last_error_code TEXT"
                )

    def register_invite(
        self,
        *,
        target_uuid: str,
        grant_token: str,
        invite: fleet_invite.FleetInvite,
        now_ms: int | None = None,
    ) -> None:
        target = _require_uuid(target_uuid)
        token = _require_token(grant_token)
        fleet_invite.verify(invite)
        _require_rendezvous_token(invite.rendezvous, token)
        now = _now_ms(now_ms)
        wire = json.dumps(
            {
                "personal_root_pub": invite.personal_root_pub,
                "rendezvous": invite.rendezvous,
                "invite_id": invite.invite_id,
                "expires_at": invite.expires_at,
                "signature": invite.signature,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT grant_token, invite_json FROM fleet_enrollment_invites "
                "WHERE target_uuid = ?",
                (target,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO fleet_enrollment_invites "
                    "(target_uuid,grant_token,invite_json,expires_at,active,created_at) "
                    "VALUES(?,?,?,?,1,?)",
                    (target, token, wire, invite.expires_at, now),
                )
            elif row["grant_token"] != token or row["invite_json"] != wire:
                raise FleetEnrollmentChannelError(
                    "fleet invitation target is already bound to different bytes"
                )

    def open_request(
        self,
        target_uuid: str,
        request: fleet_enroll.EnrollmentRequest,
        *,
        now_ms: int | None = None,
    ) -> tuple[PendingEnrollment, str | None]:
        """Create one pending request atomically; return its token once."""
        target = _require_uuid(target_uuid)
        now = _now_ms(now_ms)
        resume_token = os.urandom(32).hex()
        binding = channel_binding(resume_token)
        rid = fleet_enroll.request_id(request)
        request_wire = json.dumps(
            request.to_dict(), sort_keys=True, separators=(",", ":")
        )
        code = fleet_enroll.verification_code(request)
        terminal_approval_ids = self._terminal_approval_ids()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            invite = self._invite_row(conn, target, now)
            fleet_enroll.verify_request(request, invite=invite)
            row = conn.execute(
                "SELECT * FROM fleet_enrollment_pending WHERE request_id = ?",
                (rid,),
            ).fetchone()
            if row is not None:
                pending = _pending(row)
                if pending.target_uuid != target or pending.request != request:
                    raise FleetEnrollmentChannelError(
                        "request content id is already bound to different bytes"
                    )
                return pending, None
            capacity_rows = conn.execute(
                "SELECT source_approval_id FROM fleet_enrollment_pending "
                "WHERE target_uuid=? AND status IN ('pending','approving')",
                (target,),
            ).fetchall()
            pending_count = sum(
                1
                for capacity_row in capacity_rows
                if capacity_row["source_approval_id"] is None
                or capacity_row["source_approval_id"] not in terminal_approval_ids
            )
            if pending_count >= MAX_PENDING_REQUESTS_PER_INVITE:
                raise FleetEnrollmentChannelError(
                    "fleet invitation has reached its 100 pending-request limit"
                )
            conn.execute(
                "INSERT INTO fleet_enrollment_pending "
                "(request_id,target_uuid,request_json,channel_binding,"
                "verification_code,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'pending',?,?)",
                (rid, target, request_wire, binding, code, now, now),
            )
            row = conn.execute(
                "SELECT * FROM fleet_enrollment_pending WHERE request_id = ?",
                (rid,),
            ).fetchone()
            assert row is not None
            return _pending(row), resume_token

    def _terminal_approval_ids(self) -> set[str]:
        """Current generic decisions that no longer consume invite capacity.

        Read before taking the machine.db write lock. Approval state only moves
        from open to terminal, so a concurrent decision can make this snapshot
        conservatively over-count capacity but can never let request 101 in.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT source_approval_id "
                "FROM fleet_enrollment_pending "
                "WHERE source_approval_id IS NOT NULL"
            ).fetchall()
        try:
            from tools.dashboard import fleet_enrollment_approvals

            known = {row["source_approval_id"] for row in rows}
            return known & fleet_enrollment_approvals.terminal_approval_ids()
        except Exception:
            # Approval storage unavailable means fail closed: every unresolved
            # transport row continues to consume a slot.
            return set()

    def resume(
        self,
        *,
        target_uuid: str,
        request_id: str,
        resume_token: str,
        now_ms: int | None = None,
    ) -> PendingEnrollment:
        target = _require_uuid(target_uuid)
        rid = _require_hex64(request_id, "request_id")
        candidate = channel_binding(resume_token)
        now = _now_ms(now_ms)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._invite_row(conn, target, now)
            row = conn.execute(
                "SELECT * FROM fleet_enrollment_pending WHERE request_id = ? "
                "AND target_uuid = ?",
                (rid, target),
            ).fetchone()
            if row is None or not hmac.compare_digest(
                row["channel_binding"], candidate
            ):
                raise FleetEnrollmentChannelError(
                    "unknown fleet enrollment request or channel"
                )
            return _pending(row)

    def list_pending(self, target_uuid: str) -> tuple[PendingEnrollment, ...]:
        target = _require_uuid(target_uuid)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM fleet_enrollment_pending "
                "WHERE target_uuid = ? AND status IN ('pending','approving') "
                "ORDER BY created_at, request_id",
                (target,),
            ).fetchall()
        return tuple(_pending(row) for row in rows)

    def list_admissions(self) -> tuple[PendingEnrollment, ...]:
        """All non-delivered admission rows for the read-only Fleet view.

        Approved rows are intentionally absent: once roster evidence commits,
        only the signed roster may create a machine row. Declines remain
        generic-approval truth and are filtered by the projection.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM fleet_enrollment_pending "
                "WHERE status IN ('pending','approving','failed') "
                "ORDER BY created_at, request_id"
            ).fetchall()
        return tuple(_pending(row) for row in rows)

    def current_invitation(
        self, *, now_ms: int | None = None
    ) -> StoredFleetInvitation | None:
        """Return the newest currently usable invitation, if one exists.

        Storage predates the one-current-invitation command contract, so more
        than one active row can exist. Until cancel/reissue lands, the newest
        registered row is the deterministic presentation projection; this
        method does not mutate or silently deactivate older transport grants.
        """
        now = _now_ms(now_ms)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM fleet_enrollment_invites "
                "WHERE active=1 AND (expires_at=0 OR expires_at>?) "
                "ORDER BY created_at DESC, target_uuid DESC LIMIT 1",
                (now,),
            ).fetchone()
        if row is None:
            return None
        invite = fleet_invite.FleetInvite.from_dict(
            json.loads(row["invite_json"])
        )
        fleet_invite.verify(invite)
        _require_rendezvous_token(invite.rendezvous, row["grant_token"])
        return StoredFleetInvitation(
            target_uuid=row["target_uuid"],
            invite=invite,
            active=bool(row["active"]),
            created_at=int(row["created_at"]),
        )

    def get_request(self, request_id: str) -> PendingEnrollment | None:
        rid = _require_hex64(request_id, "request_id")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM fleet_enrollment_pending WHERE request_id=?",
                (rid,),
            ).fetchone()
        return _pending(row) if row is not None else None

    def bind_approval(self, request_id: str, approval_id: str) -> None:
        """Bind one source approval id to one request, first writer wins."""
        rid = _require_hex64(request_id, "request_id")
        if not isinstance(approval_id, str) or not approval_id:
            raise FleetEnrollmentChannelError("source approval id is required")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT source_approval_id FROM fleet_enrollment_pending "
                "WHERE request_id=?",
                (rid,),
            ).fetchone()
            if row is None:
                raise FleetEnrollmentChannelError("unknown fleet enrollment request")
            existing = row["source_approval_id"]
            if existing is not None and existing != approval_id:
                raise FleetEnrollmentChannelError(
                    "fleet request is already bound to another approval"
                )
            conn.execute(
                "UPDATE fleet_enrollment_pending SET source_approval_id=? "
                "WHERE request_id=?",
                (approval_id, rid),
            )

    def fail(
        self,
        *,
        target_uuid: str,
        request_id: str,
        error_code: str,
        now_ms: int | None = None,
    ) -> None:
        target = _require_uuid(target_uuid)
        rid = _require_hex64(request_id, "request_id")
        if not isinstance(error_code, str) or not error_code or len(error_code) > 96:
            raise FleetEnrollmentChannelError("fleet failure code is invalid")
        now = _now_ms(now_ms)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM fleet_enrollment_pending "
                "WHERE request_id=? AND target_uuid=?",
                (rid, target),
            ).fetchone()
            if row is None:
                raise FleetEnrollmentChannelError("unknown fleet enrollment request")
            if row["status"] == "approved":
                raise FleetEnrollmentChannelError(
                    "an approved enrollment cannot become failed"
                )
            conn.execute(
                "UPDATE fleet_enrollment_pending SET status='failed', "
                "last_error_code=?, updated_at=? "
                "WHERE request_id=? AND target_uuid=?",
                (error_code, now, rid, target),
            )

    def approve(
        self,
        *,
        target_uuid: str,
        request_id: str,
        approval: fleet_enroll.EnrollmentApproval,
        roster_entry,
        anchor_root_pub: str,
        org=None,
        now_ms: int | None = None,
    ) -> PendingEnrollment:
        """Verify, commit the roster, then mark this request deliverable."""
        from tools.network import fleet_roster, fleet_tunnel_server

        target = _require_uuid(target_uuid)
        rid = _require_hex64(request_id, "request_id")
        now = _now_ms(now_ms)
        approval_wire = json.dumps(
            approval.to_dict(), sort_keys=True, separators=(",", ":")
        )
        roster_wire = json.dumps(
            roster_entry.to_dict(), sort_keys=True, separators=(",", ":")
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM fleet_enrollment_pending WHERE request_id = ? "
                "AND target_uuid = ?",
                (rid, target),
            ).fetchone()
            if row is None:
                raise FleetEnrollmentChannelError(
                    "unknown fleet enrollment request"
                )
            pending = _pending(row)
            invite = self._invite_row(conn, target, now)
            if pending.status in {"approving", "approved"}:
                if (
                    row["approval_json"] != approval_wire
                    or row["roster_entry_json"] != roster_wire
                ):
                    raise FleetEnrollmentChannelError(
                        "request is already bound to different approval evidence"
                    )
            elif pending.status == "pending":
                conn.execute(
                    "UPDATE fleet_enrollment_pending SET status='approving', "
                    "approval_json=?, roster_entry_json=?, last_error_code=NULL, "
                    "updated_at=? "
                    "WHERE request_id=? AND target_uuid=?",
                    (approval_wire, roster_wire, now, rid, target),
                )
            else:
                raise FleetEnrollmentChannelError(
                    f"fleet enrollment request is {pending.status}"
                )

        # The roster write happens inside authorize_request.  Only after it
        # returns do we expose an approved state to the invitation channel.
        # ``approving`` freezes the exact evidence first, preventing two
        # concurrent operator requests from committing different authority.
        try:
            # Verify before touching even temporary operational state. If the
            # roster currently has one implicit tunnel server, materialize
            # that same choice before this commit grows it to two members;
            # otherwise both nodes would correctly fail closed as unassigned.
            fleet_enroll.verify_approval(
                approval,
                pending.request,
                invite=invite,
                channel_binding=pending.channel_binding,
                roster_entry=roster_entry,
                anchor_root_pub=anchor_root_pub,
            )
            fleet_tunnel_server.preserve_single_member_selection(
                anchor_root_pub=anchor_root_pub
            )
            fleet_enroll.authorize_request(
                approval,
                pending.request,
                invite=invite,
                channel_binding=pending.channel_binding,
                roster_entry=roster_entry,
                anchor_root_pub=anchor_root_pub,
                org=org,
            )
        except Exception:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE fleet_enrollment_pending SET status='pending', "
                    "approval_json=NULL, roster_entry_json=NULL, updated_at=? "
                    "WHERE request_id=? AND target_uuid=? "
                    "AND status='approving' AND approval_json=? "
                    "AND roster_entry_json=?",
                    (now, rid, target, approval_wire, roster_wire),
                )
            raise
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT status,approval_json,roster_entry_json "
                "FROM fleet_enrollment_pending WHERE request_id = ? "
                "AND target_uuid = ?",
                (rid, target),
            ).fetchone()
            if current is None:
                raise FleetEnrollmentChannelError(
                    "fleet enrollment request disappeared after roster commit"
                )
            if (
                current["approval_json"] != approval_wire
                or current["roster_entry_json"] != roster_wire
            ):
                raise FleetEnrollmentChannelError(
                    "approved request is already bound to different evidence"
                )
            if current["status"] not in {"approving", "approved"}:
                raise FleetEnrollmentChannelError(
                    f"fleet enrollment request became {current['status']}"
                )
            conn.execute(
                "UPDATE fleet_enrollment_pending SET status='approved', "
                "approval_json=?, roster_entry_json=?, updated_at=? "
                "WHERE request_id=? AND target_uuid=?",
                (approval_wire, roster_wire, now, rid, target),
            )
            row = conn.execute(
                "SELECT * FROM fleet_enrollment_pending WHERE request_id = ?",
                (rid,),
            ).fetchone()
            assert row is not None
            approved = _pending(row)
        assert isinstance(approved.roster_entry, fleet_roster.RosterEntry)
        return approved

    def verify_pending_approval_evidence(
        self,
        *,
        target_uuid: str,
        request_id: str,
        approval: fleet_enroll.EnrollmentApproval,
        roster_entry,
        anchor_root_pub: str,
        now_ms: int | None = None,
    ) -> None:
        """Verify frozen remote evidence without making a durable write.

        The first-member bootstrap uses this before it writes the origin's
        own roster row.  ``approve`` verifies again at commit time; this
        preflight exists solely to make malformed joiner evidence incapable
        of causing a partial local bootstrap.
        """
        target = _require_uuid(target_uuid)
        rid = _require_hex64(request_id, "request_id")
        now = _now_ms(now_ms)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM fleet_enrollment_pending WHERE request_id=? "
                "AND target_uuid=?",
                (rid, target),
            ).fetchone()
            if row is None:
                raise FleetEnrollmentChannelError(
                    "unknown fleet enrollment request"
                )
            pending = _pending(row)
            invite = self._invite_row(conn, target, now)
        fleet_enroll.verify_approval(
            approval,
            pending.request,
            invite=invite,
            channel_binding=pending.channel_binding,
            roster_entry=roster_entry,
            anchor_root_pub=anchor_root_pub,
        )

    def _invite_row(
        self, conn: sqlite3.Connection, target_uuid: str, now_ms: int
    ) -> fleet_invite.FleetInvite:
        row = conn.execute(
            "SELECT * FROM fleet_enrollment_invites WHERE target_uuid = ?",
            (target_uuid,),
        ).fetchone()
        if row is None or not row["active"]:
            raise FleetEnrollmentChannelError("fleet invitation is unavailable")
        if row["expires_at"] and now_ms >= row["expires_at"]:
            raise FleetEnrollmentChannelError("fleet invitation has expired")
        payload = json.loads(row["invite_json"])
        invite = fleet_invite.FleetInvite(**payload)
        fleet_invite.verify(invite)
        _require_rendezvous_token(invite.rendezvous, row["grant_token"])
        return invite


def handle_request(
    grant: dict,
    message: dict,
    *,
    channel_state: dict,
    store: FleetEnrollmentStore | None = None,
    armor_provider=None,
    now_ms: int | None = None,
) -> dict:
    """Handle one decoded operation from an established RelayKit channel."""
    if grant.get("target_type") != FLEET_JOIN_TARGET_TYPE:
        raise FleetEnrollmentChannelError("grant is not a fleet invitation")
    if message.get("v") != 1:
        raise FleetEnrollmentChannelError(
            "unsupported fleet invitation protocol version"
        )
    target_uuid = _require_uuid(grant.get("target_uuid"))
    state = store or FleetEnrollmentStore()
    if message.get("op") == "fleet.request":
        if set(message) != {"v", "op", "request"}:
            raise FleetEnrollmentChannelError("fleet.request has unknown fields")
        request = fleet_enroll.EnrollmentRequest.from_dict(message["request"])
        pending, issued = state.open_request(
            target_uuid, request, now_ms=now_ms
        )
        from tools.dashboard import fleet_enrollment_approvals

        fleet_enrollment_approvals.ensure_approval(pending, store=state)
        if issued is not None:
            channel_state["request_id"] = pending.request_id
            channel_state["resume_token"] = issued
        elif channel_state.get("request_id") == pending.request_id:
            issued = channel_state.get("resume_token")
        if issued is None:
            return {"v": 1, "status": "resume-required"}
        return {
            "v": 1,
            "status": pending.status,
            "request_id": pending.request_id,
            "verification_code": pending.verification_code,
            "resume_token": issued,
        }
    if message.get("op") == "fleet.resume":
        if set(message) != {"v", "op", "request_id", "resume_token"}:
            raise FleetEnrollmentChannelError("fleet.resume has unknown fields")
        pending = state.resume(
            target_uuid=target_uuid,
            request_id=message["request_id"],
            resume_token=message["resume_token"],
            now_ms=now_ms,
        )
        reply = {
            "v": 1,
            "status": pending.status,
            "request_id": pending.request_id,
            "verification_code": pending.verification_code,
        }
        from tools.dashboard import fleet_enrollment_approvals

        if (
            pending.status == "pending"
            and fleet_enrollment_approvals.decision_status(
                pending.source_approval_id
            ) == "declined"
        ):
            reply["status"] = "declined"
        if pending.status == "approved":
            if pending.approval is None or pending.roster_entry is None:
                raise FleetEnrollmentChannelError(
                    "approved request carries no signed delivery evidence"
                )
            provider = armor_provider or _personal_root_armor
            armor, anchor, armor_created_at, armor_updated_at = provider()
            if anchor != pending.request.personal_root_pub:
                raise FleetEnrollmentChannelError(
                    "stored personal identity no longer matches this invitation"
                )
            # Bootstrap peer authentication from public, individually
            # root-signed evidence: this Dashboard's own roster entry,
            # looked up directly by its own known machine id -- never a
            # roster-wide scan the joiner has to search. Ongoing roster
            # sync, not this pair, supplies the rest of the fleet and any
            # revocation freshness after first contact.
            from tools.network import fleet_roster, machine_boot

            own_machine_id = machine_boot.machine_id(org="machine")
            origin_entry = (
                fleet_roster.own_entry(
                    own_machine_id, anchor_root_pub=anchor, org=None
                )
                if own_machine_id is not None else None
            )
            if origin_entry is None:
                raise FleetEnrollmentChannelError(
                    "this Dashboard has no active Fleet roster identity"
                )
            # Deliver the WHOLE active roster, not just the origin pair, so a
            # joining machine knows every peer it may fetch a first checkpoint
            # from (fountain_fetch: every symbol any peer emits is useful). The
            # joiner's own entry was committed before this reply is assembled,
            # so it resolves into this set alongside the origin and any other
            # active members. Each entry is independently root-signed and the
            # joiner re-verifies every one; ongoing roster sync still owns
            # revocation freshness after first contact.
            active_roster = [
                entry.to_dict()
                for entry in fleet_roster.resolve(
                    fleet_roster.load_entries(org=None), anchor_root_pub=anchor
                ).values()
            ]
            reply.update({
                "approval": pending.approval.to_dict(),
                "roster_entry": pending.roster_entry.to_dict(),
                "origin_entry": origin_entry.to_dict(),
                "active_roster": active_roster,
                "personal_root_armor": armor,
                "personal_root_created_at": armor_created_at,
                "personal_root_updated_at": armor_updated_at,
            })
        return reply
    raise FleetEnrollmentChannelError("unknown fleet invitation operation")


def _pending(row: sqlite3.Row) -> PendingEnrollment:
    from tools.network import fleet_roster

    approval_wire = row["approval_json"]
    roster_wire = row["roster_entry_json"]
    return PendingEnrollment(
        request_id=row["request_id"],
        target_uuid=row["target_uuid"],
        request=fleet_enroll.EnrollmentRequest.from_dict(
            json.loads(row["request_json"])
        ),
        channel_binding=row["channel_binding"],
        verification_code=row["verification_code"],
        status=row["status"],
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
        source_approval_id=row["source_approval_id"],
        last_error_code=row["last_error_code"],
        approval=(
            fleet_enroll.EnrollmentApproval.from_dict(json.loads(approval_wire))
            if approval_wire is not None
            else None
        ),
        roster_entry=(
            fleet_roster.RosterEntry.from_dict(json.loads(roster_wire))
            if roster_wire is not None
            else None
        ),
    )


def _personal_root_armor() -> tuple[str, str, str, str]:
    """Return encrypted armor, root anchor, and its original row timestamps."""
    from tools.graph import settings_ops
    from tools.graph.schemas.personal_identity import PERSONAL_IDENTITY_SET_ID

    members = settings_ops.read_owned_set(
        PERSONAL_IDENTITY_SET_ID, org=None
    ).members
    candidates = [
        member
        for member in members
        if isinstance(member.payload, dict)
        and isinstance(member.payload.get("armored_private_key"), str)
    ]
    if len(candidates) != 1:
        raise FleetEnrollmentChannelError(
            "exactly one stored personal identity is required for delivery"
        )
    member = candidates[0]
    payload = member.payload
    root_pub = payload.get("root_pub")
    if not isinstance(root_pub, str) or not _HEX64.fullmatch(root_pub):
        from tools.network.idkit.armor import armor_root_pub

        root_pub = armor_root_pub(payload["armored_private_key"])
    return (
        payload["armored_private_key"], root_pub,
        member.created_at, member.updated_at,
    )


def _require_uuid(value) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise FleetEnrollmentChannelError(
            "fleet invitation target_uuid must be a UUID"
        ) from None


def _require_token(value) -> str:
    if not isinstance(value, str) or not _HEX32.fullmatch(value):
        raise FleetEnrollmentChannelError(
            "fleet invitation grant token must be 32 lowercase hex chars"
        )
    return value


def _require_hex64(value, what: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise FleetEnrollmentChannelError(
            f"fleet enrollment {what} must be 64 lowercase hex chars"
        )
    return value


def _require_rendezvous_token(rendezvous: str, token: str) -> None:
    parsed = urllib.parse.urlsplit(rendezvous)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path != f"/l/{token}"
        or parsed.query
        or parsed.fragment
    ):
        raise FleetEnrollmentChannelError(
            "fleet invitation rendezvous does not name its exact grant token"
        )


def _now_ms(value: int | None) -> int:
    now = int(time.time() * 1000) if value is None else value
    if not isinstance(now, int) or isinstance(now, bool) or now < 0:
        raise FleetEnrollmentChannelError("now_ms must be a non-negative int")
    return now
