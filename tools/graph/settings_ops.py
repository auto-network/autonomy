"""Settings primitive — ops layer.

Read, write, resolve, and migrate operations for the ``settings`` table.
Imported into ``tools.graph.ops`` for unified discovery; tests may reach in
here directly. Spec: graph://0d3f750f-f9c. Cross-org rules:
graph://bcce359d-a1d.

Public API contract (auto-cfb8u): every function below takes ``org`` as
a **required keyword-only** argument. There is no default. Forgetting
``org=`` is a ``TypeError`` at call time, not a silent route to the
scopeless default DB. Pass:

* a non-empty org slug — route to that org's DB (the common case);
* :data:`CALLER_ORG` — opt into the env-cascade resolver (per-request
  contextvar → ``GRAPH_ORG`` env → scopeless default). Used by CLIs and
  env-driven test contexts that legitimately want today's behavior;
* ``None`` — explicit "scopeless default DB" (rare; loud).

Background: dashboard handlers were silently writing to the scopeless DB
when the writer forgot ``org=`` while the reader carried ``X-Graph-Org``
— the row appeared to vanish (graph://53f7412f-51e). Required-org makes
that intent explicit at the API boundary.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
import time
from collections import Counter
from dataclasses import dataclass, field as dataclass_field, asdict, replace
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Generic, Iterator, TypeVar
from uuid import uuid4

from .db import GraphDB, _org_db_path, resolve_caller_db_path
from . import schemas


logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────


VALID_STATES = ("raw", "curated", "published", "canonical")
PRECEDENCE = {"canonical": 0, "published": 1, "curated": 2, "raw": 3}
PEER_VISIBLE_STATES = ("published", "canonical")
_personal_db_init_lock = threading.Lock()


# ── Slot resolution (auto-y2ubq, graph://21a0da9e-1c2) ───────
#
# An organization row is a per-signer SLOT: several members may hold the
# same (set_id, schema_revision, key, publication_state) address, one row
# each, and every store must resolve the same winner from the same slots in
# any delivery order. The ordering below is therefore a function of the
# rows plus each owning org's fold — never of anything store-local.
# ``created_at`` records when THIS store received a row and participates
# only as the legacy within-store tiebreak among UNSIGNED rows, which have
# a single writer.


def _store_rank(src_org: "str | None", reading_org: "str | None") -> int:
    """Resolution step 3 — most local wins.

    The machine store replicates nowhere, personal crosses only this
    operator's fleet, an organization's rows reach its members. When the
    read itself is personal (``reading_org is None``) the org-being-read
    tier collapses out and the relative order of the rest is unchanged.
    """
    from .cross_org import MACHINE_DB_SLUG, PERSONAL_DB_SLUG

    if src_org == MACHINE_DB_SLUG:
        return 0
    if src_org is None or src_org == PERSONAL_DB_SLUG:
        return 1
    if reading_org is not None and src_org == reading_org:
        return 2
    return 3


def _slot_tiebreak_hash(row) -> str:
    """Step 6 — ``H(set_id ‖ key ‖ terminal_persona)``, lower wins.

    Reachable only on an exact ``signed_at`` collision between two
    personas; exists to keep the ordering total, and unprofitable to
    engineer (matching a timestamp gains nothing over exceeding it).
    """
    import hashlib

    material = "\x00".join(
        (row["set_id"], row["key"], row["terminal_persona"])
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _row_col(row, name):
    """Column value, or None on a row from a store predating the column
    (a read-only peer database migrates only when next opened writable)."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


#: THE fold cache — the single one (graph://21a0da9e-1c2 "The fold";
#: auto-7c7po warns against a second ever existing). Keyed on the ledger's
#: current HEADS, so any number of resolutions against an unchanged ledger
#: build nothing and a ledger advancement invalidates by key inequality,
#: never by a sweep. The cached value is the whole FoldState, so every
#: time-independent query — membership, backward key resolution
#: (persona_for_key), key revocation — is served from one cache by every
#: consumer (resolution here; the boundary, auto-wah16, next). Delegation
#: authority is ref_ts-dependent and MUST NOT be read from a cached view —
#: a cached view silently extends an expired delegation. Owned by
#: resolution (auto-y2ubq) because it is the first consumer.
_FOLD_VIEW_CACHE: dict[str, tuple[tuple, Any]] = {}
_fold_builds = 0  # how many times a fold was actually constructed


def _ledger_heads(slug: str) -> "tuple | None":
    """The org ledger's current heads, without hydrating the ledger.

    A cheap SQL probe of the ``ledger_heads`` table in the org's own DB —
    this is the cache KEY read, performed per lookup; building the FOLD is
    what the key exists to avoid. ``None`` means no ledger at all.
    """
    from tools.network.ledger import org_ledger_db_path

    path = org_ledger_db_path(slug)
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return tuple(sorted(
            r[0] for r in conn.execute("SELECT event_id FROM ledger_heads")
        ))
    except sqlite3.Error:
        # No ledger tables, or a database that cannot be read at all (a
        # corrupt file raises DatabaseError, not OperationalError). Either
        # way the answer is "no usable ledger": the org's signed rows fail
        # eligibility closed rather than the whole read failing open.
        return None
    finally:
        conn.close()


def _org_fold_view(slug: "str | None"):
    """The current FoldState for org *slug*, or ``None``.

    Built ONCE PER LEDGER ADVANCEMENT: the heads-keyed cache above serves
    every call whose heads probe matches, so resolution cost does not scale
    with reads and a per-row fold read cannot creep back in. Callers may
    read only the time-independent surface from the returned view —
    ``members``, ``persona_for_key``, ``key_revoked`` — never delegation
    authority (see the cache comment).

    Fail-closed: no founded ledger, no ledger tables, or a fold that cannot
    be built all yield ``None``.
    """
    if not slug:
        return None
    heads = _ledger_heads(slug)
    if heads is None or not heads:
        return None
    cached = _FOLD_VIEW_CACHE.get(slug)
    if cached is not None and cached[0] == heads:
        return cached[1]
    try:
        from tools.network.ledger import LedgerStore, org_ledger_db_path

        global _fold_builds
        _fold_builds += 1
        store = LedgerStore(org_ledger_db_path(slug))
        try:
            if not store.ledger.genesis_id:
                return None
            state = store.fold()
        finally:
            store.close()
    except Exception:
        logger.warning(
            "eligibility: fold for org %r unavailable; its signed rows "
            "will not resolve", slug, exc_info=True,
        )
        return None
    _FOLD_VIEW_CACHE[slug] = (heads, state)
    return state


def _org_fold_members(slug: "str | None") -> frozenset:
    """The current fold's member personas for org *slug* (empty when the
    fold is unavailable — a signed row then fails eligibility, fail-closed)."""
    state = _org_fold_view(slug)
    return frozenset(state.members) if state is not None else frozenset()


def _resolution_peers(set_id: str, resolved_org: "str | None",
                      peers: "list[str] | None") -> list[str]:
    """THE candidate-store selection, shared by every resolution-shaped
    read (read_set, chain_setting, contested_keys).

    Candidate SELECTION is upstream of the shared ranking helper, and a
    shared ranker over different candidate sets gives different answers
    with identical ordering logic — so the selection lives here, once:

    - An organization read draws from its peers: the operator's other
      organizations, personal, and the machine store (subject to the
      subscription, which can never remove the operator's own stores).
    - A PERSONAL read takes the machine store as its ONE peer — the single
      store more local than personal — and no organization ever
      contributes to it (graph://21a0da9e-1c2 v42): the sovereignty ladder
      read one rung down, not an aggregation point for org content.
    - A set whose band forbids every peer-visible state opens no peer
      database at all: the band already refuses the write and the
      promotion, and this refuses to SERVE a row that reached a federated
      state by a path nobody anticipated.

    Id-addressed lookups (get_setting, resolve_setting_strict) and set
    enumeration (list_set_ids) are NOT resolution: they find a row the
    caller already names rather than answer "what is the value", and they
    deliberately keep the wide peer set.

    Only the store list is shared; each caller keeps its own row fetch
    (whole set, one key, bases-plus-exclusions) because the row SHAPES
    genuinely differ — the hazard was three copies of the selection, not
    three fetches downstream of one selection.
    """
    from .cross_org import MACHINE_DB_SLUG, resolve_peers

    resolved = resolve_peers(resolved_org, peers)
    if resolved_org is None:
        resolved = [p for p in resolved if p == MACHINE_DB_SLUG]
    if not set(schemas.states_allowed(set_id, 1)) & set(PEER_VISIBLE_STATES):
        return []
    return sorted(resolved)


def _rank_candidates(
    candidate_bases: list,
    *,
    reading_org: "str | None",
    now: "int | None",
    dropped: "DropAccounting | None" = None,
):
    """Filter and order candidate base rows by the six-step slot ordering.

    Steps: 1 eligibility (a signed row's terminal persona must be in the
    OWNING org's current fold), then the plausibility window (a row whose
    ``signed_at`` is beyond the reader's window is stored and ignored, not
    refused); 2 rung; 3 store; 4 schema revision; 5 ``signed_at``; 6 the
    persona hash. Unsigned rows take no eligibility test, carry no
    ``signed_at``, and fall back to the legacy created_at/rowid tiebreak —
    they have a single writer, so that value is not cross-store state.

    Returns the ordered list; ``candidate_bases`` is ``[(src_org, row)]``.
    """
    from tools.network import clock

    eligible = []
    for src_org, row in candidate_bases:
        persona = _row_col(row, "terminal_persona")
        # PERMISSIVE BRANCH, deliberately: an unsigned row (persona is None)
        # takes no eligibility test even in an organization store. The design
        # says an unsigned org row does not resolve — but that rule is safe
        # only once auto-4zzxd's one-time signing pass has migrated the store
        # atomically; today's population is entirely unsigned and enforcing it
        # would dark every org's settings. auto-4zzxd owns the flip and
        # carries the acceptance that FAILS while this branch survives it:
        # after the pass, an unsigned row in a founded org store must not
        # resolve. Ratified 2026-08-19 (crypto pillar).
        if persona is not None:
            if persona not in _org_fold_members(src_org):
                if dropped is not None:
                    dropped.ineligible_signer += 1
                continue
            signed_at = _row_col(row, "signed_at")
            if signed_at is not None and not clock.settings_signed_at_is_plausible(
                signed_at, now=now
            ):
                if dropped is not None:
                    dropped.beyond_window += 1
                continue
        eligible.append((src_org, row))

    def _signed_at_key(om):
        signed_at = _row_col(om[1], "signed_at")
        # Later wins; an unsigned row (no claim at all) orders after every
        # signed one at the same rung/store/revision.
        return -(signed_at if signed_at is not None else -1)

    def _tiebreak_key(om):
        # Signed rows: deterministic hash, ascending. Unsigned rows: empty
        # key, so the stable created_at/rowid passes below decide.
        if _row_col(om[1], "terminal_persona") is not None:
            return _slot_tiebreak_hash(om[1])
        return ""

    # Stable sorts, least significant first.
    eligible.sort(key=lambda om: _row_col(om[1], "_rowid") or 0, reverse=True)
    eligible.sort(key=lambda om: om[1]["created_at"] or "", reverse=True)
    eligible.sort(key=_tiebreak_key)
    eligible.sort(key=_signed_at_key)
    eligible.sort(key=lambda om: -int(om[1]["schema_revision"]))
    eligible.sort(key=lambda om: _store_rank(om[0], reading_org))
    eligible.sort(key=lambda om: PRECEDENCE.get(om[1]["publication_state"], 99))
    return eligible


# ── protected identity sets (dashboard-access credentials) ───
#
# These set IDs store the human dashboard-access credentials that the
# unlock gate (tools/dashboard/unlock_routes.py) enforces on. Mutating
# them from the GENERIC settings surface (POST /api/graph/setting and the
# override/exclude/promote/deprecate/delete/migrate routes, all of which
# land here) would defeat the gate outright — an attacker with only
# network reach to the still-open /api surface could:
#   * INSERT their own passkey row, then assert against it → session;
#   * INSERT a personal-identity row with a low-sorting key to SHADOW the
#     operator's, then password-unlock against their own root;
#   * DELETE/exclude the operator's enrollment → the gate reads "nothing
#     enrolled" → fail-OPEN.
# So every mutation path below refuses these set IDs UNLESS the caller is
# the trusted identity/unlock route, which brackets its write in
# :func:`identity_write_context`. This is the C1 I1 lesson applied: the
# guard lives at the one data layer every write funnels through, so no
# individual route can forget it. Reads are unaffected (the gate must be
# able to read enrollment); only mutations are gated.

import contextvars as _contextvars

PROTECTED_IDENTITY_SET_IDS = frozenset({
    "autonomy.identity.personal",
    "autonomy.identity.passkey",
    "autonomy.identity.factor-metadata",
    "autonomy.identity.factor-recipient-metadata",
})


class ProtectedSettingError(PermissionError):
    """A generic-settings mutation targeted a protected identity set
    without the internal identity-route capability. Surfaces as 403."""


class VaultSealerMissing(RuntimeError):
    """A write to a vaulted set could not be encrypted.

    Raised when no vault sealer is registered in this process, or when the
    registered one did not return a locator. Either way the row is not
    written: the value this set holds is a secret, and storing it in the
    clear is not a degraded mode of storing it encrypted.
    """


# ── why a vault member did not open ──────────────────────────
#
# Resolution serves every caller of a set at once, so one unopenable secret
# must not take the rest of the set with it. Each of these is a member-level
# outcome carried on :class:`VaultReadFailure`, not an exception — and every
# one of them is a REFUSAL. None of them is a value, none is a locator, and
# none removes the member, which a caller would read as "no such setting".
#
#: The seam itself is unfilled: nothing in this process holds the
#: organization's key control. Distinct from holding a key that does not
#: reach — the fault is in the deployment, not in the reader's grants.
VAULT_NO_KEY_HOLDER = "no_key_holder"
#: No held generation reaches the one this revision was written under.
VAULT_NO_KEY_HELD = "no_key_held"
#: A held generation DESCENDS from this revision's, so the parent bridge that
#: should have recovered it backward is absent or does not open.
VAULT_MISSING_BRIDGE = "missing_bridge"
#: An authenticated decryption failed, at any layer of the object.
VAULT_DECRYPTION_FAILED = "decryption_failed"
#: A suite identifier this build does not recognize — fails closed rather
#: than negotiating down.
VAULT_UNKNOWN_SUITE = "unknown_suite"
#: The row of a vault set does not hold a locator. Whatever it holds is not
#: what this set stores, and handing it back would present it as the value.
VAULT_NOT_A_LOCATOR = "not_a_locator"
#: The locator's tier is not the one the set declares — a stored downgrade.
VAULT_TIER_MISMATCH = "tier_mismatch"


@dataclass(frozen=True)
class VaultReadFailure:
    """Why one member of a vault set resolved to no value.

    ``reason`` is one of the ``VAULT_*`` codes above and is the part callers
    branch on; ``message`` is the operator-facing detail. Both cross the HTTP
    boundary, so an out-of-process caller distinguishes the same four faults
    an in-process one does.
    """

    reason: str
    message: str

    def to_dict(self) -> dict:
        return {"reason": self.reason, "message": self.message}


_identity_write_allowed: "_contextvars.ContextVar[bool]" = _contextvars.ContextVar(
    "settings_identity_write_allowed", default=False,
)


class _IdentityWriteContext:
    """Context manager AND decorator granting the identity-route
    capability to mutate the protected identity sets. Only
    tools/dashboard/identity_routes.py and unlock_routes.py use it."""

    def __enter__(self):
        self._token = _identity_write_allowed.set(True)
        return self

    def __exit__(self, *exc):
        _identity_write_allowed.reset(self._token)
        return False


def identity_write_context() -> _IdentityWriteContext:
    """Grant the current context permission to write the protected
    identity sets (:data:`PROTECTED_IDENTITY_SET_IDS`). The trusted
    identity/unlock routes bracket their ``upsert_by_key`` call in this;
    the generic settings API never does, so it stays refused."""
    return _IdentityWriteContext()


def _guard_protected_set(set_id: str | None) -> None:
    """Refuse a mutation of a protected identity set unless the caller
    carries the identity-route capability."""
    if set_id in PROTECTED_IDENTITY_SET_IDS and not _identity_write_allowed.get():
        raise ProtectedSettingError(
            f"{set_id!r} is a protected dashboard-identity set; it can only "
            "be mutated through the identity/unlock routes, not the generic "
            "settings API"
        )


def _guard_protected_setting_id(setting_id: str, org: str | None) -> None:
    """Guard an id-addressed mutation: resolve the target row's set_id and
    refuse if it is a protected identity set (unless capability-carrying).
    A missing row is left to the function's own not-found handling."""
    if _identity_write_allowed.get():
        return
    db = _open_read(org)
    try:
        row = db.conn.execute(
            "SELECT set_id FROM settings WHERE id = ?", (setting_id,)
        ).fetchone()
    finally:
        db.close()
    if row is not None:
        _guard_protected_set(row["set_id"])


# ── org= argument contract (auto-cfb8u) ──────────────────────


class _CallerOrgSentinel:
    """Sentinel marker for "resolve org via the caller cascade."

    Pass :data:`CALLER_ORG` as ``org=`` to opt into the env-driven
    resolver — per-request contextvar → ``GRAPH_ORG`` env → scopeless
    default. Used by CLIs, env-pinned tests, and any other process-context
    caller. A literal ``None`` is treated as "scopeless explicit"; the
    sentinel is the only path that consults the cascade.
    """

    _instance: "_CallerOrgSentinel | None" = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<settings_ops.CALLER_ORG>"


CALLER_ORG = _CallerOrgSentinel()


def _resolve_org_arg(org: "str | None | _CallerOrgSentinel") -> str | None:
    """Collapse a public ``org=`` argument to a literal slug-or-None.

    * :data:`CALLER_ORG` → run :func:`_resolve_settings_caller`
      (contextvar → ``GRAPH_ORG`` env → ``None``).
    * Any other value (a string, or ``None``) is returned as-is.

    The point of separating "literal None" from "go consult the cascade"
    is to make the dashboard foot-gun (graph://53f7412f-51e) impossible:
    a writer that lands in the scopeless DB while the reader carries
    ``X-Graph-Org`` is now either a typo (None passed where the slug was
    intended) or a deliberate choice — never silent.
    """
    if isinstance(org, _CallerOrgSentinel):
        return _resolve_settings_caller(None)
    return org


# ── Emit hook (commit-then-emit) ─────────────────────────────


# Registered by the dashboard at lifespan startup so every Settings mutation
# — CLI, dashboard route, plugin, test fixture, future caller — fires a
# uniform ``setting.changed`` notification. CLI / test processes that don't
# import the dashboard register no hook; their writes commit normally and
# the hook is a no-op.
#
# Hook signature::
#
#     def hook(*, operation: str, snapshot: dict, org: str | None) -> None
#
# ``operation`` is one of: write, override, exclude, promote, deprecate,
# delete, migrate. ``snapshot`` is metadata-only: ``set_id``,
# ``schema_revision``, ``key``, ``publication_state``, ``deprecated``.
# Subscribers re-resolve via :func:`read_set` if they need payload data.
#
# Mutators call the hook AFTER ``db.conn.commit()`` returns — race-free
# observability for subscribers that resolve on receipt. Hooks are
# best-effort: exceptions are caught and logged; the mutator never blocks
# on hook completion.
_emit_hook: Callable[..., None] | None = None


def set_emit_hook(hook: Callable[..., None] | None) -> None:
    """Register (or clear with ``None``) the post-commit emit hook.

    See module-level commentary above the ``_emit_hook`` definition for
    the contract. Pattern mirrors the action-registry's services
    injection — the dashboard wires this at lifespan startup; CLI / test
    contexts that don't import the dashboard leave it unregistered.
    """
    global _emit_hook
    _emit_hook = hook


# ── The vault seam ──────────────────────────────────────────
#
# A set whose schema declares ``@vaulted(...)`` does not store plaintext.
# Organization objects use their storage generation and an opaque locator
# (``tools.vault.storage_object``, design ``graph://0c206bd8-1c6`` §9).
# Personal secured objects have no organization membership layer: their
# opaque scalar directly carries ciphertext plus a CEK sealed to the owner's
# policy class (``tools.vault.personal_object``).
#
# Organization-domain sealing needs key control — the writer's persona, the
# authority fold at its cited frontier, held state secrets and the content
# store — none of which a settings write should acquire for itself. That
# organization sealer is injected exactly as the emit hook is. The personal
# secured branch above needs only its stored public class record.
#
# Sealer signature::
#
#     def sealer(*, set_id: str, schema_revision: int, key: str,
#                setting_id: str, payload, tier: str, org: str | None) -> str
#
# It returns the locator scalar to store in place of the payload. UNLIKE the
# emit hook it is NOT best-effort: it runs BEFORE the insert, and anything it
# raises aborts the write. A vaulted set with no sealer registered is refused
# for the same reason — the alternative to encrypting a credential is not
# writing it in the clear.
_vault_sealer: Callable[..., str] | None = None


def set_vault_sealer(sealer: Callable[..., str] | None) -> None:
    """Register (or clear with ``None``) the vault sealer.

    See the commentary above ``_vault_sealer`` for the contract. Returns
    nothing; the previously registered sealer is discarded.
    """
    global _vault_sealer
    _vault_sealer = sealer


def _seal_vault_payload(
    *,
    set_id: str,
    schema_revision: int,
    key: str,
    setting_id: str,
    payload: dict,
    tier: str,
    org: str | None,
    policy_class_id: str | None = None,
) -> str:
    """The locator this row stores in place of *payload*.

    Fails closed on every path: no sealer, a sealer that raises, or a sealer
    returning anything but a locator all abort the write with the plaintext
    unwritten. There is deliberately no branch here that stores *payload*.
    """
    # Imported here rather than at module scope: settings_ops is imported by
    # every CLI entry point, and the vault pulls in the whole storage stack.
    from tools.vault import personal_object, storage_object as vault_storage_object

    # A personal secured Setting is owner-at-rest data, not an organization
    # storage-domain object.  Its only encryption layers are a fresh CEK and
    # the named policy class's public sealing key.  In particular this path
    # does not consult the process key cache, a delegate, or a ledger fold, so
    # it remains writable when the vault is cold.
    if schemas.declared_home(set_id) == "personal" and tier == "secured":
        if not isinstance(policy_class_id, str) or not policy_class_id:
            raise VaultSealerMissing(
                "a personal secured Setting must name its policy class"
            )
        from tools.graph.schemas.vault_policy_class import (
            VAULT_POLICY_CLASS_SET_ID,
        )
        from tools.vault.key_holder import _scoped_db
        from tools.vault.policy_class import enable_public_sealing
        from tools.vault.store import VaultStore

        class_db = _scoped_db(VAULT_POLICY_CLASS_SET_ID, org)
        with VaultStore(class_db) as class_store:
            policy_class = class_store.get_class(policy_class_id)
            if policy_class.current().sealing_public_key is None:
                policy_class = enable_public_sealing(
                    policy_class, created_at=_now_iso(),
                )
                class_store.put_class(policy_class)
        return personal_object.seal_revision(
            set_id=set_id,
            key=key,
            setting_id=setting_id,
            payload=payload,
            policy_class=policy_class,
        )

    # A personal AUDITED Setting is cold-writable owner-at-rest data: its CEK
    # seals to the published delegate public key with no factor, no policy class,
    # and no consultation of the process key cache or a delegate — so it remains
    # writable when the vault is cold. The delegate's private half, warm at
    # unlock, opens it unattended on read.
    if schemas.declared_home(set_id) == "personal" and tier == "audited":
        from tools.graph.schemas.vault_policy_class import (
            VAULT_POLICY_CLASS_SET_ID,
        )
        from tools.vault.key_holder import _scoped_db
        from tools.vault.store import VaultStore

        with VaultStore(_scoped_db(VAULT_POLICY_CLASS_SET_ID, org)) as store:
            delegate_public_hex = store.get_delegate_audited_recipient()
        return personal_object.seal_audited_revision(
            set_id=set_id,
            key=key,
            setting_id=setting_id,
            payload=payload,
            delegate_public_hex=delegate_public_hex,
        )

    sealer = _vault_sealer
    if sealer is None:
        raise VaultSealerMissing(
            "The vault is locked and must be warmed by the operator "
            "(sign-in or warm client) before a secret can be written."
        )
    sealer_args = dict(
        set_id=set_id,
        schema_revision=int(schema_revision),
        key=key,
        setting_id=setting_id,
        payload=payload,
        tier=tier,
        org=org,
    )
    if policy_class_id is not None:
        sealer_args["policy_class_id"] = policy_class_id
    locator = sealer(**sealer_args)
    if not (
        vault_storage_object.is_vault_locator(locator)
        or personal_object.is_personal_locator(locator)
    ):
        raise VaultSealerMissing(
            f"the vault sealer returned {type(locator).__name__}, not a "
            f"locator; refusing to store it as {set_id}/{key}"
        )
    return locator


# ── The other half of the seam: what OPENS a vault setting ───
#
# Reading is the mirror of writing. An organization locator needs held
# generation keys and its content store, injected by the host process. A
# personal secured scalar can expose its still-wrapped CEK without either;
# only the later human-factor chokepoint can turn that view into plaintext.
#
# Key-holder signature::
#
#     def holder(*, set_id: str, org: str | None) -> VaultKeyControl | None
#
# It is consulted at most ONCE per originating org per :func:`read_set`, and
# only when a vault row is actually in the result — a set nobody vaulted never
# touches it. Returning ``None`` says this process holds no key control for
# that organization, which resolves its secrets to ``VAULT_NO_KEY_HOLDER``.
#
# UNLIKE the sealer this one does not abort the call. A write with nowhere to
# encrypt to must not happen at all; a read of five settings where one cannot
# be opened must still answer for the other four.
_vault_key_holder: Callable[..., Any] | None = None


@dataclass(frozen=True)
class VaultKeyControl:
    """What opening a vault setting takes, as one value.

    ``holdings`` is a ``tools.vault.storage_object.Holdings`` — the state
    secrets this process holds plus the public descriptors and bridges it has
    seen. ``content_store`` is the object store the locators address.

    Declared here rather than imported from the vault so that this module
    depends on the INTERFACE it needs and not on whatever fills it: the cache
    that eventually holds the real keys implements this shape, and until it
    exists a stub does.
    """

    holdings: Any
    content_store: Any


def set_vault_key_holder(holder: Callable[..., Any] | None) -> None:
    """Register (or clear with ``None``) the vault key holder.

    See the commentary above ``_vault_key_holder`` for the contract. Returns
    nothing; the previously registered holder is discarded.
    """
    global _vault_key_holder
    _vault_key_holder = holder


# The warm audited delegate private key (64-hex X25519), set at unlock and
# cleared at lock. A personal AUDITED read opens its CEK with this; when it is
# None the vault is cold and the read fails closed as VAULT_NO_KEY_HOLDER — never
# plaintext. The public half lives in the vault store so the WRITE stays cold;
# only the READ needs this warm private half.
_personal_delegate_audited_key: str | None = None


def set_personal_delegate_audited_key(private_hex: "str | None") -> None:
    """Register (or clear with ``None``) the warm audited delegate private key."""
    global _personal_delegate_audited_key
    _personal_delegate_audited_key = private_hex


def _make_snapshot(
    set_id: str,
    schema_revision: int,
    key: str,
    publication_state: str,
    deprecated: bool,
) -> dict:
    return {
        "set_id": set_id,
        "schema_revision": int(schema_revision),
        "key": key,
        "publication_state": publication_state,
        "deprecated": bool(deprecated),
    }


def _call_emit_hook(
    *,
    operation: str,
    snapshot: dict,
    org: str | None,
) -> None:
    """Invoke the registered hook (best-effort).

    Mutators call this AFTER their transaction commits and the DB
    handle is closed, so a subscriber that re-resolves via ``read_set``
    on receipt sees the new row. Exceptions are swallowed at WARN — a
    broken subscriber must not corrupt write semantics.
    """
    hook = _emit_hook
    if hook is None:
        return
    try:
        hook(operation=operation, snapshot=snapshot, org=org)
    except Exception:
        logger.warning(
            "settings_ops emit hook raised on %s for set_id=%s key=%s",
            operation, snapshot.get("set_id"), snapshot.get("key"),
            exc_info=True,
        )


# ── Throughput stats ────────────────────────────────────────


_DEFAULT_SCOPE_LABEL = "(default)"
_STATS_BURST_WINDOW_SECONDS = 10
_STATS_RECENT_WINDOW_SECONDS = 60
_STATS_RETENTION_SECONDS = 300
_STATS_TOP_LIMIT = 10


@dataclass
class _SettingsStatsRollup:
    calls: int = 0
    reads: int = 0
    writes: int = 0
    errors: int = 0
    result_count: int = 0
    total_duration_ms: float = 0.0
    latency_ms: Counter[int] = dataclass_field(default_factory=Counter)
    operations: Counter[str] = dataclass_field(default_factory=Counter)
    set_ids: Counter[str] = dataclass_field(default_factory=Counter)
    set_reads: Counter[str] = dataclass_field(default_factory=Counter)
    set_writes: Counter[str] = dataclass_field(default_factory=Counter)
    set_upserts: Counter[str] = dataclass_field(default_factory=Counter)
    set_org_calls: Counter[tuple[str, str]] = dataclass_field(default_factory=Counter)
    set_org_reads: Counter[tuple[str, str]] = dataclass_field(default_factory=Counter)
    set_org_writes: Counter[tuple[str, str]] = dataclass_field(default_factory=Counter)
    set_org_upserts: Counter[tuple[str, str]] = dataclass_field(default_factory=Counter)
    orgs: Counter[str] = dataclass_field(default_factory=Counter)


@dataclass
class _SettingsStatsBucket(_SettingsStatsRollup):
    second: int = 0


class SettingsApiStats:
    """Cheap process-local counters for Settings throughput visibility."""

    burst_window_seconds = _STATS_BURST_WINDOW_SECONDS
    recent_window_seconds = _STATS_RECENT_WINDOW_SECONDS
    retention_seconds = _STATS_RETENTION_SECONDS

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        now = time.time()
        with self._lock:
            self._started_at = now
            self._totals = _SettingsStatsRollup()
            self._buckets: dict[int, _SettingsStatsBucket] = {}
            self._last_call: dict[str, Any] | None = None
            self._last_error: dict[str, Any] | None = None

    def record(
        self,
        *,
        operation: str,
        set_id: str | None,
        org: str | None,
        kind: str,
        ok: bool,
        duration_ms: float,
        result_count: int = 0,
    ) -> None:
        now = time.time()
        second = int(now)
        scope = self._scope_label(org)
        count = max(int(result_count), 0)
        call = {
            "ts": now,
            "operation": operation,
            "set_id": set_id,
            "org": scope,
            "kind": kind,
            "ok": bool(ok),
            "duration_ms": round(float(duration_ms), 3),
            "result_count": count,
        }
        with self._lock:
            self._sweep_locked(now)
            bucket = self._buckets.get(second)
            if bucket is None:
                bucket = _SettingsStatsBucket(second=second)
                self._buckets[second] = bucket
            self._apply_rollup(
                self._totals,
                operation=operation,
                set_id=set_id,
                org=scope,
                kind=kind,
                ok=ok,
                duration_ms=duration_ms,
                result_count=count,
            )
            self._apply_rollup(
                bucket,
                operation=operation,
                set_id=set_id,
                org=scope,
                kind=kind,
                ok=ok,
                duration_ms=duration_ms,
                result_count=count,
            )
            self._last_call = call
            if not ok:
                self._last_error = dict(call)

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._sweep_locked(now)
            burst = self._aggregate_locked(self.burst_window_seconds, now)
            recent = self._aggregate_locked(self.recent_window_seconds, now)
            return {
                "started_at": self._started_at,
                "now": now,
                "burst_window_seconds": self.burst_window_seconds,
                "recent_window_seconds": self.recent_window_seconds,
                "retention_seconds": self.retention_seconds,
                "last_call": dict(self._last_call) if self._last_call else None,
                "last_error": dict(self._last_error) if self._last_error else None,
                "totals": self._render_rollup(self._totals, window_seconds=None),
                f"last_{self.burst_window_seconds}s": self._render_rollup(
                    burst, window_seconds=self.burst_window_seconds,
                ),
                f"last_{self.recent_window_seconds}s": self._render_rollup(
                    recent, window_seconds=self.recent_window_seconds,
                ),
            }

    def set_metrics_snapshot(self, *, org: str | None = None) -> dict[str, Any]:
        now = time.time()
        scope = self._scope_label(org) if org is not None else None
        with self._lock:
            self._sweep_locked(now)
            burst = self._aggregate_locked(self.burst_window_seconds, now)
            recent = self._aggregate_locked(self.recent_window_seconds, now)
            return {
                "totals": self._render_set_metrics(self._totals, scope=scope),
                f"last_{self.burst_window_seconds}s": self._render_set_metrics(
                    burst, scope=scope,
                ),
                f"last_{self.recent_window_seconds}s": self._render_set_metrics(
                    recent, scope=scope,
                ),
            }

    @staticmethod
    def _scope_label(org: str | None) -> str:
        return str(org) if org else _DEFAULT_SCOPE_LABEL

    @staticmethod
    def _apply_rollup(
        rollup: _SettingsStatsRollup,
        *,
        operation: str,
        set_id: str | None,
        org: str,
        kind: str,
        ok: bool,
        duration_ms: float,
        result_count: int,
    ) -> None:
        rounded_duration_ms = max(int(round(duration_ms)), 0)
        rollup.calls += 1
        if kind == "read":
            rollup.reads += 1
        elif kind == "write":
            rollup.writes += 1
        if not ok:
            rollup.errors += 1
        rollup.result_count += result_count
        rollup.total_duration_ms += float(duration_ms)
        rollup.latency_ms[rounded_duration_ms] += 1
        rollup.operations[operation] += 1
        rollup.orgs[org] += 1
        if set_id:
            rollup.set_ids[set_id] += 1
            rollup.set_org_calls[(org, set_id)] += 1
            if kind == "read":
                rollup.set_reads[set_id] += 1
                rollup.set_org_reads[(org, set_id)] += 1
            elif kind == "write":
                rollup.set_writes[set_id] += 1
                rollup.set_org_writes[(org, set_id)] += 1
                if operation == "upsert_by_key":
                    rollup.set_upserts[set_id] += 1
                    rollup.set_org_upserts[(org, set_id)] += 1

    def _sweep_locked(self, now: float) -> None:
        cutoff = now - self.retention_seconds
        stale = [second for second in self._buckets if second < cutoff]
        for second in stale:
            self._buckets.pop(second, None)

    def _aggregate_locked(
        self, window_seconds: int, now: float,
    ) -> _SettingsStatsRollup:
        out = _SettingsStatsRollup()
        for second, bucket in self._buckets.items():
            if now - second >= window_seconds:
                continue
            out.calls += bucket.calls
            out.reads += bucket.reads
            out.writes += bucket.writes
            out.errors += bucket.errors
            out.result_count += bucket.result_count
            out.total_duration_ms += bucket.total_duration_ms
            out.latency_ms.update(bucket.latency_ms)
            out.operations.update(bucket.operations)
            out.set_ids.update(bucket.set_ids)
            out.set_reads.update(bucket.set_reads)
            out.set_writes.update(bucket.set_writes)
            out.set_upserts.update(bucket.set_upserts)
            out.set_org_calls.update(bucket.set_org_calls)
            out.set_org_reads.update(bucket.set_org_reads)
            out.set_org_writes.update(bucket.set_org_writes)
            out.set_org_upserts.update(bucket.set_org_upserts)
            out.orgs.update(bucket.orgs)
        return out

    @staticmethod
    def _percentile_ms(
        latency_ms: Counter[int], total_calls: int, percentile: float,
    ) -> int | None:
        if total_calls <= 0:
            return None
        threshold = max(int(math.ceil(total_calls * percentile)), 1)
        seen = 0
        for duration_ms in sorted(latency_ms):
            seen += latency_ms[duration_ms]
            if seen >= threshold:
                return duration_ms
        return max(latency_ms) if latency_ms else None

    def _render_rollup(
        self,
        rollup: _SettingsStatsRollup,
        *,
        window_seconds: int | None,
    ) -> dict[str, Any]:
        calls = rollup.calls
        total_ms = round(rollup.total_duration_ms, 3)
        out = {
            "calls": calls,
            "reads": rollup.reads,
            "writes": rollup.writes,
            "errors": rollup.errors,
            "result_count": rollup.result_count,
            "total_duration_ms": total_ms,
            "avg_duration_ms": round(
                (rollup.total_duration_ms / calls), 3,
            ) if calls else 0.0,
            "latency_ms": {
                "p50": self._percentile_ms(rollup.latency_ms, calls, 0.50),
                "p95": self._percentile_ms(rollup.latency_ms, calls, 0.95),
                "p99": self._percentile_ms(rollup.latency_ms, calls, 0.99),
            },
            "operations": {
                key: rollup.operations[key]
                for key in sorted(rollup.operations)
            },
            "top_sets": [
                {
                    "set_id": set_id,
                    "calls": count,
                    "reads": rollup.set_reads.get(set_id, 0),
                    "writes": rollup.set_writes.get(set_id, 0),
                    "upserts": rollup.set_upserts.get(set_id, 0),
                }
                for set_id, count in rollup.set_ids.most_common(
                    _STATS_TOP_LIMIT,
                )
            ],
            "top_orgs": [
                {"org": org, "calls": count}
                for org, count in rollup.orgs.most_common(_STATS_TOP_LIMIT)
            ],
        }
        if window_seconds is not None:
            out["calls_per_second"] = round(
                calls / float(window_seconds), 3,
            )
        return out

    @staticmethod
    def _render_set_metrics(
        rollup: _SettingsStatsRollup,
        *,
        scope: str | None,
    ) -> dict[str, dict[str, int]]:
        if scope is None:
            set_ids = sorted(rollup.set_ids)
            return {
                set_id: {
                    "calls": rollup.set_ids.get(set_id, 0),
                    "reads": rollup.set_reads.get(set_id, 0),
                    "writes": rollup.set_writes.get(set_id, 0),
                    "upserts": rollup.set_upserts.get(set_id, 0),
                }
                for set_id in set_ids
            }
        set_ids = sorted({
            set_id for (org_name, set_id) in rollup.set_org_calls
            if org_name == scope
        })
        return {
            set_id: {
                "calls": rollup.set_org_calls.get((scope, set_id), 0),
                "reads": rollup.set_org_reads.get((scope, set_id), 0),
                "writes": rollup.set_org_writes.get((scope, set_id), 0),
                "upserts": rollup.set_org_upserts.get((scope, set_id), 0),
            }
            for set_id in set_ids
        }


_SETTINGS_API_STATS = SettingsApiStats()


def settings_api_stats_snapshot() -> dict[str, Any]:
    return _SETTINGS_API_STATS.snapshot()


def settings_api_set_metrics_snapshot(
    *,
    org: str | None = None,
) -> dict[str, Any]:
    return _SETTINGS_API_STATS.set_metrics_snapshot(org=org)


def reset_settings_api_stats() -> None:
    _SETTINGS_API_STATS.reset()


def _record_settings_api_call(
    *,
    operation: str,
    set_id: str | None,
    org: str | None,
    kind: str,
    ok: bool,
    start: float,
    result_count: int = 0,
) -> None:
    _SETTINGS_API_STATS.record(
        operation=operation,
        set_id=set_id,
        org=org,
        kind=kind,
        ok=ok,
        duration_ms=(time.perf_counter() - start) * 1000.0,
        result_count=result_count,
    )


def _stats_result_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, list):
        return len(value)
    members = getattr(value, "members", None)
    if isinstance(members, list):
        return len(members)
    if isinstance(value, dict):
        layers = value.get("layers")
        if isinstance(layers, list):
            return len(layers)
    return 1


# ── Result types ─────────────────────────────────────────────


T = TypeVar("T")


@dataclass
class ResolvedSetting(Generic[T]):
    """A Setting after resolution.

    ``target_revision`` is ``None`` when returned at its stored revision
    (the default); otherwise it carries the revision the payload was
    reshaped to. ``upconverted`` records whether the payload was rewritten
    by the upconvert chain (True) or returned at its stored shape (False).
    ``org`` is the originating DB's org slug — ``None`` in single-DB mode.

    ``payload`` is typed on the ``T`` parameter: a bare ``dict`` when no
    ``model=`` was supplied to :func:`read_set` / :func:`get_setting`; a
    validated Pydantic (or compatible) instance when a model was supplied.

    ``vault_error`` and ``sealed_content_key`` are the two outcomes of a vault
    set's member that is not a plaintext payload. Both are ``None`` on every
    ordinary setting AND on a vault secret that opened, and both are OMITTED
    from the serialized shape when they are — a member that resolved carries
    no trace of whether it was encrypted, which is what makes the vault
    transparent to a caller rather than a mode it has to know about.
    """
    id: str
    set_id: str
    stored_revision: int
    key: str
    payload: T
    state: str
    supersedes: str | None
    excludes: str | None
    deprecated: bool
    successor_id: str | None
    created_at: str
    updated_at: str
    target_revision: int | None = None
    org: str | None = None
    upconverted: bool = False
    vault_error: VaultReadFailure | None = None
    sealed_content_key: dict | None = None

    def to_dict(self) -> dict:
        """Serialize the Setting as a dict (payload included as-is)."""
        d = asdict(self)
        return _strip_absent_vault_fields(d)


@dataclass
class DropAccounting:
    """Per-query row-drop counts from :func:`read_set` / :func:`get_setting`.

    Every field is a non-negative count of rows eliminated during
    resolution. ``schema_invalid`` covers the ``model=`` validation path —
    rows whose stored payload fails ``model_validate`` are dropped and
    logged at WARN rather than crashing the query.
    """
    below_min_revision: int = 0
    no_upconvert_path: int = 0
    above_target_no_downgrade: int = 0
    schema_invalid: int = 0
    deprecated_filtered: int = 0
    #: Signed slots whose terminal persona is not in the owning org's
    #: current fold — a departed member's rows, dropped at eligibility.
    ineligible_signer: int = 0
    #: Signed slots whose ``signed_at`` is beyond the reader's plausibility
    #: window — stored and ignored until the clock reaches them.
    beyond_window: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    def values(self):
        """Back-compat: callers that tested ``any(dropped.values())``."""
        return self.to_dict().values()

    def items(self):
        """Back-compat: callers that iterated ``dict(dropped).items()``."""
        return self.to_dict().items()

    def __iter__(self):
        return iter(self.to_dict())

    def __getitem__(self, key: str) -> int:
        """Back-compat: callers that accessed ``dropped["below_min_revision"]``."""
        return getattr(self, key)


@dataclass
class SetMembers(Generic[T]):
    """Result of :func:`read_set`: resolved members + drop accounting.

    Iterable and sized over the resolved members; :meth:`to_dict` returns
    a ``{setting.key: ResolvedSetting}`` mapping since supersedes
    resolution has already deduped to one winner per key. Use
    :meth:`as_payload` for the JSON-serializable shape expected by the
    dashboard Settings API.
    """
    members: list[ResolvedSetting[T]]
    dropped: DropAccounting = dataclass_field(default_factory=DropAccounting)

    def __iter__(self) -> Iterator[ResolvedSetting[T]]:
        return iter(self.members)

    def __len__(self) -> int:
        return len(self.members)

    def to_dict(self) -> dict[str, ResolvedSetting[T]]:
        """Map keyed by resolved ``Setting.key``. Supersedes resolution has
        already deduped — one winning Setting per key.
        """
        return {rs.key: rs for rs in self.members}

    def as_payload(self) -> dict:
        """JSON-serializable shape: ``{"members": [...], "dropped": {...}}``.

        Used by the dashboard Settings API endpoints that send the full
        query result over the wire. ``to_dict()`` is reserved for the
        key-indexed consumer view.
        """
        return {
            "members": [_serialize_member(m) for m in self.members],
            "dropped": self.dropped.to_dict(),
        }


def _strip_absent_vault_fields(d: dict) -> dict:
    """Drop the vault fields when they say nothing.

    A non-vault set must serialize byte-identically to how it did before the
    vault existed, and a vault secret that opened must be indistinguishable
    from an ordinary one. Both hold exactly when the two fields are absent
    rather than present-and-null.
    """
    for name in ("vault_error", "sealed_content_key"):
        if d.get(name) is None:
            d.pop(name, None)
    return d


def _serialize_member(m: ResolvedSetting) -> dict:
    """Render a :class:`ResolvedSetting` as a JSON-friendly dict.

    Pydantic payloads are dumped via ``model_dump``; plain dicts pass
    through unchanged.
    """
    d = dict(m.__dict__)
    payload = d.get("payload")
    if hasattr(payload, "model_dump"):
        d["payload"] = payload.model_dump()
    if isinstance(d.get("vault_error"), VaultReadFailure):
        d["vault_error"] = d["vault_error"].to_dict()
    return _strip_absent_vault_fields(d)


@dataclass
class MigrationReport:
    """Result of ``migrate_setting_revisions``."""
    set_id: str
    to_revision: int
    dry_run: bool
    rewrote: int = 0
    no_upconvert_path: int = 0
    already_at_target: int = 0
    above_target: int = 0
    affected_ids: list[str] = dataclass_field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ── DB selection (mirrors ops._open) ─────────────────────────


# The destinations a caller does not provision first: saying nothing, naming
# the operator's own store, and naming this machine's own store. Nobody
# provisions any of them -- they come into being where they are used.
_CREATED_ON_DEMAND: frozenset[str | None] = frozenset(
    {None, "personal", "machine"})


def _db_path(org: str | None) -> str | None:
    """Resolve Settings DB path for a literal ``org`` value.

    ``org`` is the post-:func:`_resolve_org_arg` value: a slug (route to
    that org's DB) or ``None`` (the personal DB). No env-cascade here —
    public callers pre-resolve via :data:`CALLER_ORG` if they want it.
    ``GRAPH_DB`` env pins the path regardless (test override).

    ``None`` is deliberately special-cased instead of flowing through
    :func:`resolve_caller_db_path`: that general resolver retains a legacy
    ``data/graph.db`` fallback, but Settings stored without an org include
    the operator's personal identity. Their destination must not depend on
    whether dashboard startup happened to materialize ``personal.db`` yet.
    """
    env_db = os.environ.get("GRAPH_DB")
    if env_db and org is None:
        # The pin only applies to the org-less (personal) resolution — its
        # sole legitimate callers (test suite, ``graph --db``, multi-node
        # harness) all pass ``org=None`` (23d9m docstring). An EXPLICIT org
        # must never be silently misrouted into the pinned store: fall
        # through to ``resolve_caller_db_path``, which resolves to the org's
        # own DB or raises ``OrgResolutionConflict`` under a contradicting
        # pin, rather than honouring the pin blind.
        return env_db
    if org == "machine":
        # This machine's own store. Never replicated, so it is the one
        # destination a fleet sync can ignore wholesale rather than by
        # inspecting rows.
        machine_path = _org_db_path("machine")
        with _personal_db_init_lock:
            if not machine_path.exists():
                # Schema only. ``create_org_db`` would also insert the
                # identifying ``orgs`` row, and ``list_orgs`` lists every store
                # that has one — which is how this store reached the org
                # selector on nodes built after it started being created here.
                GraphDB(machine_path).close()
        return str(machine_path)
    if org is None:
        personal_path = _org_db_path("personal")
        with _personal_db_init_lock:
            if not personal_path.exists():
                try:
                    GraphDB.create_org_db(
                        "personal",
                        type_="personal",
                        path=personal_path,
                    ).close()
                except FileExistsError:
                    # Another process won the guarded first-open race.
                    pass
        return str(personal_path)
    # A named org resolves to that org's database, or to nothing.
    # ``resolve_caller_db_path`` keeps a pre-migration fallback to the legacy
    # single store when the per-org file is absent, which is right for reading
    # an installation that has not been migrated and wrong for a Setting: the
    # write reports success against a database the caller did not name and
    # nobody looks in for that org's settings. Going straight to the org's own
    # path makes the absence an error that names the file.
    #
    # The pinned case still goes through the resolver, which refuses a
    # ``GRAPH_DB`` that contradicts an explicit org rather than discarding it.
    if os.environ.get("GRAPH_DB"):
        return str(resolve_caller_db_path(org))
    return str(_org_db_path(org))


def unresolved_references(
    set_id: str,
    revision: int,
    payload: dict,
    *,
    org: str,
) -> list[tuple[str, str]]:
    """Declared references in *payload* whose target does not exist yet.

    A field that declares ``references`` says its value is a KEY in another
    set. That declaration is the whole input: this walks field metadata and
    knows nothing about what either set means, so a capability does not
    implement its own completeness check and cannot forget to.

    Returns ``(set_id, key)`` pairs for targets that are absent. Reporting
    rather than refusing is deliberate -- a row may legitimately be written
    before the credential it names is provisioned, and the useful thing is to
    say which key is still needed, by name.
    """
    out: list[tuple[str, str]] = []

    def walk(schema: Any, value: Any) -> None:
        meta = getattr(schema, "_field_metadata", None) or {}
        if not isinstance(value, dict):
            return
        for name, spec in meta.items():
            item = value.get(name)
            if item is None:
                continue
            element = (getattr(schema, "_element_schemas", None) or {}).get(name)
            if element is not None and isinstance(item, list):
                for entry in item:
                    walk(element, entry)
            target = spec.get("references")
            if not target:
                continue
            single = not isinstance(item, list)
            for one in (item if isinstance(item, list) else [item]):
                if not isinstance(one, str) or not one:
                    continue
                if not any(schemas.get_schema(target, r) for r in range(1, 12)):
                    # The edge itself is wrong, or its module is not imported
                    # here. Either way this is not a key anyone can provision,
                    # and reporting it as one sends a writer somewhere with no
                    # exit -- a write to an unregistered schema is refused.
                    out.append((target, "<no schema registered for this set>"))
                    continue
                scoped = spec.get("reference_scope") == "org"
                key = f"{org}:{one}" if scoped else one
                read_org, peers, _frame = _existence_frame(
                    target, org, org_scoped=scoped)
                if read_set_key(target, key, org=read_org, peers=peers) is None:
                    out.append((target, key))

    schema = schemas.get_schema(set_id, int(revision))
    if schema is not None:
        walk(schema, payload)
    return out


def _owned_but_unreadable(
    set_id: str, key: str, reader: str,
) -> tuple[str, str] | None:
    """Is this row written somewhere the reader cannot see it?

    "Nobody has written this" and "somebody has, and you may not read it" are
    both reported by a plain read as nothing, and they call for opposite
    repairs: write the row, or publish the one that exists. Told the first
    when the second is true, a reader writes a second row, and the two then
    disagree with no way to tell which one anything used.

    Returns the owning organization and the state its row is in, or ``None``
    when the row genuinely is not there. Asked only once a read has already
    failed, so the cost falls on the reporting path and never on resolution.
    """
    from tools.graph.cross_org import list_org_slugs

    try:
        slugs = list_org_slugs()
    except Exception:
        return None
    for slug in slugs:
        if slug == reader or slug in ("personal", "machine"):
            continue
        try:
            found = read_set_key(set_id, key, org=slug, peers=[])
        except Exception:
            continue
        if found is not None:
            return slug, str(found.get("state") or "raw")
    return None


@dataclass
class CheckFinding:
    """One thing that is not satisfied, and where the checker looked."""
    address: str          # set_id key=<key> in <org>
    kind: str             # "missing_reference" | "missing_path" | "unreadable"
    detail: str
    looked_in: str        # the frame the answer came from
    #: Whether this stops the thing from running, or only degrades it.
    #: Blocking by default: a checker that guesses "advisory" when nobody
    #: said so reports a launch as ready and is wrong in the direction
    #: nobody re-examines. Advisory is declared, never inferred.
    severity: str = "blocking"

    # ── The same finding as DATA ──────────────────────────────
    #
    # ``detail`` and ``looked_in`` above are rendered English. A consumer
    # given only those has to parse a sentence to recover what the finding
    # is about -- a readiness UI built against this had to regex the quoted
    # path back out of ``detail`` to put it in a heading. That is the tell
    # that the wrong thing is being transported: the checker HELD every one
    # of these values and threw them away to build a string.
    #
    # So the fields below carry them. ``detail`` stays, because the CLI
    # prints it and a sentence is the right thing THERE; it is no longer the
    # only way to reach the facts.
    set_id: str = ""
    key: str = ""
    org: str = ""
    #: The declaring field that produced this, e.g. ``host_path``. Empty when
    #: the finding is about the row itself rather than one of its fields.
    field: str = ""
    #: What is missing: the path, the variable name, the referenced key.
    subject: str = ""
    #: Where the question was answered, as an identifier rather than a
    #: sentence, so a consumer can branch on it.
    frame: str = ""
    #: Straight off the declaration, when there is one to read. These are
    #: what let a reader see WHAT the thing is instead of only where it is
    #: not -- ``help`` especially, which is the only part that says what to
    #: do about it.
    name: str = ""
    description: str = ""
    help: str = ""
    expects: str = ""     # "file" | "dir", when the declaration says
    #: The schema's description of ``field`` -- what this KIND of thing is,
    #: as opposed to what this PARTICULAR one is.
    field_description: str = ""
    #: Data-only pointer into the trusted remediation registry. Parameters are
    #: schema-authored JSON scalars, never collected input or secret values.
    remediation_id: str = ""
    remediation_params: dict[str, Any] = dataclass_field(default_factory=dict)


@dataclass
class CheckPassed:
    """One readiness predicate that was actually answered and satisfied.

    A clean findings list says only that no failure was emitted. Carrying
    positive evidence separately lets API and CLI callers show what was
    checked without weakening ``check_setting() == []`` or leaking values.
    """

    kind: str
    detail: str
    set_id: str
    key: str
    org: str
    subject: str
    field: str = ""
    frame: str = ""
    name: str = ""
    description: str = ""
    field_description: str = ""


def _existence_frame(
    target: str, org: str, *, org_scoped: bool = False,
) -> tuple[str, list[str] | None, str]:
    """Where to look for ``target``, and the words for it.

    Existence is asked with the visibility the consumer of that row has, which
    for an organization's set includes its subscribed peers: a workspace one
    organization owns is genuinely present for another that can read it, and
    answering from the owning database alone reports rows as dangling while
    the software that uses them resolves them without difficulty.

    A store that follows the operator or belongs to this machine is single --
    there is no peer to consult -- so the read is confined to it, and the
    returned frame says which one, because "not found" means something
    different in each.

    ``org_scoped`` is the referring field's declaration that it names a key
    carrying the organization. A key that repeats the organization is a key in
    a store holding several of them, which is the operator's own -- an
    organization's own database has no reason to say which one it is. Where
    the target declares a home, that is the answer and this does not arise.

    Every caller decides here. Two callers deciding separately is how the same
    row came to be looked for in two different databases, with each answer
    reported as though it settled the question.
    """
    home = schemas.declared_home(target)
    if home in ("machine", "personal"):
        return home, [], home
    if home is None and org_scoped:
        return "personal", [], "the operator's own store"
    return org, None, f"{org} and its peers"


def rows_keyed_by(
    entity_set_id: str,
    entity_key: str,
    *,
    org: str,
) -> list[tuple[str, str, str]]:
    """Every row whose KEY identifies ``entity_key`` in ``entity_set_id``.

    Reads the key segments schemas declare with ``key_references`` and
    returns each row whose segment holds this entity's key, as
    ``(set_id, key, segment_name)``.

    Metadata-driven: it names no set and no entity, so a schema that
    declares a key reference becomes traversable without touching this.
    """
    import re as _re

    out: list[tuple[str, str, str]] = []
    for set_id in schemas.list_registered_set_ids():
        schema = next((schemas.get_schema(set_id, r)
                       for r in range(1, 12) if schemas.get_schema(set_id, r)), None)
        refs = getattr(schema, "_key_references", None) or {}
        wanted = [seg for seg, target in refs.items() if target == entity_set_id]
        if not wanted:
            continue
        strategy = getattr(schema, "_key_strategy", "") or ""
        segments = [seg.strip("[]") for seg in _re.split(r"[:/]", strategy)]
        home = None
        try:
            home = schemas.declared_home(set_id)
        except Exception:
            pass
        read_org = home if home in ("machine", "personal") else org
        try:
            members = read_set(set_id, org=read_org, peers=[])
        except Exception:
            continue
        for member in members.members:
            parts = str(member.key).split(":")
            for segment in wanted:
                index = segments.index(segment)
                if index < len(parts) and parts[index] == entity_key:
                    out.append((set_id, member.key, segment))
    return out


def _running_in_a_container() -> bool:
    """Whether this process is inside a container rather than on the host.

    Only ever used to decide whether a question about the HOST's filesystem
    can be answered from here, and it fails toward answering: if this cannot
    tell, the check behaves exactly as it did before. A false positive costs
    a finding that says "ask on the host"; a false negative costs nothing
    that was not already the case.

    Overridable so a test can exercise both frames without a container, and
    so an operator running the platform in an unusual arrangement can say so
    rather than argue with a heuristic.
    """
    forced = os.environ.get("AUTONOMY_CONTAINER")
    if forced is not None:
        return forced.strip().lower() in ("1", "true", "yes")
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", encoding="utf-8", errors="replace") as fh:
            marker = fh.read()
    except OSError:
        return False
    return any(s in marker for s in ("docker", "containerd", "kubepods", "lxc"))


def check_setting(
    set_id: str,
    key: str,
    *,
    org: str,
    _seen: set | None = None,
    _org_scoped: bool = False,
    _via_field: str = "",
    _via_description: str = "",
    _via_remediation_id: str = "",
    _via_remediation_params: dict[str, Any] | None = None,
    _passed: list[CheckPassed] | None = None,
    _include_dependents: bool = True,
) -> list[CheckFinding]:
    """Is this row satisfied, and everything it declares it depends on?

    Metadata-driven. It follows fields that declare ``references``, asks
    fields that declare ``exists`` or ``names_host_env``, walks declared key
    references, and invokes a schema's optional ``readiness_findings`` hook
    for cross-field or cross-row constraints. The engine contains no
    set-specific rule; each schema declares the edges it owns.

    The traversal reaches exactly as far as those declarations go. An
    undeclared relationship is invisible to it, which is the honest limit:
    it will not silently invent an edge nobody stated.

    Every finding records the frame it was answered in, because "not found"
    from a process that cannot see a filesystem is a different fact from
    "not found" on the machine that owns it.
    """
    import os as _os

    seen = _seen if _seen is not None else set()
    if (set_id, key, org) in seen:
        return []
    seen.add((set_id, key, org))

    findings: list[CheckFinding] = []

    def _remediation_kwargs(spec: dict | None = None, *, issue=None) -> dict:
        raw = None
        issue_id = getattr(issue, "remediation_id", "") if issue is not None else ""
        if issue_id:
            raw = {
                "id": issue_id,
                "params": getattr(issue, "remediation_params", {}) or {},
            }
        elif spec is not None:
            raw = spec.get("remediation")
        if raw is None:
            return {}
        normalized = schemas.normalize_remediation_ref(raw)
        return {
            "remediation_id": normalized["id"],
            "remediation_params": dict(normalized["params"]),
        }

    via_remediation = (
        {
            "remediation_id": _via_remediation_id,
            "remediation_params": dict(_via_remediation_params or {}),
        }
        if _via_remediation_id else {}
    )

    # A set with no registered schema is not the same as a row nobody has
    # written. Reporting the first as the second sends a reader to provision
    # something that cannot be provisioned -- a write to an unregistered
    # schema is refused -- so it must be named for what it is. It stays a
    # report rather than a certainty because registration is per PROCESS:
    # some schemas register only when a consumer imports their module, so a
    # set genuinely present elsewhere can be absent here.
    if not any(schemas.get_schema(set_id, r) for r in range(1, 12)):
        return [CheckFinding(
            f"{set_id} key={key!r}", "unknown_target",
            f"no schema for {set_id!r} is registered in this process, so "
            f"nothing can satisfy this reference here. Either the reference "
            f"names a set that does not exist, or its module is not imported "
            f"in this process.",
            "this process's schema registry", **via_remediation,
        )]

    read_org, peers, frame = _existence_frame(
        set_id, org, org_scoped=_org_scoped)
    address = f"{set_id} key={key!r} in {read_org!r}"

    try:
        row = read_set_key(set_id, key, org=read_org, peers=peers)
    except Exception as exc:
        return [CheckFinding(
            address, "unreadable", f"{type(exc).__name__}: {exc}"[:160], frame,
            set_id=set_id, key=key, org=read_org, subject=key,
            field=_via_field, field_description=_via_description,
            frame="settings-store", **via_remediation)]
    if row is None:
        elsewhere = _owned_but_unreadable(set_id, key, read_org)
        if elsewhere is not None:
            owner, state = elsewhere
            return [CheckFinding(
                address, "unreadable_reference",
                f"a row exists in {owner!r} at {state!r}, which peers cannot "
                f"read. Raise it to 'published' or 'canonical', or give this "
                f"organization its own row -- writing a second one here while "
                f"the first stays unreadable leaves two answers and no way to "
                f"tell which one anything used",
                frame, **via_remediation)]
        return [CheckFinding(
            address, "missing_reference", "no row under this key", frame,
            set_id=set_id, key=key, org=read_org, subject=key,
            field=_via_field, field_description=_via_description,
            frame="settings-store", **via_remediation)]

    schema = schemas.get_schema(set_id, int(row["schema_revision"]))
    payload = row.get("payload") or {}

    if _passed is not None:
        _passed.append(CheckPassed(
            kind="resolved_setting",
            detail=f"{set_id} key={key!r} resolves in {read_org!r}",
            set_id=set_id,
            key=key,
            org=read_org,
            subject=key,
            field=_via_field,
            frame="settings-store",
            name=str(payload.get("name") or ""),
            description=str(payload.get("description") or ""),
            field_description=_via_description,
        ))

    # A row may say, in its own payload, that it is optional. Where a schema
    # names that field, everything this row asks for degrades with it: an
    # optional mount's missing directory is worth reporting and does not mean
    # the launch is broken.
    gate = schemas.readiness_gate(set_id, int(row["schema_revision"]))
    row_severity = (
        "advisory" if gate is not None and not payload.get(gate) else "blocking"
    )

    def _row_meta(*, describes_subject: bool) -> dict:
        """Row-level metadata, straight off the top-level payload.

        ``describes_subject`` is the whole care here. A row's ``name`` and
        ``description`` describe the thing THAT ROW IS ABOUT. A mount row is
        about the one path it declares, so they describe it. A workspace row
        listing six names in ``env_from_host`` is not about any one of them,
        and attaching its ``name`` to a variable produces a confident label
        that is simply wrong -- a missing GH_TOKEN reported as "Alpha",
        because a field existed and meant something else.

        So the caller says whether the field it is reporting on is the row's
        own subject, and only then do the display fields travel. Identity
        (set/key/org) always travels; it is a fact either way.
        """
        base = {"set_id": set_id, "key": key, "org": read_org}
        if not describes_subject:
            return base
        return {
            **base,
            "name": str(payload.get("name") or ""),
            "description": str(payload.get("description") or ""),
            "help": str(payload.get("help") or ""),
            "expects": str(payload.get("kind") or ""),
        }

    def _sev(field_spec: dict) -> str:
        if row_severity == "advisory":
            return "advisory"
        return field_spec.get("severity") or "blocking"

    def walk(schema_cls, value, path_prefix: str) -> None:
        meta = getattr(schema_cls, "_field_metadata", None) or {}
        if not isinstance(value, dict):
            return
        for name, spec in meta.items():
            item = value.get(name)
            if item is None:
                continue
            element = (getattr(schema_cls, "_element_schemas", None) or {}).get(name)
            if element is not None and isinstance(item, list):
                for index, entry in enumerate(item):
                    walk(element, entry, f"{path_prefix}{name}[{index}].")
                continue
            single = not isinstance(item, list)
            for one in (item if isinstance(item, list) else [item]):
                if not isinstance(one, str) or not one:
                    continue
                target = spec.get("references")
                if target:
                    scoped = spec.get("reference_scope") == "org"
                    ref_key = f"{org}:{one}" if scoped else one
                    findings.extend(check_setting(
                        target, ref_key, org=org, _seen=seen,
                        _org_scoped=scoped,
                        # The referring field is the ONLY thing that can say
                        # what a missing row was for: the row that would have
                        # described it is the one that is not there.
                        _via_field=f"{path_prefix}{name}",
                        _via_description=spec.get("description", "") or "",
                        _via_remediation_id=(
                            _remediation_kwargs(spec).get("remediation_id", "")
                        ),
                        _via_remediation_params=(
                            _remediation_kwargs(spec).get("remediation_params", {})
                        ),
                        _passed=_passed,
                        # Following a forward reference must not then walk
                        # backward into every other row that references the
                        # shared target. A workspace depends on its contract;
                        # it does not depend on every peer workspace that
                        # enables the same contract.
                        _include_dependents=False,
                    ))
                kind = spec.get("exists")
                if kind:
                    if (spec.get("exists_frame") == "platform-host"
                            and _running_in_a_container()):
                        # Refuse rather than answer. From in here the question
                        # has two wrong answers and no right one: the path is
                        # reported missing when it is present on the host, and
                        # PRESENT when a same-named directory happens to exist
                        # in this container. The second is why this cannot just
                        # be a caveat on the output — it turns green, and green
                        # is read as ready.
                        findings.append(CheckFinding(
                            address, "unanswerable_here",
                            f"{path_prefix}{name} declares {kind} at {one!r} on "
                            f"the platform host, which this container cannot "
                            f"see — run this check on the host to answer it",
                            "a container filesystem, which is not the platform "
                            "host's",
                            _sev(spec),
                            field=f"{path_prefix}{name}", subject=one,
                            frame="container-fs",
                            field_description=spec.get("description", "") or "",
                            **_remediation_kwargs(spec),
                            **_row_meta(describes_subject=single),
                        ))
                        continue
                    ok = (_os.path.isfile(one) if kind == "file"
                          else _os.path.isdir(one) if kind == "dir"
                          else _os.path.isfile(one) and _os.access(one, _os.X_OK))
                    if not ok:
                        findings.append(CheckFinding(
                            address, "missing_path",
                            f"{path_prefix}{name} declares {kind} at {one!r}, "
                            f"which is not there",
                            f"the filesystem of the process running this check",
                            _sev(spec),
                            field=f"{path_prefix}{name}", subject=one,
                            frame=("platform-host"
                                   if spec.get("exists_frame") == "platform-host"
                                   else "check-process-fs"),
                            field_description=spec.get("description", "") or "",
                            **_remediation_kwargs(spec),
                            **_row_meta(describes_subject=single),
                        ))
                    elif _passed is not None:
                        _passed.append(CheckPassed(
                            kind="present_path",
                            detail=(f"{path_prefix}{name} resolves {kind} at "
                                    f"{one!r}"),
                            set_id=set_id,
                            key=key,
                            org=read_org,
                            subject=one,
                            field=f"{path_prefix}{name}",
                            frame=("platform-host"
                                   if spec.get("exists_frame") == "platform-host"
                                   else "check-process-fs"),
                            field_description=spec.get("description", "") or "",
                        ))
                fallback_field = spec.get("env_fallback_field")
                fallback = value.get(fallback_field) if fallback_field else None
                supplied_by_payload = (
                    isinstance(fallback, dict) and one in fallback
                )
                if (spec.get("names_host_env") and one not in _os.environ
                        and not supplied_by_payload):
                    # A launcher forwards the variables that are set and
                    # skips the rest without saying so, which is why this
                    # is worth reporting at all: the container starts, and
                    # whatever needed the value fails later saying nothing
                    # about a forward that never happened.
                    findings.append(CheckFinding(
                        address, "missing_env",
                        f"{path_prefix}{name} names host environment variable "
                        f"{one!r}, which is not set — a launcher forwards only "
                        f"what is set and skips the rest in silence",
                        "the environment of the process running this check, "
                        "which is the launcher's only if they are the same "
                        "process",
                        _sev(spec),
                        field=f"{path_prefix}{name}", subject=one,
                        frame="check-process-env",
                        field_description=spec.get("description", "") or "",
                        **_remediation_kwargs(spec),
                        **_row_meta(describes_subject=single),
                    ))
                elif spec.get("names_host_env") and _passed is not None:
                    source = (
                        f"fixed {fallback_field!r} workspace environment"
                        if supplied_by_payload
                        else "dashboard/launcher host environment"
                    )
                    _passed.append(CheckPassed(
                        kind="available_env",
                        detail=f"{one!r} is supplied by the {source}",
                        set_id=set_id,
                        key=key,
                        org=read_org,
                        subject=one,
                        field=f"{path_prefix}{name}",
                        frame="launcher-effective-env",
                        field_description=spec.get("description", "") or "",
                    ))

    if schema is not None:
        walk(schema, payload, "")

        # Some schemas declare cross-row constraints that a single field's
        # ``references=`` metadata cannot express: a version is carried in an
        # adjacent field, or the edge lives in a shared key.  The schema owns
        # discovery of those rows; its checker returns data-only issues.  The
        # underlying rule remains shareable with runtime consumers.
        declared_check = getattr(schema, "readiness_findings", None)
        if callable(declared_check):
            try:
                declared_findings = declared_check(
                    key=key, payload=payload, org=org, read=read_set_key,
                )
            except Exception as exc:
                findings.append(CheckFinding(
                    address, "unreadable", f"{type(exc).__name__}: {exc}"[:160],
                    "the schema-declared readiness check",
                    set_id=set_id, key=key, org=read_org, subject=key,
                    frame="settings-store",
                ))
            else:
                for issue in declared_findings or ():
                    issue_field = str(issue.field)
                    field_name = issue_field.split(".", 1)[0].split("[", 1)[0]
                    field_spec = (
                        getattr(schema, "_field_metadata", None) or {}
                    ).get(field_name, {})
                    issue_set = str(getattr(issue, "set_id", "") or set_id)
                    issue_key = str(getattr(issue, "key", "") or key)
                    # A specialized versioned-edge finding supersedes the
                    # generic "row absent" result for the same target. Both
                    # describe one repair; returning both makes an org health
                    # view count one missing contract twice.
                    findings[:] = [
                        finding for finding in findings
                        if not (
                            finding.kind == "missing_reference"
                            and finding.set_id == issue_set
                            and finding.key == issue_key
                            and str(issue.kind).startswith("missing_")
                        )
                    ]
                    findings.append(CheckFinding(
                        address,
                        str(issue.kind),
                        str(issue.detail),
                        str(issue.looked_in),
                        str(getattr(issue, "severity", "blocking")),
                        set_id=issue_set,
                        key=issue_key,
                        org=read_org,
                        field=issue_field,
                        subject=str(issue.subject),
                        frame=str(
                            getattr(issue, "frame", "") or "settings-store"
                        ),
                        field_description=str(field_spec.get("description") or ""),
                        **_remediation_kwargs(field_spec, issue=issue),
                    ))
                if _passed is not None and not declared_findings:
                    _passed.append(CheckPassed(
                        kind="valid_declared_constraints",
                        detail=(f"{set_id} key={key!r} satisfies its "
                                "schema-declared cross-row constraints"),
                        set_id=set_id,
                        key=key,
                        org=read_org,
                        subject=key,
                        frame="settings-store",
                        name=str(payload.get("name") or ""),
                        description=str(payload.get("description") or ""),
                    ))

    # Rows keyed by this one are part of whether it is fully installed: a
    # workspace's capability enables are found through the key segment that
    # names the workspace.
    if _include_dependents:
        for dep_set, dep_key, _segment in rows_keyed_by(set_id, key, org=org):
            findings.extend(check_setting(
                dep_set, dep_key, org=org, _seen=seen, _passed=_passed,
                _include_dependents=True,
            ))
    return findings


def inspect_setting(
    set_id: str,
    key: str,
    *,
    org: str,
) -> tuple[list[CheckFinding], list[CheckPassed]]:
    """Return failures and positive evidence from one recursive traversal."""
    passed: list[CheckPassed] = []
    findings = check_setting(set_id, key, org=org, _passed=passed)
    return findings, passed


def audit_publication_bands(*, org: str | None = None) -> list[dict]:
    """Every stored settings row whose ``publication_state`` falls outside its
    schema's declared band.

    The compliance sweep for the publication-band lockdown: a row above its
    band ``max`` is readable by peer organizations that should not see it (a
    cross-org disclosure — the ``autonomy.workspace`` class); a row below its
    band ``min`` cannot be read by peers that need it (the capability-contract
    outage class). A set with no declared band is reported too, though the
    completeness gate now refuses to ship one.

    Host-run estate scan: opens each store's database DIRECTLY (every
    ``data/orgs/<slug>.db`` plus the local personal/machine stores) rather than
    the composed read-through surface, which resolves one winner per key and so
    masks a second store holding the same key in a worse state. Pass ``org`` to
    scan a single store; default scans every store on the machine.

    Each finding: ``{store, set_id, key, revision, state, band, reason}``.
    """
    from .cross_org import all_store_slugs
    from .db import GraphDB
    from .schemas.registry import declared_band, PUBLICATION_ORDER

    order = {s: i for i, s in enumerate(PUBLICATION_ORDER)}
    slugs = [org] if org else all_store_slugs()
    findings: list[dict] = []
    for slug in slugs:
        try:
            db = GraphDB.for_org(slug, mode="ro")
        except Exception:
            continue  # store absent on this machine
        try:
            rows = db.conn.execute(
                "SELECT set_id, key, schema_revision, publication_state "
                "FROM settings WHERE deprecated = 0"
            ).fetchall()
        except Exception:
            continue
        for r in rows:
            set_id = r["set_id"]
            rev = int(r["schema_revision"])
            state = r["publication_state"]
            si = order.get(state)
            if si is None:
                continue
            band = declared_band(set_id, rev)
            base = {"store": slug, "set_id": set_id, "key": r["key"],
                    "revision": rev, "state": state}
            if band is None:
                findings.append({**base, "band": None,
                                 "reason": "no band declared (defaults to full range)"})
                continue
            lo, hi = band
            if si > order[hi]:
                findings.append({**base, "band": f"{lo}..{hi}",
                                 "reason": "above max — readable by peer orgs"})
            elif si < order[lo]:
                findings.append({**base, "band": f"{lo}..{hi}",
                                 "reason": "below min — unreadable by peer orgs"})
    return findings


def orphans_of(set_id: str, *, org: str) -> list[CheckFinding]:
    """Rows whose key names an entity that no longer exists.

    Reads each declared key segment and looks for the row it identifies, in
    the home that target declares. A row keyed by something absent is
    reported here; it is valid in every other respect, so this is where it
    surfaces.
    """
    import re as _re

    findings: list[CheckFinding] = []
    schema = next((schemas.get_schema(set_id, r)
                   for r in range(1, 12) if schemas.get_schema(set_id, r)), None)
    refs = getattr(schema, "_key_references", None) or {}
    if not refs:
        return findings
    segments = [seg.strip("[]") for seg in
                _re.split(r"[:/]", getattr(schema, "_key_strategy", "") or "")]
    try:
        members = read_set(set_id, org=org, peers=[])
    except Exception:
        return findings
    for member in members.members:
        parts = str(member.key).split(":")
        for segment, target in refs.items():
            if segment not in segments:
                continue
            index = segments.index(segment)
            if index >= len(parts):
                continue
            read_org, peers, frame = _existence_frame(target, org)
            if read_set_key(target, parts[index], org=read_org, peers=peers) is None:
                findings.append(CheckFinding(
                    f"{set_id} key={member.key!r} in {org!r}", "orphaned_key",
                    f"its {segment} names {parts[index]!r} in {target}, which "
                    f"has no row",
                    frame,
                ))
    return findings


def _assert_home(set_id: str | None, org: str | None) -> None:
    """Refuse a Setting routed to a database its schema does not live in.

    ``personal`` is the operator's own store; ``organization`` is a store an
    org owns and its members read. An organization's database is what
    federates, so a value in the wrong one is either invisible to everyone
    who needs it or visible to everyone who should not have it. Neither
    failure announces itself, which is why this is checked rather than
    documented.

    Checked on the way to the database, so a read looking in the wrong place
    and a write landing in it fail the same way for the same reason.

    A ``GRAPH_DB`` pin with no org replaces the whole store outright -- it is
    how the test suite, ``graph --db`` and the multi-node harness address a
    database directly. There is no organization in that resolution to be
    right or wrong about, so nothing is asserted.
    """
    if set_id is None:
        return
    want = schemas.declared_home(set_id)
    if want is None:
        return
    if os.environ.get("GRAPH_DB") and org is None:
        return
    if want == "machine":
        if org != "machine":
            raise schemas.SchemaValidationError(
                f"{set_id} lives in this machine's own database, which never "
                f"leaves it; refusing to use {org!r}"
            )
        return
    if org == "machine":
        raise schemas.SchemaValidationError(
            f"{set_id} does not live in this machine's database — it declares "
            f"{want!r}, and a machine store never leaves the machine"
        )
    is_personal = org in (None, "personal")
    if want == "personal" and not is_personal:
        raise schemas.SchemaValidationError(
            f"{set_id} lives in the operator's own database; refusing to use "
            f"organization {org!r}"
        )
    # 'organization' does NOT refuse the operator's own store. It is the
    # unconstrained home: it says only that this is not forced into personal
    # or machine. The operator's database IS the organizational home of the
    # operator's own things -- they own workspaces, and those workspaces have
    # mounts, primers and commit policies exactly like any organization's.
    #
    # Reading it as "not personal" refused writes that were correct, and made
    # a declaration of scope look like a prohibition. The value of declaring
    # it is that the decision has been TAKEN, which is what separates it from
    # a set nobody has thought about -- not that it forbids a database.
    #
    # A machine-store write is already refused a few lines above, for every
    # declared home that is not `machine` -- so nothing extra is needed here,
    # and adding it would be a second rule saying the same thing in a
    # different voice.


def _open(
    org: str | None, set_id: str | None = None, *, for_read: bool = False,
) -> GraphDB:
    """Open the database :func:`_db_path` resolved, creating nothing.

    A NAMED organization's database must already be there, because
    creating the organization is what creates it. Letting an open
    manufacture one is silent and wrong: a path that is not mounted where
    the caller believes it is -- the ordinary case inside a container --
    yields a fresh empty database, the write reports success, and nothing
    ever reads it. Refusing turns that into an error naming the path.

    The two destinations that come into being on demand are left alone.
    :func:`_db_path` provisions the personal database itself, under a
    lock, and a ``GRAPH_DB`` pin is a path its caller chose outright.
    Naming ``personal`` reaches the same database as saying nothing, so
    it is treated the same way -- an operator's own store is not
    something anyone provisions first.

    A WRITE with no org names its destination or is refused. Placement has
    exactly three sources -- the caller's credential, an explicit selection,
    or the schema's declared home -- and no default (operator ruling
    2026-08-20; supersedes the auto-txg5.3 write-converges-on-personal
    default for Settings). A pinned home IS the destination, so absence of
    an org routes there; a set with no pinned home cannot choose an
    organization for the caller, so the write fails closed, loudly, at the
    caller. ``for_read`` exempts the read paths that fall through here: a
    read never refuses (the rubric's asymmetry), and a ``GRAPH_DB`` pin is
    unaffected because :func:`_assert_home` already asserts nothing there.
    """
    if not for_read and org is None and set_id is not None \
            and not os.environ.get("GRAPH_DB"):
        want = schemas.declared_home(set_id)
        if want == "personal":
            pass  # None already means the personal store in this layer.
        elif want == "machine":
            org = "machine"
        else:
            raise schemas.SchemaValidationError(
                f"{set_id} declares no single home, so a write must name its "
                "organization explicitly (org=<slug> / --org <slug>) — there "
                "is no default scope"
            )
    _assert_home(set_id, org)
    return GraphDB(_db_path(org), create=org in _CREATED_ON_DEMAND)


def _open_read(org: str | None, set_id: str | None = None) -> GraphDB:
    """Open for reading, read-only where that is possible.

    A read has no business taking a write lock or running schema
    initialisation, which is what an rw open does on every call. Where the
    database is already there, open it read-only and neither can happen.

    A database that is not there yet falls back to :func:`_open`, which
    decides whether it may be created. Read-only cannot open a missing
    file at all, so the fallback is what keeps the on-demand destinations
    -- a pinned path, the operator's own store -- readable before anything
    has been written to them.
    """
    # A read RESOLVES to the declared home; it does not refuse the caller for
    # naming an organization. The two are not symmetric. A write landing in the
    # wrong database is a value nobody can find or everybody can read, so
    # refusing it is the whole point of the declaration. A read is different:
    # the caller names the organization it is acting FOR, which for a
    # personal-homed set is the KEY rather than the database, and refusing it
    # breaks every consumer that legitimately scopes by org.
    #
    # That is not hypothetical. `/api/sign-key?org=anchore` asks for the
    # signing key anchore's commits are signed with -- the org is which key,
    # not which store -- and the guard turned it into a 500, surfaced to the
    # operator as "User declined signing request". A refusal that misreports
    # itself as a human decision is worse than the misrouting it prevents.
    home = schemas.declared_home(set_id) if set_id else None
    if home in ("personal", "machine"):
        org = home
    # A READ never refuses. Where a home names one database the read resolves
    # to it; everywhere else it opens the store it was asked for and finds
    # whatever is there, which for the wrong store is nothing.
    #
    # Refusing was not a stricter version of the same idea, it was a different
    # and worse one. A sweep that walks every database -- the session monitor
    # does, on a timer -- reaches the machine store, and an organization-homed
    # set raised there instead of returning no rows. The exception was thrown
    # inside a timer task and never retrieved, so the dashboard did not crash:
    # it stopped answering while holding the port, and every graph call across
    # the fleet hung rather than failing fast. Twelve minutes of outage from a
    # guard that was only ever meant to stop a value being WRITTEN somewhere
    # nobody could find it.
    path = _db_path(org)
    if path and Path(path).exists():
        return GraphDB(path, mode="ro")
    return _open(org, set_id, for_read=True)


# ── JSON merge-patch (RFC 7396) ──────────────────────────────


def json_merge_patch(target: Any, patch: Any) -> Any:
    """Apply RFC 7396 merge-patch.

    - If ``patch`` is not a dict, return it (replace).
    - For dict patches: per key, remove on null; recurse for dict-on-dict;
      otherwise replace.
    - Lists are replaced wholesale, never element-merged.
    """
    if not isinstance(patch, dict):
        return patch
    if not isinstance(target, dict):
        target = {}
    out = dict(target)
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = json_merge_patch(out[k], v)
        else:
            out[k] = v
    return out


# ── Row → ResolvedSetting ────────────────────────────────────


def _apply_declared_defaults(set_id: str, revision: int, payload: dict) -> dict:
    """Fill fields the schema declares a default for and the payload omits.

    A default belongs to the schema, so every reader should see the same one.
    While resolution left them out, each consumer supplied its own, and two
    consumers of the same field could disagree without either being obviously
    wrong -- which is exactly how a workspace ended up with host networking
    granted by a fallback nobody chose.

    Only an ABSENT key is filled. An explicitly stored ``None`` is a stated
    value and is left alone; the schema does not get to overrule what a writer
    said on purpose.

    Applied AFTER override merging, so a value supplied by any layer wins over
    the default -- a default is what you get when nobody said anything, not a
    floor.

    Two kinds of field are skipped, because for them absence is not silence:

    ``default`` of ``None`` -- present-and-null says exactly what absence
    already said, so filling it adds no information and costs every reader
    that asks whether the field is there at all.

    A field in a ``deprecated_alias_of`` pair -- two names for one value.
    Filling either makes both present, and a schema that pairs them refuses a
    payload where they are both present and disagree, so the fill would
    produce a payload the schema itself rejects. Which of the two a reader
    consults first then decides the answer, which is how a workspace that
    asked for a nested Docker daemon under the older name stops getting one.
    """
    schema = schemas.get_schema(set_id, int(revision))
    if schema is None or not isinstance(payload, dict):
        return payload
    meta = getattr(schema, "_field_metadata", None) or {}
    aliased = {
        name
        for name, spec in meta.items()
        for name in (name, spec.get("deprecated_alias_of"))
        if spec.get("deprecated_alias_of")
    }
    missing = {
        name: spec["default"]
        for name, spec in meta.items()
        if "default" in spec
        and name not in payload
        and spec["default"] is not None
        and name not in aliased
    }
    if not missing:
        return payload
    return {**missing, **payload}


def _row_to_resolved(row, *, org: str | None = None,
                     target_revision: int | None = None) -> ResolvedSetting[dict]:
    payload = row["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            payload = {}
    return ResolvedSetting(
        id=row["id"],
        set_id=row["set_id"],
        stored_revision=int(row["schema_revision"]),
        key=row["key"],
        payload=payload,
        state=row["publication_state"],
        supersedes=row["supersedes"],
        excludes=row["excludes"],
        deprecated=bool(row["deprecated"]),
        successor_id=row["successor_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        target_revision=target_revision,
        org=org,
        upconverted=False,
    )


def _apply_model(
    resolved: ResolvedSetting[dict],
    model: type[Any],
    dropped: DropAccounting,
) -> ResolvedSetting[Any] | None:
    """Validate ``resolved.payload`` against *model* and retype the payload.

    *model* is any class exposing a ``model_validate(data) -> instance``
    classmethod — Pydantic ``BaseModel`` subclasses satisfy this. On
    validation failure the row is dropped: ``dropped.schema_invalid`` is
    incremented, a WARN is logged, and ``None`` is returned so the caller
    can skip the row without crashing the query.
    """
    validator = getattr(model, "model_validate", None)
    if validator is None:
        raise TypeError(
            f"model={model!r} has no model_validate() classmethod — "
            f"pass a Pydantic BaseModel (or compatible) class"
        )
    try:
        typed_payload = validator(resolved.payload)
    except Exception as exc:
        dropped.schema_invalid += 1
        logger.warning(
            "read_set(%s, model=%s): payload validation failed for "
            "setting id=%s key=%r: %s",
            resolved.set_id, model.__name__, resolved.id, resolved.key, exc,
        )
        return None
    return replace(resolved, payload=typed_payload)


# ── Write paths ──────────────────────────────────────────────


@dataclass
class ShadowedWrite:
    """A row was stored and will never be read.

    Resolution returns ONE row per key. A row written where a
    higher-precedence one already exists is stored successfully, resolves to
    nothing, and reports success at every layer -- so the caller believes
    they changed a value they did not change. Anything that writes a Setting
    must surface this; it cannot be left to the writer to remember that
    publication state and owning organization decide who wins.
    """
    key: str
    written_id: str
    written_org: str | None
    written_state: str
    winner_id: str
    winner_org: str | None
    winner_state: str
    #: Fields whose written value is overwritten by an override on this same
    #: row. Empty when the write lost to a different BASE instead.
    masked_fields: tuple[str, ...] = ()

    def __str__(self) -> str:
        if self.masked_fields:
            return (
                f"MASKED: {self.key!r} was written, and an override on this "
                f"same row ({self.winner_id[:11]}) overwrites "
                f"{', '.join(sorted(self.masked_fields))}. Readers keep the "
                f"override's values for those fields, so the write did not "
                f"change what anything reads. Amend or remove the override; "
                f"`graph set layers {self.key}` shows both rows."
            )
        where = (f"organization {self.winner_org!r}"
                 if self.winner_org != self.written_org
                 else "the same organization")
        return (
            f"SHADOWED: {self.key!r} was written at {self.written_state!r} "
            f"but resolves to a different row -- {self.winner_id[:11]} at "
            f"{self.winner_state!r} in {where}. Nothing will read what you "
            f"just wrote. Write it at {self.winner_state!r}"
            + (f" against organization {self.winner_org!r}"
               if self.winner_org != self.written_org else "")
            + f", or promote {self.written_id[:11]} above it."
        )


_LAST_SHADOWED: dict[tuple[str, str], 'ShadowedWrite'] = {}


def shadowed_write(
    set_id: str,
    key: str,
    written_id: str,
    *,
    org: str | None,
    state: str,
) -> ShadowedWrite | None:
    """Return the row that will be read instead of ``written_id``, or None."""
    # Ask only what this means: is there another BASE under this exact key
    # that outranks mine? Resolving the whole set would answer it, and would
    # cost time proportional to every key in the set -- on a write path that
    # runs per presence heartbeat. The question is per-key, so the query is
    # per-key, which keeps the check cheap enough to always run rather than
    # behind a flag somebody turns off during the bulk write that needs it.
    from .cross_org import PEER_VISIBLE_STATES, open_peer_db, resolve_peers

    # Full rows, because the winner must be picked by _rank_candidates — the
    # resolver's OWN six-step ordering (rung, store, SCHEMA REVISION, ...).
    # Ranking here by publication_state alone made this check a different
    # opinion from the thing it checks: during a revision migration, a key
    # legitimately holds a rev-N and a rev-N+1 base at the same rung, the
    # rung-only sort tied, insertion order won, and the warning declared the
    # OLD row the winner — telling the operator their freshly migrated row
    # was dead when the very next read served it (host finding, 2026-08-30).
    sql = (
        "SELECT rowid AS _rowid, * FROM settings "
        "WHERE set_id = ? AND key = ? AND deprecated = 0 "
        "  AND supersedes IS NULL AND excludes IS NULL"
    )
    candidates: list = []
    try:
        db = _open_read(org, set_id)
        try:
            for row in db.conn.execute(sql, (set_id, key)).fetchall():
                candidates.append((org, row))
        finally:
            db.close()

        placeholders = ",".join("?" for _ in PEER_VISIBLE_STATES)
        for peer in sorted(resolve_peers(_resolve_settings_caller(org), None)):
            peer_db = open_peer_db(peer)
            if peer_db is None:
                continue
            try:
                rows = peer_db.conn.execute(
                    f"{sql} AND publication_state IN ({placeholders})",
                    (set_id, key, *PEER_VISIBLE_STATES),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            for row in rows:
                candidates.append((peer, row))
    except Exception:
        return None

    if len(candidates) >= 2:
        try:
            ranked = _rank_candidates(
                candidates,
                reading_org=_resolve_settings_caller(org),
                now=None,
            )
        except Exception:
            return None
        if ranked and ranked[0][1]["id"] != written_id:
            top_org, top = ranked[0]
            return _shadowed_by_base(
                key, written_id, org, state,
                top["id"], top["publication_state"], top_org)
    # Winning the base contest is not the same as being read. An override on
    # this row is merged over it, so a write can win and still change nothing
    # -- which is the commonest way a write is silently neutralised, and was
    # invisible here because the query above excludes overrides by design.
    return _masked_by_override(set_id, key, written_id, org, state)


def _masked_by_override(
    set_id: str, key: str, written_id: str, org: str | None, state: str,
) -> "ShadowedWrite | None":
    """Report the fields an override on this row overwrites, if any."""
    try:
        db = _open_read(org, set_id)
    except Exception:
        return None
    try:
        written = db.conn.execute(
            "SELECT payload FROM settings WHERE id = ?", (written_id,)
        ).fetchone()
        overrides = db.conn.execute(
            "SELECT id, payload, publication_state FROM settings "
            "WHERE supersedes = ? AND deprecated = 0 "
            "ORDER BY created_at ASC, rowid ASC",
            (written_id,),
        ).fetchall()
    except Exception:
        return None
    finally:
        db.close()
    if written is None or not overrides:
        return None
    try:
        base_payload = json.loads(written["payload"]) or {}
        merged = dict(base_payload)
        last = overrides[-1]
        for row in overrides:
            merged = json_merge_patch(merged, json.loads(row["payload"]) or {})
    except Exception:
        return None
    masked = tuple(
        name for name, value in base_payload.items()
        if name in merged and merged[name] != value
    )
    if not masked:
        return None
    return ShadowedWrite(
        key=key,
        written_id=written_id,
        written_org=org,
        written_state=state,
        winner_id=last["id"],
        winner_org=org,
        winner_state=last["publication_state"],
        masked_fields=masked,
    )


def _shadowed_by_base(
    key: str, written_id: str, org: str | None, state: str,
    winner_id: str, winner_state: str, winner_org: str | None,
) -> "ShadowedWrite":
    return ShadowedWrite(
        key=key,
        written_id=written_id,
        written_org=org,
        written_state=state,
        winner_id=winner_id,
        winner_org=winner_org,
        winner_state=winner_state,
    )


def take_shadowed_write(set_id: str, key: str) -> "ShadowedWrite | None":
    """Pop the shadow report for the last write to ``(set_id, key)``."""
    return _LAST_SHADOWED.pop((set_id, key), None)


def add_setting(
    set_id: str,
    schema_revision: int,
    key: str,
    payload: dict,
    *,
    org: "str | None | _CallerOrgSentinel",
    state: str = "raw",
    vault_policy_class_id: str | None = None,
) -> str:
    """Create a base Setting in org's DB.

    ``org`` is **required**: pass an org slug for that org's DB,
    :data:`CALLER_ORG` for the env-cascade resolver, or ``None`` for an
    explicit scopeless write. Forgetting ``org=`` is a ``TypeError`` —
    see module docstring.

    Validates payload against ``(set_id, schema_revision)``. Returns the new
    Setting id. Raises ``schemas.SchemaValidationError`` on validation
    failure, ``ValueError`` on bad ``state``.

    A set declared ``@vaulted`` takes one extra step: the validated payload is
    sealed into a content object and the row stores the resulting locator. The
    payload is validated first either way, so a vaulted set is held to its
    schema exactly as an ordinary one is — encryption is what happens to a
    value, not an excuse to stop checking it.
    """
    org = _resolve_org_arg(org)
    _guard_protected_set(set_id)
    if state not in VALID_STATES:
        raise ValueError(f"invalid state {state!r}; valid: {VALID_STATES}")
    _assert_publication_band(set_id, schema_revision, state)
    schemas.validate_payload(set_id, schema_revision, payload)
    schemas.validate_key(set_id, schema_revision, key)
    sid = str(uuid4())
    stored_payload = payload
    vault_tier = schemas.declared_vault_tier(set_id)
    if vault_tier is not None:
        # Before the insert, and with the row id already chosen: the revision
        # identifier is derived from it, so the object and the row that names
        # it are one write or neither.
        stored_payload = _seal_vault_payload(
            set_id=set_id,
            schema_revision=schema_revision,
            key=key,
            setting_id=sid,
            payload=payload,
            tier=vault_tier,
            org=org,
            policy_class_id=vault_policy_class_id,
        )
    now = _now_iso()
    expires_at = schemas.cache_expires_at(set_id, int(schema_revision), now)
    db = _open(org, set_id)
    try:
        db.conn.execute(
            "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
            "publication_state, created_at, updated_at, expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (sid, set_id, int(schema_revision), key, json.dumps(stored_payload),
             state, now, now, expires_at),
        )
        db.conn.commit()
    finally:
        db.close()
    _call_emit_hook(
        operation="write",
        snapshot=_make_snapshot(set_id, schema_revision, key, state, False),
        org=org,
    )
    shadow = shadowed_write(set_id, key, sid, org=org, state=state)
    if shadow is not None:
        logger.warning("%s", shadow)
        _LAST_SHADOWED[(set_id, key)] = shadow
    return sid


def append_log_entries(
    set_id: str,
    schema_revision: int,
    entries: list[tuple[str, dict]],
    *,
    org: "str | None | _CallerOrgSentinel",
    state: str = "raw",
) -> list[str]:
    """Append a validated batch to an ``@append_only_log`` in one transaction.

    Every entry supplies its immutable key and complete payload. The function
    refuses non-log and vaulted schemas, validates the entire batch before
    opening a write transaction, and emits the ordinary per-row Settings hook
    only after the commit. It exists for event producers whose natural unit is
    a batch; calling :func:`add_setting` thousands of times would otherwise
    reopen and initialize SQLite thousands of times.
    """
    org = _resolve_org_arg(org)
    _guard_protected_set(set_id)
    if state not in VALID_STATES:
        raise ValueError(f"invalid state {state!r}; valid: {VALID_STATES}")
    _assert_publication_band(set_id, schema_revision, state)
    if _access_pattern_for(set_id, schema_revision) != "append_only_log":
        raise ValueError(f"{set_id} does not declare 'append_only_log'")
    if schemas.declared_vault_tier(set_id) is not None:
        raise ValueError("bulk append does not accept vaulted Settings")
    if not entries:
        return []
    keys = [key for key, _payload in entries]
    if len(set(keys)) != len(keys):
        raise ValueError("append-only batch contains duplicate keys")
    for key, payload in entries:
        schemas.validate_payload(set_id, schema_revision, payload)
        schemas.validate_key(set_id, schema_revision, key)

    now = _now_iso()
    expires_at = schemas.cache_expires_at(set_id, int(schema_revision), now)
    setting_ids = [str(uuid4()) for _entry in entries]
    rows = [
        (
            setting_id,
            set_id,
            int(schema_revision),
            key,
            json.dumps(payload),
            state,
            now,
            now,
            expires_at,
        )
        for setting_id, (key, payload) in zip(setting_ids, entries)
    ]
    db = _open(org, set_id)
    try:
        db.conn.execute("BEGIN IMMEDIATE")
        db.conn.executemany(
            "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
            "publication_state, created_at, updated_at, expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            rows,
        )
        db.conn.commit()
    except Exception:
        db.conn.rollback()
        raise
    finally:
        db.close()
    for key in keys:
        _call_emit_hook(
            operation="write",
            snapshot=_make_snapshot(set_id, schema_revision, key, state, False),
            org=org,
        )
    return setting_ids


def upsert_by_key(
    set_id: str,
    schema_revision: int,
    key: str,
    payload: dict,
    *,
    org: "str | None | _CallerOrgSentinel",
    state: str = "raw",
    _source_created_at: str | None = None,
    _source_updated_at: str | None = None,
) -> str:
    """Atomic single-tx UPDATE-or-INSERT at ``(set_id, schema_revision, key)``.

    Closes substrate gap #2 (``graph://dff97eec-c59``): callers that want
    a single, evolving base row per composite key get one — no
    ``read_set`` / ``add_setting`` race window, no append-by-default
    surprise. The returned setting id is stable across subsequent
    upserts of the same key, so callers can hold ids long-term.

    Algorithm (one transaction, ``BEGIN IMMEDIATE``):
        1. validate ``payload`` against the registered schema,
        2. ``SELECT`` the newest base row (``supersedes IS NULL AND
           excludes IS NULL``) for ``(set_id, schema_revision, key)``,
        3. INSERT a fresh row when none exists, otherwise UPDATE that
           row in place (id, ``created_at`` preserved),
        4. restamp ``expires_at`` from :func:`schemas.cache_expires_at`
           so ``@cache``-decorated schemas keep sliding-window TTLs,
        5. fire exactly one ``set_emit_hook`` callback after commit
           (``operation="write"`` — same shape :func:`add_setting`
           emits today).

    Legacy duplicates (multiple base rows for the same composite key
    left behind by older :func:`add_setting` callers) are tolerated:
    the newest by ``created_at`` wins and is updated in place — no
    error, no cleanup. Override (``supersedes``) and exclude
    (``excludes``) rows are deliberately ignored: they belong to a
    separate layer and are not the "writable base."

    Concurrency: ``BEGIN IMMEDIATE`` acquires SQLite's reserved-write
    lock before the SELECT, so two writers in the same process serialize
    cleanly — one blocks until the other commits, then sees the row it
    just wrote.

    Returns the upserted row's setting id (new on insert, preserved on
    update). Raises :class:`schemas.SchemaValidationError` on bad
    payload or unknown schema; ``ValueError`` on bad ``state``.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    org = _resolve_org_arg(org)
    _guard_protected_set(set_id)
    if state not in VALID_STATES:
        raise ValueError(f"invalid state {state!r}; valid: {VALID_STATES}")
    _assert_publication_band(set_id, schema_revision, state)
    if _access_pattern_for(set_id, schema_revision) == "append_only_log":
        raise ValueError(
            f"{set_id} declares 'append_only_log': its rows are never "
            f"rewritten. Append a new one with add_setting()."
        )
    if schemas.declared_vault_tier(set_id) is not None:
        # A vaulted row's revision identifier is derived from the row id, so
        # rewriting the row in place would address the revision already
        # committed — and a committed revision admits only a byte-identical
        # replay (``storagekit.store``, contract Invariant 7). The value is
        # not lost by appending instead: settings are already append-only and
        # resolution takes the most recent base.
        raise ValueError(
            f"{set_id} is a vault set: its rows are encrypted object "
            f"revisions and are never rewritten. Write the first value with "
            f"add_setting() and change it with override_setting(), which "
            f"appends a new revision and leaves the old one as it was."
        )
    schemas.validate_payload(set_id, schema_revision, payload)
    schemas.validate_key(set_id, schema_revision, key)
    now = _now_iso()
    created_at = _source_created_at or now
    updated_at = _source_updated_at or now
    expires_at = schemas.cache_expires_at(set_id, int(schema_revision), now)
    payload_json = json.dumps(payload)
    db = _open(org, set_id)
    try:
        db.conn.execute("BEGIN IMMEDIATE")
        existing = db.conn.execute(
            "SELECT id FROM settings "
            "WHERE set_id = ? AND schema_revision = ? AND key = ? "
            "  AND supersedes IS NULL AND excludes IS NULL "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (set_id, int(schema_revision), key),
        ).fetchone()
        if existing is None:
            sid = str(uuid4())
            db.conn.execute(
                "INSERT INTO settings(id, set_id, schema_revision, key, "
                "payload, publication_state, created_at, updated_at, "
                "expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (sid, set_id, int(schema_revision), key, payload_json,
                 state, created_at, updated_at, expires_at),
            )
        else:
            sid = existing["id"]
            db.conn.execute(
                "UPDATE settings SET payload = ?, publication_state = ?, "
                "updated_at = ?, expires_at = ? WHERE id = ?",
                (payload_json, state, updated_at, expires_at, sid),
            )
        db.conn.commit()
    finally:
        db.close()
    _call_emit_hook(
        operation="write",
        snapshot=_make_snapshot(set_id, schema_revision, key, state, False),
        org=org,
    )
    shadow = shadowed_write(set_id, key, sid, org=org, state=state)
    if shadow is not None:
        logger.warning("%s", shadow)
        _LAST_SHADOWED[(set_id, key)] = shadow
    return sid


_REPLACED_PATTERNS = ("singleton", "keyed_per_entity")


def _access_pattern_for(set_id: str, revision: int) -> str | None:
    schema = schemas.get_schema(set_id, int(revision))
    return getattr(schema, "_access_pattern", None) if schema else None


def layers_for(set_id: str, key: str, *, org: str | None) -> dict:
    """Every stored row behind one resolved value, and what each contributes.

    Resolution returns one merged payload, and every surface in the system
    returns that -- ``read``, ``members``, both endpoints. So the store
    presents as a dictionary of key to value while it is really rows and
    layers, and a reader whose write appears to do nothing has no way to
    see why. That is not a gap in someone's knowledge; it is a gap in what
    anything will tell them.

    Reports the base, every override in application order with the fields
    each one changes, and the merged result. Rows are addressed by id
    because an override whose base has been deleted cannot be reached any
    other way.
    """
    out: dict = {"set_id": set_id, "key": key, "org": org,
                 "base": None, "overrides": [], "resolved": None,
                 "orphans": [], "deprecated": []}
    try:
        db = _open_read(org, set_id)
    except Exception:
        return out
    try:
        rows = db.conn.execute(
            "SELECT id, payload, publication_state, supersedes, excludes, "
            "       deprecated, created_at, schema_revision "
            "FROM settings WHERE set_id = ? AND key = ? "
            "ORDER BY created_at ASC, rowid ASC",
            (set_id, key),
        ).fetchall()
    except Exception:
        return out
    finally:
        db.close()

    # Deprecated rows are skipped by resolution, so a view that merges them
    # describes a value nothing returns. They are still listed, because a row
    # that exists and does not apply is worth seeing -- it is simply not part
    # of the answer. Merging one silently re-pinned a workspace's model from a
    # row resolution had already retired.
    live = [r for r in rows if not r["deprecated"]]
    bases = [r for r in live if r["supersedes"] is None and not r["excludes"]]
    overrides = [r for r in live if r["supersedes"] is not None]
    out["deprecated"] = [
        {"id": r["id"], "state": r["publication_state"],
         "is_override": r["supersedes"] is not None}
        for r in rows if r["deprecated"]
    ]
    if not bases:
        # Overrides with no base resolve to nothing and are invisible to
        # every read; they are reported here so they can be removed.
        out["orphans"] = [
            {"id": r["id"], "supersedes": r["supersedes"],
             "state": r["publication_state"]}
            for r in overrides
        ]
        return out

    bases.sort(key=lambda r: PRECEDENCE.get(r["publication_state"], 99))
    base = bases[0]
    # Competing live bases under the same key (the normal transient state of
    # a revision migration). Surfaced so callers that are about to act on
    # "the" row for a key can see the key is ambiguous and name an id.
    out["shadowed_bases"] = [
        {"id": r["id"], "state": r["publication_state"],
         "schema_revision": r["schema_revision"]}
        for r in bases[1:]
    ]
    try:
        merged = json.loads(base["payload"]) or {}
    except Exception:
        return out
    if not isinstance(merged, dict):
        # A vaulted row's payload column holds a locator STRING, not an
        # object. Report the base opaquely — there is nothing to merge and
        # an override on sealed content has no meaning.
        out["base"] = {"id": base["id"], "state": base["publication_state"],
                       "schema_revision": base["schema_revision"],
                       "payload": merged}
        out["resolved"] = merged
        return out
    out["base"] = {"id": base["id"], "state": base["publication_state"],
                   "schema_revision": base["schema_revision"],
                   "payload": dict(merged)}

    for row in overrides:
        if row["supersedes"] != base["id"]:
            out["orphans"].append({
                "id": row["id"], "supersedes": row["supersedes"],
                "state": row["publication_state"]})
            continue
        try:
            patch = json.loads(row["payload"]) or {}
        except Exception:
            continue
        before = dict(merged)
        merged = json_merge_patch(merged, patch)
        out["overrides"].append({
            "id": row["id"],
            "state": row["publication_state"],
            "deprecated": bool(row["deprecated"]),
            "changes": sorted(
                name for name in set(before) | set(merged)
                if before.get(name) != merged.get(name)
            ),
            "patch": patch,
        })
    out["resolved"] = merged
    return out


def illegal_amendments(*, org: str | None) -> list[dict]:
    """Override rows stored on sets whose schema forbids amendment.

    A ``singleton`` or ``keyed_per_entity`` set declares that its rows are
    replaced, not amended. That rule is enforced when an override is
    written; it is not consulted when one is read, so a row predating the
    rule -- or written before its schema declared a pattern -- is still
    merged into every resolved value. The store then serves a state its own
    schema says cannot exist, and no reader can tell.

    Same-organization only. An override whose base lives in another
    database is the legitimate case the rule exists to permit: adapting a
    row you do not own, which you cannot rewrite.

    Returns one entry per offending row so a caller can report it, gate on
    it, or remove it by id -- which is the only way to address a row whose
    base has since been deleted.
    """
    out: list[dict] = []
    unjudged: list[dict] = []
    try:
        db = _open_read(org, "")
    except Exception:
        return out
    try:
        rows = db.conn.execute(
            "SELECT id, set_id, schema_revision, key, supersedes, "
            "       publication_state, created_at "
            "FROM settings "
            "WHERE supersedes IS NOT NULL AND deprecated = 0"
        ).fetchall()
        own_ids = {
            r["id"] for r in db.conn.execute("SELECT id FROM settings").fetchall()
        }
        # The access pattern is DATA, not only code. Every schema flushes its
        # declaration into the store, so the question "what are this set's
        # rows" is answerable from the same database the rows are in -- and
        # gives the same answer to every caller. Reading it from the calling
        # process's registry instead made the sweep's result depend on which
        # modules that process happened to import, which is how the same data
        # counted 73 rows in one place and 64 in another with neither saying
        # so. Code remains the fallback for a set that has never flushed.
        stored_patterns: dict[str, str] = {}
        for r in db.conn.execute(
            "SELECT key, payload FROM settings WHERE set_id = ? "
            "AND supersedes IS NULL AND deprecated = 0",
            (schemas.SCHEMA_META_SET_ID,),
        ).fetchall():
            try:
                pattern = (json.loads(r["payload"]) or {}).get("access_pattern")
            except Exception:
                continue
            if pattern:
                stored_patterns[r["key"]] = pattern
    except Exception:
        return out
    finally:
        db.close()

    for row in rows:
        pattern = stored_patterns.get(
            f"{row['set_id']}#{row['schema_revision']}")
        if pattern is None:
            pattern = _access_pattern_for(row["set_id"], row["schema_revision"])
        if pattern is None:
            # Neither the database nor this process can say what this set's
            # rows are. Unknown is not permitted, and the difference decides
            # whether a row gets deleted, so it is returned separately.
            unjudged.append({
                "id": row["id"], "set_id": row["set_id"], "key": row["key"],
                "supersedes": row["supersedes"],
                "reason": "no access pattern recorded in the database or "
                          "registered in this process",
            })
            continue
        if pattern not in _REPLACED_PATTERNS:
            continue
        if row["supersedes"] not in own_ids:
            # Not in this database means either a peer's row -- which is
            # exactly what overriding is for -- or a row that was deleted,
            # leaving this one stranded. Those look identical from here and
            # need opposite treatment, so ask whether the target exists at
            # all. A stranded row resolves to nothing, is unreachable by
            # key, and is invisible to every read: precisely the row a sweep
            # exists to find, and the one an "assume peer" shortcut skips.
            try:
                if _fetch_setting_any_org(row["supersedes"], org) is not None:
                    continue
            except Exception:
                continue
        out.append({
            "id": row["id"],
            "set_id": row["set_id"],
            "key": row["key"],
            "supersedes": row["supersedes"],
            "access_pattern": pattern,
            "state": row["publication_state"],
            "base_present": row["supersedes"] in own_ids,
            "created_at": row["created_at"],
        })
    _LAST_UNJUDGED[org or ""] = unjudged
    return out


#: Amendment rows the last :func:`illegal_amendments` call could not judge,
#: per org. Kept beside the result rather than raising, because a partial
#: answer is still useful -- it just must not be mistaken for a whole one.
_LAST_UNJUDGED: dict[str, list[dict]] = {}


def unjudged_amendments(*, org: str | None) -> list[dict]:
    """Rows the last sweep of ``org`` could not evaluate in this process."""
    return list(_LAST_UNJUDGED.get(org or "", []))


def _assert_publication_band(
    set_id: str, revision: int, state: str, *, action: str = "written at",
) -> None:
    """Refuse a publication state this set's schema does not permit.

    The state is the only control over who reads a row across an
    organization boundary, and a set knows what it is for: a sealed
    credential is never for peers, a shared contract is useless unless they
    can read it. Checking it at every write AND at promotion matters
    because the two failures arrive by different doors -- one row created
    carelessly, one row promoted later by someone tidying up.

    The message names the states that are allowed rather than only the one
    refused, because the next thing the caller needs is what to type.
    """
    allowed = schemas.states_allowed(set_id, int(revision))
    if state in allowed:
        return
    band = schemas.declared_band(set_id, int(revision))
    raise ValueError(
        f"{set_id}#{revision} declares publication band "
        f"{band[0]!r}..{band[1]!r}: it cannot be {action} {state!r}. "
        f"Allowed: {', '.join(allowed)}. The band says what this set is for "
        f"-- rows that must not leave their own database, or definitions "
        f"other organizations have to be able to read."
    )


def _collapse_amendment(
    target: dict, payload_overrides: dict, org: str | None,
) -> str | None:
    """Rewrite the row instead of appending a patch, where that is possible.

    Returns the rewritten row's id, or None when a patch row is genuinely
    required -- the set permits amendment, or the row belongs to another
    organization and cannot be rewritten from here.

    The row keeps its own publication state. The caller's ``state`` argument
    described the patch row that is no longer created, and applying it here
    would let an unstated default silently demote a published row.
    """
    pattern = _access_pattern_for(target["set_id"], target["schema_revision"])
    if pattern not in _REPLACED_PATTERNS:
        return None
    if not _is_own_row(target["id"], org):
        return None
    if schemas.declared_vault_tier(target["set_id"]) is not None:
        # A vaulted row is an encrypted object revision, and its identity is
        # derived from the row id — so rewriting the row in place would
        # address the revision already committed, which admits only a
        # byte-identical replay. The amendment appends instead. This is the
        # same reason a patch row remains for another organization's row: what
        # forces one is physics, not intent.
        return None
    merged = json_merge_patch(json.loads(target["payload"]), payload_overrides)
    upsert_by_key(
        target["set_id"], int(target["schema_revision"]), target["key"],
        merged, org=org, state=target["publication_state"],
    )
    return target["id"]


def _is_own_row(setting_id: str, org: str | None) -> bool:
    """Whether ``setting_id`` lives in the caller's own database."""
    try:
        db = _open_read(org)
    except Exception:
        return False
    try:
        return db.conn.execute(
            "SELECT 1 FROM settings WHERE id = ?", (setting_id,)
        ).fetchone() is not None
    finally:
        db.close()


def _existing_base_id(
    set_id: str, schema_revision: int, key: str, org: "str | None",
) -> "str | None":
    """The live base row for a key, or None. Bases only — an override or an
    exclusion is not a write target."""
    # This is a pure existence probe.  A writable open needlessly runs schema
    # initialization, takes write locks, and on a fleet-synced personal store
    # can collide with the authored-write hook before the real write begins.
    db = _open_read(org, set_id)
    try:
        row = db.conn.execute(
            "SELECT id FROM settings "
            "WHERE set_id = ? AND schema_revision = ? AND key = ? "
            "  AND supersedes IS NULL AND excludes IS NULL "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (set_id, int(schema_revision), key),
        ).fetchone()
    finally:
        db.close()
    return row["id"] if row else None


def write_by_key(
    set_id: str,
    schema_revision: int,
    key: str,
    payload: dict,
    *,
    org: "str | None | _CallerOrgSentinel",
    state: str = "raw",
    vault_policy_class_id: str | None = None,
) -> str:
    """Write a complete payload to ``key``, by whichever call the set allows.

    One entry point for callers who have a key and a full payload and simply
    want the value to be current — the API route and ``graph set add``. It
    exists because "write this value" is a single intent that three different
    functions implement, and which one is correct is a property of the SET, not
    something the caller can reasonably know.

    Getting that wrong is quiet rather than loud, which is why this dispatches
    rather than documenting. :func:`upsert_by_key` refuses the two patterns it
    cannot serve, and the obvious repair — fall back to :func:`add_setting` — is
    right for only one of them. On a vault set, ``add_setting`` against a key
    that already exists mints a SECOND base for it, which is the duplicate-base
    bug upserting was introduced to fix, resurfacing where it is hardest to see:
    vault rows are opaque locators, so the two bases cannot be told apart by
    reading them.

    The three cases:

    * ``append_only_log`` — :func:`add_setting` always. Every write is a new
      row; that is what the pattern means.
    * a vault set — :func:`add_setting` for the FIRST value, since it mints and
      seals the initial revision, then :func:`override_setting` to change it,
      which seals a fresh revision of the same object and leaves the old one as
      it was. ``override_setting`` takes the complete payload on a vault set,
      not a patch, so passing this one straight through is correct.
    * everything else — :func:`upsert_by_key`, unchanged, so the fix that
      introduced it still holds.

    A personal secured set seals directly to its persisted policy-class public
    key and is intentionally cold-writable. Organization vault sets still need
    the process sealer registered at unlock; without it the write below raises
    ``VaultSealerMissing`` with the plaintext unwritten. This function adds no
    plaintext fallback on either path.
    """
    org = _resolve_org_arg(org)
    append_only = _access_pattern_for(set_id, schema_revision) == "append_only_log"
    vaulted = schemas.declared_vault_tier(set_id) is not None

    if not append_only and not vaulted:
        return upsert_by_key(
            set_id, schema_revision, key, payload, org=org, state=state,
        )
    if append_only:
        return add_setting(
            set_id, schema_revision, key, payload, org=org, state=state,
            vault_policy_class_id=vault_policy_class_id,
        )

    existing = _existing_base_id(set_id, int(schema_revision), key, org)
    if existing is None:
        return add_setting(
            set_id, schema_revision, key, payload, org=org, state=state,
            vault_policy_class_id=vault_policy_class_id,
        )
    return override_setting(
        existing, payload, org=org, state=state,
        vault_policy_class_id=vault_policy_class_id,
    )


def override_setting(
    target_id: str,
    payload_overrides: dict,
    *,
    org: "str | None | _CallerOrgSentinel",
    state: str = "raw",
    vault_policy_class_id: str | None = None,
) -> str:
    """Create a Setting with ``supersedes=target_id`` and partial payload.

    Validation runs against the target's ``(set_id, schema_revision)``
    using the *merged* shape — what consumers will actually see. The
    override Setting itself lives in org's DB; the *target* may
    be either own-org or peer-origin — overriding peer content is the
    expected way to adapt shared primitives to a local org. Raises
    ``LookupError`` only when the target exists nowhere (own or peers).

    On a ``@vaulted`` set this is how a secret CHANGES: the target's stored
    payload is an opaque locator, so the merge replaces it whole and what this
    row carries is the complete new value — validated as such, sealed into a
    fresh revision of the same object, and stored as its own locator. A
    partial patch is not available there and would not be meaningful anyway:
    the writer cannot merge onto a plaintext it may hold no factor to open.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    org = _resolve_org_arg(org)
    _guard_protected_setting_id(target_id, org)
    if state not in VALID_STATES:
        raise ValueError(f"invalid state {state!r}; valid: {VALID_STATES}")
    target = _fetch_setting_any_org(target_id, org)
    if target is None:
        raise LookupError(f"override target not found: {target_id!r}")

    # Resolution applies ONLY overrides whose ``supersedes`` points at the
    # chosen base row (see read_set / explain_setting). An override of an
    # override is silently inert: the write succeeds, the row is canonical and
    # undeprecated, and the value never resolves. Rather than create a dead
    # row, retarget to the base when the caller passed the current tail — they
    # meant "patch the latest state" — and refuse when they passed an older
    # revision, which would be editing history.
    #
    # Retargeting also fixes a second-order surprise: validation merges the
    # patch onto the *target's* payload, so overriding a partial override used
    # to fail schema validation for missing required fields. Against the base
    # the payload is complete and validation behaves.
    if target["supersedes"]:
        base_id, tail_id = _base_and_tail_for(target, org)
        if target_id != tail_id:
            raise ValueError(
                f"cannot override {target_id!r}: it is a superseded revision, "
                f"not the current one. Override the base ({base_id!r}) or the "
                f"newest override ({tail_id!r})."
            )
        target_id = base_id
        target = _fetch_setting_any_org(base_id, org)
        if target is None:
            raise LookupError(f"override base not found: {base_id!r}")

    # A set whose schema declares one row per key is REPLACED, not amended.
    # Appending a patch row there contradicts the declaration and makes every
    # later read merge a chain that grows by one layer per edit. The caller
    # asked for a value to win, which is what "override" means; a patch row
    # was never part of that promise, only of how it happened to be stored.
    #
    # So the same request is satisfied by rewriting the row. What forces a
    # patch row is not intent but physics: a row in ANOTHER organization's
    # database cannot be rewritten from here, which is the case overrides
    # exist for and the one place the chain remains.
    _assert_publication_band(
        target["set_id"], target["schema_revision"], state)

    collapsed = _collapse_amendment(target, payload_overrides, org)
    if collapsed is not None:
        return collapsed

    db = _open(org)
    try:
        target_payload = json.loads(target["payload"])
        merged = json_merge_patch(target_payload, payload_overrides)
        schemas.validate_payload(
            target["set_id"], int(target["schema_revision"]), merged,
        )
        sid = str(uuid4())
        stored_payload = payload_overrides
        vault_tier = schemas.declared_vault_tier(target["set_id"])
        if vault_tier is not None:
            # A vaulted target's stored payload is an opaque locator, so the
            # merge above replaced it whole: what this row supersedes it with
            # is the WHOLE new value, which is why validation just held the
            # override to the complete schema rather than to a patch. It is
            # sealed into its own revision of the same object; the row stores
            # the new locator, and the previous revision stays exactly as it
            # was written.
            stored_payload = _seal_vault_payload(
                set_id=target["set_id"],
                schema_revision=int(target["schema_revision"]),
                key=target["key"],
                setting_id=sid,
                payload=merged,
                tier=vault_tier,
                org=org,
                policy_class_id=vault_policy_class_id,
            )
        now = _now_iso()
        expires_at = schemas.cache_expires_at(
            target["set_id"], int(target["schema_revision"]), now,
        )
        db.conn.execute(
            "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
            "publication_state, supersedes, created_at, updated_at, "
            "expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (sid, target["set_id"], int(target["schema_revision"]),
             target["key"], json.dumps(stored_payload),
             state, target_id, now, now, expires_at),
        )
        db.conn.commit()
    finally:
        db.close()
    _call_emit_hook(
        operation="override",
        snapshot=_make_snapshot(
            target["set_id"], int(target["schema_revision"]),
            target["key"], state, False,
        ),
        org=org,
    )
    return sid


def exclude_setting(
    target_id: str,
    *,
    org: "str | None | _CallerOrgSentinel",
    state: str = "raw",
) -> str:
    """Create a Setting with ``excludes=target_id`` and empty payload.

    The exclude row itself lives in org's DB and only affects
    reads scoped to this caller — so peer-origin targets are allowed.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    org = _resolve_org_arg(org)
    _guard_protected_setting_id(target_id, org)
    if state not in VALID_STATES:
        raise ValueError(f"invalid state {state!r}; valid: {VALID_STATES}")
    target = _fetch_setting_any_org(target_id, org)
    if target is None:
        raise LookupError(f"exclude target not found: {target_id!r}")
    db = _open(org)
    try:
        sid = str(uuid4())
        now = _now_iso()
        expires_at = schemas.cache_expires_at(
            target["set_id"], int(target["schema_revision"]), now,
        )
        db.conn.execute(
            "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
            "publication_state, excludes, created_at, updated_at, "
            "expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (sid, target["set_id"], int(target["schema_revision"]),
             target["key"], "{}", state, target_id, now, now, expires_at),
        )
        db.conn.commit()
    finally:
        db.close()
    _call_emit_hook(
        operation="exclude",
        snapshot=_make_snapshot(
            target["set_id"], int(target["schema_revision"]),
            target["key"], state, False,
        ),
        org=org,
    )
    return sid


def _reject_peer_setting_target(
    setting_id: str,
    org: str | None,
) -> None:
    """Raise :class:`ops.CrossOrgWriteError` when ``setting_id`` lives in a peer DB.

    Lookup order mirrors :func:`_fetch_setting_any_org`, but instead of
    returning the row we raise so promote/deprecate/remove fail fast
    with the structured cross-org error rather than a bare LookupError.
    """
    from .cross_org import open_peer_db, resolve_peers
    from .ops import CrossOrgWriteError  # local: avoid import cycle at top

    # Internal helper — public callers have already normalized via
    # ``_resolve_org_arg``, so ``org`` is a literal slug-or-None.
    resolved_org = org
    for peer in sorted(resolve_peers(resolved_org, None)):
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        row = peer_db.conn.execute(
            "SELECT 1 FROM settings WHERE id = ?", (setting_id,)
        ).fetchone()
        if row is not None:
            raise CrossOrgWriteError(setting_id, peer)


def promote_setting(
    setting_id: str,
    to_state: str,
    *,
    org: "str | None | _CallerOrgSentinel",
) -> None:
    """Transition publication_state. ``LookupError`` if not present.

    Peer-origin targets raise :class:`ops.CrossOrgWriteError` — only the
    origin org may alter a Setting's publication state.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    org = _resolve_org_arg(org)
    _guard_protected_setting_id(setting_id, org)
    if to_state not in VALID_STATES:
        raise ValueError(f"invalid state {to_state!r}; valid: {VALID_STATES}")
    existing = _fetch_setting_any_org(setting_id, org)
    if existing is not None:
        _assert_publication_band(
            existing["set_id"], existing["schema_revision"], to_state,
            action="promoted to")
    now = _now_iso()
    db = _open(org)
    snapshot = None
    try:
        # Look up set_id/schema_revision before the UPDATE so we can
        # recompute expires_at for cache rows. TTL is a sliding window
        # from updated_at, so any UPDATE that bumps updated_at must
        # restamp expires_at (HTTP-cache max-age semantics).
        pre = db.conn.execute(
            "SELECT set_id, schema_revision FROM settings WHERE id = ?",
            (setting_id,),
        ).fetchone()
        if pre is None:
            _reject_peer_setting_target(setting_id, org)
            raise LookupError(f"setting not found: {setting_id!r}")
        expires_at = schemas.cache_expires_at(
            pre["set_id"], int(pre["schema_revision"]), now,
        )
        db.conn.execute(
            "UPDATE settings SET publication_state = ?, updated_at = ?, "
            "expires_at = ? WHERE id = ?",
            (to_state, now, expires_at, setting_id),
        )
        post = db.conn.execute(
            "SELECT set_id, schema_revision, key, publication_state, "
            "deprecated FROM settings WHERE id = ?",
            (setting_id,),
        ).fetchone()
        if post is not None:
            snapshot = _make_snapshot(
                post["set_id"], post["schema_revision"], post["key"],
                post["publication_state"], post["deprecated"],
            )
        db.conn.commit()
    finally:
        db.close()
    if snapshot is not None:
        _call_emit_hook(operation="promote", snapshot=snapshot, org=org)


def deprecate_setting(
    setting_id: str,
    *,
    org: "str | None | _CallerOrgSentinel",
    successor_id: str | None = None,
) -> None:
    """Mark a Setting deprecated, optionally pointing at a successor.

    Peer-origin targets raise :class:`ops.CrossOrgWriteError`.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    org = _resolve_org_arg(org)
    _guard_protected_setting_id(setting_id, org)
    now = _now_iso()
    db = _open(org)
    snapshot = None
    try:
        pre = db.conn.execute(
            "SELECT set_id, schema_revision FROM settings WHERE id = ?",
            (setting_id,),
        ).fetchone()
        if pre is None:
            _reject_peer_setting_target(setting_id, org)
            raise LookupError(f"setting not found: {setting_id!r}")
        expires_at = schemas.cache_expires_at(
            pre["set_id"], int(pre["schema_revision"]), now,
        )
        db.conn.execute(
            "UPDATE settings SET deprecated = 1, successor_id = ?, "
            "updated_at = ?, expires_at = ? WHERE id = ?",
            (successor_id, now, expires_at, setting_id),
        )
        post = db.conn.execute(
            "SELECT set_id, schema_revision, key, publication_state, "
            "deprecated FROM settings WHERE id = ?",
            (setting_id,),
        ).fetchone()
        if post is not None:
            snapshot = _make_snapshot(
                post["set_id"], post["schema_revision"], post["key"],
                post["publication_state"], post["deprecated"],
            )
        db.conn.commit()
    finally:
        db.close()
    if snapshot is not None:
        _call_emit_hook(operation="deprecate", snapshot=snapshot, org=org)


def undeprecate_setting(
    setting_id: str,
    *,
    org: "str | None | _CallerOrgSentinel",
) -> None:
    """Reverse a previous :func:`deprecate_setting` -- put the row back into
    the live, resolvable set and clear any successor pointer it carried.

    Only reverses a deprecation; it does not restore a hard-deleted row (see
    :func:`remove_setting`, which is not reversible). ``org`` is **required**
    — see :func:`add_setting` for the contract.
    """
    org = _resolve_org_arg(org)
    _guard_protected_setting_id(setting_id, org)
    now = _now_iso()
    db = _open(org)
    snapshot = None
    try:
        pre = db.conn.execute(
            "SELECT set_id, schema_revision FROM settings WHERE id = ?",
            (setting_id,),
        ).fetchone()
        if pre is None:
            _reject_peer_setting_target(setting_id, org)
            raise LookupError(f"setting not found: {setting_id!r}")
        expires_at = schemas.cache_expires_at(
            pre["set_id"], int(pre["schema_revision"]), now,
        )
        db.conn.execute(
            "UPDATE settings SET deprecated = 0, successor_id = NULL, "
            "updated_at = ?, expires_at = ? WHERE id = ?",
            (now, expires_at, setting_id),
        )
        post = db.conn.execute(
            "SELECT set_id, schema_revision, key, publication_state, "
            "deprecated FROM settings WHERE id = ?",
            (setting_id,),
        ).fetchone()
        if post is not None:
            snapshot = _make_snapshot(
                post["set_id"], post["schema_revision"], post["key"],
                post["publication_state"], post["deprecated"],
            )
        db.conn.commit()
    finally:
        db.close()
    if snapshot is not None:
        _call_emit_hook(operation="undeprecate", snapshot=snapshot, org=org)


def remove_settings_by_key_prefix(
    set_id: str,
    *,
    prefix: str,
    org: "str | None | _CallerOrgSentinel",
) -> int:
    """Hard-delete every base/override Setting under ``set_id`` whose key
    matches ``<prefix>:%`` (composite-key child rows).

    ``org`` is **required** — see :func:`add_setting` for the contract.

    Bypasses the per-row ``raw``-only constraint that :func:`remove_setting`
    enforces because this is a system-level cleanup path: callers wipe
    binding rows when the worktree they describe is destroyed, regardless
    of publication state. The companion review_state cache is *not*
    deleted by callers using this — bindings die per-worktree, caches
    persist per-org.

    Returns the number of rows deleted. Cross-org targets raise
    :class:`ops.CrossOrgWriteError` indirectly: peer DBs are read-only
    from this caller's perspective, so the SQL ``DELETE`` simply finds
    nothing in the local DB and returns 0 — there is no silent writethrough.
    """
    org = _resolve_org_arg(org)
    _guard_protected_set(set_id)
    db = _open(org, set_id)
    deleted_keys: list[tuple[str, int, str, str, bool]] = []
    try:
        like = _prefix_like_pattern(prefix)
        rows = db.conn.execute(
            "SELECT id, set_id, schema_revision, key, publication_state, "
            "deprecated FROM settings "
            "WHERE set_id = ? AND key LIKE ? ESCAPE '\\'",
            (set_id, like),
        ).fetchall()
        for row in rows:
            deleted_keys.append((
                row["set_id"], int(row["schema_revision"]), row["key"],
                row["publication_state"], bool(row["deprecated"]),
            ))
        cur = db.conn.execute(
            "DELETE FROM settings "
            "WHERE set_id = ? AND key LIKE ? ESCAPE '\\'",
            (set_id, like),
        )
        n = cur.rowcount or 0
        db.conn.commit()
    finally:
        db.close()
    for sid, srev, key, state, dep in deleted_keys:
        _call_emit_hook(
            operation="delete",
            snapshot=_make_snapshot(sid, srev, key, state, dep),
            org=org,
        )
    return n


def remove_setting(
    setting_id: str,
    *,
    org: "str | None | _CallerOrgSentinel",
) -> None:
    """Hard-delete a Setting. Spec restricts to ``raw``; higher states must
    be deprecated first.

    Peer-origin targets raise :class:`ops.CrossOrgWriteError`.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    org = _resolve_org_arg(org)
    _guard_protected_setting_id(setting_id, org)
    db = _open(org)
    snapshot = None
    try:
        row = db.conn.execute(
            "SELECT set_id, schema_revision, key, publication_state, "
            "deprecated FROM settings WHERE id = ?",
            (setting_id,),
        ).fetchone()
        if not row:
            _reject_peer_setting_target(setting_id, org)
            raise LookupError(f"setting not found: {setting_id!r}")
        if row["publication_state"] != "raw":
            raise ValueError(
                f"can only remove raw Settings; this is "
                f"{row['publication_state']!r} — deprecate first"
            )
        # Capture pre-delete state so the post-commit hook still has
        # set_id/key/etc. — the row is gone after DELETE.
        snapshot = _make_snapshot(
            row["set_id"], row["schema_revision"], row["key"],
            row["publication_state"], row["deprecated"],
        )
        db.conn.execute("DELETE FROM settings WHERE id = ?", (setting_id,))
        db.conn.commit()
    finally:
        db.close()
    if snapshot is not None:
        _call_emit_hook(operation="delete", snapshot=snapshot, org=org)


def remove_raw_settings(
    setting_ids: list[str],
    *,
    org: "str | None | _CallerOrgSentinel",
) -> int:
    """Hard-delete a validated batch of raw Settings in one transaction."""
    org = _resolve_org_arg(org)
    ids = list(dict.fromkeys(str(value) for value in setting_ids if str(value)))
    if not ids:
        return 0
    db = _open(org)
    snapshots: list[dict[str, Any]] = []
    try:
        db.conn.execute("BEGIN IMMEDIATE")
        for offset in range(0, len(ids), 900):
            chunk = ids[offset : offset + 900]
            placeholders = ",".join("?" for _value in chunk)
            rows = db.conn.execute(
                "SELECT id, set_id, schema_revision, key, publication_state, "
                f"deprecated FROM settings WHERE id IN ({placeholders})",
                chunk,
            ).fetchall()
            found = {row["id"] for row in rows}
            missing = [setting_id for setting_id in chunk if setting_id not in found]
            if missing:
                raise LookupError(f"setting not found: {missing[0]!r}")
            for set_id in {row["set_id"] for row in rows}:
                _guard_protected_set(set_id)
            non_raw = [row for row in rows if row["publication_state"] != "raw"]
            if non_raw:
                raise ValueError(
                    "can only remove raw Settings; "
                    f"{non_raw[0]['id']!r} is {non_raw[0]['publication_state']!r}"
                )
            snapshots.extend(
                _make_snapshot(
                    row["set_id"],
                    row["schema_revision"],
                    row["key"],
                    row["publication_state"],
                    row["deprecated"],
                )
                for row in rows
            )
            db.conn.execute(
                f"DELETE FROM settings WHERE id IN ({placeholders})",
                chunk,
            )
        db.conn.commit()
    except Exception:
        db.conn.rollback()
        raise
    finally:
        db.close()
    for snapshot in snapshots:
        _call_emit_hook(operation="delete", snapshot=snapshot, org=org)
    return len(snapshots)


# ── Read paths ───────────────────────────────────────────────


def list_set_ids(
    *,
    org: "str | None | _CallerOrgSentinel",
    peers: list[str] | None = None,
) -> list[str]:
    """Distinct ``set_id`` values visible to org.

    Own DB contributes every ``set_id``; peer DBs contribute only
    ``set_id`` values backed by a ``published``/``canonical`` row. See
    graph://bcce359d-a1d § External view.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    from .cross_org import (
        PEER_VISIBLE_STATES,
        open_peer_db,
        resolve_peers,
    )

    org = _resolve_org_arg(org)
    seen: set[str] = set()
    db = _open_read(org)
    try:
        rows = db.conn.execute(
            "SELECT DISTINCT set_id FROM settings"
        ).fetchall()
        for r in rows:
            if r[0]:
                seen.add(r[0])
    finally:
        db.close()

    resolved_org = org
    for peer in sorted(resolve_peers(resolved_org, peers)):
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        placeholders = ",".join("?" for _ in PEER_VISIBLE_STATES)
        rows = peer_db.conn.execute(
            f"SELECT DISTINCT set_id FROM settings "
            f"WHERE publication_state IN ({placeholders}) "
            f"  AND excludes IS NULL AND deprecated = 0",
            list(PEER_VISIBLE_STATES),
        ).fetchall()
        for r in rows:
            if r[0]:
                seen.add(r[0])
    return sorted(seen)


def _resolve_settings_caller(org: str | None) -> str | None:
    """Thin wrapper over ``ops._resolve_org`` to avoid the import cycle
    at module import time. One resolution, no ambient source: explicit
    kwarg, then the per-request contextvar the dashboard middleware binds
    from the caller's credential, then ``None`` (scopeless)."""
    from . import ops as _ops
    return _ops._resolve_org(org)


def resolve_setting_strict(
    value: str,
    *,
    org: "str | None | _CallerOrgSentinel",
    peers: list[str] | None = None,
) -> dict | list[dict] | None:
    """Resolve a Setting by full id or id-prefix, own-first then peers.

    Mirrors :func:`resolve_source_strict` semantics. Own-org sees every
    state; peer DBs only contribute rows whose ``publication_state`` is
    ``published`` or ``canonical``. Returns:

    * dict — single row (success)
    * list[dict] — multiple matches in one scope (caller surfaces as
      ambiguity error with candidate UUIDs)
    * None — no match anywhere in scope

    The resolver short-circuits as soon as a scope (own, then each peer)
    yields any match, so an exact own-org hit wins over a peer prefix
    match — consistent with how source resolution already behaves.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    from .cross_org import (
        PEER_VISIBLE_STATES,
        open_peer_db,
        resolve_peers,
    )

    org = _resolve_org_arg(org)
    db = _open_read(org)
    try:
        # 1. Exact id, own org
        row = db.conn.execute(
            "SELECT * FROM settings WHERE id = ?", (value,)
        ).fetchone()
        if row is not None:
            return dict(row)
        # 2. Prefix id, own org
        rows = db.conn.execute(
            "SELECT * FROM settings WHERE id LIKE ?", (f"{value}%",)
        ).fetchall()
        if len(rows) == 1:
            return dict(rows[0])
        if len(rows) > 1:
            return [dict(r) for r in rows]
    finally:
        db.close()

    # 3. Peer scan (public surface only)
    resolved_org = org
    placeholders = ",".join("?" for _ in PEER_VISIBLE_STATES)
    for peer in sorted(resolve_peers(resolved_org, peers)):
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        # exact id
        row = peer_db.conn.execute(
            "SELECT * FROM settings WHERE id = ?", (value,)
        ).fetchone()
        if row is not None and row["publication_state"] in PEER_VISIBLE_STATES:
            return dict(row)
        # prefix id
        rows = peer_db.conn.execute(
            f"SELECT * FROM settings WHERE id LIKE ? "
            f"  AND publication_state IN ({placeholders})",
            (f"{value}%", *PEER_VISIBLE_STATES),
        ).fetchall()
        if len(rows) == 1:
            return dict(rows[0])
        if len(rows) > 1:
            return [dict(r) for r in rows]
    return None


def read_set_key(
    set_id: str,
    key: str,
    *,
    org: "str | None | _CallerOrgSentinel",
    peers: list[str] | None = None,
) -> dict | None:
    """The one member of ``set_id`` under ``key``, as ``read_set`` sees it.

    Named for ``read_set``, whose semantics it shares exactly: this is that
    call narrowed to a single key. The previous name said "resolve", which
    reads as "the resolved value" and hid that the answer is a ROW -- so a
    caller reasonably took its payload for the resolved one when it was the
    base's own, and every override was silently absent.

    Uses :func:`read_set` so the answer matches what consumers see at
    runtime — same precedence, same cross-org visibility, same exclude
    rules. Returns the underlying base row dict (the one that becomes
    ``ResolvedSetting.id`` post-merge), or None if no member matches.

    ``payload`` comes back parsed, with the schema's declared defaults
    applied, exactly as a :func:`read_set` member's does. Handing back the
    stored JSON string here instead would mean every caller that wants a
    field parses it itself, and each of them would separately have to know
    to apply the defaults — which is the disagreement those defaults exist
    to end.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    org = _resolve_org_arg(org)
    members = read_set(set_id, org=org, peers=peers)
    for m in members.members:
        if m.key == key:
            # m.id is the chosen base id; fetch the row in its origin DB.
            row = _fetch_setting_any_org(m.id, org, set_id)
            if row is None:
                return None
            # The row identifies the BASE -- which is what a caller
            # targeting an override, a promote or an exclude needs. Its
            # payload is the RESOLVED one: base plus every override that
            # applies, with declared defaults filled, exactly what a
            # read_set member carries. Handing back the base's own payload
            # here reads as an answer and silently drops every override,
            # and the shape gives nothing away.
            row["payload"] = m.payload
            # A vault secret that did not open has no payload, and a row whose
            # payload is None and says nothing else is exactly the absence a
            # refusal must not become. Carried the same way the member carries
            # it, and absent on every other setting.
            if m.vault_error is not None:
                row["vault_error"] = m.vault_error.to_dict()
            if m.sealed_content_key is not None:
                row["sealed_content_key"] = m.sealed_content_key
            return row
    return None


def chain_setting(
    set_id: str,
    key: str,
    *,
    org: "str | None | _CallerOrgSentinel",
    peers: list[str] | None = None,
) -> dict | None:
    """Walk the supersedes chain for ``(set_id, key)`` and return the
    per-layer contributions.

    Returns ``None`` if no winning base exists. Otherwise returns:

    .. code-block:: python

        {
            "set_id": ...,
            "key": ...,
            "layers": [
                {
                    "id": ..., "kind": "base"|"override",
                    "state": ..., "stored_revision": ..., "org": ...,
                    "supersedes": ..., "created_at": ...,
                    "patch": {...},   # full base payload, or override patch
                    "result": {...},  # merged-so-far after this layer
                },
                ...
            ],
            "final": {...},          # final resolved payload
        }

    Override application order matches :func:`read_set`: each override
    whose ``supersedes`` points at the chosen base contributes one
    layer. Overrides of overrides are not currently followed (the
    underlying resolver does not chase them either).

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    from .cross_org import (
        PEER_VISIBLE_STATES,
        open_peer_db,
        resolve_peers,
    )

    org = _resolve_org_arg(org)
    resolved_org = org
    raw_rows: list[tuple[str | None, Any]] = []
    db = _open(org, set_id, for_read=True)
    try:
        rows = db.conn.execute(
            "SELECT rowid AS _rowid, * FROM settings WHERE set_id = ? AND key = ? "
            "  AND deprecated = 0",
            (set_id, key),
        ).fetchall()
        for r in rows:
            raw_rows.append((resolved_org, r))
    finally:
        db.close()

    placeholders = ",".join("?" for _ in PEER_VISIBLE_STATES)
    for peer in _resolution_peers(set_id, resolved_org, peers):
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        rows = peer_db.conn.execute(
            f"SELECT rowid AS _rowid, * FROM settings WHERE set_id = ? AND key = ? "
            f"  AND deprecated = 0 "
            f"  AND publication_state IN ({placeholders})",
            (set_id, key, *PEER_VISIBLE_STATES),
        ).fetchall()
        for r in rows:
            raw_rows.append((peer, r))

    if not raw_rows:
        return None

    bases: list[tuple[str | None, Any]] = []
    overrides: list[tuple[str | None, Any]] = []
    excludes: list[tuple[str | None, Any]] = []
    for src_org, r in raw_rows:
        if r["excludes"] is not None:
            excludes.append((src_org, r))
        elif r["supersedes"] is not None:
            overrides.append((src_org, r))
        else:
            bases.append((src_org, r))

    excluded_ids = {r["excludes"] for (_, r) in excludes}
    candidate_bases = [
        (o, r) for (o, r) in bases if r["id"] not in excluded_ids
    ]
    if not candidate_bases:
        return None
    # Same six-step ordering the resolver uses — a chain view that picks a
    # different base than read_set describes a value nothing returns.
    candidate_bases = _rank_candidates(
        candidate_bases,
        reading_org=resolved_org,
        now=None,
    )
    if not candidate_bases:
        return None
    chosen_org, chosen_row = candidate_bases[0]

    base_payload = json.loads(chosen_row["payload"])
    layers: list[dict] = [{
        "id": chosen_row["id"],
        "kind": "base",
        "state": chosen_row["publication_state"],
        "stored_revision": int(chosen_row["schema_revision"]),
        "org": chosen_org,
        "supersedes": chosen_row["supersedes"],
        "created_at": chosen_row["created_at"],
        "patch": base_payload,
        "result": dict(base_payload),
    }]

    merged = dict(base_payload)
    # Same ordering as read_set, so an explanation matches what actually
    # resolves rather than describing a different merge order.
    for ov_org, ov_row in sorted(
        overrides, key=lambda om: (om[1]["created_at"] or "", om[1]["_rowid"]),
    ):
        if ov_row["supersedes"] != chosen_row["id"]:
            continue
        ov_payload = json.loads(ov_row["payload"])
        merged = json_merge_patch(merged, ov_payload)
        layers.append({
            "id": ov_row["id"],
            "kind": "override",
            "state": ov_row["publication_state"],
            "stored_revision": int(ov_row["schema_revision"]),
            "org": ov_org,
            "supersedes": ov_row["supersedes"],
            "created_at": ov_row["created_at"],
            "patch": ov_payload,
            "result": dict(merged),
        })

    return {
        "set_id": set_id,
        "key": key,
        "layers": layers,
        "final": merged,
    }


def _base_and_tail_for(row: dict, org: str | None) -> tuple[str, str]:
    """Walk ``row``'s supersedes chain to its base; return ``(base_id, tail_id)``.

    ``tail_id`` is the newest live override on that base — the row a caller
    means when they say "the current one" — and equals ``base_id`` when the
    base carries no live overrides.

    Used by :func:`override_setting` to decide whether an override aimed at
    another override is a reasonable "patch the latest state" (retarget it to
    the base, which is the only thing resolution honours) or an attempt to
    edit a superseded revision (refuse).
    """
    seen: set[str] = set()
    cur = row
    while cur["supersedes"] and cur["id"] not in seen:
        seen.add(cur["id"])
        nxt = _fetch_setting_any_org(cur["supersedes"], org)
        if nxt is None:
            break
        cur = nxt
    base_id = str(cur["id"])
    db = _open_read(org)
    tail = db.conn.execute(
        "SELECT id FROM settings WHERE supersedes = ? AND deprecated = 0 "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (base_id,),
    ).fetchone()
    return base_id, (str(tail["id"]) if tail else base_id)


def _fetch_setting_any_org(
    setting_id: str,
    org: str | None,
    set_id: str | None = None,
) -> dict | None:
    """Return the Setting row as a plain dict, searching own-org then peers.

    Peer rows must satisfy the public-surface filter
    (``publication_state IN ('published','canonical')``). Used by
    override/exclude targets, which are allowed to reference peer
    content — the *override row* itself still lands in org's DB.

    ``set_id`` lets the search start at the set's DECLARED home. Without it
    the search begins at the caller's organization, and the operator's own
    store is nobody's peer -- so a personal-homed row was found by the
    member read and then lost by the lookup that fetches it, one line later,
    for the same key. Two halves of one read disagreeing about which
    database holds the answer is the shape this whole class of bug takes.
    """
    from .cross_org import (
        PEER_VISIBLE_STATES,
        open_peer_db,
        resolve_peers,
    )

    db = _open_read(org, set_id)
    try:
        row = db.conn.execute(
            "SELECT * FROM settings WHERE id = ?", (setting_id,)
        ).fetchone()
        if row is not None:
            return dict(row)
    finally:
        db.close()

    resolved_org = _resolve_settings_caller(org)
    for peer in sorted(resolve_peers(resolved_org, None)):
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        row = peer_db.conn.execute(
            "SELECT * FROM settings WHERE id = ?", (setting_id,)
        ).fetchone()
        if row is not None and row["publication_state"] in PEER_VISIBLE_STATES:
            return dict(row)
    return None


def get_setting(
    setting_id: str,
    *,
    org: "str | None | _CallerOrgSentinel",
    peers: list[str] | None = None,
    target_revision: int | None = None,
    model: type[Any] | None = None,
) -> ResolvedSetting[Any] | None:
    """Resolve a single Setting by id, own-first then peers.

    ``None`` if not found or dropped by revision constraints. Peer rows
    only surface when their ``publication_state`` is ``published`` or
    ``canonical``.

    ``model=`` mirrors :func:`read_set`: when supplied, the payload is
    passed through ``model.model_validate`` and the returned
    :class:`ResolvedSetting` carries the typed instance. A validation
    failure logs WARN and returns ``None``.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    from .cross_org import (
        PEER_VISIBLE_STATES,
        open_peer_db,
        resolve_peers,
    )

    org = _resolve_org_arg(org)
    resolved_org = org
    db = _open_read(org)
    try:
        row = db.conn.execute(
            "SELECT * FROM settings WHERE id = ?", (setting_id,)
        ).fetchone()
        if row:
            resolved = _row_to_resolved(row, org=resolved_org)
    finally:
        db.close()

    if 'resolved' not in locals():
        resolved = None
        for peer in sorted(resolve_peers(resolved_org, peers)):
            peer_db = open_peer_db(peer)
            if peer_db is None:
                continue
            row = peer_db.conn.execute(
                "SELECT * FROM settings WHERE id = ?", (setting_id,)
            ).fetchone()
            if row and row["publication_state"] in PEER_VISIBLE_STATES:
                resolved = _row_to_resolved(row, org=peer)
                break
        if resolved is None:
            return None

    if target_revision is not None:
        transformed, _reason = _shape_to_target(resolved, target_revision)
        if transformed is None:
            return None
        resolved = transformed

    if model is not None:
        typed = _apply_model(resolved, model, DropAccounting())
        return typed
    return resolved


def _set_is_org_key_namespaced(set_id: str) -> bool:
    """Whether a set's KEY places an org PREFIX on a compound ``<org>:<...>``
    key.

    True when the set declares an @org_writeback_namespace (org sessions
    write into ``org_slug:<suffix>``) OR its primary key strategy is COMPOUND
    with ``org`` / ``org_slug`` as the first of several segments
    (``org:workspace_id``, ``org_slug:host``). Those keys are per-org rows in
    one shared store, so an org caller's read is scoped to its ``<org>:``
    prefix.

    A single-segment ``org_slug`` key (``autonomy.org`` — the org's OWN
    identity row, keyed by the bare slug) is NOT namespaced this way and
    returns False: it carries no ``<org>:`` prefix to filter on, and it is
    read across orgs BY DESIGN (that is how the dashboard renders every org's
    name/colour/icon). Filtering it hid every org but the caller's — the
    2026-08-31 dropdown regression. Org-HOMED sets also return False; their
    own database isolates them.
    """
    if schemas.declared_org_writeback_key_strategy(set_id):
        return True
    for revision in range(1, 12):
        cls = schemas.get_schema(set_id, revision)
        if cls is None:
            continue
        strategy = getattr(cls, "_key_strategy", "") or ""
        # Compound only: the org must be a PREFIX of a larger key, not the
        # whole key. A bare 'org_slug' (the org's own identity) has no
        # <org>:<suffix> shape to isolate on.
        if ":" not in strategy:
            return False
        first = strategy.split(":", 1)[0].strip("[]")
        return first in ("org", "org_slug")
    return False


def _prefix_like_pattern(prefix: str) -> str:
    r"""Build the SQL ``LIKE`` pattern for a composite-key prefix match.

    ``prefix=X`` becomes ``'X:%'`` — the ``:`` separator is auto-appended,
    so callers pass parent identity unadorned (e.g., ``prefix="enterprise-ng"``
    matches ``enterprise-ng:vuln-diff`` but not ``enterprise-ng-alt:foo``).
    ``%`` / ``_`` / ``\`` inside the prefix are escaped so literal
    characters keep literal meaning under ``LIKE``.
    """
    escaped = (
        prefix
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"{escaped}:%"


def count_set_rows(
    set_id: str,
    *,
    org: "str | None | _CallerOrgSentinel",
    prefix: str | None = None,
) -> int:
    """Raw row count for *set_id* (optionally under a composite-key prefix).

    One ``COUNT(*)`` against the caller org's own database — no resolution,
    no peers, no payload parsing. This exists for progress reporting: a
    loader that wants to say "0 of N settings" before paying for the real
    :func:`read_set`. The count is rows, not resolved members (overrides
    and historical duplicates are included), so treat it as a ceiling.
    """
    org = _resolve_org_arg(org)
    clause = " AND deprecated = 0"
    params: list[Any] = [set_id]
    if prefix is not None:
        clause += " AND key LIKE ? ESCAPE '\\'"
        params.append(_prefix_like_pattern(prefix))
    db = _open_read(org, set_id)
    try:
        row = db.conn.execute(
            "SELECT COUNT(*) FROM settings WHERE set_id = ?" + clause,
            params).fetchone()
        return int(row[0]) if row else 0
    finally:
        db.close()


def _vault_key_control(org: str | None, set_id: str, cache: dict):
    """The key control for *org*, consulted at most once per read.

    Returns ``(control, failure_message)`` — a control, or the reason there
    isn't one. Cached because one ``read_set`` can resolve many secrets of one
    organization and the holder may be doing real work (opening a store,
    reading a ramfs page) to answer.
    """
    if org in cache:
        return cache[org]
    holder = _vault_key_holder
    if holder is None:
        answer = (None, "The vault is locked in this process and only the "
                        "operator can unlock it (sign-in or warm client).")
    else:
        try:
            control = holder(set_id=set_id, org=org)
        except Exception as exc:  # noqa: BLE001 — one refusal, whatever failed
            answer = (None, "The vault key holder failed while opening keys "
                            f"and cannot be used ({exc}).")
        else:
            answer = (
                (control, None) if control is not None
                else (None, "The vault holds no keys for this scope and only "
                            "the operator can unlock it (sign-in or warm "
                            "client).")
            )
    cache[org] = answer
    return answer


def _unwrap_vault_locator(
    locator, *, set_id: str, key: str, setting_id: str, declared_tier: str,
    org: str | None, cache: dict,
):
    """Step six for one member: the locator, resolved back to its value.

    Returns ``(payload, sealed_content_key, failure)`` with exactly one of the
    three not ``None``. Nothing here derives a key, opens a bridge or touches
    a suite: ``storage_object.open_revision_for_member`` runs the whole
    sequence through ``storagekit.objects.read_object``, and this function's
    entire job is choosing which refusal a failure is.
    """
    # Imported here rather than at module scope for the reason the sealer is:
    # settings_ops is imported by every CLI entry point and the vault pulls in
    # the whole storage stack.
    from tools.network.storagekit.errors import StorageError, SuiteError
    from tools.network.storagekit.objects import StateUnreachableError
    from tools.vault import personal_object, storage_object as vault_storage_object
    from tools.vault.errors import VaultError

    def refuse(reason: str, message: str):
        return None, None, VaultReadFailure(
            reason=reason, message=f"{set_id}/{key}: {message}"
        )

    if personal_object.is_personal_locator(locator):
        if declared_tier == "secured":
            try:
                gated = personal_object.inspect_revision(
                    locator, set_id=set_id, key=key,
                )
            except SuiteError:
                return refuse(
                    VAULT_UNKNOWN_SUITE,
                    "This record's encryption suite is unrecognized and cannot "
                    "be processed.",
                )
            except VaultError:
                return refuse(
                    VAULT_DECRYPTION_FAILED,
                    "This record could not be verified and cannot be processed.",
                )
            return None, asdict(gated), None
        if declared_tier == "audited":
            # Audited releases unattended: the warm delegate private half opens
            # the CEK inline. Cold, there is no private half and the read fails
            # closed — never plaintext.
            private_hex = _personal_delegate_audited_key
            if private_hex is None:
                return refuse(
                    VAULT_NO_KEY_HOLDER,
                    "The vault is locked in this process and only the operator "
                    "can unlock it (sign-in or warm client).",
                )
            try:
                payload = personal_object.open_audited_revision(
                    locator,
                    set_id=set_id,
                    key=key,
                    setting_id=setting_id,
                    delegate_private_hex=private_hex,
                )
            except SuiteError:
                return refuse(
                    VAULT_UNKNOWN_SUITE,
                    "This record's encryption suite is unrecognized and cannot "
                    "be processed.",
                )
            except VaultError:
                return refuse(
                    VAULT_DECRYPTION_FAILED,
                    "This record could not be verified and cannot be processed.",
                )
            return payload, None, None
        return refuse(
            VAULT_TIER_MISMATCH,
            "This record's release tier does not match the set's and "
            "cannot be processed.",
        )

    if not vault_storage_object.is_vault_locator(locator):
        return refuse(
            VAULT_NOT_A_LOCATOR,
            "This record presented an unexpected value and cannot be processed.",
        )
    try:
        reference = vault_storage_object.parse_locator(locator)
    except VaultError:
        return refuse(
            VAULT_NOT_A_LOCATOR,
            "This record presented an unexpected value and cannot be processed.",
        )
    if reference["tier"] != declared_tier:
        # The set says how its secrets are released; a row saying otherwise is
        # a downgrade sitting in the database, not a per-row preference.
        return refuse(
            VAULT_TIER_MISMATCH,
            "This record's release tier does not match the set's and cannot "
            "be processed.",
        )

    control, missing = _vault_key_control(org, set_id, cache)
    if control is None:
        return refuse(VAULT_NO_KEY_HOLDER, missing)

    try:
        opened = vault_storage_object.open_revision_for_member(
            locator, holdings=control.holdings, content_store=control.content_store,
        )
    except SuiteError:
        return refuse(
            VAULT_UNKNOWN_SUITE,
            "This record's encryption suite is unrecognized and cannot be "
            "processed.",
        )
    except StateUnreachableError:
        # The storage layer refuses both the same way. Holding a generation
        # descended from this one means the backward recovery SHOULD have
        # worked, so the edge is what is missing.
        descended = vault_storage_object.holds_a_descendant_of(
            reference["storage_state_id"], control.holdings,
        )
        if descended:
            return refuse(
                VAULT_MISSING_BRIDGE,
                "This identity holds a newer generation but the link back to "
                "this secret has not reached this machine — check the "
                "synchronization frontier.",
            )
        return refuse(
            VAULT_NO_KEY_HELD,
            "No key that opens this secret has reached this identity yet — "
            "check the synchronization frontier.",
        )
    except (StorageError, VaultError):
        return refuse(
            VAULT_DECRYPTION_FAILED,
            "This record could not be decrypted and cannot be processed.",
        )

    if isinstance(opened, vault_storage_object.SealedContentKey):
        # A secured secret, opened as far as membership goes. The human factor
        # is applied above this layer, by whoever holds it.
        return None, asdict(opened), None
    return opened, None, None


def _resolved_vault_locator(
    set_id: str,
    key: str,
    base_id: str,
    *,
    org: str | None,
):
    """Return the opaque locator selected for one already-resolved member.

    ``read_set`` deliberately replaces a secured member's locator with the
    factor-gated ``sealed_content_key`` view.  The operator ceremony needs the
    locator again only after the human has supplied the factor, and it must use
    the *same* base/override fold as the original read.  Keep that recovery
    private to the host process; a locator is never added to an HTTP response.

    Vault sets are band-pinned raw, so their winning base and overrides live in
    the set's declared home rather than a peer database.  ``_open_read`` routes
    that home (personal for the built-in credential sets) exactly as
    ``read_set`` does.
    """
    db = _open_read(org, set_id)
    try:
        rows = db.conn.execute(
            "SELECT rowid AS _rowid, * FROM settings "
            "WHERE set_id = ? AND key = ? AND deprecated = 0 "
            "AND (id = ? OR supersedes = ?)",
            (set_id, key, base_id, base_id),
        ).fetchall()
    finally:
        db.close()

    base = next((row for row in rows if row["id"] == base_id), None)
    if base is None:
        raise LookupError(
            f"the secured setting changed before approval ({set_id}/{key})"
        )
    locator = json.loads(base["payload"])
    for row in sorted(
        (row for row in rows if row["supersedes"] == base_id),
        key=lambda row: (row["created_at"] or "", row["_rowid"]),
    ):
        locator = json_merge_patch(locator, json.loads(row["payload"]))
    return locator


def open_secured_setting(
    set_id: str,
    key: str,
    *,
    setting_id: str,
    sealed_content_key_digest: str,
    opener_seeds: dict[str, bytes],
    org: "str | None | _CallerOrgSentinel",
) -> dict:
    """Open one frozen secured Setting through the human-factor chokepoint.

    This is intentionally the only production seam that turns a
    ``sealed_content_key`` outcome into plaintext.  The approval layer freezes
    ``setting_id`` and a digest of the factor-gated view; this function re-runs
    normal Settings resolution and refuses any drift before applying opener
    material.  Future audit-before-open attribution belongs immediately above
    the final ``open_revision`` call below.

    The returned value is the Setting payload, never its content-encryption
    key.  Callers own and must zero ``opener_seeds`` after this call.
    """
    import hashlib

    from tools.network.idkit.canonical import canonical_json
    from tools.vault import personal_object, storage_object as vault_storage_object
    from tools.vault.errors import VaultError
    from tools.vault.key_holder import _scoped_db
    from tools.vault.store import VaultStore
    from tools.graph.schemas.vault_policy_class import VAULT_POLICY_CLASS_SET_ID

    org = _resolve_org_arg(org)
    if schemas.declared_vault_tier(set_id) != "secured":
        raise VaultError(f"{set_id!r} is not a secured vault set")

    current = next(
        (member for member in read_set(set_id, org=org, peers=[]).members
         if member.key == key),
        None,
    )
    if current is None or current.id != setting_id:
        raise VaultError(
            f"the secured setting changed before approval ({set_id}/{key})"
        )
    sealed = current.sealed_content_key
    if not isinstance(sealed, dict):
        raise VaultError(
            f"{set_id}/{key} is not awaiting a human-factor open"
        )
    current_digest = hashlib.sha256(canonical_json(sealed)).hexdigest()
    if current_digest != sealed_content_key_digest:
        raise VaultError(
            f"the secured setting changed before approval ({set_id}/{key})"
        )

    locator = _resolved_vault_locator(
        set_id, key, setting_id, org=org,
    )
    personal_direct = personal_object.is_personal_locator(locator)
    control = None
    if personal_direct:
        gated = personal_object.inspect_revision(
            locator, set_id=set_id, key=key,
        )
    else:
        control, missing = _vault_key_control(current.org, set_id, {})
        if control is None:
            raise VaultError(missing)

        # Re-derive the factor-gated view from the immutable object named by
        # the locator. This closes the small read/fetch race without exposing
        # the locator or trusting only row metadata.
        gated = vault_storage_object.open_revision_for_member(
            locator,
            holdings=control.holdings,
            content_store=control.content_store,
        )
    if not isinstance(gated, vault_storage_object.SealedContentKey):
        raise VaultError(f"{set_id}/{key} no longer requires a factor")
    gated_digest = hashlib.sha256(canonical_json(asdict(gated))).hexdigest()
    if gated_digest != sealed_content_key_digest:
        raise VaultError(
            f"the secured setting changed before approval ({set_id}/{key})"
        )

    with VaultStore(_scoped_db(VAULT_POLICY_CLASS_SET_ID, current.org)) as store:
        policy_class = store.get_class(gated.policy_class_id)

    # The ONE mandatory future insertion point: write the attributed,
    # fail-closed audit event here, immediately before either storage shape can
    # apply a factor and yield plaintext.
    if personal_direct:
        opened = personal_object.open_revision(
            locator,
            set_id=set_id,
            key=key,
            setting_id=setting_id,
            policy_class=policy_class,
            opener_seeds=opener_seeds,
        )
    else:
        assert control is not None
        opened = vault_storage_object.open_revision(
            locator,
            holdings=control.holdings,
            content_store=control.content_store,
            policy_class=policy_class,
            opener_seeds=opener_seeds,
        )
    if not isinstance(opened, dict):
        raise VaultError(f"{set_id}/{key} opened to a non-object payload")
    return opened


def contested_keys(
    set_id: str,
    *,
    org: "str | None | _CallerOrgSentinel",
    peers: list[str] | None = None,
    now: int | None = None,
) -> list[dict]:
    """Keys of *set_id* whose value the organization is contesting.

    Contention is more than one ELIGIBLE signed slot at the winning rung and
    store (graph://21a0da9e-1c2 "Resolution"): resolution still answers
    exactly one thing — there is no third state for consumers — and this is
    the separate query that makes the disagreement visible. Two authorized
    members churning a value is governance's problem to settle; while it
    lasts, it shows here.

    Returns ``[{key, slots: [{terminal_persona, signed_at, state, org,
    resolves}]}]`` — ``resolves`` marks the slot resolution currently
    answers with. Slots are the contenders at the winning rung and store
    only; unsigned rows never contest (single writer, one base).
    """
    from .cross_org import PEER_VISIBLE_STATES, open_peer_db, resolve_peers

    org = _resolve_org_arg(org)
    resolved_org = org
    raw_rows: list[tuple[str | None, Any]] = []
    excluded_ids: set = set()
    db = _open_read(org, set_id)
    try:
        for r in db.conn.execute(
            "SELECT rowid AS _rowid, * FROM settings WHERE set_id = ? "
            "  AND deprecated = 0 AND supersedes IS NULL AND excludes IS NULL",
            (set_id,),
        ).fetchall():
            raw_rows.append((resolved_org, r))
        # Exclusions apply here exactly as in read_set, or this report's
        # winning rung/store baseline could name a base resolution drops.
        # Only unsigned rows can carry or be targeted by one (the envelope
        # forbids excludes on org rows), so this matters solely in the
        # pre-signing transitional world — which is the world this ships in.
        for r in db.conn.execute(
            "SELECT excludes FROM settings WHERE set_id = ? "
            "  AND deprecated = 0 AND excludes IS NOT NULL",
            (set_id,),
        ).fetchall():
            excluded_ids.add(r["excludes"])
    finally:
        db.close()
    placeholders = ",".join("?" for _ in PEER_VISIBLE_STATES)
    for peer in _resolution_peers(set_id, resolved_org, peers):
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        try:
            rows = peer_db.conn.execute(
                f"SELECT rowid AS _rowid, * FROM settings WHERE set_id = ? "
                f"  AND deprecated = 0 AND supersedes IS NULL "
                f"  AND excludes IS NULL "
                f"  AND publication_state IN ({placeholders})",
                (set_id, *PEER_VISIBLE_STATES),
            ).fetchall()
            for r in peer_db.conn.execute(
                f"SELECT excludes FROM settings WHERE set_id = ? "
                f"  AND deprecated = 0 AND excludes IS NOT NULL "
                f"  AND publication_state IN ({placeholders})",
                (set_id, *PEER_VISIBLE_STATES),
            ).fetchall():
                excluded_ids.add(r["excludes"])
        except sqlite3.OperationalError as exc:
            if "no such table: settings" in str(exc).lower():
                continue
            raise
        for r in rows:
            raw_rows.append((peer, r))

    by_key: dict[str, list] = {}
    for src_org, row in raw_rows:
        if row["id"] in excluded_ids:
            continue
        by_key.setdefault(row["key"], []).append((src_org, row))

    contested: list[dict] = []
    for key in sorted(by_key):
        ranked = _rank_candidates(
            by_key[key],
            reading_org=resolved_org,
            now=now,
        )
        if not ranked:
            continue
        top_org, top_row = ranked[0]
        top_rung = PRECEDENCE.get(top_row["publication_state"], 99)
        top_store = _store_rank(top_org, resolved_org)
        slots = [
            (src, row) for src, row in ranked
            if _row_col(row, "terminal_persona") is not None
            and PRECEDENCE.get(row["publication_state"], 99) == top_rung
            and _store_rank(src, resolved_org) == top_store
        ]
        if len(slots) < 2:
            continue
        contested.append({
            "key": key,
            "slots": [
                {
                    "terminal_persona": row["terminal_persona"],
                    "signed_at": _row_col(row, "signed_at"),
                    "state": row["publication_state"],
                    "org": src,
                    "resolves": row["id"] == top_row["id"],
                }
                for src, row in slots
            ],
        })
    return contested


def read_set(
    set_id: str,
    *,
    org: "str | None | _CallerOrgSentinel",
    peers: list[str] | None = None,
    target_revision: int | None = None,
    min_revision: int | None = None,
    prefix: str | None = None,
    model: type[Any] | None = None,
    now: int | None = None,
) -> SetMembers[Any]:
    """Resolve members of *set_id* visible to org's session.

    Six-step pipeline (see graph://0d3f750f-f9c § Resolution algorithm):
    1. per-DB fetch (single DB today, peers loop ready),
    2. group by key into bases / overrides / exclusions,
    3. drop excluded bases,
    4. pick highest-precedence base per key (tie-break: most recent),
    5. apply overrides via JSON-merge-patch,
    6. on a ``@vaulted`` set only, open the merged locator.

    Step six is last because steps one to five branch on row METADATA alone
    and never parse a payload. Precedence discards every base candidate but
    one, and the store is append-only, so decrypting earlier would open the
    whole edit history and throw it away. It is also the only ordering that
    is correct: merge patch cannot combine two ciphertexts, which is why the
    locator is a scalar and why what step six opens is one object.

    A vault member that does not open resolves to a
    :class:`VaultReadFailure` on ``vault_error`` — never to a locator, never
    to ciphertext, and never to absence — and the rest of the set resolves
    normally around it. A ``secured`` one resolves to its still-sealed
    content key on ``sealed_content_key``; applying the human factor belongs
    to whoever holds it, not to resolution.

    Optional ``min_revision`` filters before transform; ``target_revision``
    upconverts (or drops if no chain). ``prefix=X`` restricts the query
    to composite keys under the ``X:`` parent (child-set pattern —
    ``<parent>:<child>`` with ``:`` as the canonical separator). ``model=``
    validates each resolved payload through ``model.model_validate`` and
    produces ``SetMembers[Model]``; validation failures are dropped and
    counted in ``dropped.schema_invalid``. Returns a ``SetMembers`` with
    drop accounting populated.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    from .cross_org import (
        PEER_VISIBLE_STATES,
        open_peer_db,
        resolve_peers,
    )

    org = _resolve_org_arg(org)
    resolved_org = org
    raw_rows: list[tuple[str | None, Any]] = []
    deprecated_filtered = 0
    prefix_clause = ""
    prefix_params: tuple[Any, ...] = ()
    if prefix is not None:
        prefix_clause = " AND key LIKE ? ESCAPE '\\'"
        prefix_params = (_prefix_like_pattern(prefix),)

    # ORG-NAMESPACE ISOLATION. A set whose KEY is organizationally namespaced
    # — its key strategy's first segment is the org (``org:...`` /
    # ``org_slug:...``), or it declares an @org_writeback_namespace — lives in
    # a SHARED store (the machine store or the operator's personal store), one
    # database holding every org's rows. An organization-scoped caller reading
    # such a set may see ONLY rows in its own ``<org>:`` namespace: another
    # org's key names and existence are not its business. This is the read
    # side of the boundary the write side already enforces
    # (derive_org_writeback_key / the org-prefixed key strategy). Generic by
    # construction — it triggers on the declared key SHAPE, not on any set id
    # (today: vault.secured, credential-file, workspace.image-build). An
    # org-HOMED set needs no filter: its own per-org database already isolates
    # it, and its key is not org-prefixed. Values stay sealed regardless — a
    # @vaulted read returns ciphertext / a sealed locator, never plaintext.
    #
    # resolved_org is an authenticated org slug (org_ops._validate_slug forbids
    # ':' and empties), so the LIKE '<org>:%' prefix cannot be widened or
    # escaped; the truthiness check is belt-and-braces against an empty slug.
    _iso_prefix = None
    if (
        _set_is_org_key_namespaced(set_id)
        and isinstance(resolved_org, str)
        and resolved_org
        and resolved_org not in ("personal", "machine")
    ):
        _iso_prefix = resolved_org
        prefix_clause += " AND key LIKE ? ESCAPE '\\'"
        prefix_params = (*prefix_params, _prefix_like_pattern(resolved_org))
    db = _open_read(org, set_id)
    try:
        rows = db.conn.execute(
            f"SELECT rowid AS _rowid, * FROM settings WHERE set_id = ? "
            f"  AND deprecated = 0"
            f"{prefix_clause}",
            (set_id, *prefix_params),
        ).fetchall()
        for r in rows:
            raw_rows.append((resolved_org, r))
        # Count what we just filtered out so operators can spot drift
        # (e.g. a deprecated row still surfacing through some other path).
        dep_row = db.conn.execute(
            f"SELECT COUNT(*) AS n FROM settings WHERE set_id = ? "
            f"  AND deprecated = 1"
            f"{prefix_clause}",
            (set_id, *prefix_params),
        ).fetchone()
        if dep_row is not None:
            deprecated_filtered += int(dep_row["n"])
    finally:
        db.close()

    # Peer-org contributions: public surface only.
    #
    # A set whose band forbids every peer-visible state has no public
    # surface by construction, so no peer database is opened for it at all.
    # The band already refuses the write and the promotion that would make
    # such a row readable; this refuses to SERVE one, so a row that reached
    # a peer-visible state by some path nobody anticipated -- a direct
    # write, a restore, a migration -- still does not cross the boundary.
    # The two guards fail independently, which is the point of having both.
    resolved_peers = _resolution_peers(set_id, resolved_org, peers)
    for peer in resolved_peers:
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        placeholders = ",".join("?" for _ in PEER_VISIBLE_STATES)
        try:
            rows = peer_db.conn.execute(
                f"SELECT rowid AS _rowid, * FROM settings WHERE set_id = ? "
                f"  AND deprecated = 0 "
                f"  AND publication_state IN ({placeholders})"
                f"{prefix_clause}",
                (set_id, *PEER_VISIBLE_STATES, *prefix_params),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table: settings" in str(exc).lower():
                continue
            raise
        for r in rows:
            raw_rows.append((peer, r))
        try:
            dep_row = peer_db.conn.execute(
                f"SELECT COUNT(*) AS n FROM settings WHERE set_id = ? "
                f"  AND deprecated = 1 "
                f"  AND publication_state IN ({placeholders})"
                f"{prefix_clause}",
                (set_id, *PEER_VISIBLE_STATES, *prefix_params),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table: settings" in str(exc).lower():
                continue
            raise
        if dep_row is not None:
            deprecated_filtered += int(dep_row["n"])

    dropped = DropAccounting()
    dropped.deprecated_filtered = deprecated_filtered

    # Apply min_revision floor before transform (drops live rows wholesale).
    survivors: list[tuple[str | None, Any]] = []
    for src_org, row in raw_rows:
        if min_revision is not None and int(row["schema_revision"]) < min_revision:
            dropped.below_min_revision += 1
            continue
        survivors.append((src_org, row))

    # Group by key.
    bases: dict[str, list[tuple[str | None, Any]]] = {}
    overrides: dict[str, list[tuple[str | None, Any]]] = {}
    excludes: dict[str, list[tuple[str | None, Any]]] = {}
    for src_org, row in survivors:
        bucket = (
            "excludes" if row["excludes"] is not None
            else "overrides" if row["supersedes"] is not None
            else "bases"
        )
        target = (excludes if bucket == "excludes"
                  else overrides if bucket == "overrides"
                  else bases)
        target.setdefault(row["key"], []).append((src_org, row))

    members: list[ResolvedSetting[Any]] = []
    keys_seen = sorted(bases.keys())
    # Step six applies to a vaulted set and to nothing else, so an ordinary
    # set answers this once and never consults the vault again.
    declared_tier = schemas.declared_vault_tier(set_id)
    key_control_cache: dict = {}
    for key in keys_seen:
        excluded_ids = {row["excludes"] for (_, row) in excludes.get(key, [])}
        candidate_bases = [
            (src_org, row) for (src_org, row) in bases[key]
            if row["id"] not in excluded_ids
        ]
        if not candidate_bases:
            continue

        # The six-step slot ordering (auto-y2ubq): eligibility and the
        # plausibility window filter, then rung → store → revision →
        # signed_at → persona hash, with the legacy created_at tiebreak
        # surviving only among unsigned single-writer rows. Revision sits
        # below store because the reachability filter below has already
        # dropped every candidate that cannot serve a requested revision;
        # within one store it still discriminates two coexisting
        # generations of a value.
        candidate_bases = _rank_candidates(
            candidate_bases,
            reading_org=resolved_org,
            now=now,
            dropped=dropped,
        )
        if not candidate_bases:
            continue

        # Asking for a revision should consider the rows that can be served as
        # it, rather than picking a winner first and discovering afterwards
        # that it cannot be. Otherwise a row stored at exactly the requested
        # revision loses to one that cannot reach it, and the read comes back
        # empty with the answer in the set the whole time.
        #
        # If NOTHING can reach the target, the winner is chosen as usual and
        # dropped below with its reason, so drop accounting says the same thing
        # it always did.
        if target_revision is not None:
            reachable = [
                om for om in candidate_bases
                if _can_reach_revision(set_id, om[1], target_revision)
            ]
            if reachable:
                candidate_bases = reachable

        chosen_org, chosen_row = candidate_bases[0]

        # Apply overrides whose supersedes targets this base, oldest first so
        # last-write-wins is guaranteed rather than incidental. Without an
        # explicit order, two overrides patching the same key resolve by
        # whatever order SQLite happened to return rows in.
        merged_payload = json.loads(chosen_row["payload"])
        for (_, ov_row) in sorted(
            overrides.get(key, []),
            key=lambda om: (om[1]["created_at"] or "", om[1]["_rowid"]),
        ):
            if ov_row["supersedes"] == chosen_row["id"]:
                ov_payload = json.loads(ov_row["payload"])
                merged_payload = json_merge_patch(merged_payload, ov_payload)

        resolved = _row_to_resolved(chosen_row, org=chosen_org)

        # Step six — the merged locator, opened.
        if declared_tier is not None:
            opened, sealed, failure = _unwrap_vault_locator(
                merged_payload,
                set_id=set_id,
                key=key,
                setting_id=chosen_row["id"],
                declared_tier=declared_tier,
                org=chosen_org,
                cache=key_control_cache,
            )
            if failure is not None or sealed is not None:
                # Neither is a value, so nothing downstream that shapes a
                # value applies: defaults, upconversion and model validation
                # would all be operating on something that is not the payload,
                # and model validation in particular would DROP the member —
                # turning a refusal into an absence.
                resolved.payload = None
                resolved.vault_error = failure
                resolved.sealed_content_key = sealed
                members.append(resolved)
                continue
            merged_payload = opened

        resolved.payload = _apply_declared_defaults(
            chosen_row["set_id"], chosen_row["schema_revision"], merged_payload,
        )

        # Optional revision transform.
        if target_revision is not None:
            transformed, reason = _shape_to_target(resolved, target_revision)
            if transformed is None:
                if reason == "no_upconvert_path":
                    dropped.no_upconvert_path += 1
                elif reason == "above_target_no_downgrade":
                    dropped.above_target_no_downgrade += 1
                continue
            resolved = transformed

        # Optional payload typing via Pydantic (or compatible) model.
        if model is not None:
            typed = _apply_model(resolved, model, dropped)
            if typed is None:
                continue
            resolved = typed

        members.append(resolved)

    return SetMembers(members=members, dropped=dropped)


def read_owned_set(
    set_id: str,
    *,
    org: "str | None | _CallerOrgSentinel",
    target_revision: int | None = None,
    min_revision: int | None = None,
    prefix: str | None = None,
    model: type[Any] | None = None,
) -> SetMembers[Any]:
    """Resolve *set_id* from its owning database only.

    ``read_set`` deliberately composes the owner's rows with published and
    canonical rows from subscribed peer orgs.  Identity, authentication, and
    authority-binding decisions must not use that federated view: choosing
    ``org=None`` or a concrete org selects the owning database, but does *not*
    by itself disable peer composition.  This named reader makes the security
    boundary explicit and prevents those call sites from silently forgetting
    the otherwise easy-to-miss ``peers=[]`` argument.
    """
    return read_set(
        set_id,
        org=org,
        peers=[],
        target_revision=target_revision,
        min_revision=min_revision,
        prefix=prefix,
        model=model,
    )


# ── Schema versioning helpers ────────────────────────────────


def _can_reach_revision(set_id: str, row: Any, target_revision: int) -> bool:
    """Whether this stored row can be served AS ``target_revision``.

    Exactly at it, or below it with an upconvert chain. Above it is not
    reachable: there is no downconvert, by design.
    """
    stored = int(row["schema_revision"])
    if stored == target_revision:
        return True
    if stored > target_revision:
        return False
    return schemas.upconvert_chain(set_id, stored, target_revision) is not None


def _shape_to_target(
    resolved: ResolvedSetting,
    target_revision: int,
) -> tuple[ResolvedSetting | None, str]:
    """Return (transformed, '') on success, or (None, reason) on drop.

    Identity case copies through. Lower stored → upconvert via registry chain.
    Higher stored → drop with reason ``above_target_no_downgrade``.
    Missing chain → drop with reason ``no_upconvert_path``.
    """
    stored = resolved.stored_revision
    if stored == target_revision:
        out = replace(resolved, target_revision=target_revision)
        return out, ""
    if stored > target_revision:
        return None, "above_target_no_downgrade"
    converted = schemas.upconvert_payload(
        resolved.set_id, stored, target_revision, resolved.payload,
    )
    if converted is None:
        return None, "no_upconvert_path"
    out = replace(
        resolved,
        payload=converted,
        target_revision=target_revision,
        upconverted=True,
    )
    return out, ""


# ── Storage migration ────────────────────────────────────────


def migrate_setting_revisions(
    set_id: str,
    to_revision: int,
    *,
    org: "str | None | _CallerOrgSentinel",
    dry_run: bool = False,
) -> MigrationReport:
    """Rewrite stored rows at lower revisions up to ``to_revision``.

    Optional housekeeping — read-time upconvert keeps things working without
    it. Rows already at ``to_revision`` are skipped; rows above it are left
    alone (downgrades are explicit opt-ins, not part of migrate). Rows with
    no upconvert chain are reported, not rewritten.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    org = _resolve_org_arg(org)
    _guard_protected_set(set_id)
    report = MigrationReport(
        set_id=set_id, to_revision=int(to_revision), dry_run=dry_run,
    )
    now = _now_iso()
    # Migrate rewrites every row to ``to_revision`` — its TTL is the only
    # one we need to know. Compute once and reuse for every cache-row
    # rewrite in this loop.
    expires_at = schemas.cache_expires_at(set_id, int(to_revision), now)
    db = _open(org, set_id)
    affected_snapshots: list[dict] = []
    try:
        rows = db.conn.execute(
            "SELECT id, schema_revision, payload, key, publication_state, "
            "deprecated FROM settings "
            "WHERE set_id = ? AND excludes IS NULL",
            (set_id,),
        ).fetchall()
        for row in rows:
            stored = int(row["schema_revision"])
            if stored == to_revision:
                report.already_at_target += 1
                continue
            if stored > to_revision:
                report.above_target += 1
                continue
            payload = json.loads(row["payload"])
            converted = schemas.upconvert_payload(
                set_id, stored, to_revision, payload,
            )
            if converted is None:
                report.no_upconvert_path += 1
                continue
            report.affected_ids.append(row["id"])
            report.rewrote += 1
            if not dry_run:
                db.conn.execute(
                    "UPDATE settings SET payload = ?, schema_revision = ?, "
                    "updated_at = ?, expires_at = ? WHERE id = ?",
                    (json.dumps(converted), int(to_revision), now,
                     expires_at, row["id"]),
                )
                # Schema revision is now to_revision; key / state /
                # deprecated unchanged by migrate.
                affected_snapshots.append(_make_snapshot(
                    set_id, int(to_revision), row["key"],
                    row["publication_state"], row["deprecated"],
                ))
        if not dry_run:
            db.conn.commit()
    finally:
        db.close()
    if not dry_run:
        for snap in affected_snapshots:
            _call_emit_hook(operation="migrate", snapshot=snap, org=org)
    return report


# ── Helpers ──────────────────────────────────────────────────


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tracked_set_id_from_row_id(
    row_id: str | None, org: str | None,
) -> str | None:
    if not row_id:
        return None
    # Resolve the caller sentinel BEFORE the fetch (settings-owner ack,
    # 2026-08-14): raw CALLER_ORG leaked into cross_org.resolve_peers'
    # sqlite binding and crashed; the wrapper's guard swallowed it into
    # set_id=None, undercounting per-set diag activity. The public API
    # normalizes exactly this way on every call — this helper missed it.
    org = _resolve_org_arg(org)
    row = _fetch_setting_any_org(row_id, org)
    if row is None:
        return None
    return row.get("set_id")


def _tracked_settings_call(
    operation: str,
    *,
    kind: str,
    set_id_getter: Callable[[tuple[Any, ...], dict[str, Any], Any], str | None] | None = None,
    result_count_getter: Callable[[tuple[Any, ...], dict[str, Any], Any], int] | None = None,
):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            # Public API normalizes ``org=`` via ``_resolve_org_arg``;
            # mirror that here so stats labels reflect the resolved slug
            # (including the cascade result when ``CALLER_ORG`` is in
            # play). Missing ``org=`` would raise ``TypeError`` inside
            # the wrapped function — the wrapper still records the
            # failed call.
            try:
                resolved_org = _resolve_org_arg(kwargs.get("org"))
            except Exception:
                resolved_org = None
            ok = False
            result = None
            try:
                result = fn(*args, **kwargs)
                ok = True
                return result
            finally:
                set_id = None
                if set_id_getter is not None:
                    try:
                        set_id = set_id_getter(args, kwargs, result)
                    except Exception:
                        set_id = None
                result_count = 0
                if ok:
                    if result_count_getter is not None:
                        try:
                            result_count = max(
                                int(result_count_getter(args, kwargs, result)),
                                0,
                            )
                        except Exception:
                            result_count = _stats_result_count(result)
                    else:
                        result_count = _stats_result_count(result)
                _record_settings_api_call(
                    operation=operation,
                    set_id=set_id,
                    org=resolved_org,
                    kind=kind,
                    ok=ok,
                    start=start,
                    result_count=result_count,
                )

        return wrapper

    return decorator


add_setting = _tracked_settings_call(
    "add_setting",
    kind="write",
    set_id_getter=lambda args, _kwargs, _result: args[0] if args else None,
)(add_setting)
upsert_by_key = _tracked_settings_call(
    "upsert_by_key",
    kind="write",
    set_id_getter=lambda args, _kwargs, _result: args[0] if args else None,
)(upsert_by_key)
override_setting = _tracked_settings_call(
    "override_setting",
    kind="write",
    set_id_getter=lambda _args, kwargs, result: _tracked_set_id_from_row_id(
        result, kwargs.get("org"),
    ),
)(override_setting)
exclude_setting = _tracked_settings_call(
    "exclude_setting",
    kind="write",
    set_id_getter=lambda _args, kwargs, result: _tracked_set_id_from_row_id(
        result, kwargs.get("org"),
    ),
)(exclude_setting)
promote_setting = _tracked_settings_call(
    "promote_setting",
    kind="write",
    set_id_getter=lambda args, kwargs, _result: _tracked_set_id_from_row_id(
        args[0] if args else None, kwargs.get("org"),
    ),
)(promote_setting)
deprecate_setting = _tracked_settings_call(
    "deprecate_setting",
    kind="write",
    set_id_getter=lambda args, kwargs, _result: _tracked_set_id_from_row_id(
        args[0] if args else None, kwargs.get("org"),
    ),
)(deprecate_setting)
remove_settings_by_key_prefix = _tracked_settings_call(
    "remove_settings_by_key_prefix",
    kind="write",
    set_id_getter=lambda args, _kwargs, _result: args[0] if args else None,
    result_count_getter=lambda _args, _kwargs, result: result,
)(remove_settings_by_key_prefix)
remove_setting = _tracked_settings_call(
    "remove_setting",
    kind="write",
)(remove_setting)
list_set_ids = _tracked_settings_call(
    "list_set_ids",
    kind="read",
)(list_set_ids)
resolve_setting_strict = _tracked_settings_call(
    "resolve_setting_strict",
    kind="read",
)(resolve_setting_strict)
chain_setting = _tracked_settings_call(
    "chain_setting",
    kind="read",
    set_id_getter=lambda args, _kwargs, _result: args[0] if args else None,
)(chain_setting)
get_setting = _tracked_settings_call(
    "get_setting",
    kind="read",
    set_id_getter=lambda _args, _kwargs, result: (
        getattr(result, "set_id", None)
        if result is not None else None
    ),
)(get_setting)
read_set = _tracked_settings_call(
    "read_set",
    kind="read",
    set_id_getter=lambda args, _kwargs, _result: args[0] if args else None,
)(read_set)
migrate_setting_revisions = _tracked_settings_call(
    "migrate_setting_revisions",
    kind="write",
    set_id_getter=lambda args, _kwargs, _result: args[0] if args else None,
    result_count_getter=lambda _args, _kwargs, result: (
        result.rewrote if result is not None else 0
    ),
)(migrate_setting_revisions)
