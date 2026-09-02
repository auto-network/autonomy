"""Roster-driven personal-database synchronization inside the Dashboard.

The service is deliberately in-process: one scheduler owns the direct
listener and outbound peer sessions, while each SQLite operation uses a short
fresh connection on a worker thread. No file watcher, daemon, or long-lived
database transaction participates.

Each receiver acknowledges the serving database's local transaction row id
only after it verifies the terminal stream summary. A reconnect can replay the
last incomplete page safely, while an ordinary poll requests only transactions
inserted after the last completed stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from tools.network.fleet_roster import RosterEntry, resolve
from tools.network.fleet_sync_channel import (
    FleetAuthenticator,
    FleetDirectServer,
    fleet_direct_connect,
)
from tools.network.fleet_sync_connection import FleetSyncConnection
from tools.network.fleet_sync.catalog import (
    AuthoredMutation,
    MAX_TRANSACTION_OPERATIONS,
    MutationCatalog,
    WatermarkError,
    attach_active_production_catalog,
)
from tools.network.fleet_sync.codec import (
    decode_mutation_frame,
    encode_mutation_frame,
)
from tools.network.idkit import KeyPair, canonical_json
from tools.network.idkit import DelegationCert
from tools.network.relaykit.channel import MAX_MESSAGE_SIZE
from tools.network.relaykit.direct import new_session_id

logger = logging.getLogger(__name__)

FLEET_SYNC_PROTOCOL_VERSION = 3
_REQUEST_FIELDS = frozenset({
    "v", "op", "roster_epoch", "resume", "compat",
})
_REQUEST_OPTIONAL_FIELDS = frozenset({"scope", "bootstrap"})
FILE_MAGIC = b"FSB1"
_REFUSAL_FIELDS = frozenset({"v", "kind", "digest"})
_DONE_FIELDS = frozenset(
    {
        "v", "kind", "roster_epoch", "message_count", "digest",
        "through_transaction_ref", "through_breadcrumb",
    }
)
_MUTATION_MAGIC = b"FST1"
_DONE_MAGIC = b"FSD1"
_REFUSAL_MAGIC = b"FSR1"
_HEADER_LIMIT = 4096
_SETTINGS_HINT_LIMIT = 256
#: A resume trail carries the last few verified stream positions plus an
#: exponentially thinned history, so its length is logarithmic in stream age.
MAX_RESUME_BREADCRUMBS = 64
#: A pull whose stream goes silent this long is failed and retried through
#: ordinary backoff. Without it, one wedged serve freezes the puller's
#: entire round loop forever (observed as a rare whole-fleet stall under
#: heavy CPU contention).
PULL_STREAM_SILENCE_LIMIT_S = 30.0


@dataclass(frozen=True, slots=True)
class MaterializedSettingsAddress:
    """One payload-free Settings address durably received through sync."""

    set_id: str
    schema_revision: int
    key: str


_settings_materialization_hook: Callable[..., object] | None = None


def set_settings_materialization_hook(hook: Callable[..., object] | None) -> None:
    """Install the process-local, best-effort post-materialization observer.

    The hook is a wake hint, never synchronization authority.  It receives at
    most 256 payload-free Settings addresses and a ``gap`` flag after a delta
    transaction commits or a checkpoint is published.  Hook failure can never
    fail or roll back Fleet synchronization.
    """
    global _settings_materialization_hook
    if hook is not None and not callable(hook):
        raise TypeError("settings materialization hook must be callable")
    _settings_materialization_hook = hook


def _emit_settings_materialized(
    items: Iterable[AuthoredMutation] = (), *, gap: bool = False,
) -> None:
    hook = _settings_materialization_hook
    if hook is None:
        return
    addresses: set[MaterializedSettingsAddress] = set()
    overflow = bool(gap)
    try:
        for item in items:
            mutation = item.mutation
            if mutation.table != "settings":
                continue
            address = mutation.address
            if len(address) < 3:
                overflow = True
                continue
            set_id, revision, key = address[:3]
            if (
                not isinstance(set_id, str)
                or isinstance(revision, bool)
                or not isinstance(revision, int)
                or not isinstance(key, str)
            ):
                overflow = True
                continue
            addresses.add(MaterializedSettingsAddress(set_id, revision, key))
            if len(addresses) > _SETTINGS_HINT_LIMIT:
                addresses.clear()
                overflow = True
                break
        hook(addresses=tuple(sorted(
            addresses, key=lambda row: (row.set_id, row.schema_revision, row.key)
        )), gap=overflow)
    except Exception:
        logger.warning("post-materialization Settings hint failed", exc_info=True)


class FleetSyncProtocolError(ValueError):
    """A peer sent malformed, inconsistent, or unsupported sync data."""


class FleetSyncSchemaMismatch(FleetSyncProtocolError):
    """The peer's replicated schema differs; synchronization is paused.

    Not a fault: one fleet machine upgraded before the other.  The refusal
    is typed so status surfaces show a pause, and the ordinary poll/backoff
    loop resumes automatically once the lagging machine's own software
    applies the same migration locally.
    """


@dataclass(frozen=True)
class FleetSyncRuntimeConfig:
    """Runtime material the unlocked fleet-identity boundary must supply.

    ``peer_addresses`` maps active machine public keys to direct RelayKit
    WebSocket candidates. The peer never supplies a local database path.
    """

    machine_key: KeyPair
    personal_root_pub: str
    roster_entries: Callable[[], Iterable[RosterEntry]]
    peer_addresses: Callable[[], Mapping[str, Sequence[str]]]
    personal_db_path: Path
    roster_machine_pub: str | None = None
    delegation_cert: DelegationCert | None = None
    require_delegation: bool = False
    listen_host: str = "127.0.0.1"
    listen_port: int = 0
    poll_interval: float = 10.0
    connect_timeout: float = 3.0
    min_backoff: float = 0.25
    max_backoff: float = 5.0
    telemetry_recorder: Callable[..., object] | None = None
    #: Returns the locally stored resume breadcrumb trail for one
    #: (peer, scope) — each breadcrumb names one verified transaction
    #: (origin, transaction id, timestamp), newest first — never row
    #: numbers.
    resume_cursor: Callable[
        [str, str], Sequence[tuple[str, str, int]]
    ] | None = None
    #: Organization databases synchronized beside the personal one, as
    #: scope slug -> database path. The personal scope is implicit and
    #: always first. Databases share the graph schema; each keeps its own
    #: catalog, journal, peer state, and breadcrumb trails.
    sync_scopes: Callable[[], Mapping[str, Path]] | None = None
    #: Bounded pulls per round; the stalest-first ranking below decides
    #: which peers fill the slots. Zero or negative means unbounded.
    max_concurrent_pulls: int = 3


def discover_org_sync_scopes() -> dict[str, Path]:
    """Organization databases present on this machine, by slug.

    Resolved fresh on every round so an organization created mid-run joins
    synchronization without a restart. The personal database is excluded —
    it is the implicit first scope.
    """
    from tools.graph.db import _org_db_path

    orgs_dir = Path(_org_db_path("personal")).parent / "orgs"
    if not orgs_dir.is_dir():
        return {}
    scopes: dict[str, Path] = {}
    for candidate in sorted(orgs_dir.glob("*.db")):
        slug = candidate.stem
        if slug and slug != "personal" and ":" not in slug:
            scopes[slug] = candidate
    return scopes


def rank_peers(
    candidates: Sequence[str],
    last_success_ns: Mapping[str, int],
    *,
    limit: int,
) -> list[str]:
    """Stalest-first bounded peer selection for one sync round.

    A peer with no recorded success ever ranks ahead of every peer with
    one, so a machine returning from a long absence — or never yet synced
    — fills the first slot on its first eligible round. Among recorded
    successes, oldest first. Ties break on the key itself so rounds are
    deterministic. Starvation-free by construction: an unselected peer's
    staleness only grows, monotonically raising its rank until selected.
    """
    ordered = sorted(
        candidates,
        key=lambda pub: (last_success_ns.get(pub) or 0, pub),
    )
    if limit <= 0:
        return ordered
    return ordered[:limit]


def roster_epoch(entries: Iterable[RosterEntry], root_pub: str) -> str:
    active = sorted(resolve(entries, anchor_root_pub=root_pub))
    return hashlib.sha256(
        b"autonomy.network.fleet-sync.roster-epoch.v1\n"
        + canonical_json(active)
    ).hexdigest()


def _json_loose(raw: bytes) -> dict:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FleetSyncProtocolError(
            "fleet stream control frame is not JSON"
        ) from exc
    if not isinstance(value, dict):
        raise FleetSyncProtocolError(
            "fleet stream control frame must be an object"
        )
    return value


def _json_object(raw: bytes, fields: frozenset[str], what: str) -> dict:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise FleetSyncProtocolError(f"{what} is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != fields:
        raise FleetSyncProtocolError(
            f"{what} must carry exactly {sorted(fields)}"
        )
    if value["v"] != FLEET_SYNC_PROTOCOL_VERSION:
        raise FleetSyncProtocolError(
            f"unsupported {what} version: {value['v']!r}"
        )
    return value


def encode_breadcrumb(breadcrumb: tuple[str, str, int]) -> dict:
    origin, transaction, timestamp = breadcrumb
    return {"origin": origin, "transaction": transaction, "timestamp": timestamp}


def decode_breadcrumb(value: object, what: str) -> tuple[str, str, int]:
    if not isinstance(value, dict) or set(value) != {
        "origin", "transaction", "timestamp"
    }:
        raise FleetSyncProtocolError(f"{what} has wrong fields")
    origin = value["origin"]
    transaction = value["transaction"]
    timestamp = value["timestamp"]
    if (
        not isinstance(origin, str)
        or len(origin) != 64
        or any(ch not in "0123456789abcdef" for ch in origin)
    ):
        raise FleetSyncProtocolError(f"{what} origin is malformed")
    if not isinstance(transaction, str) or not transaction:
        raise FleetSyncProtocolError(f"{what} transaction is malformed")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        raise FleetSyncProtocolError(f"{what} timestamp is malformed")
    return origin, transaction, timestamp


def _require_hex64(value: object, what: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise FleetSyncProtocolError(f"{what} is malformed")
    return value


def encode_schema_refusal(*, digest: str) -> bytes:
    return _REFUSAL_MAGIC + canonical_json(
        {
            "v": FLEET_SYNC_PROTOCOL_VERSION,
            "kind": "schema-refused",
            "digest": _require_hex64(digest, "schema refusal digest"),
        }
    )


def decode_schema_refusal(raw: bytes) -> str:
    if not raw.startswith(_REFUSAL_MAGIC):
        raise FleetSyncProtocolError("fleet sync refusal has wrong magic")
    value = _json_object(
        raw[len(_REFUSAL_MAGIC):], _REFUSAL_FIELDS, "fleet sync refusal"
    )
    if value["kind"] != "schema-refused":
        raise FleetSyncProtocolError("fleet sync refusal has wrong kind")
    return _require_hex64(value["digest"], "fleet sync refusal digest")


def encode_checkpoint_file(relative: str, body: bytes) -> bytes:
    header = canonical_json({
        "path": relative,
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    })
    return FILE_MAGIC + struct.pack(">I", len(header)) + header + body


def decode_checkpoint_file(raw: bytes) -> tuple[str, bytes]:
    from pathlib import PurePosixPath

    if not isinstance(raw, bytes) or not raw.startswith(FILE_MAGIC) or len(raw) < 8:
        raise FleetSyncProtocolError("checkpoint file frame is malformed")
    header_size = struct.unpack(">I", raw[4:8])[0]
    if header_size > 4096 or 8 + header_size > len(raw):
        raise FleetSyncProtocolError("checkpoint file header is malformed")
    try:
        header = json.loads(raw[8:8 + header_size])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FleetSyncProtocolError("checkpoint file header is not JSON") from exc
    if not isinstance(header, dict) or set(header) != {"path", "size", "sha256"}:
        raise FleetSyncProtocolError("checkpoint file header has unknown fields")
    relative = header["path"]
    path = PurePosixPath(relative) if isinstance(relative, str) else None
    if (
        path is None
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise FleetSyncProtocolError("checkpoint file path escapes its stage")
    body = raw[8 + header_size:]
    if header["size"] != len(body) or header["sha256"] != hashlib.sha256(body).hexdigest():
        raise FleetSyncProtocolError("checkpoint file digest does not match")
    return path.as_posix(), body


def serve_checkpoint_decision(
    resume_position: int, requested: bool, journal_gap: bool
) -> bool:
    """A resolvable trail always means deltas; an unresolvable one means a
    checkpoint when the peer asked or when replay would omit retired
    history."""
    return resume_position == 0 and (requested or journal_gap)


def encode_pull_request(
    epoch: str,
    *,
    compat: str,
    resume: Sequence[tuple[str, str, int]] = (),
    scope: str = "personal",
    bootstrap: bool = False,
) -> bytes:
    """Ask for every journal transaction after a content-addressed position.

    ``resume`` is the puller's verified breadcrumb trail, newest first.  It
    names transactions, never the serving database's private row numbers,
    so the server recomputes the position from its own journal on every
    pull and a server restored from backup is re-served from the newest
    breadcrumb it still holds.
    """
    if len(resume) > MAX_RESUME_BREADCRUMBS:
        raise FleetSyncProtocolError("fleet sync resume trail exceeds bound")
    body = {
        "v": FLEET_SYNC_PROTOCOL_VERSION,
        "op": "pull",
        "roster_epoch": epoch,
        "compat": _require_hex64(compat, "fleet sync compat digest"),
        "resume": [
            encode_breadcrumb(breadcrumb) for breadcrumb in resume
        ],
    }
    # The personal scope keeps the historical request bytes, so a fleet with
    # mixed software versions synchronizes its personal database regardless;
    # only organization-scope pulls carry the field an older server refuses.
    if scope != "personal":
        if not scope or not isinstance(scope, str) or ":" in scope:
            raise FleetSyncProtocolError("fleet sync scope is malformed")
        body["scope"] = scope
    if bootstrap:
        body["bootstrap"] = True
    return canonical_json(body)


def decode_pull_request(
    raw: bytes,
) -> tuple[str, tuple[tuple[str, str, int], ...], str, str, bool]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise FleetSyncProtocolError(
            "fleet sync request is not valid JSON"
        ) from exc
    if (
        not isinstance(value, dict)
        or not _REQUEST_FIELDS <= set(value)
        or not set(value) <= (_REQUEST_FIELDS | _REQUEST_OPTIONAL_FIELDS)
    ):
        raise FleetSyncProtocolError(
            f"fleet sync request must carry {sorted(_REQUEST_FIELDS)} "
            f"plus only {sorted(_REQUEST_OPTIONAL_FIELDS)}"
        )
    if value["v"] != FLEET_SYNC_PROTOCOL_VERSION:
        raise FleetSyncProtocolError("unsupported fleet sync request version")
    scope = value.get("scope", "personal")
    if not isinstance(scope, str) or not scope or ":" in scope:
        raise FleetSyncProtocolError("fleet sync scope is malformed")
    bootstrap = value.get("bootstrap", False)
    if not isinstance(bootstrap, bool):
        raise FleetSyncProtocolError("fleet sync bootstrap flag must be bool")
    if value["op"] != "pull":
        raise FleetSyncProtocolError("unsupported fleet sync operation")
    epoch = _require_hex64(value["roster_epoch"], "fleet sync roster_epoch")
    compat = _require_hex64(value["compat"], "fleet sync compat digest")
    resume = value["resume"]
    if not isinstance(resume, list) or len(resume) > MAX_RESUME_BREADCRUMBS:
        raise FleetSyncProtocolError("fleet sync resume trail is malformed")
    return epoch, tuple(
        decode_breadcrumb(entry, "fleet sync resume breadcrumb")
        for entry in resume
    ), compat, scope, bootstrap


def encode_authored(item: AuthoredMutation, *, transaction_operations: int) -> bytes:
    frame = encode_mutation_frame(item.mutation)
    header = canonical_json(
        {
            "origin": item.origin_incarnation,
            "transaction": item.transaction_id,
            "operation": item.operation_index,
            "transaction_operations": transaction_operations,
        }
    )
    message = _MUTATION_MAGIC + struct.pack(">I", len(header)) + header + frame
    if len(header) > _HEADER_LIMIT or len(message) > MAX_MESSAGE_SIZE:
        raise FleetSyncProtocolError(
            "authored mutation exceeds the fleet channel message bound"
        )
    return message


def decode_authored(raw: bytes) -> tuple[AuthoredMutation, int]:
    if len(raw) < 8 or raw[:4] != _MUTATION_MAGIC:
        raise FleetSyncProtocolError("fleet mutation message has wrong magic")
    header_len = struct.unpack(">I", raw[4:8])[0]
    if not 1 <= header_len <= _HEADER_LIMIT or 8 + header_len >= len(raw):
        raise FleetSyncProtocolError("fleet mutation header length is invalid")
    try:
        header = json.loads(raw[8:8 + header_len])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FleetSyncProtocolError("fleet mutation header is not valid JSON") from exc
    if not isinstance(header, dict) or set(header) != {
        "origin", "transaction", "operation", "transaction_operations"
    }:
        raise FleetSyncProtocolError("fleet mutation header has wrong fields")
    origin = header["origin"]
    transaction = header["transaction"]
    operation = header["operation"]
    operation_count = header["transaction_operations"]
    if (
        not isinstance(origin, str)
        or len(origin) != 64
        or any(ch not in "0123456789abcdef" for ch in origin)
    ):
        raise FleetSyncProtocolError("fleet mutation origin is malformed")
    if not isinstance(transaction, str) or not transaction:
        raise FleetSyncProtocolError("fleet mutation transaction is malformed")
    if not isinstance(operation, int) or isinstance(operation, bool) or operation < 0:
        raise FleetSyncProtocolError("fleet mutation operation is malformed")
    if (
        not isinstance(operation_count, int)
        or isinstance(operation_count, bool)
        or not 1 <= operation_count <= MAX_TRANSACTION_OPERATIONS
    ):
        raise FleetSyncProtocolError(
            "fleet mutation transaction operation count is malformed"
        )
    try:
        mutation = decode_mutation_frame(raw[8 + header_len:])
    except Exception as exc:
        raise FleetSyncProtocolError("fleet mutation frame is malformed") from exc
    return AuthoredMutation(origin, transaction, operation, mutation), operation_count


def encode_done(
    *,
    epoch: str,
    count: int,
    digest: str,
    through_transaction_ref: int = 0,
    through_breadcrumb: tuple[str, str, int] | None = None,
) -> bytes:
    if (
        isinstance(through_transaction_ref, bool)
        or not isinstance(through_transaction_ref, int)
        or through_transaction_ref < 0
    ):
        raise FleetSyncProtocolError("through_transaction_ref is malformed")
    return _DONE_MAGIC + canonical_json(
        {
            "v": FLEET_SYNC_PROTOCOL_VERSION,
            "kind": "done",
            "roster_epoch": epoch,
            "message_count": count,
            "digest": digest,
            "through_transaction_ref": through_transaction_ref,
            "through_breadcrumb": (
                None
                if through_breadcrumb is None
                else encode_breadcrumb(through_breadcrumb)
            ),
        }
    )


def decode_done(raw: bytes) -> tuple[str, int, str, int, tuple[str, str, int] | None]:
    if not raw.startswith(_DONE_MAGIC):
        raise FleetSyncProtocolError("fleet sync summary has wrong magic")
    value = _json_object(raw[len(_DONE_MAGIC):], _DONE_FIELDS, "fleet sync summary")
    if value["kind"] != "done":
        raise FleetSyncProtocolError("fleet sync summary has wrong kind")
    epoch = value["roster_epoch"]
    digest = value["digest"]
    count = value["message_count"]
    through = value["through_transaction_ref"]
    for name, candidate in (("roster_epoch", epoch), ("digest", digest)):
        if (
            not isinstance(candidate, str)
            or len(candidate) != 64
            or any(ch not in "0123456789abcdef" for ch in candidate)
        ):
            raise FleetSyncProtocolError(f"fleet sync summary {name} is malformed")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise FleetSyncProtocolError("fleet sync summary message_count is malformed")
    if isinstance(through, bool) or not isinstance(through, int) or through < 0:
        raise FleetSyncProtocolError(
            "fleet sync summary through_transaction_ref is malformed"
        )
    raw_breadcrumb = value["through_breadcrumb"]
    breadcrumb = (
        None
        if raw_breadcrumb is None
        else decode_breadcrumb(
            raw_breadcrumb, "fleet sync summary through breadcrumb"
        )
    )
    return epoch, count, digest, through, breadcrumb


def _digest_add(digest, message: bytes) -> None:
    digest.update(len(message).to_bytes(8, "big"))
    digest.update(message)


class SQLiteFleetSyncStore:
    """Short-connection access to one fixed activated personal database."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _open(self) -> tuple[FleetSyncConnection, MutationCatalog]:
        import sqlite3
        from tools.network.fleet_sync.streaming import register_streaming_functions

        conn = sqlite3.connect(self.path, factory=FleetSyncConnection)
        conn.row_factory = sqlite3.Row
        register_streaming_functions(conn)
        catalog = attach_active_production_catalog(conn)
        if catalog is None:
            conn.close()
            raise WatermarkError("personal database fleet writers are not active")
        # Remote batches carrying attachment rows realize from bytes this
        # machine already holds; a digest no local file satisfies defers to
        # quarantine instead of failing the batch.
        from tools.network.fleet_sync.materialize import production_blob_store

        catalog.blob_store = production_blob_store(self.path)
        return conn, catalog

    def next_transaction(
        self, after_transaction_ref: int
    ) -> tuple[int, list[AuthoredMutation]] | None:
        conn, catalog = self._open()
        try:
            return catalog.next_journal_transaction_ref(after_transaction_ref)
        finally:
            conn.close()

    def resume_ref(self, breadcrumbs: Sequence[tuple[str, str, int]]) -> int:
        conn, catalog = self._open()
        try:
            return catalog.journal_resume_ref(breadcrumbs)
        finally:
            conn.close()

    def breadcrumb(
        self, transaction_ref: int
    ) -> tuple[str, str, int] | None:
        conn, catalog = self._open()
        try:
            return catalog.journal_breadcrumb(transaction_ref)
        finally:
            conn.close()

    def compatibility_digest(self) -> str:
        """The replicated-surface digest for this database.

        Uses a plain connection: the digest is meaningful (and needed, for
        the first checkpoint pull) before fleet writers are activated.
        """
        import sqlite3
        from tools.network.fleet_sync.policies import compatibility_digest

        conn = sqlite3.connect(self.path)
        try:
            return compatibility_digest(conn)
        finally:
            conn.close()

    def apply(self, items: list[AuthoredMutation]) -> tuple[int, int]:
        conn, catalog = self._open()
        try:
            return catalog.apply_remote_batch(items)
        finally:
            conn.close()

    def peer_last_success(self) -> dict[str, int]:
        """Newest recorded pull success per peer, across epochs, for
        stalest-first ranking. Absent machines simply have no entry."""
        import sqlite3 as _sqlite3

        if not Path(self.path).exists():
            return {}
        conn = _sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            return {
                str(row[0]): int(row[1] or 0)
                for row in conn.execute(
                    "SELECT machine_public_key,MAX(last_success_ns) "
                    "FROM fleet_sync_peer_state GROUP BY machine_public_key"
                )
            }
        except _sqlite3.Error:
            return {}
        finally:
            conn.close()

    def has_state(self) -> bool:
        """Any applied or authored sync state at all, receipts included.

        Read with a plain connection: the answer is needed before fleet
        writers are activated on a brand-new database, where absent tables
        simply mean no state.
        """
        import sqlite3 as _sqlite3

        if not Path(self.path).exists():
            return False
        conn = _sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            receipt = conn.execute(
                "SELECT 1 FROM fleet_sync_peer_state "
                "WHERE checkpoints_received>0 LIMIT 1"
            ).fetchone()
            if receipt is not None:
                return True
            return conn.execute(
                "SELECT 1 FROM fleet_sync_transactions LIMIT 1"
            ).fetchone() is not None
        except _sqlite3.Error:
            return False
        finally:
            conn.close()

    def journal_gap(self) -> bool:
        from tools.network.fleet_sync.catalog import journal_has_gap

        import sqlite3 as _sqlite3

        conn = _sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            return journal_has_gap(conn)
        except _sqlite3.Error:
            return False
        finally:
            conn.close()

    def attachment_backlog(self):
        from tools.network.fleet_sync.blob_transport import (
            pending_attachment_backlog,
        )

        conn, _catalog = self._open()
        try:
            return pending_attachment_backlog(conn)
        finally:
            conn.close()

    def drain_attachments(self, entries) -> int:
        from tools.network.fleet_sync.blob_transport import drain_backlog

        conn, catalog = self._open()
        try:
            return drain_backlog(catalog, entries)
        finally:
            conn.close()

    def blob_store_root(self):
        from tools.network.fleet_sync.materialize import production_blob_store

        return production_blob_store(self.path)

    def record_served_ack(
        self, machine_pub: str, epoch: str, acked_transaction_ref: int
    ) -> None:
        conn, catalog = self._open()
        try:
            catalog.record_served_ack(
                machine_pub, epoch, acked_transaction_ref
            )
        finally:
            conn.close()

    def prune_acknowledged(
        self, machine_pubs: Sequence[str], epoch: str
    ) -> tuple[int, int]:
        conn, catalog = self._open()
        try:
            return catalog.prune_acknowledged(machine_pubs, epoch)
        finally:
            conn.close()

    def record_peer(
        self,
        machine_pub: str,
        epoch: str,
        *,
        online: bool,
        bytes_sent: int = 0,
        bytes_received: int = 0,
        checkpoints_received: int = 0,
        deltas_received: int = 0,
        transactions_applied: int = 0,
        acknowledgements: int = 0,
        retries: int = 0,
        peer_watermark: int | None = None,
        error: str | None = None,
        success: bool = False,
    ) -> None:
        conn, _catalog = self._open()
        now = time.time_ns()
        try:
            conn.execute(
                "INSERT INTO fleet_sync_peer_state("
                "machine_public_key,roster_epoch,online,last_success_ns,"
                "peer_watermark,bytes_sent,bytes_received,checkpoints_received,"
                "deltas_received,transactions_applied,acknowledgements,retries,"
                "last_error_code,updated_at_ns) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(machine_public_key,roster_epoch) DO UPDATE SET "
                "online=excluded.online,"
                "last_success_ns=CASE WHEN ? THEN excluded.updated_at_ns "
                "ELSE fleet_sync_peer_state.last_success_ns END,"
                "peer_watermark=CASE "
                "WHEN excluded.peer_watermark IS NULL THEN "
                "fleet_sync_peer_state.peer_watermark "
                "WHEN fleet_sync_peer_state.peer_watermark IS NULL OR "
                "excluded.peer_watermark>fleet_sync_peer_state.peer_watermark "
                "THEN excluded.peer_watermark "
                "ELSE fleet_sync_peer_state.peer_watermark END,"
                "bytes_sent=fleet_sync_peer_state.bytes_sent+excluded.bytes_sent,"
                "bytes_received=fleet_sync_peer_state.bytes_received+excluded.bytes_received,"
                "checkpoints_received=fleet_sync_peer_state.checkpoints_received+"
                "excluded.checkpoints_received,"
                "deltas_received=fleet_sync_peer_state.deltas_received+"
                "excluded.deltas_received,"
                "transactions_applied=fleet_sync_peer_state.transactions_applied+"
                "excluded.transactions_applied,"
                "acknowledgements=fleet_sync_peer_state.acknowledgements+"
                "excluded.acknowledgements,"
                "retries=fleet_sync_peer_state.retries+excluded.retries,"
                "last_error_code=excluded.last_error_code,"
                "updated_at_ns=excluded.updated_at_ns",
                (
                    machine_pub, epoch, int(online), now if success else None,
                    peer_watermark, bytes_sent, bytes_received,
                    checkpoints_received, deltas_received, transactions_applied,
                    acknowledgements, retries, error, now, int(success),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def _transaction_identity(item: AuthoredMutation) -> tuple[str, str, int]:
    return (
        item.origin_incarnation,
        item.transaction_id,
        item.mutation.timestamp_ns,
    )


class FleetSyncScheduler:
    """One direct listener plus bounded, roster-filtered outbound pulls."""

    def __init__(self, config: FleetSyncRuntimeConfig):
        self.config = config
        self._roster_snapshot: tuple[RosterEntry, ...] = ()
        self.authenticator = FleetAuthenticator(
            config.machine_key,
            root_pub=config.personal_root_pub,
            roster_entries=lambda: self._roster_snapshot,
            roster_machine_pub=config.roster_machine_pub,
            delegation_cert=config.delegation_cert,
            require_delegation=config.require_delegation,
        )
        self.store = SQLiteFleetSyncStore(config.personal_db_path)
        self.server = FleetDirectServer(
            self.authenticator,
            self._handle,
            host=config.listen_host,
            port=config.listen_port,
        )
        self._task: asyncio.Task | None = None
        self._roster_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._failures: dict[str, int] = {}
        self._next_attempt: dict[str, float] = {}
        self._activated_scopes: set[Path] = set()

    @property
    def port(self) -> int:
        return self.server.port

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._roster_snapshot = await asyncio.to_thread(
            lambda: tuple(self.config.roster_entries())
        )
        self.authenticator.authorize(self.authenticator.machine_pub)
        await self.server.start()
        self._stopping.clear()
        self._roster_task = asyncio.create_task(
            self._refresh_roster(), name="fleet-sync-roster"
        )
        self._task = asyncio.create_task(self._run(), name="fleet-sync-scheduler")

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        roster_task = self._roster_task
        self._task = None
        self._roster_task = None
        tasks = [candidate for candidate in (task, roster_task) if candidate]
        for candidate in tasks:
            candidate.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.server.stop()

    def _current_epoch(self) -> str:
        return roster_epoch(
            self._roster_snapshot, self.config.personal_root_pub
        )

    def _scope_paths(self) -> dict[str, Path]:
        """Synchronized databases by scope slug, personal always first."""
        scopes: dict[str, Path] = {
            "personal": Path(self.config.personal_db_path)
        }
        if self.config.sync_scopes is not None:
            for slug, path in self.config.sync_scopes().items():
                if slug != "personal":
                    scopes[str(slug)] = Path(path)
        return scopes

    def _store_for(self, scope: str) -> SQLiteFleetSyncStore:
        """The scope's store, with its fleet writers activated once.

        Organization databases share the graph schema, so the same policy
        audit applies; activation fails closed on any unpoliced table.
        """
        paths = self._scope_paths()
        if scope not in paths:
            raise FleetSyncProtocolError(
                f"unknown fleet sync scope: {scope!r}"
            )
        path = paths[scope]
        if scope == "personal":
            return self.store
        if path not in self._activated_scopes:
            from tools.graph.db import GraphDB

            graph = GraphDB(path)
            try:
                graph.activate_fleet_sync_writers(
                    self.authenticator.machine_pub
                )
            finally:
                graph.close()
            self._activated_scopes.add(path)
        return SQLiteFleetSyncStore(path)

    async def _refresh_roster(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.config.poll_interval
                )
                continue
            except asyncio.TimeoutError:
                pass
            try:
                snapshot = await asyncio.to_thread(
                    lambda: tuple(self.config.roster_entries())
                )
                # Resolve before publishing. A broken provider never replaces
                # the last verified authorization snapshot.
                resolve(snapshot, anchor_root_pub=self.config.personal_root_pub)
                self._roster_snapshot = snapshot
            except Exception:
                logger.warning("fleet roster refresh failed", exc_info=True)

    async def _handle(
        self,
        _token: str,
        message: bytes,
        peer_pub: str,
        *,
        telemetry_channel: str = "direct",
        telemetry_mode: str = "delta",
        telemetry_stats: dict[str, int] | None = None,
        telemetry_started_at_ns: int | None = None,
        telemetry_started_monotonic_ns: int | None = None,
        allow_checkpoint: bool = True,
    ):
        from tools.network.fleet_sync.blob_transport import peek_request_op

        if peek_request_op(message) == "blob":
            return self._blob_response(message, peer_pub)
        _requested_epoch, resume_trail, peer_digest, scope, bootstrap = (
            decode_pull_request(message)
        )
        store = await asyncio.to_thread(self._store_for, scope)
        epoch = self._current_epoch()
        record_here = telemetry_stats is None
        stats = telemetry_stats if telemetry_stats is not None else {}
        stats.setdefault("bytes_sent", 0)
        stats.setdefault("bytes_received", len(message))
        stats.setdefault("mutation_frames", 0)
        stats.setdefault("transactions", 0)
        stats.setdefault("checkpoint_bytes", 0)
        started_at_ns = telemetry_started_at_ns or time.time_ns()
        started_monotonic_ns = (
            telemetry_started_monotonic_ns or time.monotonic_ns()
        )

        async def response():
            digest = hashlib.sha256()
            count = 0
            outcome = "failed"
            error_code = "stream_incomplete"
            try:
                # Frames are only intelligible between machines that agree on
                # the replicated surface. Refuse a mixed-schema pull with a
                # typed frame the puller records as a pause, and let the
                # ordinary poll loop resume once the lagging machine's own
                # software applies the same migration locally.
                local_digest = await asyncio.to_thread(
                    store.compatibility_digest
                )
                if peer_digest != local_digest:
                    refusal = encode_schema_refusal(digest=local_digest)
                    stats["bytes_sent"] += len(refusal)
                    error_code = "schema_mismatch"
                    yield refusal
                    return
                # The presented trail names transactions, never local row
                # ids; the position is recomputed here so a database restored
                # from backup re-serves its divergence window instead of
                # honouring a cursor into journal rows that no longer exist.
                cursor = 0
                if resume_trail:
                    cursor = await asyncio.to_thread(
                        store.resume_ref, resume_trail
                    )
                journal_gap = await asyncio.to_thread(store.journal_gap)
                # An empty server has nothing a checkpoint delivers; two
                # freshly prepared machines must meet through (empty) deltas,
                # not by installing each other's blank databases.
                server_has_content = await asyncio.to_thread(store.has_state)
                if allow_checkpoint and server_has_content and (
                    serve_checkpoint_decision(cursor, bootstrap, journal_gap)
                ):
                    import shutil as _shutil
                    import tempfile as _tempfile

                    from tools.network.fleet_sync.sync import FleetSyncAlpha

                    scope_path = self._scope_paths()[scope]
                    active = tuple(sorted(resolve(
                        self._roster_snapshot,
                        anchor_root_pub=self.config.personal_root_pub,
                    )))
                    stage = Path(_tempfile.mkdtemp(
                        prefix="fleet-direct-checkpoint-"
                    ))
                    built = stage / "checkpoint"
                    try:
                        def build() -> None:
                            with FleetSyncAlpha(
                                scope_path, self.authenticator.machine_pub
                            ) as alpha:
                                alpha.checkpoint(
                                    built,
                                    roster_epoch=epoch,
                                    active_roster=active,
                                )

                        await asyncio.to_thread(build)
                        files = tuple(sorted(
                            path for path in built.rglob("*")
                            if path.is_file()
                        ))
                        total = sum(path.stat().st_size for path in files)
                        begin = canonical_json({
                            "v": FLEET_SYNC_PROTOCOL_VERSION,
                            "kind": "checkpoint.begin",
                            "file_count": len(files),
                            "total_bytes": total,
                            "source_machine_pub":
                                self.authenticator.machine_pub,
                            "roster_epoch": epoch,
                        })
                        stats["bytes_sent"] += len(begin)
                        yield begin
                        for path in files:
                            self.authenticator.authorize(peer_pub)
                            encoded = encode_checkpoint_file(
                                path.relative_to(built).as_posix(),
                                await asyncio.to_thread(path.read_bytes),
                            )
                            stats["bytes_sent"] += len(encoded)
                            stats["checkpoint_bytes"] += len(encoded)
                            yield encoded
                        end = canonical_json({
                            "v": FLEET_SYNC_PROTOCOL_VERSION,
                            "kind": "checkpoint.end",
                            "file_count": len(files),
                            "total_bytes": total,
                        })
                        stats["bytes_sent"] += len(end)
                        yield end
                    finally:
                        _shutil.rmtree(stage, ignore_errors=True)
                # A resolvable trail is the peer's durable acknowledgement of
                # this journal's prefix through that transaction. Record it
                # before serving; the fleet-wide floor of these
                # acknowledgements is what authorizes journal pruning.
                if cursor > 0:
                    try:
                        await asyncio.to_thread(
                            store.record_served_ack,
                            peer_pub, epoch, cursor,
                        )
                    except Exception:
                        logger.warning(
                            "fleet sync served-ack record failed",
                            exc_info=True,
                        )
                while True:
                    self.authenticator.authorize(peer_pub)
                    page = await asyncio.to_thread(
                        store.next_transaction, cursor
                    )
                    if page is None:
                        break
                    cursor, items = page
                    stats["transactions"] += 1
                    operation_count = len(items)
                    for item in items:
                        self.authenticator.authorize(peer_pub)
                        encoded = encode_authored(
                            item, transaction_operations=operation_count
                        )
                        _digest_add(digest, encoded)
                        count += 1
                        stats["mutation_frames"] += 1
                        stats["bytes_sent"] += len(encoded)
                        yield encoded
                self.authenticator.authorize(peer_pub)
                through_breadcrumb = None
                if cursor:
                    through_breadcrumb = await asyncio.to_thread(
                        store.breadcrumb, cursor
                    )
                done = encode_done(
                    epoch=epoch,
                    count=count,
                    digest=digest.hexdigest(),
                    through_transaction_ref=cursor,
                    through_breadcrumb=through_breadcrumb,
                )
                stats["bytes_sent"] += len(done)
                yield done
                outcome = "success"
                error_code = ""
                # Retire journal frames every active peer has acknowledged.
                # Best-effort maintenance: a prune failure never fails the
                # serve, but it is logged rather than swallowed. A solo
                # roster prunes nothing (acknowledged_journal_floor returns
                # None for an empty peer list), so a concurrently enrolling
                # machine can never race a full retirement.
                try:
                    active = resolve(
                        self._roster_snapshot,
                        anchor_root_pub=self.config.personal_root_pub,
                    )
                    others = [
                        pub for pub in active
                        if pub != self.authenticator.machine_pub
                    ]
                    journal_rows, transaction_rows = await asyncio.to_thread(
                        store.prune_acknowledged, others, epoch
                    )
                    if journal_rows or transaction_rows:
                        logger.info(
                            "fleet sync journal pruned: %d frames, "
                            "%d transactions",
                            journal_rows, transaction_rows,
                        )
                except Exception:
                    logger.warning(
                        "fleet sync journal prune failed", exc_info=True
                    )
            except asyncio.CancelledError:
                outcome = "cancelled"
                error_code = ""
                raise
            except Exception as exc:
                error_code = type(exc).__name__
                raise
            finally:
                recorder = self.config.telemetry_recorder
                if record_here and recorder is not None:
                    duration_ms = max(
                        0,
                        (time.monotonic_ns() - started_monotonic_ns) // 1_000_000,
                    )
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(
                            recorder,
                            peer_pub,
                            channel=telemetry_channel,
                            direction="serve",
                            mode=telemetry_mode,
                            outcome=outcome,
                            started_at_ns=started_at_ns,
                            duration_ms=duration_ms,
                            error_code=error_code,
                            scope=scope,
                            **stats,
                        )

        return response()

    def _blob_response(self, message: bytes, peer_pub: str):
        """Serve requested attachment objects in bounded chunk frames."""
        from tools.network.fleet_sync.blob_transport import (
            decode_blob_request,
            iter_blob_frames,
        )

        digests = decode_blob_request(message)
        # Digests are self-certifying, so every synchronized scope's store
        # and attachment rows are legitimate candidates regardless of which
        # scope's backlog asked.
        db_paths = list(self._scope_paths().values())

        async def response():
            frames = iter_blob_frames(db_paths, digests)
            while True:
                self.authenticator.authorize(peer_pub)
                frame = await asyncio.to_thread(next, frames, None)
                if frame is None:
                    return
                yield frame

        return response()

    async def _install_direct_checkpoint(
        self, stage: Path, scope: str, source_machine_pub: str, epoch: str
    ) -> None:
        """Quiesce the scope database and publish a received checkpoint.

        The scheduler's own store connections are short-lived, so between
        stream messages nothing of ours holds the database; any OTHER live
        production handle makes the quiescence gate refuse, the pull fails,
        and the ordinary backoff retries.
        """
        from tools.graph.db import GraphDB
        from tools.network.fleet_checkpoint_handoff import (
            install_quiesced_checkpoint,
        )
        from tools.network.fleet_sync_connection import (
            acquire_database_quiescence,
        )

        scope_path = self._scope_paths()[scope]
        active = tuple(sorted(resolve(
            self._roster_snapshot,
            anchor_root_pub=self.config.personal_root_pub,
        )))

        def install() -> None:
            GraphDB.close_pooled_path(scope_path)
            token = acquire_database_quiescence(scope_path)
            try:
                install_quiesced_checkpoint(
                    stage,
                    scope_path,
                    quiescence=token,
                    target_origin_incarnation=(
                        self.config.roster_machine_pub
                        or self.config.machine_key.public_hex
                    ),
                    expected_roster_epoch=epoch,
                    expected_active_roster=active,
                    source_machine_pub=source_machine_pub,
                )
            finally:
                token.release()

        await asyncio.to_thread(install)
        _emit_settings_materialized(gap=True)

    async def _drain_attachment_backlog(
        self, machine_pub: str, addresses: Sequence[str],
        scope: str = "personal",
    ) -> None:
        """Best-effort post-pull drain of the attachment byte backlog."""
        from tools.network.fleet_sync.blob_transport import (
            BlobReceiver,
            encode_blob_request,
            MAX_BLOB_REQUEST_DIGESTS,
        )

        scope_store = await asyncio.to_thread(self._store_for, scope)
        entries = await asyncio.to_thread(scope_store.attachment_backlog)
        if not entries:
            return
        digests = sorted({entry.digest for entry in entries})[
            :MAX_BLOB_REQUEST_DIGESTS
        ]
        store = await asyncio.to_thread(scope_store.blob_store_root)
        receiver = BlobReceiver(store)
        channel = None
        try:
            last_error: Exception | None = None
            for address in addresses:
                try:
                    channel = await fleet_direct_connect(
                        address,
                        authenticator=self.authenticator,
                        expected_machine_pub=machine_pub,
                        session=new_session_id(),
                        timeout=self.config.connect_timeout,
                    )
                    break
                except Exception as exc:
                    last_error = exc
            if channel is None:
                assert last_error is not None
                raise last_error
            await channel.send_message(encode_blob_request(digests))
            stream = channel.recv_message_stream().__aiter__()
            while not receiver.done:
                try:
                    frame, _final = await asyncio.wait_for(
                        stream.__anext__(), PULL_STREAM_SILENCE_LIMIT_S
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError as exc:
                    raise FleetSyncProtocolError(
                        "fleet blob stream went silent"
                    ) from exc
                self.authenticator.authorize(machine_pub)
                await asyncio.to_thread(receiver.feed, frame)
            if not receiver.done:
                raise FleetSyncProtocolError(
                    "blob stream ended without terminal frame"
                )
            cleared = await asyncio.to_thread(
                scope_store.drain_attachments, entries
            )
            logger.info(
                "fleet attachment drain: %d adopted, %d cleared, %d missing",
                len(receiver.adopted), cleared, len(receiver.missing),
            )
        finally:
            receiver.close()

    async def _run(self) -> None:
        while not self._stopping.is_set():
            entries = self._roster_snapshot
            active = resolve(entries, anchor_root_pub=self.config.personal_root_pub)
            addresses = self.config.peer_addresses()
            now = asyncio.get_running_loop().time()
            eligible = [
                machine_pub
                for machine_pub in sorted(active)
                if machine_pub != self.authenticator.machine_pub
                and addresses.get(machine_pub)
                and now >= self._next_attempt.get(machine_pub, 0.0)
            ]
            selected = eligible
            if eligible:
                try:
                    weights = await asyncio.to_thread(
                        self.store.peer_last_success
                    )
                except Exception:
                    weights = {}
                selected = rank_peers(
                    eligible, weights,
                    limit=self.config.max_concurrent_pulls,
                )
                self._last_round_selection = tuple(selected)
            if selected:
                await asyncio.gather(
                    *(self._sync_peer(
                        machine_pub,
                        tuple(addresses.get(machine_pub, ())),
                    ) for machine_pub in selected),
                    return_exceptions=True,
                )
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.config.poll_interval
                )
            except asyncio.TimeoutError:
                pass

    async def _sync_peer(self, machine_pub: str, addresses: Sequence[str]) -> None:
        """Pull every synchronized scope from one peer, personal first.

        A schema mismatch pauses only its own scope: the typed refusal is
        recorded and the remaining scopes still sync. Any other failure is
        transport-level and backs off the whole peer.
        """
        for scope in self._scope_paths():
            try:
                await self._pull_scope(machine_pub, addresses, scope)
            except FleetSyncSchemaMismatch:
                logger.info(
                    "fleet sync scope %r paused on schema mismatch", scope
                )
                continue
            except Exception:
                return

    async def _pull_scope(
        self, machine_pub: str, addresses: Sequence[str], scope: str
    ) -> None:
        store = await asyncio.to_thread(self._store_for, scope)
        epoch = self._current_epoch()
        channel = None
        sent = 0
        received = 0
        mutation_frames = 0
        transactions = 0
        started_at_ns = time.time_ns()
        started_monotonic_ns = time.monotonic_ns()
        peer_watermark: int | None = None

        async def record(
            outcome: str,
            error_code: str = "",
            acknowledged_transaction_ref: int | None = None,
            acknowledged_breadcrumb: tuple[str, str, int] | None = None,
        ) -> None:
            recorder = self.config.telemetry_recorder
            if recorder is None:
                return
            duration_ms = max(
                0, (time.monotonic_ns() - started_monotonic_ns) // 1_000_000
            )
            values = {
                "channel": "direct",
                "direction": "pull",
                "mode": "delta",
                "scope": scope,
                "outcome": outcome,
                "started_at_ns": started_at_ns,
                "duration_ms": duration_ms,
                "bytes_sent": sent,
                "bytes_received": received,
                "mutation_frames": mutation_frames,
                "transactions": transactions,
                "error_code": error_code,
            }
            if acknowledged_transaction_ref is not None:
                values["acknowledged_transaction_ref"] = (
                    acknowledged_transaction_ref
                )
            if acknowledged_breadcrumb is not None:
                values["acknowledged_breadcrumb"] = (
                    encode_breadcrumb(acknowledged_breadcrumb)
                )
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    recorder,
                    machine_pub,
                    **values,
                )
        try:
            last_error: Exception | None = None
            for address in addresses:
                try:
                    channel = await fleet_direct_connect(
                        address,
                        authenticator=self.authenticator,
                        expected_machine_pub=machine_pub,
                        session=new_session_id(),
                        timeout=self.config.connect_timeout,
                    )
                    break
                except Exception as exc:
                    last_error = exc
            if channel is None:
                assert last_error is not None
                raise last_error

            await asyncio.to_thread(
                store.record_peer, machine_pub, epoch, online=True
            )
            resume_trail: Sequence[tuple[str, str, int]] = ()
            if self.config.resume_cursor is not None:
                resume_trail = tuple(await asyncio.to_thread(
                    self.config.resume_cursor, machine_pub, scope
                ))
            local_digest = await asyncio.to_thread(
                store.compatibility_digest
            )
            bootstrap = not await asyncio.to_thread(store.has_state)
            request = encode_pull_request(
                epoch, compat=local_digest, resume=resume_trail, scope=scope,
                bootstrap=bootstrap,
            )
            sent += len(request)
            await channel.send_message(request)

            digest = hashlib.sha256()
            message_count = 0
            checkpoint_stage: Path | None = None
            checkpoint_expected: tuple[int, int] | None = None
            checkpoint_seen = [0, 0]
            installed_checkpoint = False
            pending: list[AuthoredMutation] = []
            pending_identity = None
            pending_count: int | None = None
            saw_done = False
            through_transaction_ref = 0
            through_breadcrumb: tuple[str, str, int] | None = None

            async def apply_pending(items: list[AuthoredMutation]) -> None:
                nonlocal peer_watermark, transactions
                won, _ignored = await asyncio.to_thread(store.apply, items)
                if won == len(items):
                    _emit_settings_materialized(items)
                elif won:
                    # The catalog currently returns counts rather than the
                    # winning subset.  Never mislabel a losing address as
                    # changed: one coalesced gap asks consumers to re-resolve
                    # their durable truth.
                    _emit_settings_materialized(gap=True)
                transactions += 1
                peer_watermark = max(
                    item.mutation.timestamp_ns for item in items
                )
                if won:
                    await asyncio.to_thread(
                        store.record_peer,
                        machine_pub,
                        epoch,
                        online=True,
                        transactions_applied=1,
                        peer_watermark=peer_watermark,
                    )

            def validate_pending() -> None:
                if not pending:
                    return
                if pending_count != len(pending) or len({
                    item.operation_index for item in pending
                }) != len(pending):
                    raise FleetSyncProtocolError(
                        "fleet transaction is incomplete or out of order"
                    )

            stream = channel.recv_message_stream().__aiter__()
            while True:
                try:
                    message, stream_final = await asyncio.wait_for(
                        stream.__anext__(), PULL_STREAM_SILENCE_LIMIT_S
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError as exc:
                    raise FleetSyncProtocolError(
                        "fleet pull stream went silent"
                    ) from exc
                # A kick that lands after the hello revokes this live session
                # before another application message is accepted.
                self.authenticator.authorize(machine_pub)
                received += len(message)
                if message.startswith(_REFUSAL_MAGIC):
                    decode_schema_refusal(message)
                    raise FleetSyncSchemaMismatch(
                        "peer replicated schema differs; synchronization "
                        "pauses until this machine applies the same migration"
                    )
                if message.startswith(_DONE_MAGIC):
                    (
                        remote_epoch,
                        expected_count,
                        expected_digest,
                        through_transaction_ref,
                        through_breadcrumb,
                    ) = decode_done(message)
                    if not stream_final:
                        raise FleetSyncProtocolError("fleet summary is not final")
                    if pending:
                        validate_pending()
                        await apply_pending(pending)
                        pending = []
                    if message_count != expected_count:
                        raise FleetSyncProtocolError("fleet message count mismatch")
                    if digest.hexdigest() != expected_digest:
                        raise FleetSyncProtocolError("fleet stream digest mismatch")
                    # A differing epoch is observable state, not authority:
                    # each accepted key already passed the receiver's roster.
                    _ = remote_epoch
                    saw_done = True
                    break
                if stream_final:
                    raise FleetSyncProtocolError("fleet mutation ended the stream")
                if message.startswith(FILE_MAGIC):
                    if checkpoint_stage is None:
                        raise FleetSyncProtocolError(
                            "checkpoint file arrived before its header"
                        )
                    relative, body = decode_checkpoint_file(message)
                    target_file = checkpoint_stage / relative
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    target_file.write_bytes(body)
                    checkpoint_seen[0] += 1
                    checkpoint_seen[1] += len(body)
                    continue
                if message.startswith(b"{"):
                    control = _json_loose(message)
                    kind = control.get("kind")
                    if kind == "checkpoint.begin":
                        if checkpoint_stage is not None:
                            raise FleetSyncProtocolError(
                                "nested checkpoint stream"
                            )
                        import tempfile as _tempfile

                        checkpoint_stage = Path(_tempfile.mkdtemp(
                            prefix="fleet-direct-received-"
                        ))
                        checkpoint_expected = (
                            int(control["file_count"]),
                            int(control["total_bytes"]),
                        )
                        continue
                    if kind == "checkpoint.end":
                        if (
                            checkpoint_stage is None
                            or checkpoint_expected is None
                            or tuple(checkpoint_seen) != checkpoint_expected
                        ):
                            raise FleetSyncProtocolError(
                                "checkpoint stream is incomplete"
                            )
                        await self._install_direct_checkpoint(
                            checkpoint_stage, scope, machine_pub, epoch
                        )
                        installed_checkpoint = True
                        await asyncio.to_thread(
                            store.record_peer,
                            machine_pub,
                            epoch,
                            online=True,
                            checkpoints_received=1,
                        )
                        continue
                    raise FleetSyncProtocolError(
                        "unknown fleet stream control frame"
                    )
                item, operation_count = decode_authored(message)
                identity = _transaction_identity(item)
                if pending_identity is not None and identity != pending_identity:
                    validate_pending()
                    await apply_pending(pending)
                    pending = []
                pending_identity = identity
                if pending and pending_count != operation_count:
                    raise FleetSyncProtocolError(
                        "fleet transaction operation count changed"
                    )
                pending_count = operation_count
                pending.append(item)
                _digest_add(digest, message)
                message_count += 1
                mutation_frames += 1
            if not saw_done:
                raise FleetSyncProtocolError("fleet stream ended without summary")

            await asyncio.to_thread(
                store.record_peer,
                machine_pub,
                epoch,
                online=False,
                bytes_sent=sent,
                bytes_received=received,
                deltas_received=1,
                acknowledgements=1,
                peer_watermark=peer_watermark,
                success=True,
            )
            self._failures.pop(machine_pub, None)
            self._next_attempt.pop(machine_pub, None)
            await record(
                "success",
                acknowledged_transaction_ref=through_transaction_ref,
                acknowledged_breadcrumb=through_breadcrumb,
            )
            # Best-effort: a drain failure never fails the pull that
            # preceded it, but it is logged rather than swallowed.
            try:
                await self._drain_attachment_backlog(
                    machine_pub, addresses, scope
                )
            except Exception:
                logger.warning(
                    "fleet attachment drain failed", exc_info=True
                )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    store.record_peer,
                    machine_pub,
                    epoch,
                    online=False,
                    bytes_sent=sent,
                    bytes_received=received,
                )
            await record("cancelled")
            raise
        except Exception as exc:
            failures = self._failures.get(machine_pub, 0) + 1
            self._failures[machine_pub] = failures
            delay = min(
                self.config.max_backoff,
                self.config.min_backoff * (2 ** min(failures - 1, 16)),
            )
            self._next_attempt[machine_pub] = (
                asyncio.get_running_loop().time() + delay
            )
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    store.record_peer,
                    machine_pub,
                    epoch,
                    online=False,
                    bytes_sent=sent,
                    bytes_received=received,
                    retries=1,
                    error=type(exc).__name__,
                )
            await record("failed", type(exc).__name__)
            logger.warning(
                "fleet sync peer %s scope %r failed (%s)",
                machine_pub[:12],
                scope,
                type(exc).__name__,
            )
            # Re-raise so the scope loop distinguishes a per-scope schema
            # pause (continue with the other scopes) from a transport
            # failure (back off the whole peer). Recording and backoff
            # already happened above.
            raise
        finally:
            if checkpoint_stage is not None:
                import shutil as _shutil

                _shutil.rmtree(checkpoint_stage, ignore_errors=True)
            if channel is not None:
                await channel.close()


class DashboardFleetSyncService:
    """Lifecycle-owned scheduler that remains healthy before unlock/config."""

    def __init__(self):
        self._config: FleetSyncRuntimeConfig | None = None
        self._scheduler: FleetSyncScheduler | None = None
        self._task: asyncio.Task | None = None
        self._changed = asyncio.Event()
        self._stopping = False
        self._transition = asyncio.Lock()

    @property
    def scheduler(self) -> FleetSyncScheduler | None:
        return self._scheduler

    def configure(self, config: FleetSyncRuntimeConfig | None) -> None:
        self._config = config
        self._changed.set()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(
            self._run(), name="dashboard-fleet-sync-service"
        )

    async def stop(self) -> None:
        self._stopping = True
        self._changed.set()
        task = self._task
        self._task = None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def install_checkpoint(
        self, checkpoint_directory: Path, *, source_machine_pub: str
    ):
        """Pause this runtime, publish a received base, then resume deltas.

        Closing the scheduler and pooled GraphDB handle is the cooperative
        quiescence path. Any other live production store handle causes the
        process-wide gate to refuse publication instead of swapping beneath
        an unknown writer.
        """
        config = self._config
        if config is None:
            raise RuntimeError("fleet sync runtime is not configured")
        from tools.graph.db import GraphDB
        from tools.network.fleet_checkpoint_handoff import (
            install_quiesced_checkpoint,
        )
        from tools.network.fleet_sync_connection import (
            acquire_database_quiescence,
        )

        async with self._transition:
            if self._scheduler is not None:
                await self._scheduler.stop()
                self._scheduler = None
            token = None
            installed = None
            epoch = None
            try:
                await asyncio.to_thread(
                    GraphDB.close_pooled_path, config.personal_db_path
                )
                token = await asyncio.to_thread(
                    acquire_database_quiescence, config.personal_db_path
                )
                entries = await asyncio.to_thread(
                    lambda: tuple(config.roster_entries())
                )
                active = tuple(sorted(resolve(
                    entries, anchor_root_pub=config.personal_root_pub
                )))
                if (
                    source_machine_pub not in active
                    or source_machine_pub == (
                        config.roster_machine_pub or config.machine_key.public_hex
                    )
                ):
                    raise RuntimeError(
                        "checkpoint source is not an active remote fleet machine"
                    )
                epoch = roster_epoch(entries, config.personal_root_pub)
                installed = await asyncio.to_thread(
                    install_quiesced_checkpoint,
                    checkpoint_directory,
                    config.personal_db_path,
                    quiescence=token,
                    target_origin_incarnation=(
                        config.roster_machine_pub or config.machine_key.public_hex
                    ),
                    expected_roster_epoch=epoch,
                    expected_active_roster=active,
                    source_machine_pub=source_machine_pub,
                )
            finally:
                if token is not None:
                    token.release()
                if not self._stopping and self._config is config:
                    self._scheduler = FleetSyncScheduler(config)
                    await self._scheduler.start()
            assert installed is not None and epoch is not None
            # A checkpoint replaces the personal database as one published
            # snapshot.  Enumerating every changed address would be both
            # expensive and misleading, so receivers get one bounded gap hint
            # and reconcile durable truth through their own Settings reader.
            _emit_settings_materialized(gap=True)
            return installed

    async def _run(self) -> None:
        current: FleetSyncRuntimeConfig | None = None
        while not self._stopping:
            self._changed.clear()
            desired = self._config
            if desired is not current:
                async with self._transition:
                    if self._scheduler is not None:
                        await self._scheduler.stop()
                        self._scheduler = None
                    current = desired
                    if desired is not None:
                        self._scheduler = FleetSyncScheduler(desired)
                        await self._scheduler.start()
            await self._changed.wait()
        async with self._transition:
            if self._scheduler is not None:
                await self._scheduler.stop()
                self._scheduler = None


dashboard_fleet_sync_service = DashboardFleetSyncService()


def configure_dashboard_fleet_sync(config: FleetSyncRuntimeConfig | None) -> None:
    """Install/clear unlocked runtime material without persisting key bytes."""
    dashboard_fleet_sync_service.configure(config)
