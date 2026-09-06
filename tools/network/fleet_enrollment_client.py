"""Fresh-install client for a machine-neutral fleet invitation.

The invitation authenticates the exact public RelayKit URL. The registry
envelope supplies the serving organization pin for the ordinary RelayKit
handshake; the encrypted application channel then carries only ``fleet.request``
and ``fleet.resume``. Before approval, the joining machine persists only its
public machine id and retry state in ``machine.db`` -- never a machine key.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from tools.graph.db import _org_db_path
from tools.network import fleet_enroll, fleet_invite, fleet_roster
from tools.network.idkit import canonical_json
from tools.network.relaykit.viewer import ViewerChannel

log = logging.getLogger(__name__)


_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class FleetEnrollmentClientError(RuntimeError):
    """The invitation channel or its authenticated reply was invalid."""


class FleetInvitationExpired(FleetEnrollmentClientError):
    """The signed invitation reached its terminal expiry."""


@dataclass(frozen=True)
class EnrollmentRecovery:
    invite: fleet_invite.FleetInvite
    request: fleet_enroll.EnrollmentRequest
    request_id: str
    resume_token: str
    verification_code: str

    @property
    def channel_binding(self) -> str:
        return fleet_enroll.resume_channel_binding(self.resume_token)

    def to_dict(self) -> dict:
        return {
            "invite": self.invite.to_dict(),
            "request": self.request.to_dict(),
            "request_id": self.request_id,
            "resume_token": self.resume_token,
            "verification_code": self.verification_code,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "EnrollmentRecovery":
        expected = {
            "invite", "request", "request_id", "resume_token",
            "verification_code",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise FleetEnrollmentClientError(
                "fleet enrollment recovery has unknown or missing fields"
            )
        invite = fleet_invite.FleetInvite.from_dict(payload["invite"])
        fleet_invite.verify(invite)
        request = fleet_enroll.EnrollmentRequest.from_dict(payload["request"])
        fleet_enroll.verify_request(request, invite=invite)
        request_id = _require_hex64(payload["request_id"], "request_id")
        if request_id != fleet_enroll.request_id(request):
            raise FleetEnrollmentClientError(
                "fleet recovery request id does not match its request"
            )
        resume_token = _require_hex64(payload["resume_token"], "resume_token")
        code = payload["verification_code"]
        if code != fleet_enroll.verification_code(request):
            raise FleetEnrollmentClientError(
                "fleet recovery verification code does not match its request"
            )
        return cls(invite, request, request_id, resume_token, code)


@dataclass(frozen=True)
class EnrollmentResult:
    status: str
    recovery: EnrollmentRecovery
    delivery: fleet_enroll.EnrollmentDelivery | None = None
    personal_root_armor: str | None = None
    personal_root_created_at: str | None = None
    personal_root_updated_at: str | None = None
    #: For status == "unavailable": which invitation fault the serving side
    #: named — invite_inactive | invite_unknown | invite_expired — so the UI
    #: can tell the operator the exact cause and fix.
    reason: str | None = None


class FleetJoinStateStore:
    """Joining-side retry state in machine.db; never part of personal sync."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else _org_db_path("machine")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS fleet_enrollment_join_state ("
                "request_id TEXT PRIMARY KEY, invite_id TEXT NOT NULL, "
                "recovery_json TEXT NOT NULL, created_at INTEGER NOT NULL, "
                "updated_at INTEGER NOT NULL, delivery_json TEXT)"
            )
            columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(fleet_enrollment_join_state)"
                )
            }
            if "delivery_json" not in columns:
                conn.execute(
                    "ALTER TABLE fleet_enrollment_join_state "
                    "ADD COLUMN delivery_json TEXT"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS fleet_enrollment_join_invite "
                "ON fleet_enrollment_join_state(invite_id, updated_at)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA secure_delete=ON")
        return conn

    def save(
        self, recovery: EnrollmentRecovery, *, now_ms: int | None = None
    ) -> None:
        frozen = EnrollmentRecovery.from_dict(recovery.to_dict())
        now = _now_ms(now_ms)
        wire = json.dumps(
            frozen.to_dict(), sort_keys=True, separators=(",", ":")
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO fleet_enrollment_join_state "
                "(request_id,invite_id,recovery_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET "
                "recovery_json=excluded.recovery_json, "
                "updated_at=excluded.updated_at",
                (
                    frozen.request_id,
                    frozen.invite.invite_id,
                    wire,
                    now,
                    now,
                ),
            )

    def load(self, request_id: str) -> EnrollmentRecovery | None:
        rid = _require_hex64(request_id, "request_id")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT recovery_json FROM fleet_enrollment_join_state "
                "WHERE request_id=?",
                (rid,),
            ).fetchone()
        return (
            EnrollmentRecovery.from_dict(json.loads(row["recovery_json"]))
            if row is not None
            else None
        )

    def latest(self, invite_id: str) -> EnrollmentRecovery | None:
        iid = _require_hex64(invite_id, "invite_id")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT recovery_json FROM fleet_enrollment_join_state "
                "WHERE invite_id=? ORDER BY updated_at DESC, request_id DESC "
                "LIMIT 1",
                (iid,),
            ).fetchone()
        return (
            EnrollmentRecovery.from_dict(json.loads(row["recovery_json"]))
            if row is not None
            else None
        )

    def latest_any(self) -> EnrollmentRecovery | None:
        """Newest unfinished enrollment, for the local first-render shell."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT recovery_json FROM fleet_enrollment_join_state "
                "ORDER BY updated_at DESC, request_id DESC LIMIT 1"
            ).fetchone()
        return (
            EnrollmentRecovery.from_dict(json.loads(row["recovery_json"]))
            if row is not None
            else None
        )

    def save_delivery(
        self,
        request_id: str,
        delivery: fleet_enroll.EnrollmentDelivery,
        *,
        now_ms: int | None = None,
    ) -> None:
        rid = _require_hex64(request_id, "request_id")
        frozen = fleet_enroll.EnrollmentDelivery(
            fleet_enroll.EnrollmentApproval.from_dict(
                delivery.approval.to_dict()
            ),
            fleet_roster.RosterEntry.from_dict(
                delivery.roster_entry.to_dict()
            ),
            (
                fleet_roster.RosterEntry.from_dict(delivery.origin_entry.to_dict())
                if delivery.origin_entry is not None else None
            ),
            tuple(
                fleet_roster.RosterEntry.from_dict(entry.to_dict())
                for entry in delivery.active_roster
            ),
        )
        wire = json.dumps({
            "approval": frozen.approval.to_dict(),
            "roster_entry": frozen.roster_entry.to_dict(),
            "origin_entry": (
                frozen.origin_entry.to_dict()
                if frozen.origin_entry is not None else None
            ),
            "active_roster": [
                entry.to_dict() for entry in frozen.active_roster
            ],
        }, sort_keys=True, separators=(",", ":"))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE fleet_enrollment_join_state "
                "SET delivery_json=?, updated_at=? WHERE request_id=?",
                (wire, _now_ms(now_ms), rid),
            ).rowcount
            if changed != 1:
                raise FleetEnrollmentClientError(
                    "cannot save fleet delivery without its recovery state"
                )

    def load_delivery(
        self, request_id: str
    ) -> fleet_enroll.EnrollmentDelivery | None:
        rid = _require_hex64(request_id, "request_id")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT delivery_json FROM fleet_enrollment_join_state "
                "WHERE request_id=?",
                (rid,),
            ).fetchone()
        if row is None or row["delivery_json"] is None:
            return None
        payload = json.loads(row["delivery_json"])
        base_fields = {"approval", "roster_entry", "origin_entry"}
        if (
            not isinstance(payload, dict)
            or not base_fields <= set(payload)
            or set(payload) - base_fields - {"active_roster"}
        ):
            # A shape this version does not understand is treated as ABSENT, not
            # as an error. Join state is spent once enrollment completes — the
            # machine is already on the roster — so discarding a stale record
            # loses nothing, while raising here is unrecoverable for the
            # operator: this is read during the index page render, so a node
            # that enrolled under an older field set answers 500 on `/` after an
            # upgrade with no way back except hand-editing machine.db.
            #
            # Observed live on 2026-08-30: a node enrolled under 0e4e81bf had
            # {approval, roster_entry, roster_entries}; `roster_entries` was
            # later renamed `origin_entry`, and the strict comparison turned a
            # dead row into a bricked front page.
            log.warning(
                "discarding saved fleet delivery for %s: field set %s is not "
                "understood by this version (expected approval, roster_entry, "
                "origin_entry) — enrollment is already complete, so this state "
                "is spent",
                rid[:12], sorted(payload) if isinstance(payload, dict) else type(payload).__name__,
            )
            return None
        origin_payload = payload["origin_entry"]
        active_payload = payload.get("active_roster") or []
        return fleet_enroll.EnrollmentDelivery(
            fleet_enroll.EnrollmentApproval.from_dict(payload["approval"]),
            fleet_roster.RosterEntry.from_dict(payload["roster_entry"]),
            (
                fleet_roster.RosterEntry.from_dict(origin_payload)
                if origin_payload is not None else None
            ),
            tuple(
                fleet_roster.RosterEntry.from_dict(entry)
                for entry in active_payload
            ),
        )

    def delete(self, request_id: str) -> None:
        rid = _require_hex64(request_id, "request_id")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM fleet_enrollment_join_state WHERE request_id=?",
                (rid,),
            )


class FleetEnrollmentClient:
    """One request/response exchange per independently authenticated channel."""

    def __init__(
        self,
        *,
        envelope_fetcher=None,
        channel_connector=None,
        state_store: FleetJoinStateStore | None = None,
        now_ms=None,
    ):
        self.envelope_fetcher = envelope_fetcher or _fetch_envelope
        self.channel_connector = channel_connector or ViewerChannel.connect
        self.state_store = state_store or FleetJoinStateStore()
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))

    async def start_or_recover(
        self,
        invite: fleet_invite.FleetInvite,
        *,
        machine_id: str | None = None,
    ) -> EnrollmentRecovery:
        """Reuse durable retry state before creating another pending request."""
        existing = self.state_store.latest(invite.invite_id)
        try:
            self._verify_live_invite(invite)
        except FleetInvitationExpired:
            # Drop only an UNDELIVERED retry record. A row that already holds
            # the delivered armor is a completed-but-unfinished join; deleting
            # it destroys the machine's path to finishing and forces a ghost
            # re-enrollment under a fresh identity.
            if (
                existing is not None
                and existing.invite == invite
                and self.state_store.load_delivery(existing.request_id) is None
            ):
                self.state_store.delete(existing.request_id)
            raise
        if existing is not None:
            if existing.invite != invite:
                raise FleetEnrollmentClientError(
                    "saved fleet recovery belongs to different invitation bytes"
                )
            return existing
        return await self.start(invite, machine_id=machine_id)

    async def start(
        self,
        invite: fleet_invite.FleetInvite,
        *,
        machine_id: str | None = None,
    ) -> EnrollmentRecovery:
        self._verify_live_invite(invite)
        request = fleet_enroll.build_request(
            invite=invite,
            machine_id=machine_id,
        )
        reply = await self._exchange(invite, {
            "v": 1,
            "op": "fleet.request",
            "request": request.to_dict(),
        })
        expected = {
            "v", "status", "request_id", "verification_code", "resume_token"
        }
        if set(reply) != expected or reply.get("v") != 1 \
                or reply.get("status") != "pending":
            raise FleetEnrollmentClientError(
                "fleet request returned an invalid pending response"
            )
        recovery = EnrollmentRecovery.from_dict({
            "invite": invite.to_dict(),
            "request": request.to_dict(),
            "request_id": reply["request_id"],
            "resume_token": reply["resume_token"],
            "verification_code": reply["verification_code"],
        })
        self.state_store.save(recovery, now_ms=self.now_ms())
        return recovery

    async def resume(self, recovery: EnrollmentRecovery) -> EnrollmentResult:
        frozen = EnrollmentRecovery.from_dict(recovery.to_dict())
        try:
            self._verify_live_invite(frozen.invite)
        except FleetInvitationExpired:
            self.state_store.delete(frozen.request_id)
            return EnrollmentResult(status="expired", recovery=frozen)
        reply = await self._exchange(frozen.invite, {
            "v": 1,
            "op": "fleet.resume",
            "request_id": frozen.request_id,
            "resume_token": frozen.resume_token,
        })
        base = {"v", "status", "request_id", "verification_code"}
        # The serving side reached us and named a specific invitation fault
        # (deactivated / unknown / expired). This is NOT a transport failure —
        # home answered — so surface the reason for the UI to act on, rather
        # than the generic "did not return a valid response" below.
        if (
            isinstance(reply, dict)
            and reply.get("v") == 1
            and reply.get("status") == "unavailable"
        ):
            return EnrollmentResult(
                status="unavailable", recovery=frozen,
                reason=reply.get("reason"),
            )
        # Distinguish the failure the joiner can act on. The common one is a
        # serving side that is not actually answering: an offline, locked, or
        # stale-code home Dashboard returns a relay/gateway error rather than a
        # fleet enrollment envelope, so the reply is not a well-formed
        # response at all. A well-formed response whose ids differ is a real
        # request mismatch (stale/duplicate, or a different invitation) — a
        # separate, separately-actionable condition.
        if not isinstance(reply, dict) or reply.get("v") != 1 or not base <= set(reply):
            raise FleetEnrollmentClientError(
                "the other Dashboard did not return a valid enrollment "
                "response. It is usually offline, locked, or running "
                "outdated code that needs a restart. Restart and unlock the "
                "other Dashboard; setup here continues on its own once it is "
                "serving again."
            )
        if reply["request_id"] != frozen.request_id:
            raise FleetEnrollmentClientError(
                "the enrollment response is for a different request than this "
                "machine sent (a stale or duplicate request). Start the join "
                "again on this machine."
            )
        if reply["verification_code"] != frozen.verification_code:
            raise FleetEnrollmentClientError(
                "the enrollment response's verification code does not match "
                "this request — the other Dashboard may be using a different "
                "invitation. Mint a fresh invite and re-run the join."
            )
        status = reply.get("status")
        if status in {"pending", "declined"} and set(reply) == base:
            return EnrollmentResult(status=status, recovery=frozen)
        approved_fields = base | {
            "approval", "roster_entry", "origin_entry",
            "personal_root_armor", "personal_root_created_at",
            "personal_root_updated_at",
        }
        # ``active_roster`` (the whole fleet, for peer bootstrap) is optional so
        # a joiner still verifies against an origin that predates the field.
        optional_fields = {"active_roster"}
        extra = set(reply) - approved_fields
        if (
            status != "approved"
            or not approved_fields <= set(reply)
            or extra - optional_fields
        ):
            raise FleetEnrollmentClientError(
                "fleet resume returned an invalid status envelope"
            )
        armor = reply["personal_root_armor"]
        if not isinstance(armor, str) or not armor:
            raise FleetEnrollmentClientError(
                "approved fleet response carries no encrypted personal root"
            )
        timestamps = []
        parsed_timestamps = []
        for field in ("personal_root_created_at", "personal_root_updated_at"):
            value = reply[field]
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (AttributeError, ValueError):
                raise FleetEnrollmentClientError(
                    f"approved fleet response carries invalid {field}"
                ) from None
            if parsed.tzinfo is None:
                raise FleetEnrollmentClientError(
                    f"approved fleet response carries invalid {field}"
                )
            timestamps.append(value)
            parsed_timestamps.append(parsed)
        if parsed_timestamps[0] > parsed_timestamps[1]:
            raise FleetEnrollmentClientError(
                "approved fleet response carries reversed personal-root timestamps"
            )
        approval = fleet_enroll.EnrollmentApproval.from_dict(reply["approval"])
        roster_entry = fleet_roster.RosterEntry.from_dict(reply["roster_entry"])
        origin_payload = reply["origin_entry"]
        if not isinstance(origin_payload, dict):
            raise FleetEnrollmentClientError(
                "fleet delivery names no origin roster entry"
            )
        origin_entry = fleet_roster.RosterEntry.from_dict(origin_payload)
        active_roster = _parse_active_roster(
            reply.get("active_roster"),
            anchor_root_pub=frozen.invite.personal_root_pub,
        )
        fleet_enroll.verify_approval(
            approval,
            frozen.request,
            invite=frozen.invite,
            channel_binding=frozen.channel_binding,
            roster_entry=roster_entry,
            anchor_root_pub=frozen.invite.personal_root_pub,
        )
        delivery = fleet_enroll.EnrollmentDelivery(
            approval, roster_entry, origin_entry, active_roster
        )
        fleet_enroll.verify_bootstrap_roster(
            delivery,
            anchor_root_pub=frozen.invite.personal_root_pub,
            joining_machine_pub=roster_entry.machine_pub,
        )
        return EnrollmentResult(
            status="approved",
            recovery=frozen,
            delivery=delivery,
            personal_root_armor=armor,
            personal_root_created_at=timestamps[0],
            personal_root_updated_at=timestamps[1],
        )

    async def _exchange(self, invite: fleet_invite.FleetInvite, message: dict):
        location = _invite_location(invite)
        envelope = await self.envelope_fetcher(location.http_base, location.token)
        _verify_envelope(envelope)
        channel = await self.channel_connector(
            location.ws_base,
            location.token,
            root_pub=envelope["root_pub"],
            org=envelope["org"],
        )
        try:
            await channel.send_message(canonical_json(message))
            raw = await channel.recv_message()
        finally:
            await channel.close()
        try:
            reply = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            raise FleetEnrollmentClientError(
                "fleet invitation channel returned non-JSON"
            ) from None
        if not isinstance(reply, dict):
            raise FleetEnrollmentClientError(
                "fleet invitation channel response is not an object"
            )
        return reply

    def _verify_live_invite(self, invite: fleet_invite.FleetInvite) -> None:
        fleet_invite.verify(invite)
        now = _now_ms(self.now_ms())
        if invite.expires_at and now >= invite.expires_at:
            raise FleetInvitationExpired("fleet invitation has expired")


@dataclass(frozen=True)
class _InviteLocation:
    http_base: str
    ws_base: str
    token: str


def _invite_location(invite: fleet_invite.FleetInvite) -> _InviteLocation:
    parsed = urllib.parse.urlsplit(invite.rendezvous)
    parts = parsed.path.split("/")
    token = parts[-1] if len(parts) == 3 and parts[1] == "l" else ""
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not _HEX32.fullmatch(token)
    ):
        raise FleetEnrollmentClientError(
            "fleet invitation does not name an exact https grant URL"
        )
    return _InviteLocation(
        http_base=f"https://{parsed.netloc}",
        ws_base=f"wss://{parsed.netloc}",
        token=token,
    )


async def _fetch_envelope(http_base: str, token: str) -> dict:
    async with httpx.AsyncClient(base_url=http_base, timeout=10.0) as client:
        response = await client.get(f"/v1/links/{token}/envelope")
        if response.status_code != 200:
            raise FleetEnrollmentClientError(
                "fleet invitation is unavailable at its rendezvous"
            )
        try:
            value = response.json()
        except ValueError:
            raise FleetEnrollmentClientError(
                "fleet invitation envelope is not JSON"
            ) from None
    return value


def _verify_envelope(value: dict) -> None:
    if not isinstance(value, dict) or value.get("target_type") != "fleet:join":
        raise FleetEnrollmentClientError(
            "rendezvous grant is not a fleet invitation"
        )
    if not isinstance(value.get("org"), str) or not value["org"]:
        raise FleetEnrollmentClientError("fleet grant envelope has no organization")
    _require_hex64(value.get("root_pub"), "serving root_pub")
    try:
        uuid.UUID(str(value.get("target_uuid")))
    except (ValueError, TypeError, AttributeError):
        raise FleetEnrollmentClientError(
            "fleet grant envelope has no invitation target"
        ) from None


def _parse_active_roster(
    payload, *, anchor_root_pub: str
) -> tuple[fleet_roster.RosterEntry, ...]:
    """Decode a delivered active roster, keeping only entries that verify.

    ``None`` or an empty list (an origin that predates the whole-roster
    delivery) yields an empty tuple; a joiner then falls back to the origin
    pair. A malformed or foreign entry is dropped rather than failing the whole
    approved delivery, matching ``fleet_roster.resolve`` which drops the
    unverifiable on read.
    """
    if payload is None:
        return ()
    if not isinstance(payload, list):
        raise FleetEnrollmentClientError(
            "fleet delivery active_roster must be a list"
        )
    entries: list[fleet_roster.RosterEntry] = []
    for item in payload:
        try:
            entry = fleet_roster.RosterEntry.from_dict(item)
            fleet_roster.verify(entry, anchor_root_pub=anchor_root_pub)
        except (fleet_roster.FleetRosterError, TypeError, ValueError):
            continue
        entries.append(entry)
    return tuple(entries)


def _require_hex64(value, name: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise FleetEnrollmentClientError(
            f"fleet enrollment {name} must be 64 lowercase hex chars"
        )
    return value


def _now_ms(value: int | None) -> int:
    now = int(time.time() * 1000) if value is None else value
    if not isinstance(now, int) or isinstance(now, bool) or now < 0:
        raise FleetEnrollmentClientError("now_ms must be a non-negative int")
    return now
