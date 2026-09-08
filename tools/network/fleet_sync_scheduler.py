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
import os
import random
import logging
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Mapping, Sequence

from tools.network.fleet_roster import RosterEntry, resolve
from tools.network.fleet_sync_channel import (
    FleetAuthenticator,
    FleetDirectServer,
    fleet_direct_connect,
)
from tools.network.fleet_sync_connection import (
    FleetSyncConnection,
    FleetSyncQuiescenceError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tools.network.fleet_org_channel import OrgFleetAuthenticator
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

FLEET_SYNC_PROTOCOL_VERSION = 4
#: The server answers in the requester's declared version, so a v3 puller
#: against a v4 server syncs unchanged. v3 repeats the full transaction
#: header on every operation; v4 sends one transaction-header frame followed
#: by bare operation frames.
SUPPORTED_PROTOCOL_VERSIONS = frozenset({3, 4})
_REQUEST_FIELDS = frozenset({
    "v", "op", "roster_epoch", "resume", "compat",
})
_REQUEST_OPTIONAL_FIELDS = frozenset({"scope", "bootstrap", "accept_checkpoint", "watermarks"})
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
_TRANSACTION_MAGIC = b"FSTX"
_OPERATION_MAGIC = b"FSO1"
_HEADER_LIMIT = 4096
_SETTINGS_HINT_LIMIT = 256
#: A resume trail carries the last few verified stream positions plus an
#: exponentially thinned history, so its length is logarithmic in stream age.
MAX_RESUME_BREADCRUMBS = 64


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


class FleetSyncStreamSilence(FleetSyncProtocolError):
    """The peer's stream went silent past the decided liveness bound.

    This is the application-level liveness policy (auto-fzy8s), scoped to
    the one class the transport cannot see: a serve wedged while its event
    loop stays alive and keeps answering websocket pongs. Dead and frozen
    peers are already handled beneath it — the direct channel's pinned
    ping/pong breaks the socket and unblocks the receive in
    ``ping_interval + ping_timeout + close_timeout`` (measured 50.0s), so
    the inter-frame bound sits just above that. The exception type is the
    telemetry error code, so a wedged peer is named on the money line
    instead of sitting invisibly at ``online=1`` with zero deltas.
    """


class FleetSyncFoundedLedgerRefusal(FleetSyncProtocolError):
    """The peer offered a checkpoint to a store that holds a founded ledger.

    A checkpoint never carries the ledger, so installing it would be refused
    (sync.py install guard); refusing at the offer saves the transfer. This
    machine is an origin of authority for that scope and syncs by delta only.
    """


class FleetSyncFirstFrameSilence(FleetSyncStreamSilence):
    """No first frame within the pull's checkpoint-build allowance.

    The one legitimately long silence in the protocol: a checkpoint serve
    sends nothing between the pull request and ``checkpoint.begin`` for
    the whole build (~60s/GB measured). The allowance is sized for that
    phase; every later gap is a single bounded DB query or file read and
    gets the much tighter inter-frame bound.
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
    #: Peers pulled per round (stalest-first ranking fills the slots; zero
    #: or negative means every eligible peer). ONE by default (auto-mfgko):
    #: with per-origin watermarks every server can supply every origin's
    #: writes, so a second concurrent pull in the same round mostly carries
    #: the same new transactions -- measured 1.65x receipts/minimum at 3
    #: per round versus 1.00x at 1 per round (N=5, 2026-09-07). Rounds are
    #: 1-10 s apart and starvation-free, so every peer is still reached.
    max_concurrent_pulls: int = 1
    #: Stream liveness policy (auto-fzy8s). The first frame of a pull may
    #: lag for an entire server-side checkpoint build (~60s/GB measured),
    #: so it gets its own allowance: 900s covers a ~15GB database, an
    #: order of magnitude above today's production size. Every later gap
    #: is structurally one DB query or one <=4MB file read; 60s is
    #: generous under load and sits just above the transport keepalive's
    #: measured 50s recovery, so a dead transport still surfaces as the
    #: more diagnostic ConnectionClosed rather than a silence timeout.
    pull_first_frame_allowance_s: float = 900.0
    pull_stream_silence_limit_s: float = 60.0
    #: Org channels (auto-coea3, graph://c2baad48-0a3): scope slug -> this
    #: machine's OrgFleetAuthenticator for that organization (its persona
    #: certificate, its membership proof, its adopted checkpoints). With
    #: one, the scope can be pulled from and served to a co-member's machine
    #: through the org hello; without one the scope still syncs inside the
    #: personal fleet exactly as before. Resolved fresh per round.
    org_channels: Callable[
        [], Mapping[str, "OrgFleetAuthenticator"]
    ] | None = None
    #: Co-member machines outside the personal roster, as scope slug ->
    #: {machine_pub: direct address candidates}. Discovery fills this
    #: (auto-mldvv); the harness injects it. A machine that is also in the
    #: personal roster is pulled on the personal path and skipped here.
    org_peer_addresses: Callable[
        [], Mapping[str, Mapping[str, Sequence[str]]]
    ] | None = None
    #: This machine's dialable direct addresses (fleet_direct_config
    #: advertise_addrs). With org channels, the scheduler publishes them
    #: as this machine's row in autonomy.org.fleet-reachability#1 of every
    #: org scope it has a channel for -- only when the set changes, never
    #: as a heartbeat (auto-mldvv) -- and reads co-members' rows from the
    #: same set as their addresses. None: publish nothing.
    advertised_addresses: Callable[[], Sequence[str]] | None = None
    #: Called with a peer's machine key when a pull of it failed at the
    #: transport (no candidate connected, stream cut). The dashboard wires
    #: the reachability cache's note_failed so that peer is looked up again
    #: before the next full interval (auto-8dw0w); None: nothing.
    on_peer_failure: Callable[[str], None] | None = None


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


def materialize_org_scopes_from_roster() -> list[str]:
    """Create the local ``orgs/<slug>.db`` stub for every org in the SYNCED
    org roster that this machine does not yet have, so ``discover_org_sync_scopes``
    finds it and the org-DB sync fills it.

    This is the receiver-side bootstrap that closes the org chicken-and-egg: a
    fresh fleet member learns its orgs from the ``autonomy.fleet.org-roster``
    record (carried by the personal-scope sync), not from whichever DB files
    happen to exist locally. The stub is created with the roster's recorded
    ``org_id`` so it is the SAME org as home's, not merely the same slug.
    Idempotent: an org whose DB already exists is left untouched, and one
    malformed entry never aborts the rest. Returns the slugs newly created.
    """
    from tools.graph.db import GraphDB, _org_db_path
    from tools.network import fleet_org_roster

    try:
        roster = fleet_org_roster.current_orgs()
    except Exception:
        return []
    if not roster:
        return []
    orgs_dir = Path(_org_db_path("personal")).parent / "orgs"
    created: list[str] = []
    for slug, entry in sorted(roster.items()):
        if not slug or slug == "personal" or ":" in slug:
            continue
        path = orgs_dir / f"{slug}.db"
        if path.exists():
            continue
        try:
            GraphDB.create_org_db(
                slug, type_="shared", org_id=entry.org_id, path=path,
            ).close()
            created.append(slug)
        except FileExistsError:
            # Raced with another round or a concurrent create — the file is
            # there now, which is all we needed.
            continue
        except Exception:
            # A single bad entry (unwritable path, bad id) must not stop the
            # rest of the fleet's orgs from bootstrapping.
            continue
    return created


#: With a random source, selection draws from the stalest pool of this many
#: times the limit (at least 3), so fairness holds while lockstep breaks.
RANK_POOL_FACTOR = 3


def rank_peers(
    candidates: Sequence[str],
    last_success_ns: Mapping[str, int],
    *,
    limit: int,
    rng=None,
) -> list[str]:
    """Stalest-first bounded peer selection for one sync round.

    A peer with no recorded success ever ranks ahead of every peer with
    one, so a machine returning from a long absence — or never yet synced
    — fills the first slot on its first eligible round. Among recorded
    successes, oldest first. Starvation-free by construction: an
    unselected peer's staleness only grows, monotonically raising its rank
    until selected.

    Without ``rng`` ties break on the key and rounds are deterministic
    (tests). With ``rng`` (production: each scheduler's own Random) the
    pick is uniform among the RANK_POOL_FACTOR x limit stalest candidates.
    The deterministic tie-break made every machine of a fresh fleet rank
    the SAME peer first and rotate in lockstep: one hot server per round
    (19 pullers on one machine at N=20, 2026-09-07) and no gossip fan-out,
    so a write needed ~N rounds to spread instead of ~log N. Pull gossip
    on a complete graph converges in about log2(N) + ln(N) rounds only when
    each machine picks independently at random.
    """
    if rng is None:
        ordered = sorted(
            candidates,
            key=lambda pub: (last_success_ns.get(pub) or 0, pub),
        )
        if limit <= 0:
            return ordered
        return ordered[:limit]
    ordered = sorted(
        candidates,
        key=lambda pub: (last_success_ns.get(pub) or 0, rng.random()),
    )
    if limit <= 0:
        return ordered
    # Measured 2026-09-07 (N=50, poll 1 s, one pull per round, this box):
    # whole-roster uniform random 16.8 s settle vs 15.6 s for this pool.
    # Selection width is not a settle lever; the pool stays narrow for
    # the fairness it gives a returning peer.
    pool = ordered[:max(RANK_POOL_FACTOR * limit, 3)]
    if len(pool) <= limit:
        return pool
    return rng.sample(pool, limit)


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
    if value["v"] not in SUPPORTED_PROTOCOL_VERSIONS:
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


def encode_schema_refusal(
    *, digest: str, version: int = FLEET_SYNC_PROTOCOL_VERSION,
    built_at: str | None = None,
) -> bytes:
    body = {
        "v": version,
        "kind": "schema-refused",
        "digest": _require_hex64(digest, "schema refusal digest"),
    }
    # built_at is the serving side's build timestamp — informational, so the
    # puller can tell the operator WHICH build the incompatible peer runs, not
    # just that a hash differs. Optional: an older peer omits it and the digest
    # comparison (the authoritative compatibility key) is unchanged.
    if built_at is not None:
        body["built_at"] = str(built_at)
    return _REFUSAL_MAGIC + canonical_json(body)


def decode_schema_refusal(raw: bytes) -> tuple[str, str | None]:
    """Return ``(digest, built_at)`` — built_at is None from an older peer."""
    if not raw.startswith(_REFUSAL_MAGIC):
        raise FleetSyncProtocolError("fleet sync refusal has wrong magic")
    try:
        value = json.loads(raw[len(_REFUSAL_MAGIC):])
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise FleetSyncProtocolError("fleet sync refusal is not valid JSON") from exc
    if (
        not isinstance(value, dict)
        or not _REFUSAL_FIELDS <= set(value)
        or not set(value) <= (_REFUSAL_FIELDS | {"built_at"})
        or value["v"] not in SUPPORTED_PROTOCOL_VERSIONS
        or value["kind"] != "schema-refused"
    ):
        raise FleetSyncProtocolError("fleet sync refusal is malformed")
    built_at = value.get("built_at")
    if built_at is not None and not isinstance(built_at, str):
        raise FleetSyncProtocolError("fleet sync refusal built_at is malformed")
    return _require_hex64(value["digest"], "fleet sync refusal digest"), built_at


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


def watermarks_from_trail(
    resume_trail: Sequence[tuple[str, str, int]] | None,
) -> dict[str, int]:
    """Per-origin watermarks a breadcrumb trail proves: the newest
    timestamp the peer verified from each origin named in the trail."""
    out: dict[str, int] = {}
    for origin, _transaction_id, timestamp_ns in resume_trail or ():
        try:
            ts = int(timestamp_ns)
        except (TypeError, ValueError):
            continue
        if ts > out.get(str(origin), 0):
            out[str(origin)] = ts
    return out


def serve_checkpoint_decision(
    resume_position: int, requested: bool, journal_gap: bool = False
) -> bool:
    """A snapshot goes ONLY to a machine that declares it holds no sync
    state (``requested`` = the puller's bootstrap flag) and whose trail
    resolves to nothing. Every other unresolved puller is served the
    retained journal from its oldest surviving frame.

    ``journal_gap`` is accepted for call compatibility and ignored. It used
    to select a checkpoint ("replay would omit retired history"), which is
    true of every store that has ever pruned or installed -- so any first
    contact between two established machines, and any restore, re-based a
    live database (design of record graph://1155b8f4-8cf: a checkpoint is a
    bulk-transfer optimization, never the answer to an unknown position;
    measured 2026-09-06: 1,365 snapshots for 103 writes at N=50). Under the
    served-ack pruning invariant no active peer is ever behind the retained
    floor, so the omitted prefix is content the puller already holds.
    """
    return resume_position == 0 and requested


def encode_pull_request(
    epoch: str,
    *,
    compat: str,
    resume: Sequence[tuple[str, str, int]] = (),
    scope: str = "personal",
    bootstrap: bool = False,
    version: int = FLEET_SYNC_PROTOCOL_VERSION,
    accept_checkpoint: bool = True,
    watermarks: Mapping[str, int] | None = None,
) -> bytes:
    """Ask for every journal transaction after a content-addressed position.

    ``watermarks`` (design of record graph://1155b8f4-8cf) is the puller's
    per-origin map ``{author_pub: max timestamp_ns held}``. A server that
    receives it serves, per origin, only transactions newer than the
    puller's watermark for that origin and never the puller's own writes --
    so each write crosses the wire to each machine once, whichever server
    it comes from. The trail stays for servers that predate the field.

    ``accept_checkpoint=False`` (sent only when False, so the historical
    request bytes are unchanged) tells the server this store holds a
    founded ledger and will refuse any checkpoint: when the trail does not
    resolve, replay the retained journal from its start instead. That is
    how an ORIGIN receives a member's writes -- the member's retained
    journal is everything it originated or received since its own install,
    and deterministic merge makes rows the origin already holds inert.

    ``resume`` is the puller's verified breadcrumb trail, newest first.  It
    names transactions, never the serving database's private row numbers,
    so the server recomputes the position from its own journal on every
    pull and a server restored from backup is re-served from the newest
    breadcrumb it still holds.
    """
    if len(resume) > MAX_RESUME_BREADCRUMBS:
        raise FleetSyncProtocolError("fleet sync resume trail exceeds bound")
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise FleetSyncProtocolError("fleet sync request version is unsupported")
    body = {
        "v": version,
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
    if not accept_checkpoint:
        body["accept_checkpoint"] = False
    if watermarks is not None:
        if len(watermarks) > MAX_WATERMARK_ORIGINS:
            raise FleetSyncProtocolError("fleet sync watermark map exceeds bound")
        body["watermarks"] = {
            _require_hex64(origin, "fleet sync watermark origin"): int(value)
            for origin, value in sorted(watermarks.items())
        }
    return canonical_json(body)


#: Bound on the per-origin map: a roster, not the world.
MAX_WATERMARK_ORIGINS = 4096


def decode_pull_request(
    raw: bytes,
) -> tuple[str, tuple[tuple[str, str, int], ...], str, str, bool, int, bool, dict[str, int] | None]:
    """-> (epoch, resume trail, compat, scope, bootstrap, version, accept_checkpoint, watermarks)."""
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
    if value["v"] not in SUPPORTED_PROTOCOL_VERSIONS:
        raise FleetSyncProtocolError("unsupported fleet sync request version")
    version = int(value["v"])
    scope = value.get("scope", "personal")
    if not isinstance(scope, str) or not scope or ":" in scope:
        raise FleetSyncProtocolError("fleet sync scope is malformed")
    bootstrap = value.get("bootstrap", False)
    if not isinstance(bootstrap, bool):
        raise FleetSyncProtocolError("fleet sync bootstrap flag must be bool")
    accept_checkpoint = value.get("accept_checkpoint", True)
    if not isinstance(accept_checkpoint, bool):
        raise FleetSyncProtocolError("fleet sync accept_checkpoint flag must be bool")
    watermarks = value.get("watermarks")
    if watermarks is not None:
        if (
            not isinstance(watermarks, dict)
            or len(watermarks) > MAX_WATERMARK_ORIGINS
            or any(
                not isinstance(k, str) or len(k) != 64
                or isinstance(v, bool) or not isinstance(v, int) or v < 0
                for k, v in watermarks.items()
            )
        ):
            raise FleetSyncProtocolError("fleet sync watermark map is malformed")
        watermarks = {str(k): int(v) for k, v in watermarks.items()}
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
    ), compat, scope, bootstrap, version, accept_checkpoint, watermarks


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
            "originated mutation exceeds the fleet channel message bound"
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


def encode_transaction_header(
    origin: str, transaction_id: str, operations: int
) -> bytes:
    """Protocol v4: one header frame opens a transaction group.

    The origin, transaction id, and operation count are sent once here
    instead of being repeated inside every operation frame — the wire
    wrapper changes; mutation frame bytes, candidate hashes, and journal
    storage are untouched.
    """
    header = canonical_json({
        "origin": origin,
        "transaction": transaction_id,
        "operations": operations,
    })
    if len(header) > _HEADER_LIMIT:
        raise FleetSyncProtocolError(
            "fleet transaction header exceeds the channel bound"
        )
    return _TRANSACTION_MAGIC + header


def decode_transaction_header(raw: bytes) -> tuple[str, str, int]:
    if not raw.startswith(_TRANSACTION_MAGIC):
        raise FleetSyncProtocolError("fleet transaction header has wrong magic")
    try:
        header = json.loads(raw[len(_TRANSACTION_MAGIC):])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FleetSyncProtocolError(
            "fleet transaction header is not valid JSON"
        ) from exc
    if not isinstance(header, dict) or set(header) != {
        "origin", "transaction", "operations"
    }:
        raise FleetSyncProtocolError("fleet transaction header has wrong fields")
    origin = header["origin"]
    transaction = header["transaction"]
    operations = header["operations"]
    if (
        not isinstance(origin, str)
        or len(origin) != 64
        or any(ch not in "0123456789abcdef" for ch in origin)
    ):
        raise FleetSyncProtocolError("fleet transaction origin is malformed")
    if not isinstance(transaction, str) or not transaction:
        raise FleetSyncProtocolError("fleet transaction id is malformed")
    if (
        not isinstance(operations, int)
        or isinstance(operations, bool)
        or not 1 <= operations <= MAX_TRANSACTION_OPERATIONS
    ):
        raise FleetSyncProtocolError(
            "fleet transaction operation count is malformed"
        )
    return origin, transaction, operations


def encode_operation_frame(item: AuthoredMutation) -> bytes:
    """Protocol v4: a bare operation — index plus canonical mutation bytes."""
    frame = encode_mutation_frame(item.mutation)
    message = (
        _OPERATION_MAGIC
        + struct.pack(">I", item.operation_index)
        + frame
    )
    if len(message) > MAX_MESSAGE_SIZE:
        raise FleetSyncProtocolError(
            "originated mutation exceeds the fleet channel message bound"
        )
    return message


def decode_operation_frame(raw: bytes):
    if len(raw) < 9 or not raw.startswith(_OPERATION_MAGIC):
        raise FleetSyncProtocolError("fleet operation frame has wrong magic")
    operation = struct.unpack(">I", raw[4:8])[0]
    try:
        mutation = decode_mutation_frame(raw[8:])
    except Exception as exc:
        raise FleetSyncProtocolError("fleet mutation frame is malformed") from exc
    return operation, mutation


def encode_done(
    *,
    epoch: str,
    count: int,
    digest: str,
    through_transaction_ref: int = 0,
    through_breadcrumb: tuple[str, str, int] | None = None,
    version: int = FLEET_SYNC_PROTOCOL_VERSION,
) -> bytes:
    if (
        isinstance(through_transaction_ref, bool)
        or not isinstance(through_transaction_ref, int)
        or through_transaction_ref < 0
    ):
        raise FleetSyncProtocolError("through_transaction_ref is malformed")
    return _DONE_MAGIC + canonical_json(
        {
            "v": version,
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


async def bounded_stream_frames(
    channel, *, first_allowance_s: float, silence_limit_s: float
):
    """Yield ``recv_message_stream`` items, bounding each silent wait.

    The timeout measures exactly the await for the peer's next frame —
    the consumer's own work between frames (applying a transaction,
    writing a checkpoint file) never counts against the peer. A first
    silence past the bound raises :class:`FleetSyncFirstFrameSilence`
    (the checkpoint-build allowance); any later one raises
    :class:`FleetSyncStreamSilence`. On timeout the stream is abandoned,
    never resumed — the caller tears the channel down.

    One transport nuance: the serving channel holds a one-item
    final-boundary lookahead (``_serve_channel_records``), so delivery
    lags production by one frame. A serve wedged after producing exactly
    one frame is therefore observed here as FIRST-frame silence and gets
    the larger allowance — still bounded, just at the other constant.
    """
    stream = channel.recv_message_stream().__aiter__()
    allowance = first_allowance_s
    first = True
    while True:
        try:
            item = await asyncio.wait_for(stream.__anext__(), allowance)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            if first:
                raise FleetSyncFirstFrameSilence(
                    "peer served no frame within the "
                    f"{allowance:.0f}s first-frame allowance"
                ) from None
            raise FleetSyncStreamSilence(
                "peer stream went silent past the "
                f"{allowance:.0f}s inter-frame bound"
            ) from None
        first = False
        allowance = silence_limit_s
        yield item


class SQLiteFleetSyncStore:
    """Short-connection access to one fixed activated personal database."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _open(self) -> tuple[FleetSyncConnection, MutationCatalog]:
        import sqlite3
        from tools.network.fleet_sync.streaming import register_streaming_functions

        # 30 s busy wait: several pulls (and the local writer) share one
        # file; the 5 s default surfaced as OperationalError at N=50.
        conn = sqlite3.connect(
            self.path, factory=FleetSyncConnection, timeout=30.0,
        )
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
        self, after_transaction_ref: int = 0,
    ) -> tuple[int, list[AuthoredMutation]] | None:
        """Drain helper: the next learned transaction after a local row id."""
        conn, catalog = self._open()
        try:
            return catalog.next_transaction_after_ref(after_transaction_ref)
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

    def origin_watermarks(self) -> dict[str, int]:
        conn, catalog = self._open()
        try:
            return catalog.origin_watermarks()
        finally:
            conn.close()

    def origin_watermarks_through(self, through_ref: int) -> dict[str, int]:
        conn, catalog = self._open()
        try:
            return catalog.origin_watermarks(through_ref=through_ref)
        finally:
            conn.close()

    def origin_list(self) -> list[str]:
        conn, catalog = self._open()
        try:
            return catalog.origin_list()
        finally:
            conn.close()

    def next_transaction_for_origin(self, incarnation, after_timestamp_ns, after_transaction_id=None):
        conn, catalog = self._open()
        try:
            return catalog.next_transaction_for_origin(
                incarnation, after_timestamp_ns, after_transaction_id
            )
        finally:
            conn.close()

    def next_transaction_heads_for_origin(self, incarnation, after_timestamp_ns,
                                          after_transaction_id=None, *, limit=200):
        conn, catalog = self._open()
        try:
            return catalog.next_transaction_heads_for_origin(
                incarnation, after_timestamp_ns, after_transaction_id, limit=limit,
            )
        finally:
            conn.close()

    def transaction_group(self, transaction_ref, incarnation, transaction_id,
                          *, offset: int, limit: int):
        """One bounded slice of a transaction's items on a fresh connection;
        returns ``(items, more)``."""
        conn, catalog = self._open()
        try:
            return catalog.transaction_group(
                transaction_ref, incarnation, transaction_id,
                offset=offset, limit=limit,
            )
        finally:
            conn.close()

    def next_transactions_for_origin(self, incarnation, after_timestamp_ns,
                                     after_transaction_id=None, *, limit=200):
        conn, catalog = self._open()
        try:
            return catalog.next_transactions_for_origin(
                incarnation, after_timestamp_ns, after_transaction_id, limit=limit
            )
        finally:
            conn.close()

    def implied_ack_ref(self, watermarks) -> int:
        conn, catalog = self._open()
        try:
            return catalog.implied_ack_ref(watermarks)
        finally:
            conn.close()

    def newest_transaction_ref(self) -> int:
        conn, catalog = self._open()
        try:
            return catalog.newest_transaction_ref()
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

    def apply_many(
        self, groups: list[list[AuthoredMutation]]
    ) -> list[tuple[int, int]]:
        """Apply several complete transactions on ONE connection.

        Opening a connection (schema audit, streaming functions, blob store)
        per transaction was the pull's dominant cost: ~4 transactions per
        second per machine under load (auto-89b7q), so pulls ran 10-60 s and
        overlapped, and every overlapping pull carried the same new writes.
        Each group still commits on its own (apply_remote_batch's
        BEGIN IMMEDIATE), so a failure mid-batch leaves earlier groups
        applied and the later ones unapplied, exactly as before.
        """
        conn, catalog = self._open()
        try:
            results = [catalog.apply_remote_batch(items) for items in groups]
            # Fold what was just applied from the WAL into the main file
            # while this connection is still open. PASSIVE never blocks a
            # reader or writer; it does as much as it can. Without it the
            # WAL grows until the last connection closes (a busy server
            # rarely has none open: personal.db-wal pinned at 102 MB, live
            # 2026-09-06), and anything reading the main file alone sees
            # the applied rows only then.
            with contextlib.suppress(Exception):
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            return results
        finally:
            conn.close()

    def record_transactions(self, entries) -> int:
        """Record transactions the server reported empty (nothing of them
        survives there), so our watermark for their origin moves past."""
        conn, catalog = self._open()
        try:
            return catalog.record_transactions(list(entries))
        finally:
            conn.close()

    def peer_ack_rows(self, epoch: str) -> list[tuple[str, int | None, int]]:
        """``(machine, local_watermark, updated_at_ns)`` for every peer-state
        row under *epoch*: which machines have acknowledged this store's
        prefix (a completed pull) and when each was last seen."""
        import sqlite3 as _sqlite3

        if not Path(self.path).exists():
            return []
        conn = _sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            return [
                (str(row[0]), None if row[1] is None else int(row[1]), int(row[2] or 0))
                for row in conn.execute(
                    "SELECT machine_public_key,local_watermark,updated_at_ns "
                    "FROM fleet_sync_peer_state WHERE roster_epoch=?", (epoch,),
                )
            ]
        except _sqlite3.Error:
            return []
        finally:
            conn.close()

    def peer_machines(self, epoch: str) -> list[str]:
        """Machines with a peer-state row under *epoch* (the org scope's
        state key: every co-member machine that has pulled or been served
        here), for the served-ack prune's acknowledgement set."""
        import sqlite3 as _sqlite3

        if not Path(self.path).exists():
            return []
        conn = _sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            return sorted(
                str(row[0]) for row in conn.execute(
                    "SELECT DISTINCT machine_public_key FROM fleet_sync_peer_state "
                    "WHERE roster_epoch=?", (epoch,),
                )
            )
        except _sqlite3.Error:
            return []
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
        """Any applied or originated sync state at all, receipts included.

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

    def attachment_backlog(self):
        from tools.network.fleet_sync.blob_transport import (
            pending_attachment_backlog,
        )

        conn, _catalog = self._open()
        try:
            return pending_attachment_backlog(conn)
        finally:
            conn.close()

    def drain_pending_signatures(self) -> int:
        conn, catalog = self._open()
        try:
            return catalog.drain_pending_signatures()
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
        """Retention maintenance that yields to contention: the prune is
        ~30 ms of query work on a quiescent copy of a 73k-transaction store
        (anchore, measured 2026-09-07), and 150-170 s on the live file when
        another writer holds the store's write lock. Waiting the store's
        30 s busy timeout per statement is wrong for a background sweep;
        it waits at most PRUNE_BUSY_TIMEOUT_MS and otherwise skips this
        pass, naming the store so the long writer can be found."""
        import sqlite3 as _sqlite3

        conn, catalog = self._open()
        try:
            conn.execute(f"PRAGMA busy_timeout={int(PRUNE_BUSY_TIMEOUT_MS)}")
            return catalog.prune_acknowledged(machine_pubs, epoch)
        except _sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            logger.warning(
                "fleet sync: served-ack prune skipped, %s is write-locked by "
                "another writer for more than %.1fs (%s)",
                Path(self.path).name, PRUNE_BUSY_TIMEOUT_MS / 1000.0, exc,
            )
            return (0, 0)
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
        peer_built_at: str | None = None,
    ) -> None:
        conn, _catalog = self._open()
        now = time.time_ns()
        try:
            conn.execute(
                "INSERT INTO fleet_sync_peer_state("
                "machine_public_key,roster_epoch,online,last_success_ns,"
                "peer_watermark,bytes_sent,bytes_received,checkpoints_received,"
                "deltas_received,transactions_applied,acknowledgements,retries,"
                "last_error_code,peer_built_at,updated_at_ns) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(machine_public_key,roster_epoch) DO UPDATE SET "
                "online=excluded.online,"
                # A pull that isn't a schema refusal carries no peer build, so
                # keep the last one we learned rather than nulling it.
                "peer_built_at=CASE WHEN excluded.peer_built_at IS NULL THEN "
                "fleet_sync_peer_state.peer_built_at "
                "ELSE excluded.peer_built_at END,"
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
                    acknowledgements, retries, error, peer_built_at, now,
                    int(success),
                ),
            )
            conn.commit()
        finally:
            conn.close()


#: Minimum wait before re-pulling from a peer after a pull that received a
#: full checkpoint and still failed (mirrors the relay redelivery guard).
CHECKPOINT_FAILURE_BACKOFF_S = 600.0

#: Harness forensics: when the per-pull ledger is on, each pull's record
#: lists the transactions it received.
_PULL_TRACE = bool(os.environ.get("AUTONOMY_HARNESS_PULL_LOG"))

#: Batched apply bounds (auto-t43kz): complete transactions queue until one
#: of these trips, then apply on a single store connection.
#: Most operations in one wire transaction group. A transaction with more
#: surviving rows is served as several groups under the same (origin,
#: transaction id): the puller applies each group as it arrives, so a large
#: transaction never needs to be built in full before its first frame (the
#: 60 s stream-silence bound) and never exceeds the receiver's per-group
#: operation bound (MAX_TRANSACTION_OPERATIONS).
SERVE_GROUP_OPERATIONS = 2_000

#: A serve phase (heads page, transaction slice) slower than this is logged
#: with what it was doing: the evidence a stalled stream needs.
SLOW_SERVE_PHASE_S = 2.0

#: Longest a pull holds received, unapplied transactions before committing
#: them: progress survives a round that is cut after this many seconds.
APPLY_FLUSH_INTERVAL_S = 5.0
APPLY_BATCH_TRANSACTIONS = 200
APPLY_BATCH_OPERATIONS = 5_000

#: While a direct serve is silent (a checkpoint build sends nothing for
#: minutes), the observer is touched this often so the supervisor's
#: activity window sees a LIVE stream, not a stuck counter.
DIRECT_STREAM_HEARTBEAT_S = 10.0


async def _observe_stream(stream, observer):
    """Yield *stream*'s frames while reporting begin/touch/end to *observer*.

    ``touch`` fires on every frame AND every DIRECT_STREAM_HEARTBEAT_S of
    silence while the inner stream is still working (the build phase), so
    activity age stays fresh for as long as the serve is genuinely alive.
    Consumer cancellation (the peer disconnected) cancels the pending inner
    step and closes the inner generator; ``end`` always fires exactly once.
    """
    observer.begin()
    iterator = stream.__aiter__()
    pending = None
    try:
        while True:
            pending = asyncio.ensure_future(iterator.__anext__())
            while True:
                try:
                    frame = await asyncio.wait_for(
                        asyncio.shield(pending), DIRECT_STREAM_HEARTBEAT_S
                    )
                    break
                except asyncio.TimeoutError:
                    observer.touch()
                except StopAsyncIteration:
                    pending = None
                    return
            pending = None
            observer.touch()
            yield frame
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(BaseException):
                await pending
        with contextlib.suppress(BaseException):
            await stream.aclose()
        observer.end()


async def _install_personal_handoff(installer, stage: Path, source_machine_pub: str) -> None:
    """Run the service installer for a direct-path personal checkpoint and
    always clean the staged files; a failure is logged, never raised into
    the loop (the next pull, after the backoff, tells the truth again)."""
    import shutil as _shutil

    try:
        await installer(stage, source_machine_pub=source_machine_pub)
        logger.info(
            "fleet sync: direct-path personal checkpoint from %s installed",
            source_machine_pub[:12],
        )
    except Exception:
        logger.warning(
            "fleet sync: direct-path personal checkpoint from %s failed to install",
            source_machine_pub[:12], exc_info=True,
        )
    finally:
        _shutil.rmtree(stage, ignore_errors=True)


def _transaction_identity(item: AuthoredMutation) -> tuple[str, str, int]:
    return (
        item.origin_incarnation,
        item.transaction_id,
        item.mutation.timestamp_ns,
    )


#: Transactions fetched per store connection while serving a pull.
SERVE_PAGE_TRANSACTIONS = 200

#: Least time between two served-ack prunes of one scope on this machine.
#: The prune holds the store's write lock for up to its budget; once a
#: minute is plenty for retention and invisible to the dashboard's writers.
PRUNE_MIN_INTERVAL_S = 60.0
#: Ruling on auto-coea3 (membership half, 2026-09-07): membership grants the
#: right to pull, not the power to freeze another member's compaction. A
#: still-member machine that has not completed a pull of an org scope for
#: this long stops holding that store's served-ack retirement; its row is
#: kept, so its own next pull resumes from its watermark (a checkpoint if
#: the rows are gone).
ORG_PRUNE_ABSENCE_S = 7 * 24 * 3600.0

#: How long the served-ack prune waits for a store's write lock before it
#: skips the pass (a background sweep must never queue behind a long writer).
PRUNE_BUSY_TIMEOUT_MS = 2_000


class _OriginPager:
    """Per-origin paging: for each origin other than the puller, every
    retained transaction with timestamp_ns above the puller's watermark for
    that origin, in (timestamp_ns, transaction_id) order. Each call opens a
    short-lived store connection, as the legacy pager does."""

    def __init__(self, store, watermarks, exclude_origin: str | None,
                 skip_origins: Sequence[str] = ()):
        self.store = store
        self.watermarks = dict(watermarks)
        self.exclude = exclude_origin
        self.skip = set(skip_origins)
        self._authors: list[str] | None = None
        self._index = 0
        self._position: tuple[int, str | None] | None = None
        self.newest_ref = 0
        self._buffer: list = []

    def next(self):
        if self._authors is None:
            # The puller's own origin is served like any other: normally
            # the server holds nothing above the puller's own watermark
            # (zero rows, zero cost), but a machine restored from a backup
            # has lost its own newest writes and its own-origin watermark
            # says so. Excluding it left scenario (e) unrecoverable
            # (test_restored_backup_server_reconverges, 2026-09-07).
            self._authors = [
                origin for origin in self.store.origin_list()
                if origin not in self.skip
            ]
        while self._index < len(self._authors):
            origin = self._authors[self._index]
            if self._buffer:
                ref, timestamp, transaction_id = self._buffer.pop(0)
                self._position = (timestamp, transaction_id)
                self.newest_ref = max(self.newest_ref, ref)
                return ref, (origin, transaction_id, timestamp)
            if self._position is None:
                self._position = (int(self.watermarks.get(origin, 0)), None)
            timestamp, transaction_id = self._position
            self._buffer = self.store.next_transaction_heads_for_origin(
                origin, timestamp, transaction_id, limit=SERVE_PAGE_TRANSACTIONS,
            )
            if not self._buffer:
                self._index += 1
                self._position = None
        return None


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
            org_channel_for=self._org_channel_for_genesis,
        )
        self._task: asyncio.Task | None = None
        self._roster_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._failures: dict[str, int] = {}
        self._next_attempt: dict[str, float] = {}
        self._activated_scopes: set[Path] = set()
        #: Per-peer declared pull version. A peer whose server rejected a v4
        #: request before serving any frame is retried at v3 for the rest of
        #: this process (v3 works against every server); a restart re-probes
        #: v4. Only wire efficiency rides on this, never correctness.
        self._peer_protocol: dict[str, int] = {}
        #: Per-machine random source for peer selection (see rank_peers).
        self._last_prune_at: dict[str, float] = {}
        self._after_serve_tasks: set = set()
        #: scope -> address-set fingerprint last published to the org's
        #: reachability set by this process (the row itself is compared
        #: too, so a restart with unchanged addresses writes nothing).
        self._published_reachability: dict[str, str] = {}
        self._rng = random.Random(int.from_bytes(os.urandom(8), "big"))
        #: ``async (stage_dir, *, source_machine_pub) -> installed`` for the
        #: PERSONAL scope, set by the owning DashboardFleetSyncService. The
        #: dashboard process holds live production handles on personal.db,
        #: so an inline quiesce always refuses there; the service's install
        #: pauses this scheduler, quiesces, installs, and resumes -- the same
        #: path the relay puller uses. None (connector, tests) installs inline.
        self.personal_checkpoint_installer = None
        #: Optional ``begin()/touch()/end()`` observer for DIRECT-path serves
        #: (the connector installs one). It is how a serving connector's
        #: supervisor learns a direct stream is live: relay streams already
        #: count in the connector's active_streams, direct ones did not, and
        #: the currency watchdog recycled a connector 61s into SJC's 2.27 GB
        #: direct autonomy build -- twice (2026-09-06 20:18Z).
        self.stream_observer = None

    @property
    def port(self) -> int:
        return self.server.port

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        await asyncio.to_thread(self._recover_interrupted_installs)
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

    def _scope_epochs(self, scope: str) -> tuple[str, str]:
        """``(wire epoch, peer-state key)`` for one scope.

        Personal, and an org scope with no org channel on this machine:
        both are the personal roster hash, as always. An org scope with an
        org channel (auto-coea3, design §2): the wire epoch is
        org_epoch(org, newest adopted seq) and the peer-state key is
        org_state_key(org) -- one key per (machine pair, org), unchanged
        by membership changes, the same on every one of this machine's
        pulls and serves of that scope whichever hello admitted the peer.
        """
        if scope != "personal":
            channel = self._org_channels().get(scope)
            if channel is not None:
                from tools.network.fleet_org_channel import org_epoch, org_state_key

                return (
                    org_epoch(channel.org, channel.newest_adopted_seq()),
                    org_state_key(channel.org),
                )
        epoch = self._current_epoch()
        return epoch, epoch

    def _recover_interrupted_installs(self) -> None:
        """Consume any crashed install's marker and backup before syncing.

        Recovery normally runs at the next install attempt, but a machine
        that crashed mid-install and thereafter syncs by deltas may never
        install again — leaving a stale marker and a full database backup
        on disk indefinitely. Startup is the natural recovery moment: no
        connections exist yet, and recover_checkpoint_handoff's own
        contract (inode-checked publish/restore/clean) decides the rest.
        """
        from tools.network.fleet_checkpoint_handoff import (
            _backup_path,
            _marker_path,
            recover_checkpoint_handoff,
        )
        from tools.network.fleet_sync_connection import (
    FleetSyncQuiescenceError,
            acquire_database_quiescence,
        )

        for scope, path in self._scope_paths().items():
            if not (_marker_path(path).exists() or _backup_path(path).exists()):
                continue
            try:
                token = acquire_database_quiescence(path)
                try:
                    outcome = recover_checkpoint_handoff(
                        path, quiescence=token
                    )
                finally:
                    token.release()
                logger.warning(
                    "fleet sync scope %r recovered interrupted install: %s",
                    scope, outcome,
                )
            except Exception:
                logger.warning(
                    "fleet sync scope %r startup install recovery failed",
                    scope, exc_info=True,
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

    # -- org channels (auto-coea3) -----------------------------------------

    def _org_channels(self) -> dict[str, "OrgFleetAuthenticator"]:
        """scope slug -> org hello authenticator, for the scopes that have
        one. A failing provider yields no channels rather than a crash: the
        personal path does not depend on it."""
        provider = self.config.org_channels
        if provider is None:
            return {}
        try:
            return dict(provider())
        except Exception:
            logger.warning("fleet sync: org channel provider failed", exc_info=True)
            return {}

    def _org_channel_for_genesis(self, org: str) -> "OrgFleetAuthenticator | None":
        """The org hello authenticator for an organization's genesis id (the
        listener's lookup for an incoming org hello), or None."""
        for channel in self._org_channels().values():
            if channel.org == org:
                return channel
        return None

    def _org_scopes_for(self, admitted_org: str) -> list[str]:
        """The scope slugs this machine syncs for *admitted_org*."""
        return [
            scope for scope, channel in self._org_channels().items()
            if channel.org == admitted_org and scope != "personal"
        ]

    def _confine_scope(self, scope: str, admitted_org: str | None) -> None:
        """A connection admitted by an org hello may request only that
        organization's scope: never the personal scope, never another
        organization's. A personal-admitted connection is unrestricted."""
        if admitted_org is None:
            return
        if scope not in self._org_scopes_for(admitted_org):
            raise FleetSyncProtocolError(
                f"scope {scope!r} is not the organization this connection "
                "was admitted to"
            )

    def _blob_paths(self, admitted_org: str | None) -> list[Path]:
        """Stores a blob request may be answered from: every synchronized
        scope for a personal-admitted peer (digests are self-certifying),
        only the admitted organization's scope(s) for an org-admitted one."""
        paths = self._scope_paths()
        if admitted_org is None:
            return list(paths.values())
        return [paths[s] for s in self._org_scopes_for(admitted_org) if s in paths]

    def _publish_org_reachability(self) -> None:
        """This machine's row in each org scope's reachability set, written
        only when its address set changed since the stored row. Skipped for
        a scope whose database is not the one the settings write path
        resolves for that slug (an explicitly pathed scope in a test or
        harness process without AUTONOMY_ORGS_DIR), so a row never lands in
        a store this scheduler does not sync."""
        provider = self.config.advertised_addresses
        if provider is None:
            return
        channels = self._org_channels()
        if not channels:
            return
        try:
            addresses = tuple(provider())
        except Exception:
            logger.warning("fleet sync: advertised address provider failed", exc_info=True)
            return
        from tools.graph.db import _org_db_path
        from tools.network.fleet_org_reachability import (
            digest_addresses, publish_if_changed,
        )

        fingerprint = digest_addresses(addresses)
        paths = self._scope_paths()
        for scope, channel in channels.items():
            if scope == "personal" or scope not in paths:
                continue
            if self._published_reachability.get(scope) == fingerprint:
                continue
            try:
                if Path(_org_db_path(scope)).resolve() != Path(paths[scope]).resolve():
                    logger.info(
                        "fleet sync scope %r: reachability row not published; the "
                        "scope's database is not the settings home for that slug",
                        scope,
                    )
                    self._published_reachability[scope] = fingerprint
                    continue
                written = publish_if_changed(
                    scope, self.config.machine_key, channel.persona_cert, addresses,
                )
            except Exception:
                logger.warning(
                    "fleet sync scope %r: reachability row publish failed",
                    scope, exc_info=True,
                )
                continue
            self._published_reachability[scope] = fingerprint
            if written:
                logger.info(
                    "fleet sync scope %r: published this machine's %d address(es) "
                    "to the org reachability set", scope, len(addresses),
                )

    def _org_peer_candidates(
        self, channels: Mapping[str, "OrgFleetAuthenticator"],
    ) -> dict[str, dict[str, tuple[str, ...]]]:
        """scope -> {machine_pub: addresses} of co-member machines: the
        verified reachability rows replicated into each org scope's own
        database, unioned with the org_peer_addresses hook (first contact;
        the harness). Hook addresses come first for a machine both name."""
        from tools.network.fleet_org_reachability import co_member_addresses

        merged: dict[str, dict[str, list[str]]] = {}
        provider = self.config.org_peer_addresses
        if provider is not None:
            try:
                for scope, peers in provider().items():
                    merged.setdefault(str(scope), {})
                    for machine_pub, addresses in peers.items():
                        merged[str(scope)][machine_pub] = list(addresses)
            except Exception:
                logger.warning(
                    "fleet sync: org peer address provider failed", exc_info=True
                )
        paths = self._scope_paths()
        for scope, channel in channels.items():
            if scope == "personal" or scope not in paths:
                continue
            try:
                rows = co_member_addresses(
                    paths[scope], org=channel.org,
                    own_machine_pub=self.authenticator.machine_pub,
                    is_member=channel.is_member,
                )
            except Exception:
                logger.warning(
                    "fleet sync scope %r: reachability rows unreadable",
                    scope, exc_info=True,
                )
                continue
            bucket = merged.setdefault(scope, {})
            for machine_pub, addresses in rows.items():
                known = bucket.get(machine_pub, [])
                bucket[machine_pub] = known + [a for a in addresses if a not in known]
            # Machines that dialled us and introduced themselves in their
            # org hello: sync is pull-only, so this is how the first-dialled
            # side learns where to pull back until the peer's row crosses.
            for machine_pub, addresses in channel.admitted_addresses().items():
                if machine_pub == self.authenticator.machine_pub:
                    continue
                known = bucket.get(machine_pub, [])
                bucket[machine_pub] = known + [a for a in addresses if a not in known]
        return {
            scope: {pub: tuple(addrs) for pub, addrs in peers.items() if addrs}
            for scope, peers in merged.items()
        }

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
                # This is THE roster change point for this process: a kick
                # must land on open streams now, not within the cache TTL.
                self.authenticator.invalidate_authorization_cache()
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
        resume_floor_ref: int | None = None,
        authorize: Callable[[str], None] | None = None,
        admitted_org: str | None = None,
    ):
        """``authorize`` / ``admitted_org``: set by the listener for a
        connection admitted by the ORG hello (fleet_org_channel): the
        per-message re-check is that authenticator's, and the connection is
        confined to the admitted organization's scope. Absent, the
        connection is a personal-roster one and behaves exactly as before.

        ``resume_floor_ref``: a caller that already served this peer a
        checkpoint passes the journal's newest transaction ref captured
        BEFORE that checkpoint's cut. The delta then starts there instead of
        at the peer's (empty, first-contact) trail — everything at or below
        the floor is inside the checkpoint by construction, and every later
        transaction is still replayed. Without it a first-contact pull sent
        the checkpoint AND the entire journal (~700k operations live
        2026-09-06), authorizing each one on the way."""
        from tools.network.fleet_sync.blob_transport import peek_request_op

        if authorize is None:
            authorize = self.authenticator.authorize
        if peek_request_op(message) == "blob":
            return self._blob_response(
                message, peer_pub, telemetry_channel,
                authorize=authorize, admitted_org=admitted_org,
            )
        (
            _requested_epoch, resume_trail, peer_digest, scope, bootstrap,
            protocol_version, accept_checkpoint, watermarks,
        ) = decode_pull_request(message)
        self._confine_scope(scope, admitted_org)
        store = await asyncio.to_thread(self._store_for, scope)
        epoch, state_epoch = self._scope_epochs(scope)
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
            deferred_after_done = False
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
                    from tools.network import build_version
                    refusal = encode_schema_refusal(
                        digest=local_digest, version=protocol_version,
                        built_at=build_version.disk_built_at(),
                    )
                    stats["bytes_sent"] += len(refusal)
                    error_code = "schema_mismatch"
                    yield refusal
                    return
                # The presented trail names transactions, never local row
                # ids; the position is recomputed here so a database restored
                # from backup re-serves its divergence window instead of
                # honouring a cursor into journal rows that no longer exist.
                # Local alias (assigning the parameter name inside this
                # generator would make it generator-local and unbound).
                # A request without a watermark map (older client, or a
                # trail-only resume): the trail names transactions the peer
                # verified, so its newest timestamp per origin is a
                # watermark the peer has earned. Origins absent from the
                # trail replay from 0; last-writer-wins makes that inert.
                origin_watermarks = (
                    dict(watermarks) if watermarks is not None
                    else watermarks_from_trail(resume_trail)
                )
                # The map proves the prefix it covers, which feeds the same
                # served-ack floor the trail did (may be 0).
                cursor = await asyncio.to_thread(
                    store.implied_ack_ref, origin_watermarks
                )
                # An empty server has nothing a checkpoint delivers; two
                # freshly prepared machines must meet through (empty) deltas,
                # not by installing each other's blank databases.
                server_has_content = await asyncio.to_thread(store.has_state)
                served_checkpoint = False
                # Local alias: assigning the parameter name inside this
                # generator would make it generator-local (unbound on the
                # no-checkpoint path).
                floor_ref = resume_floor_ref
                checkpoint_frontier: dict[str, int] = {}
                wants_checkpoint = serve_checkpoint_decision(cursor, bootstrap)
                if wants_checkpoint and not accept_checkpoint:
                    # A founded origin never installs a checkpoint; serve it
                    # deltas from its watermarks instead. Rows it already
                    # holds merge inert.
                    logger.warning(
                        "fleet sync peer %s scope %r refuses checkpoints "
                        "(founded origin); serving deltas from position %d",
                        peer_pub[:12], scope, cursor,
                    )
                    wants_checkpoint = False
                if allow_checkpoint and server_has_content and wants_checkpoint:
                    served_checkpoint = True
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
                        # Taken BEFORE the cut: a transaction landing between
                        # this read and the freeze has a higher ref and is
                        # replayed below — redundancy in that window, never
                        # a skip.
                        try:
                            floor_ref = await asyncio.to_thread(
                                store.newest_transaction_ref
                            )
                            # Same rule for the per-origin frontier the
                            # delta phase serves above: read before the
                            # cut, so anything that lands after this read
                            # is served as a delta (redundant inside the
                            # window, never skipped).
                            checkpoint_frontier = await asyncio.to_thread(
                                store.origin_watermarks
                            )
                        except WatermarkError:
                            floor_ref = None  # inactive store: full replay
                            checkpoint_frontier = {}

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
                            "v": protocol_version,
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
                            authorize(peer_pub)
                            encoded = encode_checkpoint_file(
                                path.relative_to(built).as_posix(),
                                await asyncio.to_thread(path.read_bytes),
                            )
                            stats["bytes_sent"] += len(encoded)
                            stats["checkpoint_bytes"] += len(encoded)
                            yield encoded
                        end = canonical_json({
                            "v": protocol_version,
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
                            peer_pub, state_epoch, cursor,
                        )
                    except Exception:
                        logger.warning(
                            "fleet sync served-ack record failed",
                            exc_info=True,
                        )
                # The ack above records only what the peer PROVED it holds
                # (its trail). The checkpoint floor is applied after it so a
                # transfer that dies mid-stream never advances the pruning
                # frontier past what the peer actually installed.
                if floor_ref is not None:
                    cursor = max(cursor, floor_ref)
                if served_checkpoint:
                    # The checkpoint carried everything through its cut;
                    # the delta phase serves above the frontier read just
                    # before that cut (checkpoint_frontier).
                    origin_watermarks = checkpoint_frontier
                elif floor_ref:
                    # The caller (relay path) served the checkpoint itself
                    # and names the position it was cut at: serve above
                    # the per-origin frontier that position carried.
                    carried = await asyncio.to_thread(
                        store.origin_watermarks_through, floor_ref
                    )
                    for origin_key, ts in carried.items():
                        if ts > origin_watermarks.get(origin_key, 0):
                            origin_watermarks[origin_key] = ts
                # Every origin is served from the puller's watermark, the
                # puller's own included (a machine restored from a backup
                # has lost its own newest writes). Frames are built from
                # catalog and live rows (catalog.transaction_items).
                pager = _OriginPager(store, origin_watermarks, None)
                slowest_phase = ("", 0.0, "")
                while True:
                    authorize(peer_pub)
                    phase_started = time.monotonic()
                    page = await asyncio.to_thread(pager.next)
                    phase_s = time.monotonic() - phase_started
                    if phase_s > slowest_phase[1]:
                        slowest_phase = ("heads", phase_s, "")
                    if phase_s >= SLOW_SERVE_PHASE_S:
                        logger.warning(
                            "fleet sync serve %s scope %r: fetching the next "
                            "transaction heads took %.1fs", peer_pub[:12],
                            scope, phase_s,
                        )
                    if page is None:
                        break
                    ref, header = page
                    cursor = max(cursor, ref)
                    stats["transactions"] += 1
                    origin_key, transaction_id, timestamp_ns = header
                    served_any = False
                    offset = 0
                    more = True
                    if True:
                        while more:
                            authorize(peer_pub)
                            phase_started = time.monotonic()
                            items, more = await asyncio.to_thread(
                                store.transaction_group, ref, origin_key,
                                transaction_id, offset=offset,
                                limit=SERVE_GROUP_OPERATIONS,
                            )
                            phase_s = time.monotonic() - phase_started
                            if phase_s > slowest_phase[1]:
                                slowest_phase = ("slice", phase_s, transaction_id)
                            if phase_s >= SLOW_SERVE_PHASE_S:
                                tables = sorted({i.mutation.table for i in items})
                                logger.warning(
                                    "fleet sync serve %s scope %r: building "
                                    "%d row(s) of transaction %s (offset %d, "
                                    "tables %s) took %.1fs", peer_pub[:12],
                                    scope, len(items), transaction_id, offset,
                                    ",".join(tables), phase_s,
                                )
                            offset += SERVE_GROUP_OPERATIONS
                            if not items:
                                continue
                            served_any = True
                            operation_count = len(items)
                            if protocol_version >= 4:
                                # v4: origin, transaction id and the group's
                                # operation count travel once in a header
                                # frame. A large transaction arrives as
                                # several groups under one transaction id;
                                # the receiver applies each as it lands.
                                opening = encode_transaction_header(
                                    origin_key, transaction_id, operation_count,
                                )
                                _digest_add(digest, opening)
                                stats["bytes_sent"] += len(opening)
                                yield opening
                            for item in items:
                                authorize(peer_pub)
                                encoded = (
                                    encode_operation_frame(item)
                                    if protocol_version >= 4
                                    else encode_authored(
                                        item, transaction_operations=operation_count
                                    )
                                )
                                _digest_add(digest, encoded)
                                count += 1
                                stats["mutation_frames"] += 1
                                stats["bytes_sent"] += len(encoded)
                                yield encoded
                    if not served_any and protocol_version >= 4:
                        # Nothing of this transaction survives here (every
                        # row it wrote was overwritten later). The puller
                        # still needs to advance its watermark past it, so
                        # it is named with its timestamp. Outside the
                        # digest and count, like the other control frames.
                        empty = canonical_json({
                            "v": protocol_version,
                            "kind": "transaction.empty",
                            "origin": origin_key,
                            "transaction_id": transaction_id,
                            "timestamp_ns": int(timestamp_ns),
                        })
                        stats["bytes_sent"] += len(empty)
                        yield empty
                authorize(peer_pub)
                if served_checkpoint:
                    newest = await asyncio.to_thread(
                        store.newest_transaction_ref
                    )
                    cursor = max(cursor, newest)
                through_breadcrumb = None
                if cursor:
                    through_breadcrumb = await asyncio.to_thread(
                        store.breadcrumb, cursor
                    )
                # WARNING on purpose: the serving connector's log level is
                # WARNING, and this one line per round is the evidence that
                # the serve reached its end (a puller reporting silence for
                # a round that has this line was not served slowly; it was
                # not delivered to).
                logger.warning(
                    "fleet sync serve %s scope %r: done after %d "
                    "transaction(s), %d frame(s); slowest phase %s %.1fs %s",
                    peer_pub[:12], scope, stats.get("transactions", 0), count,
                    slowest_phase[0] or "none", slowest_phase[1],
                    slowest_phase[2],
                )
                done = encode_done(
                    epoch=epoch,
                    count=count,
                    digest=digest.hexdigest(),
                    through_transaction_ref=cursor,
                    through_breadcrumb=through_breadcrumb,
                    version=protocol_version,
                )
                stats["bytes_sent"] += len(done)
                yield done
                outcome = "success"
                error_code = ""
                # The channel server marks a streamed record final only
                # when the generator yields the next one or ENDS (one-
                # message lookahead, relaykit connector._response_messages).
                # Everything that used to run here after the done frame
                # (served-ack prune, the telemetry write) held that frame
                # back for as long as it took; on home's autonomy store the
                # puller saw every data frame and then 60 s of silence
                # (SJC-2, 2026-09-07 07:06Z). The tail work is handed to a
                # task and the generator ends now.
                deferred_after_done = True
                self._spawn_after_serve(
                    store=store, scope=scope, epoch=state_epoch, peer_pub=peer_pub,
                    telemetry=(
                        dict(
                            channel=telemetry_channel, direction="serve",
                            mode=telemetry_mode, outcome="success",
                            started_at_ns=started_at_ns, error_code="",
                            scope=scope, **stats,
                        ) if record_here else None
                    ),
                    started_monotonic_ns=started_monotonic_ns,
                )
                return
            except asyncio.CancelledError:
                outcome = "cancelled"
                error_code = ""
                raise
            except Exception as exc:
                error_code = type(exc).__name__
                raise
            finally:
                if outcome != "success":
                    # A serve that did not reach its done frame: the
                    # generator was closed (peer gone, connector stopping)
                    # or failed. Named with how far it got, so a puller's
                    # silence can be matched to the server's side.
                    logger.warning(
                        "fleet sync serve %s scope %r: ended early (%s%s) "
                        "after %d transaction(s), %d frame(s)",
                        peer_pub[:12], scope, outcome,
                        f" {error_code}" if error_code else "",
                        stats.get("transactions", 0), count,
                    )
                recorder = self.config.telemetry_recorder
                if record_here and recorder is not None and not deferred_after_done:
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

        return self._observed(response(), telemetry_channel)

    def _spawn_after_serve(self, *, store, scope, epoch, peer_pub, telemetry,
                           started_monotonic_ns) -> None:
        """Run the post-serve work (telemetry write, served-ack prune) off
        the response generator, so the done frame is never held back."""
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._after_serve(
            store=store, scope=scope, epoch=epoch, peer_pub=peer_pub,
            telemetry=telemetry, started_monotonic_ns=started_monotonic_ns,
        ))
        self._after_serve_tasks.add(task)
        task.add_done_callback(self._after_serve_tasks.discard)

    def _org_ack_holders(
        self, store, channel: "OrgFleetAuthenticator", scope_path: Path,
        state_epoch: str, *, now_ns: int | None = None,
    ) -> list[str]:
        """The machines whose acknowledgement an org store waits for before
        retiring rows (ruling on auto-coea3): every machine with a COMPLETED
        pull recorded under the org key -- own fleet not special-cased --
        except one whose persona is outside the newest adopted member set
        (it cannot pull again; retire now) and one absent longer than
        ORG_PRUNE_ABSENCE_S (its row stays as its watermark). A machine
        with no completed pull holds nothing. A persona is known from the
        peer's admitted hello or its verified reachability row; unknown
        counts as a member within the absence bound."""
        from tools.network.fleet_org_reachability import read_rows, verify_row

        now_ns = time.time_ns() if now_ns is None else now_ns
        absence_ns = int(ORG_PRUNE_ABSENCE_S * 1e9)
        personas: dict[str, str] = {}
        for key, payload in read_rows(scope_path).items():
            verified = verify_row(key, payload, org=channel.org)
            if verified is not None:
                personas[key] = verified[0]
        holders: list[str] = []
        for machine, ack, updated_at_ns in store.peer_ack_rows(state_epoch):
            if machine == self.authenticator.machine_pub or ack is None:
                continue
            admitted = channel.admitted(machine)
            persona = admitted.persona_pub if admitted is not None else personas.get(machine)
            if persona is not None and channel.is_member(persona) is False:
                continue
            if now_ns - updated_at_ns > absence_ns:
                continue
            holders.append(machine)
        return sorted(holders)

    async def _after_serve(self, *, store, scope, epoch, peer_pub, telemetry,
                           started_monotonic_ns) -> None:
        recorder = self.config.telemetry_recorder
        if telemetry is not None and recorder is not None:
            duration_ms = max(
                0, (time.monotonic_ns() - started_monotonic_ns) // 1_000_000,
            )
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    recorder, peer_pub, duration_ms=duration_ms, **telemetry,
                )
        # Retire transaction rows every active peer has acknowledged.
        # Best-effort maintenance: a failure is logged, never raised. A
        # solo roster prunes nothing (acknowledged_journal_floor returns
        # None for an empty peer list). Rate-limited per scope: the prune
        # takes the store's write lock for up to its budget, and a peer
        # polling every second re-armed it after every serve (home's
        # autonomy store write-locked continuously, 2026-09-07 04:42Z).
        try:
            loop_now = asyncio.get_running_loop().time()
            last = self._last_prune_at.get(scope, 0.0)
            if loop_now - last < PRUNE_MIN_INTERVAL_S:
                return
            self._last_prune_at[scope] = loop_now
            active = resolve(
                self._roster_snapshot,
                anchor_root_pub=self.config.personal_root_pub,
            )
            others = sorted(
                pub for pub in active
                if pub != self.authenticator.machine_pub
            )
            channel = self._org_channels().get(scope) if scope != "personal" else None
            if channel is not None:
                # An org scope is also pulled by co-members' machines, which
                # no personal roster names: the acknowledgement set is the
                # machines with a completed pull under the org key, minus
                # removed personas and the long absent (_org_ack_holders).
                others = await asyncio.to_thread(
                    self._org_ack_holders, store, channel,
                    self._scope_paths()[scope], epoch,
                )
            started = time.monotonic()
            journal_rows, transaction_rows = await asyncio.to_thread(
                store.prune_acknowledged, others, epoch
            )
            took = time.monotonic() - started
            if transaction_rows or took >= SLOW_SERVE_PHASE_S:
                logger.warning(
                    "fleet sync scope %r: served-ack prune retired %d "
                    "transaction row(s) in %.1fs", scope, transaction_rows, took,
                )
        except Exception:
            logger.warning("fleet sync journal prune failed", exc_info=True)

    def _blob_response(
        self, message: bytes, peer_pub: str, telemetry_channel: str = "direct",
        *, authorize: Callable[[str], None] | None = None,
        admitted_org: str | None = None,
    ):
        """Serve requested attachment objects in bounded chunk frames.

        ``telemetry_channel`` was read here without being a parameter after
        the stream observer landed (NameError on every blob request, so no
        attachment ever crossed a fleet: test_end_to_end_attachment_crosses_
        the_fleet, found 2026-09-07)."""
        from tools.network.fleet_sync.blob_transport import (
            decode_blob_request,
            iter_blob_frames,
        )

        digests = decode_blob_request(message)
        if authorize is None:
            authorize = self.authenticator.authorize
        # Digests are self-certifying, so every synchronized scope's store
        # and attachment rows are legitimate candidates regardless of which
        # scope's backlog asked -- for a personal-admitted peer. An
        # org-admitted peer is answered from that organization's scope only.
        db_paths = self._blob_paths(admitted_org)

        async def response():
            frames = iter_blob_frames(db_paths, digests)
            while True:
                authorize(peer_pub)
                frame = await asyncio.to_thread(next, frames, None)
                if frame is None:
                    return
                yield frame

        return self._observed(response(), telemetry_channel)

    def _observed(self, stream, telemetry_channel: str):
        """Wrap a direct-path serve stream with the stream observer, if any.
        Relay serves count themselves in the connector runtime; wrapping
        them too would double-count."""
        observer = self.stream_observer
        if observer is None or telemetry_channel != "direct":
            return stream
        return _observe_stream(stream, observer)

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
        scope: str = "personal", *, authenticator=None,
    ) -> None:
        """Best-effort post-pull drain of the attachment byte backlog.
        ``authenticator`` is the one the pull connected with (the org
        channel for a co-member's machine); default the personal one."""
        if authenticator is None:
            authenticator = self.authenticator
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
                        authenticator=authenticator,
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
            # Blob serves have no build phase; the inter-frame bound
            # covers both positions.
            async for frame, _final in bounded_stream_frames(
                channel,
                first_allowance_s=self.config.pull_stream_silence_limit_s,
                silence_limit_s=self.config.pull_stream_silence_limit_s,
            ):
                authenticator.authorize(machine_pub)
                await asyncio.to_thread(receiver.feed, frame)
                if receiver.done:
                    break
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
                    rng=self._rng,
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
            # Fleet-first (graph://c2baad48-0a3 §3): the machine's own
            # roster is pulled above, then co-members' machines, so a fleet
            # converges internally before it presents one face outward.
            await self._sync_org_peers(set(active), now)
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.config.poll_interval
                )
            except asyncio.TimeoutError:
                pass

    async def _sync_org_peers(self, own_fleet: set[str], now: float) -> None:
        """One round's outward pulls: for every org scope with an org
        channel, a bounded stalest-first selection of co-member machines
        that are NOT in the personal roster, each pulling that scope only,
        through the org hello."""
        channels = self._org_channels()
        if not channels:
            return
        await asyncio.to_thread(self._publish_org_reachability)
        by_scope = await asyncio.to_thread(self._org_peer_candidates, channels)
        paths = self._scope_paths()
        jobs: list[tuple[str, str, tuple[str, ...], "OrgFleetAuthenticator"]] = []
        for scope, peers in by_scope.items():
            channel = channels.get(scope)
            if channel is None or scope not in paths or scope == "personal":
                continue
            eligible = [
                machine_pub for machine_pub in sorted(peers)
                if machine_pub != self.authenticator.machine_pub
                and machine_pub not in own_fleet
                and peers.get(machine_pub)
                and now >= self._next_attempt.get(machine_pub, 0.0)
            ]
            if not eligible:
                continue
            try:
                store = await asyncio.to_thread(self._store_for, scope)
                weights = await asyncio.to_thread(store.peer_last_success)
            except Exception:
                weights = {}
            selected = rank_peers(
                eligible, weights,
                limit=self.config.max_concurrent_pulls, rng=self._rng,
            )
            jobs.extend(
                (scope, machine_pub, tuple(peers[machine_pub]), channel)
                for machine_pub in selected
            )
        if jobs:
            await asyncio.gather(
                *(self._sync_scope(machine_pub, addresses, scope, org_channel=channel)
                  for scope, machine_pub, addresses, channel in jobs),
                return_exceptions=True,
            )

    async def _sync_peer(self, machine_pub: str, addresses: Sequence[str]) -> None:
        """Pull every synchronized scope from one peer, personal first.

        A schema mismatch pauses only its own scope: the typed refusal is
        recorded and the remaining scopes still sync. Any other failure is
        transport-level and backs off the whole peer.
        """
        for scope in self._scope_paths():
            if not await self._sync_scope(machine_pub, addresses, scope):
                return

    async def _sync_scope(
        self, machine_pub: str, addresses: Sequence[str], scope: str,
        *, org_channel: "OrgFleetAuthenticator | None" = None,
    ) -> bool:
        """Pull one scope from one peer, then absorb any ledger-event rows
        it carried. False when the failure was transport-level (the caller
        stops trying this peer for the round); True after success or a
        schema-mismatch pause, which affects only this scope."""
        try:
            await self._pull_scope(
                machine_pub, addresses, scope, org_channel=org_channel
            )
        except FleetSyncSchemaMismatch:
            logger.info(
                "fleet sync scope %r paused on schema mismatch", scope
            )
            return True
        except Exception:
            return False
        return True

    async def _pull_scope(
        self, machine_pub: str, addresses: Sequence[str], scope: str,
        *, org_channel: "OrgFleetAuthenticator | None" = None,
    ) -> None:
        """``org_channel``: pull *scope* from a co-member's machine through
        the org hello (auto-coea3) instead of the personal roster's; the
        request, the stream and the apply are otherwise identical."""
        store = await asyncio.to_thread(self._store_for, scope)
        epoch, state_epoch = self._scope_epochs(scope)
        authenticator = org_channel if org_channel is not None else self.authenticator
        channel = None
        sent = 0
        received = 0
        mutation_frames = 0
        transactions = 0
        started_at_ns = time.time_ns()
        started_monotonic_ns = time.monotonic_ns()
        peer_watermark: int | None = None
        # Bound before the connect: the commit-on-failure path below reads
        # them on ANY failure, including one before the receive loop ever
        # ran (connect refused), where they used to be unbound and the
        # handler itself crashed, masking the real error (SJC-2, 2026-09-07).
        batch: list[list[AuthoredMutation]] = []
        batch_bytes = 0
        empty_transactions: list[tuple[str, str, int]] = []
        flush_batch = None
        #: The candidate that actually connected -- the tier-used readout.
        connected_address: str | None = None
        #: Harness-only trace of (origin[:8], transaction_id, timestamp) per
        #: received transaction, for duplication forensics.
        received_ids: list[tuple[str, str, int]] = []
        # Initialized BEFORE the try: the except/finally paths read them,
        # and a pull that fails at connect never reaches the in-try inits
        # (a bad candidate stopped being recorded as a retry, 2026-09-06).
        checkpoint_stage: Path | None = None
        checkpoint_offered = False
        checkpoint_seen = [0, 0]
        protocol_version = self._peer_protocol.get(
            machine_pub, FLEET_SYNC_PROTOCOL_VERSION
        )

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
            if connected_address is not None:
                from tools.network.fleet_direct_config import path_class as _pc

                values["address"] = connected_address
                values["path_class"] = _pc(connected_address)
            if _PULL_TRACE:
                values["received"] = list(received_ids)
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
            candidate_failures: list[tuple[str, str]] = []
            for address in addresses:
                try:
                    channel = await fleet_direct_connect(
                        address,
                        authenticator=authenticator,
                        expected_machine_pub=machine_pub,
                        session=new_session_id(),
                        timeout=self.config.connect_timeout,
                    )
                    connected_address = address
                    break
                except Exception as exc:
                    last_error = exc
                    candidate_failures.append(
                        (address, f"{type(exc).__name__}: {exc}"[:160])
                    )
            if channel is None:
                assert last_error is not None
                # Every candidate failed. Name each one: the raised error is
                # only the LAST candidate's (often the harmless container
                # bridge address, refused in milliseconds), which hid what
                # happened to the reachable one (SJC-2 -> home, 2026-09-07).
                logger.warning(
                    "fleet sync peer %s scope %r: no candidate connected: %s",
                    machine_pub[:12], scope,
                    "; ".join(f"{a} -> {e}" for a, e in candidate_failures),
                )
                raise last_error

            await asyncio.to_thread(
                store.record_peer, machine_pub, state_epoch, online=True
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
            from tools.network.fleet_sync.sync import founded_ledger_rows

            founded_rows = await asyncio.to_thread(
                founded_ledger_rows, self._scope_paths()[scope]
            )
            watermarks = await asyncio.to_thread(store.origin_watermarks)
            request = encode_pull_request(
                epoch, compat=local_digest, resume=resume_trail,
                scope=scope, bootstrap=bootstrap, version=protocol_version,
                accept_checkpoint=founded_rows == 0,
                watermarks=watermarks,
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
            transaction_group: tuple[str, str] | None = None
            saw_done = False
            through_transaction_ref = 0
            through_breadcrumb: tuple[str, str, int] | None = None
            checkpoint_offered = False

            # Complete transactions are queued and applied in bounded batches
            # on one connection (auto-t43kz); see SQLiteFleetSyncStore.apply_many.
            async def flush_batch() -> None:
                nonlocal peer_watermark, transactions, batch, batch_bytes
                nonlocal empty_transactions
                if empty_transactions:
                    empties, empty_transactions = empty_transactions, []
                    await asyncio.to_thread(store.record_transactions, empties)
                    transactions += len(empties)
                    peer_watermark = max(
                        peer_watermark or 0, max(ts for _o, _t, ts in empties)
                    )
                if not batch:
                    return
                groups, batch, batch_bytes = batch, [], 0
                results = await asyncio.to_thread(store.apply_many, groups)
                applied = 0
                for items, (won, _ignored) in zip(groups, results):
                    if won == len(items):
                        _emit_settings_materialized(items)
                    elif won:
                        # The catalog returns counts, not the winning subset:
                        # one coalesced gap asks consumers to re-resolve.
                        _emit_settings_materialized(gap=True)
                    transactions += 1
                    peer_watermark = max(
                        peer_watermark or 0,
                        max(item.mutation.timestamp_ns for item in items),
                    )
                    if won:
                        applied += 1
                if applied:
                    await asyncio.to_thread(
                        store.record_peer,
                        machine_pub,
                        state_epoch,
                        online=True,
                        transactions_applied=applied,
                        peer_watermark=peer_watermark,
                    )

            last_flush_at = asyncio.get_running_loop().time()

            async def apply_pending(items: list[AuthoredMutation]) -> None:
                nonlocal batch_bytes, last_flush_at
                if _PULL_TRACE and items:
                    received_ids.append((
                        items[0].origin_incarnation[:8], items[0].transaction_id,
                        items[0].mutation.timestamp_ns,
                    ))
                batch.append(list(items))
                # Operations, not bytes, bound the batch: frames were
                # already size-checked on decode, and a transaction is at
                # most MAX_TRANSACTION_OPERATIONS operations.
                batch_bytes += len(items)
                now_flush = asyncio.get_running_loop().time()
                if (
                    len(batch) >= APPLY_BATCH_TRANSACTIONS
                    or batch_bytes >= APPLY_BATCH_OPERATIONS
                    # Time also bounds a batch: a round cut by the server
                    # (a connector recycled mid-serve, a dropped link)
                    # keeps what arrived before the cut instead of losing
                    # the whole round and repeating it identically
                    # (SJC-2 autonomy, 23 identical failed rounds,
                    # 2026-09-07).
                    or now_flush - last_flush_at >= APPLY_FLUSH_INTERVAL_S
                ):
                    await flush_batch()
                    last_flush_at = asyncio.get_running_loop().time()

            def validate_pending() -> None:
                if not pending:
                    return
                if pending_count != len(pending) or len({
                    item.operation_index for item in pending
                }) != len(pending):
                    raise FleetSyncProtocolError(
                        "fleet transaction is incomplete or out of order"
                    )

            async for message, stream_final in bounded_stream_frames(
                channel,
                first_allowance_s=self.config.pull_first_frame_allowance_s,
                silence_limit_s=self.config.pull_stream_silence_limit_s,
            ):
                # A kick that lands after the hello revokes this live session
                # before another application message is accepted.
                authenticator.authorize(machine_pub)
                received += len(message)
                if message.startswith(_REFUSAL_MAGIC):
                    # Capture — do NOT discard — the peer's digest and build
                    # timestamp, so the fleet view can name WHICH build the
                    # incompatible peer runs, not just that a hash differs.
                    peer_refusal_digest, peer_built_at = decode_schema_refusal(
                        message
                    )
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(
                            store.record_peer, machine_pub, state_epoch,
                            online=False, error="schema_mismatch",
                            peer_built_at=peer_built_at,
                        )
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
                    await flush_batch()
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
                if message.startswith(_TRANSACTION_MAGIC):
                    # v4: the header opens a group; a previous group must be
                    # complete before it applies, exactly like the v3
                    # identity-change boundary.
                    origin, transaction_id, operations = (
                        decode_transaction_header(message)
                    )
                    if pending:
                        validate_pending()
                        await apply_pending(pending)
                        pending = []
                    pending_identity = None
                    transaction_group = (origin, transaction_id)
                    pending_count = operations
                    _digest_add(digest, message)
                    continue
                if message.startswith(_OPERATION_MAGIC):
                    if transaction_group is None:
                        raise FleetSyncProtocolError(
                            "fleet operation frame arrived before its "
                            "transaction header"
                        )
                    operation, mutation = decode_operation_frame(message)
                    pending.append(AuthoredMutation(
                        transaction_group[0], transaction_group[1],
                        operation, mutation,
                    ))
                    _digest_add(digest, message)
                    message_count += 1
                    mutation_frames += 1
                    if pending_count is not None and len(pending) >= pending_count:
                        # The group is complete: queue it now rather than
                        # at the NEXT header. A stall after the last
                        # frame of a group used to leave that group (and
                        # the timed flush, which only ran from here)
                        # waiting for a header that never came (SJC-2
                        # autonomy, 2026-09-07).
                        validate_pending()
                        await apply_pending(pending)
                        pending = []
                        transaction_group = None
                        pending_count = None
                    continue
                if message.startswith(FILE_MAGIC):
                    if checkpoint_stage is None:
                        raise FleetSyncProtocolError(
                            "checkpoint file arrived before its header"
                        )
                    relative, body = decode_checkpoint_file(message)
                    target_file = checkpoint_stage / relative

                    def _stage_file(path=target_file, data=body) -> None:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data)

                    # Off the loop: at tailnet speed a 4 MiB chunk lands
                    # every few ms, and a synchronous write per chunk starved
                    # the dashboard's event loop for minutes (2026-09-06).
                    await asyncio.to_thread(_stage_file)
                    checkpoint_seen[0] += 1
                    checkpoint_seen[1] += len(body)
                    continue
                if message.startswith(b"{"):
                    control = _json_loose(message)
                    kind = control.get("kind")
                    if kind == "keepalive":
                        # Tolerated, never emitted (yet): a future server
                        # may keep a long build phase live with these.
                        # They are outside the summary digest and count.
                        continue
                    if kind == "transaction.empty":
                        # A transaction of which nothing survives on the
                        # server; recorded so our watermark for its origin
                        # advances past it. Outside the digest.
                        if pending:
                            validate_pending()
                            await apply_pending(pending)
                            pending = []
                        transaction_group = None
                        try:
                            empty_transactions.append((
                                str(control["origin"]),
                                str(control["transaction_id"]),
                                int(control["timestamp_ns"]),
                            ))
                        except (KeyError, TypeError, ValueError) as exc:
                            raise FleetSyncProtocolError(
                                "malformed empty-transaction frame"
                            ) from exc
                        if len(empty_transactions) >= APPLY_BATCH_TRANSACTIONS:
                            await flush_batch()
                        continue
                    if kind == "retired":
                        # The server skipped these origins for this pull
                        # (its retained history starts above our watermark).
                        # Our watermark for them does not move; another
                        # holder supplies the prefix. Outside the digest.
                        origins = control.get("origins") or []
                        logger.info(
                            "fleet sync peer %s scope %r: %d origin(s) not "
                            "served here (history retired above our "
                            "watermark): %s",
                            machine_pub[:12], scope, len(origins),
                            ",".join(str(a)[:12] for a in origins),
                        )
                        continue
                    if kind == "checkpoint.begin":
                        if checkpoint_stage is not None:
                            raise FleetSyncProtocolError(
                                "nested checkpoint stream"
                            )
                        await flush_batch()
                        # Refuse at the OFFER, before a single chunk lands:
                        # this store holds a founded ledger, so the install
                        # would refuse anyway (ca33ba7); receiving hundreds
                        # of MB first only to fail is the loop we had.
                        founded = founded_rows
                        if founded:
                            checkpoint_offered = True
                            raise FleetSyncFoundedLedgerRefusal(
                                f"peer offered a checkpoint for scope {scope!r} "
                                f"but this store holds a founded ledger "
                                f"({founded} rows); refusing before transfer"
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
                        installer = self.personal_checkpoint_installer
                        if scope == "personal" and installer is not None:
                            # Hand the received base to the service: it stops
                            # THIS scheduler (cancelling this task), quiesces
                            # personal.db, installs, and restarts us; the delta
                            # after the base is pulled next round from the
                            # installed floor. Stage ownership transfers to
                            # the handoff task, which cleans it up.
                            stage, checkpoint_stage = checkpoint_stage, None
                            await record("checkpoint-handoff")
                            logger.info(
                                "fleet sync peer %s scope 'personal': checkpoint "
                                "received over direct (%d files, %d bytes); "
                                "handing off to the runtime installer",
                                machine_pub[:12], *checkpoint_seen,
                            )
                            asyncio.get_running_loop().create_task(
                                _install_personal_handoff(
                                    installer, stage, machine_pub
                                ),
                                name="fleet-direct-personal-install",
                            )
                            return
                        await self._install_direct_checkpoint(
                            checkpoint_stage, scope, machine_pub, state_epoch
                        )
                        # No receipt recording here: install_checkpoint's
                        # _record_checkpoint_receipt already records it
                        # durably with source attribution — a second write
                        # double-counted every direct-path install.
                        installed_checkpoint = True
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
                state_epoch,
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
                    machine_pub, addresses, scope, authenticator=authenticator,
                )
            except Exception:
                logger.warning(
                    "fleet attachment drain failed", exc_info=True
                )
            try:
                scope_store = await asyncio.to_thread(self._store_for, scope)
                cleared = await asyncio.to_thread(
                    scope_store.drain_pending_signatures
                )
                if cleared:
                    logger.info(
                        "fleet sync scope %r: %d signed row(s) verified after "
                        "their organization genesis arrived", scope, cleared,
                    )
            except Exception:
                logger.warning(
                    "fleet signature drain failed", exc_info=True
                )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    store.record_peer,
                    machine_pub,
                    state_epoch,
                    online=False,
                    bytes_sent=sent,
                    bytes_received=received,
                )
            await record("cancelled")
            raise
        except Exception as exc:
            # Complete transaction groups received before the failure
            # are verified units; commit them so a round cut short
            # keeps its progress instead of repeating identically
            # (SJC-2 autonomy: 35 identical failed rounds, 2026-09-07).
            try:
                if batch and flush_batch is not None:
                    await flush_batch()
            except Exception:
                logger.warning(
                    "fleet sync: could not commit the groups received "
                    "before the failure", exc_info=True,
                )
            if (
                protocol_version >= 4
                and received == 0
                and not isinstance(
                    exc, (FleetSyncSchemaMismatch, FleetSyncStreamSilence)
                )
            ):
                # The peer's server closed the pull without serving a single
                # frame — the signature of pre-v4 software rejecting the
                # declared request version. Retry this peer at v3 for the
                # rest of the process; only wire efficiency rides on it,
                # and a plain transport flake merely costs the same
                # harmless downgrade.
                self._peer_protocol[machine_pub] = 3
                logger.info(
                    "fleet sync peer %s: retrying at protocol v3",
                    machine_pub[:12],
                )
            failures = self._failures.get(machine_pub, 0) + 1
            self._failures[machine_pub] = failures
            if self.config.on_peer_failure is not None and not isinstance(
                exc, (FleetSyncSchemaMismatch,)
            ):
                with contextlib.suppress(Exception):
                    self.config.on_peer_failure(machine_pub)
            delay = min(
                self.config.max_backoff,
                self.config.min_backoff * (2 ** min(failures - 1, 16)),
            )
            transient = isinstance(exc, (
                FleetSyncQuiescenceError, ConnectionError, OSError,
            )) or type(exc).__name__.startswith("ConnectionClosed")
            if (checkpoint_stage is not None or checkpoint_offered) and not transient:
                # A whole base arrived (or was offered and refused) and the
                # pull failed for a reason that will recur (an install
                # refusal, a founded-ledger refusal). A quiescence collision
                # or a dropped connection is NOT that: it clears in seconds,
                # and the long wait turned one collision at bootstrap into a
                # ten-minute stall of every scope from that peer
                # (test_org_databases_sync_with_isolation, 2026-09-07).
                # The server just BUILT that base; the
                # refused, stream cut after it). The ordinary backoff caps
                # at seconds; retrying asks the peer to rebuild and resend
                # hundreds of MB every round -- the loop seen live on
                # 2026-09-06 (343 MB every ~10s). Back off like the relay
                # path's redelivery guard instead.
                delay = max(delay, CHECKPOINT_FAILURE_BACKOFF_S)
                logger.warning(
                    "fleet sync peer %s scope %r: checkpoint received "
                    "(%d files, %d bytes) but the pull failed; not asking "
                    "again for %.0fs",
                    machine_pub[:12], scope, *checkpoint_seen, delay,
                )
            self._next_attempt[machine_pub] = (
                asyncio.get_running_loop().time() + delay
            )
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    store.record_peer,
                    machine_pub,
                    state_epoch,
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
                    self._scheduler.personal_checkpoint_installer = (
                        self.install_checkpoint
                    )
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
                        self._scheduler.personal_checkpoint_installer = (
                            self.install_checkpoint
                        )
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
