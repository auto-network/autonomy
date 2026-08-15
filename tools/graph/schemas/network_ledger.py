"""auto.network authority-ledger Settings — replica state + read models.

Two contracts back the graph/dashboard side of the org authority ledger
(spec ``graph://eb245082-b76`` §2, §6; invariant L8; bead auto-0kkpq/F2):

* ``autonomy.network.ledger-state#1`` — this node's replica state for one
  org ledger: current heads, fold fingerprint, last *witnessed* head (F4
  equivocation-witness cursor), and the per-peer sync cursor. Hashes and
  cursors only.
* ``autonomy.network.ledger-projection#1`` — fold-derived READ MODELS
  (roster with sponsor provenance, role matrix, live-key set). Ops are
  the replicated primitive; these rows are rebuildable caches of the
  fold and always carry the heads + fingerprint they were derived from.

**L8 at the Settings layer**: neither contract can carry event content.
Payloads hold event *hashes*; a payload smuggling ``events``/``wire``/
``payload`` fields is rejected with an explicit L8 message. The event
store itself (``tools/network/ledger/store.py``) enforces L8 twice more
(parser whitelist + SQL CHECK).

Vocabulary is duplicated (not imported) from ``tools/network/ledger`` so
this module stays importable without ``cryptography``;
``test_network_ledger_schemas.py`` cross-pins the constants and validates
real fold-derived payloads against these schemas.
"""

from __future__ import annotations

import re
from typing import Any

from .registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    keyed_per_entity,
)

NETWORK_LEDGER_STATE_SET_ID = "autonomy.network.ledger-state"
NETWORK_LEDGER_STATE_REVISION = 1
NETWORK_LEDGER_PROJECTION_SET_ID = "autonomy.network.ledger-projection"
NETWORK_LEDGER_PROJECTION_REVISION = 1

# ── F1/F2 vocabulary pins (tools/network/ledger) ─────────────
#
# Duplicated so tools.graph never imports `cryptography` at import time;
# a test asserts these match the ledger library's exports exactly.

LEDGER_EVENT_TYPES = (
    "checkpoint",
    "delegate",
    "genesis",
    "invite",
    "key.epoch",
    "key.rotate",
    "member.claim",
    "member.rekey",
    "revoke",
    "role.define",
    "role.grant",
    "role.revoke",
)
LEDGER_PROJECTION_NAMES = ("live-keys", "roles", "roster")
LEDGER_CLAIM_REQUIRES = ("admin-ack", "self", "sponsor")
LEDGER_HASH_HEX_LEN = 64  # sha256 event id, lowercase hex
LEDGER_PUB_HEX_LEN = 64  # raw Ed25519 public key, lowercase hex

#: Field names that smell like event content — the L8 tripwire.
_L8_CONTENT_FIELDS = frozenset({"events", "event", "wire", "payload", "body_events"})

_HEX_RE = re.compile(r"^[0-9a-f]+$")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_ROSTER_ROW_KEYS = frozenset(
    {"persona", "current_key", "roles", "sponsor", "claim_id", "invite_id"}
)
_ROLE_ROW_KEYS = frozenset(
    {"version", "claim_requires", "scope_set", "event_id", "holders"}
)


SYNOPSIS = {
    "summary": (
        "auto.network org authority-ledger replica state and read models: "
        "autonomy.network.ledger-state (this node's heads, fold "
        "fingerprint, last witnessed head, per-peer sync cursor — hashes "
        "only, never event content, L8) and "
        "autonomy.network.ledger-projection (fold-derived roster with "
        "sponsor provenance, role matrix, and live-key set — rebuildable "
        "read models stamped with the heads+fingerprint they derive from)."
    ),
    "nouns": [
        "authority ledger", "ledger state", "ledger heads", "roster",
        "role matrix", "live keys", "membership", "sync cursor",
        "witnessed head", "projection", "read model", "org members",
        "auto.network", "revocation",
    ],
    "related_set_ids": [
        "autonomy.network.binding#1",
        "autonomy.network.org-key#1",
        "autonomy.network.link-grant#1",
    ],
}


# ── shared validators ─────────────────────────────────────────


def _fail(cls_name: str, msg: str) -> None:
    raise SchemaValidationError(f"{cls_name}: {msg}")


def _l8_sweep(payload: dict, cls_name: str) -> None:
    smuggled = _L8_CONTENT_FIELDS & set(payload)
    if smuggled:
        _fail(
            cls_name,
            f"fields {sorted(smuggled)} look like ledger event content — the "
            "graph holds hashes and derived views only; authority events "
            "live in the replicated ledger store (L8)",
        )


def _require_hex(value: Any, what: str, cls_name: str, length: int) -> str:
    if not isinstance(value, str) or len(value) != length or not _HEX_RE.match(value):
        _fail(cls_name, f"{what} must be exactly {length} lowercase hex chars")
    return value


def _require_heads(value: Any, cls_name: str, what: str = "heads") -> list:
    if not isinstance(value, list) or not value or len(value) > 64:
        _fail(cls_name, f"{what} must be a non-empty list of at most 64 event ids")
    for entry in value:
        _require_hex(entry, f"{what} entry", cls_name, LEDGER_HASH_HEX_LEN)
    if value != sorted(set(value)):
        _fail(cls_name, f"{what} must be sorted and free of duplicates")
    return value


# ── autonomy.network.ledger-state ─────────────────────────────


@keyed_per_entity(key_strategy="genesis_id")
class NetworkLedgerStateV1(SettingSchema):
    """One replica's view of one org ledger — hashes and cursors only.

    Key: the org's network-identity label (mirrors
    ``autonomy.network.org-key``). Everything here is derivable from the
    event store; the row exists so dashboards and sync workers can read
    "where is this replica" without opening SQLite.
    """

    set_id = NETWORK_LEDGER_STATE_SET_ID
    schema_revision = NETWORK_LEDGER_STATE_REVISION

    org_uuid: str = field(required=True, description="The org's registry UUID.")
    genesis_id: str = field(
        required=True,
        description="Event id of the ledger's genesis (the DAG anchor).",
    )
    heads: list = field(
        required=True,
        element=str,
        description="Current DAG heads (sorted, unique, 64-hex event ids).",
    )
    fingerprint: str = field(
        required=True,
        description="Fold fingerprint at these heads (L1 state hash).",
    )
    root_pub: str = field(
        required=True,
        description="Current org root public key per the folded lineage.",
    )
    event_count: int = field(
        required=True, description="Events held by the local replica store."
    )
    last_witnessed_head: str = field(
        required=False,
        description=(
            "Most recent head the equivocation witness attested (F4/L5 "
            "cursor; absent until the witness path lands)."
        ),
    )
    sync_cursor: dict = field(
        required=False,
        description=(
            "Per-peer sync cursor: peer identifier → last event id "
            "exchanged with that peer (F3 consumes this)."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        name = cls.__name__
        _l8_sweep(payload, name)
        allowed = {
            "org_uuid", "genesis_id", "heads", "fingerprint", "root_pub",
            "event_count", "last_witnessed_head", "sync_cursor",
        }
        unknown = set(payload) - allowed
        if unknown:
            _fail(name, f"unknown fields: {sorted(unknown)}")
        missing = {"org_uuid", "genesis_id", "heads", "fingerprint", "root_pub", "event_count"} - set(payload)
        if missing:
            _fail(name, f"missing required fields: {sorted(missing)}")

        org_uuid = payload["org_uuid"]
        if not isinstance(org_uuid, str) or not org_uuid or len(org_uuid) > 64:
            _fail(name, "org_uuid must be a non-empty string of at most 64 chars")
        _require_hex(payload["genesis_id"], "genesis_id", name, LEDGER_HASH_HEX_LEN)
        _require_heads(payload["heads"], name)
        _require_hex(payload["fingerprint"], "fingerprint", name, LEDGER_HASH_HEX_LEN)
        _require_hex(payload["root_pub"], "root_pub", name, LEDGER_PUB_HEX_LEN)
        count = payload["event_count"]
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            _fail(name, "event_count must be a positive integer")

        witnessed = payload.get("last_witnessed_head")
        if witnessed is not None:
            _require_hex(witnessed, "last_witnessed_head", name, LEDGER_HASH_HEX_LEN)
        cursor = payload.get("sync_cursor")
        if cursor is not None:
            if not isinstance(cursor, dict) or len(cursor) > 256:
                _fail(name, "sync_cursor must be an object of at most 256 peers")
            for peer, head in cursor.items():
                if not isinstance(peer, str) or not peer or len(peer) > 128:
                    _fail(name, "sync_cursor keys must be non-empty peer ids")
                _require_hex(head, f"sync_cursor[{peer!r}]", name, LEDGER_HASH_HEX_LEN)


# ── autonomy.network.ledger-projection ────────────────────────


@keyed_per_entity(key_strategy="genesis_id:projection")
class NetworkLedgerProjectionV1(SettingSchema):
    """A fold-derived read model — a rebuildable cache, never the truth.

    Key: ``<label>:<projection>`` (e.g. ``default:roster``). Rows are
    produced by ``tools/network/ledger/projections.py`` and stamped with
    the heads + fingerprint of the fold they derive from; a consumer that
    finds the stamp stale re-folds rather than trusting the row.
    """

    set_id = NETWORK_LEDGER_PROJECTION_SET_ID
    schema_revision = NETWORK_LEDGER_PROJECTION_REVISION

    projection: str = field(
        required=True,
        enum=list(LEDGER_PROJECTION_NAMES),
        description="Which read model this row carries.",
    )
    org: str = field(required=True, description="The org UUID this ledger belongs to.")
    heads: list = field(
        required=True,
        element=str,
        description="DAG heads of the fold this projection derives from.",
    )
    fingerprint: str = field(
        required=True,
        description="Fold fingerprint at those heads (staleness check).",
    )
    body: Any = field(
        required=True,
        description=(
            "The read model. roster: member rows with sponsor provenance; "
            "roles: role matrix with holders; live-keys: key → held scope "
            "patterns. Derived data only — rebuildable from the event store."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        name = cls.__name__
        _l8_sweep(payload, name)
        allowed = {"projection", "org", "heads", "fingerprint", "body"}
        unknown = set(payload) - allowed
        if unknown:
            _fail(name, f"unknown fields: {sorted(unknown)}")
        missing = allowed - set(payload)
        if missing:
            _fail(name, f"missing required fields: {sorted(missing)}")

        projection = payload["projection"]
        if projection not in LEDGER_PROJECTION_NAMES:
            _fail(name, f"projection must be one of {LEDGER_PROJECTION_NAMES}")
        org = payload["org"]
        if not isinstance(org, str) or not org or len(org) > 64:
            _fail(name, "org must be a non-empty string of at most 64 chars")
        _require_heads(payload["heads"], name)
        _require_hex(payload["fingerprint"], "fingerprint", name, LEDGER_HASH_HEX_LEN)

        body = payload["body"]
        if projection == "roster":
            cls._validate_roster(body, name)
        elif projection == "roles":
            cls._validate_roles(body, name)
        else:
            cls._validate_live_keys(body, name)

    @classmethod
    def _validate_roster(cls, body: Any, name: str) -> None:
        if not isinstance(body, list):
            _fail(name, "roster body must be a list of member rows")
        for row in body:
            if not isinstance(row, dict) or set(row) != _ROSTER_ROW_KEYS:
                _fail(
                    name,
                    f"roster rows must be objects with exactly {sorted(_ROSTER_ROW_KEYS)}",
                )
            _require_hex(row["persona"], "roster persona", name, LEDGER_PUB_HEX_LEN)
            _require_hex(row["current_key"], "roster current_key", name, LEDGER_PUB_HEX_LEN)
            _require_hex(row["sponsor"], "roster sponsor", name, LEDGER_PUB_HEX_LEN)
            _require_hex(row["claim_id"], "roster claim_id", name, LEDGER_HASH_HEX_LEN)
            _require_hex(row["invite_id"], "roster invite_id", name, LEDGER_HASH_HEX_LEN)
            roles = row["roles"]
            if not isinstance(roles, list) or any(
                not isinstance(r, str) or not r for r in roles
            ):
                _fail(name, "roster roles must be a list of role names")

    @classmethod
    def _validate_roles(cls, body: Any, name: str) -> None:
        if not isinstance(body, dict):
            _fail(name, "roles body must be an object keyed by role name")
        for role, row in body.items():
            if not isinstance(role, str) or not role:
                _fail(name, "role names must be non-empty strings")
            if not isinstance(row, dict) or set(row) != _ROLE_ROW_KEYS:
                _fail(
                    name,
                    f"role rows must be objects with exactly {sorted(_ROLE_ROW_KEYS)}",
                )
            if row["claim_requires"] not in LEDGER_CLAIM_REQUIRES:
                _fail(name, f"claim_requires must be one of {LEDGER_CLAIM_REQUIRES}")
            if not isinstance(row["version"], int) or isinstance(row["version"], bool) or row["version"] < 1:
                _fail(name, "role version must be a positive integer")
            _require_hex(row["event_id"], "role event_id", name, LEDGER_HASH_HEX_LEN)
            if not isinstance(row["scope_set"], list):
                _fail(name, "role scope_set must be a list")
            if not isinstance(row["holders"], list):
                _fail(name, "role holders must be a list")
            for holder in row["holders"]:
                _require_hex(holder, "role holder", name, LEDGER_PUB_HEX_LEN)

    @classmethod
    def _validate_live_keys(cls, body: Any, name: str) -> None:
        if not isinstance(body, dict):
            _fail(name, "live-keys body must be an object keyed by public key")
        for key, scopes in body.items():
            _require_hex(key, "live-keys key", name, LEDGER_PUB_HEX_LEN)
            if not isinstance(scopes, list) or not scopes:
                _fail(name, "live-keys values must be non-empty scope lists")
            if any(not isinstance(s, str) or not s for s in scopes):
                _fail(name, "live-keys scopes must be non-empty strings")
            if scopes != sorted(scopes):
                _fail(name, "live-keys scopes must be sorted")
