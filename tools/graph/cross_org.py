"""Cross-org read helpers (auto-txg5.4).

Centralises peer-org resolution, peer-DB opening, and merge primitives
(RRF for ranked search, chronological merge for recency feeds). Both
``ops.*`` and ``settings_ops.*`` call into this module so the peer
semantics stay in one place.

Peer-org visibility rules (from graph://bcce359d-a1d):

- Caller's own org DB is the "full surface" — every row is visible.
- Peer-org DBs are the "public surface" —
  ``publication_state IN ('published','canonical')`` only.
- Peer set defaults to *every other org DB under* ``data/orgs/*.db``; an
  operator may pin a narrower list via the
  ``autonomy.org.peer-subscription#1`` Setting in ``personal.db`` keyed
  by the caller's own slug. An **empty** list means "isolated from every
  other organization" — the operator's own stores (``personal``,
  ``machine``) remain peers, since they are not organizations one
  subscribes to; an **absent** Setting means "subscribe to every peer"
  (the default). Consumers MUST distinguish the two.
- ``GRAPH_DB`` env var shorts every routing decision so tests + overrides
  still pin a single DB; peer resolution returns an empty list in that
  mode.

This module never writes — peer DBs are opened ``mode='ro'`` so peer
state cannot be accidentally mutated. Pooled via ``GraphDB.for_org``.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from tools.data_paths import resolve_orgs_root

from .db import GraphDB, resolve_caller_db_path


# Public so tests can import and assert against it.
PEER_VISIBLE_STATES = ("published", "canonical")
PEER_STATE_SQL_CLAUSE = " AND s.publication_state IN ('published','canonical')"

PEER_SUBSCRIPTION_SET_ID = "autonomy.org.peer-subscription"
PEER_SUBSCRIPTION_REVISION = 1
PERSONAL_DB_SLUG = "personal"
MACHINE_DB_SLUG = "machine"

# RRF tuning per graph://bcce359d-a1d § Merge algorithms.
RRF_K = 60
OWN_ORG_BOOST = 1.5


# ── Peer resolution ──────────────────────────────────────────


def _orgs_root() -> Path:
    """Return ``data/orgs/`` honouring ``AUTONOMY_ORGS_DIR`` override."""
    from .db import DEFAULT_ORGS_DIR  # avoid circular at import time
    return resolve_orgs_root(None, default=DEFAULT_ORGS_DIR)


def list_org_slugs(*, root: Path | str | None = None) -> list[str]:
    """Enumerate org slugs by globbing ``data/orgs/*.db``. Alphabetical.

    Unlike :func:`org_ops.list_orgs`, does not open each DB to read the
    bootstrap row — cross-org reads treat filename as slug truth. This
    matches the peer-lookup side of ``find_references`` (which also
    falls back to ``path.stem`` when the orgs row is missing).
    """
    d = Path(root) if root else _orgs_root()
    if not d.exists():
        return []
    from .db import LOCAL_STORE_SLUGS

    # The local stores are not organizations (auto-35kmy). They live
    # beside this directory, not in it — and until the one-time relocation
    # has run on an older tree, a file still sitting here is STILL not an
    # organization, so enumeration never yields one either way.
    return sorted(
        p.stem for p in d.glob("*.db") if p.stem not in LOCAL_STORE_SLUGS
    )


def all_store_slugs(*, root: Path | str | None = None) -> list[str]:
    """Every store on this machine: organizations plus the operator's local
    stores that exist. For CONTENT scans — find a source wherever it lives,
    union sessions across stores — where the personal and machine stores
    hold rows exactly as organizations do. NOT for settings peer
    resolution: a local store enters a settings read only through
    ``resolve_peers``' explicit own-stores rule (auto-9uj7i), and an
    organization enumeration stays :func:`list_org_slugs`.
    """
    from .db import LOCAL_STORE_SLUGS, _local_store_db_path

    slugs = list(list_org_slugs(root=root))
    for name in LOCAL_STORE_SLUGS:
        if _local_store_db_path(name, root).exists():
            slugs.append(name)
    return sorted(slugs)


def _read_peer_subscription(caller_slug: str) -> list[str] | None:
    """Look up the peer-subscription Setting in ``personal.db``.

    Returns the configured ``peers`` list, or ``None`` when the Setting is
    absent (= default "subscribe to every peer"). Silent on IO errors so
    a missing ``personal.db`` collapses to the default.

    The Setting lives in ``personal.db`` because peer subscription is a
    per-operator preference, not a shared-org policy (see schema doc in
    ``schemas/org_peer_subscription.py``).
    """
    if os.environ.get("GRAPH_DB"):
        # Test-pinned mode: there are no real per-org DBs; skip.
        return None
    try:
        path = resolve_caller_db_path(PERSONAL_DB_SLUG)
    except Exception:
        return None
    if not Path(path).exists():
        return None
    try:
        db = GraphDB(path, mode="ro")
    except sqlite3.Error:
        return None
    try:
        try:
            row = db.conn.execute(
                "SELECT payload, publication_state FROM settings "
                "WHERE set_id = ? AND schema_revision = ? AND key = ? "
                "  AND excludes IS NULL AND deprecated = 0 "
                "ORDER BY CASE publication_state "
                "    WHEN 'canonical' THEN 0 WHEN 'published' THEN 1 "
                "    WHEN 'curated' THEN 2 ELSE 3 END, "
                "  created_at DESC LIMIT 1",
                (PEER_SUBSCRIPTION_SET_ID, PEER_SUBSCRIPTION_REVISION, caller_slug),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
    finally:
        db.close()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        return None
    peers = payload.get("peers") if isinstance(payload, dict) else None
    if not isinstance(peers, list):
        return None
    return [p for p in peers if isinstance(p, str) and p]


def resolve_peers(
    org: str | None,
    explicit_peers: list[str] | None,
    *,
    root: Path | str | None = None,
) -> list[str]:
    """Pick the peer slug list for a call.

    Precedence (highest wins):

    1. Explicit ``explicit_peers`` kwarg (call-site override; ``[]`` means
       "isolated", a non-empty list pins the set).
    2. ``autonomy.org.peer-subscription#1`` Setting in ``personal.db``
       keyed by ``org`` — when the Setting declares ``peers``, its value
       decides which ORGANIZATIONS contribute, however many other org DBs
       exist; the operator's own stores (``personal``, ``machine``) are
       appended regardless of what it names (auto-9uj7i).
    3. Default: every other org slug under ``data/orgs/*.db``, minus the
       caller.

    ``GRAPH_DB`` pinning short-circuits to ``[]`` — the pinned DB has no
    peers to route to. Peers that don't exist on disk are dropped
    silently; peer opens would fail later otherwise, and the spec says
    the set of org DBs IS the registry.
    """
    if explicit_peers is not None:
        return _filter_existing_peers(explicit_peers, org, root=root)

    if os.environ.get("GRAPH_DB"):
        # Pinned path → no peers. Matches what a fresh ``GraphDB(path)``
        # sees: a single DB with no siblings to route into.
        return []

    if org:
        subscribed = _read_peer_subscription(org)
        if subscribed is not None:
            pinned = _filter_existing_peers(subscribed, org, root=root)
            # The operator's own stores are ALWAYS peers, whatever the
            # subscription declares (auto-9uj7i, graph://21a0da9e-1c2). The
            # subscription exists to opt out of other ORGANIZATIONS'
            # published settings; personal.db never leaves the operator's
            # fleet and the machine store never leaves the machine, so
            # neither can carry another user's content into a read and there
            # is nothing for a subscription to protect against. Silently
            # dropping them would deny D4's sovereignty (every personal
            # answer for this org) and break schema metadata (auto-n77vh
            # puts it in the machine store). BOTH, named individually — an
            # "own stores" list derived from a two-store mental model
            # re-adds personal and drops machine. The explicit
            # ``explicit_peers`` kwarg stays literal, because ``peers=[]``
            # deliberately means own-store-only for identity, credential
            # and policy readers.
            return _with_local_stores(pinned, org, root=root)

    # Default: every other org under data/orgs — which since auto-35kmy is
    # ORGANIZATIONS ONLY, so the operator's own stores enter here the same
    # way they enter a pinned subscription: by the explicit rule, never by
    # a glob happening to include them.
    all_slugs = [s for s in list_org_slugs(root=root) if s != (org or "")]
    return _with_local_stores(all_slugs, org, root=root)


def _with_local_stores(
    peers: list[str], org: str | None, *, root: Path | str | None = None,
) -> list[str]:
    """Append the operator's own stores that exist on disk (auto-9uj7i).

    THE one place a local store enters a peer set. Both named
    individually; existence is the store's own file, since neither
    appears in the org enumeration any more (auto-35kmy).
    """
    from .db import _local_store_db_path

    for own in (PERSONAL_DB_SLUG, MACHINE_DB_SLUG):
        if (
            own != org
            and own not in peers
            and _local_store_db_path(own, root).exists()
        ):
            peers.append(own)
    return peers


def _filter_existing_peers(
    peers: Iterable[str],
    org: str | None,
    *,
    root: Path | str | None = None,
) -> list[str]:
    """Drop the caller from ``peers`` and filter to slugs whose DB exists.

    A LOCAL store named explicitly (``personal``, ``machine``) exists when
    its own file does — it is no longer in the org enumeration
    (auto-35kmy), and an explicit mention must stay addressable.
    """
    from .db import LOCAL_STORE_SLUGS, _local_store_db_path

    all_slugs = set(list_org_slugs(root=root))
    out: list[str] = []
    seen: set[str] = set()
    for p in peers:
        if not p or p == org or p in seen:
            continue
        exists = (
            _local_store_db_path(p, root).exists()
            if p in LOCAL_STORE_SLUGS
            else p in all_slugs
        )
        if exists:
            out.append(p)
            seen.add(p)
    return out


# ── Peer-DB opening ──────────────────────────────────────────


def open_peer_db(slug: str) -> GraphDB | None:
    """Open a peer DB read-only via the pool. ``None`` if file missing.

    Pool keyed on ``(slug, 'ro')``; the returned instance is shared
    across calls for this process lifetime. Callers MUST NOT close it —
    ``GraphDB.close_all_pooled()`` is owned by startup/teardown.
    """
    try:
        return GraphDB.for_org(slug, mode="ro")
    except FileNotFoundError:
        return None


# ── Merge primitives ─────────────────────────────────────────


def rrf_merge(
    lists: list[tuple[str, list[dict]]],
    *,
    limit: int,
    own_org: str | None = None,
    key: str = "id",
) -> list[dict]:
    """Reciprocal-rank-fusion merge.

    ``lists`` is ``[(org_slug, ranked_results), ...]``. Each inner list must
    already be sorted from best to worst for its origin. Every returned
    row is annotated with ``org`` (= origin slug) and ``rrf_score``.

    Scoring: ``score = sum over inner lists of boost / (k + rank)`` where
    rank is 1-based. The same row showing up across lists (duplicate
    ``key``) accumulates its contributions — but we only keep the
    first-seen row's metadata (so own-org data beats peer snippets for
    content shape).

    Round 7l source-aware behaviour: when ``key='source_id'`` (or the
    rows expose a ``source_id`` field for any other key choice that
    repeats), all rows belonging to a surviving source are preserved in
    the output, in their original per-org order. The source's RRF score
    is computed once per (org, source) pair using the source's *first
    appearance* as the rank position — so head + excerpt rows from the
    same source contribute exactly once to a score, then ride the
    source's slot together. ``LIMIT`` applies at the source level
    (distinct keys), not the row level.

    Own-org list is boosted by :data:`OWN_ORG_BOOST`. Top ``limit``
    sources returned, with all their rows in tow.
    """
    # Per-source bookkeeping: {key: {"score": float, "rows": [...], "org": str}}
    # Key insertion order is preserved across orgs so the first org to
    # surface a source pins its row metadata (= own-org wins on ties,
    # matching pre-Round-7l behaviour).
    scored: dict[str, dict[str, Any]] = {}
    for org_slug, results in lists:
        boost = OWN_ORG_BOOST if org_slug == own_org else 1.0
        # Within ONE org's list, group rows by source key in their order
        # of first appearance. Rank-position (1-based) is computed over
        # *distinct sources*, not raw rows — so a source's RRF score is
        # set by its position among distinct sources, not by how many
        # rows it emits. That keeps the source-aware contract intact.
        seen_in_org: dict[str, list[dict]] = {}
        order_in_org: list[str] = []
        for row in results:
            k = row.get(key)
            if not k:
                continue
            if k not in seen_in_org:
                seen_in_org[k] = []
                order_in_org.append(k)
            seen_in_org[k].append(row)
        for rank, k in enumerate(order_in_org, start=1):
            contribution = boost / (RRF_K + rank)
            rows_for_source = seen_in_org[k]
            entry = scored.get(k)
            if entry is None:
                # First org to surface this source pins the row payload.
                # Each row gets a copy with org annotation; rrf_score
                # is filled in later (after we've summed across orgs).
                row_copies = []
                for r in rows_for_source:
                    rc = dict(r)
                    rc.setdefault("org", org_slug)
                    row_copies.append(rc)
                scored[k] = {
                    "score": contribution,
                    "rows": row_copies,
                    "org": org_slug,
                }
            else:
                entry["score"] += contribution
    # Source-level sort + LIMIT, then expand back to row-level output.
    sorted_sources = sorted(scored.values(), key=lambda e: -e["score"])
    output: list[dict] = []
    for entry in sorted_sources[:limit]:
        for row in entry["rows"]:
            row["rrf_score"] = entry["score"]
            output.append(row)
    return output


def chronological_merge(
    lists: list[tuple[str, list[dict]]],
    *,
    limit: int,
    time_field: str = "created_at",
    key: str = "source_id",
) -> list[dict]:
    """Merge per-org lists by ``time_field`` DESC. Truncate to ``limit``
    distinct sources (``key`` field). All rows of a surviving source
    are preserved in the output.

    Each incoming row is annotated with ``org`` (= origin slug) before
    merging. Ties on timestamp fall back to insertion order (Python's
    sort is stable), so the DB the row came from determines the tie
    direction — fine for a recency feed.

    Round 7l source-aware behaviour: ``LIMIT`` counts distinct
    ``source_id``s rather than raw rows. A 30-hit session contributes
    one source-slot; its head + tail rows ride that slot together. This
    is the recency-mode counterpart of the source-level LIMIT applied
    by ``rrf_merge`` for relevance mode.
    """
    combined: list[dict] = []
    for org_slug, rows in lists:
        for row in rows:
            rec = dict(row)
            rec.setdefault("org", org_slug)
            combined.append(rec)
    # Stable sort by timestamp DESC. Rows of one source share a
    # ``source_created_at`` value so they cluster naturally.
    combined.sort(key=lambda r: r.get(time_field) or "", reverse=True)
    # Per-source bucket; first-occurrence pins the source's slot order.
    seen: dict[str, list[dict]] = {}
    for row in combined:
        sid = row.get(key) or row.get("id")
        if sid is None:
            # Fall back to row-level inclusion when no source key is
            # present (legacy callers); each such row gets its own slot.
            sid = id(row)
        if sid in seen:
            seen[sid].append(row)
        elif len(seen) < limit:
            seen[sid] = [row]
        # else: drop — we've hit the source-level limit
    output: list[dict] = []
    for rows in seen.values():
        output.extend(rows)
    return output


# ── Cross-org scanning ───────────────────────────────────────


def run_across_orgs(
    org: str | None,
    peers: list[str] | None,
    fetch_own: "callable[[GraphDB], list[dict]]",
    fetch_peer: "callable[[GraphDB, str], list[dict]]",
    *,
    own_db: GraphDB | None = None,
    include_own: bool = True,
) -> list[tuple[str, list[dict]]]:
    """Run a fetcher against own + peers. Returns [(org, results), ...].

    ``fetch_own`` runs on the caller's own open ``GraphDB`` and gets the
    full surface; ``fetch_peer`` runs on each peer's read-only DB and is
    responsible for applying the ``publication_state`` filter. The
    passed-in ``own_db`` is reused when provided (saves an open per
    call), otherwise a fresh one is opened.

    The output order is ``own`` then peers alphabetical — matches the
    spec's fixed scan order for ``graph://uuid`` resolution.
    """
    results: list[tuple[str, list[dict]]] = []

    close_own = False
    if include_own:
        if own_db is None:
            from .ops import _open as _ops_open  # local: avoid circular
            own_db = _ops_open(org)
            close_own = True
        own_slug = org or ""
        try:
            results.append((own_slug, list(fetch_own(own_db))))
        finally:
            if close_own:
                own_db.close()

    for slug in sorted(peers or []):
        peer_db = open_peer_db(slug)
        if peer_db is None:
            continue
        rows = list(fetch_peer(peer_db, slug))
        results.append((slug, rows))

    return results
