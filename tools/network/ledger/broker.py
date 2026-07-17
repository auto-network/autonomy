"""Broker-path sync — the registry mailbox as "a peer that is always awake".

Spec ``graph://eb245082-b76`` §6–7, bead ``auto-rrzrt`` (F3). The broker
(auto.network registry) is T0-blind store-and-forward: nodes publish head
hints to per-org topics and deposit :mod:`~.bundles`-sealed event blobs;
offline nodes drain the mailbox later. The broker sees topic + hashes +
sizes (L6) and can deny service but never forge events — every event is
author-signed and content-addressed, and bundles only open under the org
sync key with the right org+topic AAD.

**Plane separation** is explicit in :func:`broker_pull`: it reads the
hint stream first (hashes only) and touches the bundle mailbox *only*
when the hints announce heads this replica does not hold — the 32-byte
notification ripples; payloads move on demand.

**Anti-entropy** is symmetric with the peer path: :func:`broker_push`
diffs the local DAG against the mailbox's hash manifests (metadata-only
fetch) and deposits exactly the delta, so the mailbox accumulates the
org's full event set with no duplicates; any replica that drains it from
seq 0 (or from its cursor) converges in one pass.

Transport is injected as ``transport(method, path, json_body) ->
(status, body)`` so the same client runs over httpx in production and a
FastAPI ``TestClient`` in tests. Every call is a signed envelope through
the registry's I4 gate (root-direct, or a delegation chain carrying the
exact scope ``topic:<name>``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from tools.network.idkit import DelegationCert, KeyPair
from tools.network.registry.signing import sign_request

from .bundles import MAX_BUNDLE_EVENTS, open_bundle, seal_bundle
from .errors import LedgerError
from .store import LedgerStore
from .sync import MAX_SYNC_HEADS

#: The one mandatory, low-volume topic every member follows (§7): the
#: authority ledger itself. Content streams are separate opt-in topics —
#: never mixed, so following authority alone never moves content bundles.
AUTHORITY_TOPIC = "authority"

#: Registry-side caps this client must stay under (cross-pinned by a
#: test in tools/network/registry/tests/test_topics.py).
MAX_WANT_HASHES = 4_096
MAX_DEPOSIT_BYTES = 4 * 1024 * 1024

#: Per-bundle plaintext budget for push chunking. The sealed blob is
#: plaintext + 28 bytes and the plaintext is a JSON list of wire strings
#: (every quote in a wire escapes to two chars), so budgeting each event
#: at twice its wire size keeps the ciphertext safely under the
#: registry's byte cap even in the worst case.
_PLAINTEXT_BUDGET = 3 * 1024 * 1024

Transport = Callable[[str, str, dict], Tuple[int, dict]]


class BrokerError(LedgerError):
    """The broker refused a call or returned a malformed body."""


def _row_bundle(row: dict) -> dict:
    """A mailbox row (seq, v, hashes, size, ciphertext) as a sealed bundle.

    ``v`` is whatever the depositor sealed — stored and returned verbatim
    by the broker — so a format bump never mislabels old rows.
    """
    try:
        return {
            "v": row["v"],
            "hashes": row["hashes"],
            "size": row["size"],
            "ciphertext": row["ciphertext"],
        }
    except (TypeError, KeyError) as exc:
        raise BrokerError(f"broker returned a malformed mailbox row: {row!r}") from exc


def _drain(fetch: Callable[[int], Tuple[list, int]], since: int) -> Tuple[list, int]:
    """Page a cursor endpoint until it runs dry; returns (rows, cursor)."""
    rows: list = []
    while True:
        page, next_since = fetch(since)
        rows.extend(page)
        if not page or next_since == since:
            return rows, since
        since = next_since


class BrokerClient:
    """Signed-envelope client for one org's topic surface."""

    def __init__(
        self,
        transport: Transport,
        org_uuid: str,
        key: KeyPair,
        *,
        cert: Optional[DelegationCert] = None,
        now_fn: Optional[Callable[[], float]] = None,
    ):
        self._transport = transport
        self.org_uuid = org_uuid
        self._key = key
        self._cert = cert
        self._now_fn = now_fn or time.time

    def _call(self, path: str, payload: dict) -> dict:
        envelope = sign_request(
            self._key, "POST", path, payload, ts=int(self._now_fn()), cert=self._cert
        )
        status, body = self._transport("POST", path, envelope)
        if status not in (200, 201):
            raise BrokerError(f"broker refused POST {path}: {status} {body!r}")
        if not isinstance(body, dict):
            raise BrokerError(f"broker returned a non-object body for POST {path}")
        return body

    def _topic_path(self, topic: str, suffix: str) -> str:
        return f"/v1/orgs/{self.org_uuid}/topics/{topic}/{suffix}"

    # -- notification plane ------------------------------------------------------

    def publish_heads(self, topic: str, heads: List[str]) -> int:
        """Announce this replica's DAG heads (hashes only); returns seq."""
        body = self._call(self._topic_path(topic, "heads"), {"heads": sorted(set(heads))})
        return body["seq"]

    def poll_hints(self, topic: str, since: int = 0) -> Tuple[list, Optional[dict], int]:
        """Hints after *since*; returns ``(hints, latest, next_since)``."""
        body = self._call(self._topic_path(topic, "heads/poll"), {"since": since})
        return body["hints"], body["latest"], body["next_since"]

    # -- data plane ----------------------------------------------------------------

    def deposit(self, topic: str, sealed: dict) -> int:
        """Mail one sealed bundle; returns its mailbox seq."""
        body = self._call(
            self._topic_path(topic, "bundles"),
            {"v": sealed["v"], "hashes": sealed["hashes"],
             "ciphertext": sealed["ciphertext"]},
        )
        return body["seq"]

    def fetch_bundles(self, topic: str, since: int = 0) -> Tuple[list, int]:
        """Mailbox rows after *since*; returns ``(bundles, next_since)``."""
        body = self._call(self._topic_path(topic, "bundles/fetch"), {"since": since})
        return body["bundles"], body["next_since"]

    def fetch_manifest(self, topic: str) -> frozenset:
        """Every event id the mailbox holds — metadata only, no ciphertext."""

        def _meta(since: int) -> Tuple[list, int]:
            body = self._call(
                self._topic_path(topic, "bundles/fetch"),
                {"since": since, "meta_only": True},
            )
            return body["bundles"], body["next_since"]

        rows, _ = _drain(_meta, 0)
        return frozenset(h for row in rows for h in row["hashes"])

    def fetch_by_hash(self, topic: str, want: List[str]) -> list:
        """Fetch-missing-by-hash against the mailbox (chunked to the
        registry's want cap, so any-size want lists work)."""
        want = sorted(set(want))
        rows: list = []
        for start in range(0, len(want), MAX_WANT_HASHES):
            body = self._call(
                self._topic_path(topic, "bundles/fetch"),
                {"want": want[start:start + MAX_WANT_HASHES]},
            )
            rows.extend(body["bundles"])
        return rows


@dataclass
class BrokerCursor:
    """Where this replica has read to on one topic: two mailbox seqs.

    In-memory only in F3 — a caller that drops it just re-drains from
    seq 0 (correct, slower). Durable per-peer cursor state (and its
    representation in the ``autonomy.network.ledger-state#1`` Settings
    row) lands when a long-running sync worker exists to own it (F8).
    """

    hints: int = 0
    bundles: int = 0


def broker_push(
    store: LedgerStore, client: BrokerClient, sync_key: bytes, topic: str = AUTHORITY_TOPIC
) -> int:
    """Anti-entropy, outbound: mail exactly what the mailbox is missing.

    Diffs the local event set against the mailbox hash manifests (a
    metadata-only sweep — O(mailbox manifest) per push, the simple/robust
    v1 trade), seals the delta under the org sync key, deposits it in
    chunks bounded by both event count and byte budget, then announces
    the new heads. Deposit-then-announce means an announced head is
    always fetchable; a crash between the two leaves data unannounced
    until the next push by anyone re-announces (self-healing, and
    broker_pull additionally chases announced-but-missing ids by hash).
    Returns the number of events deposited.
    """
    all_ids = store.ledger.all_ids()
    if not all_ids:
        return 0
    present = client.fetch_manifest(topic)
    missing = sorted(all_ids - present)
    chunk: list = []
    cost = 0
    for event_id in missing:
        event = store.get(event_id)
        event_cost = 2 * len(event.to_json()) + 2
        if chunk and (len(chunk) >= MAX_BUNDLE_EVENTS or cost + event_cost > _PLAINTEXT_BUDGET):
            client.deposit(topic, seal_bundle(sync_key, client.org_uuid, topic, chunk))
            chunk, cost = [], 0
        chunk.append(event)
        cost += event_cost
    if chunk:
        client.deposit(topic, seal_bundle(sync_key, client.org_uuid, topic, chunk))
    client.publish_heads(topic, list(store.heads())[:MAX_SYNC_HEADS])
    return len(missing)


def broker_pull(
    store: LedgerStore,
    client: BrokerClient,
    sync_key: bytes,
    topic: str = AUTHORITY_TOPIC,
    cursor: Optional[BrokerCursor] = None,
) -> Tuple[int, BrokerCursor]:
    """Anti-entropy, inbound: hints first, ciphertext only when behind.

    One call is one complete reconciliation pass against the mailbox: a
    replica that slept through any number of rounds drains everything it
    lacks here. Returns ``(events_ingested, cursor)``.
    """
    cursor = cursor or BrokerCursor()
    fresh: dict = {}
    poisoned: list = []

    def _open(row: dict) -> None:
        """Open one mailbox row into ``fresh``; quarantine rows that fail.

        The broker cannot validate ciphertext (it is blind), so one bad
        deposit — wrong key, truncated blob — must not brick the whole
        mailbox for every honest replica. Skipped rows are remembered and
        surfaced loudly below if the pass could not converge without them.
        """
        if all(h in store.ledger or h in fresh for h in row["hashes"]):
            return  # our own deposits / already-held events: skip decrypt
        try:
            events = open_bundle(sync_key, client.org_uuid, topic, _row_bundle(row))
        except BrokerError:
            raise  # the SERVER sent a malformed row; that is not quarantine
        except LedgerError as exc:
            poisoned.append((row.get("seq"), str(exc)))
            return
        for event in events:
            if event.event_id not in store.ledger:
                fresh[event.event_id] = event

    # Notification plane: drain the hint stream (hashes only, paginated).
    latest_seen: dict = {}

    def _poll(since: int) -> Tuple[list, int]:
        hints, latest, next_since = client.poll_hints(topic, since)
        if latest is not None:
            latest_seen.update(latest)
        return hints, next_since

    hints, cursor.hints = _drain(_poll, cursor.hints)
    announced = {h for hint in hints for h in hint["heads"]}
    announced.update(latest_seen.get("heads", []))
    if all(h in store.ledger for h in announced):
        return 0, cursor  # hints say up to date; the data plane never moves

    # Data plane: drain the mailbox from our cursor.
    bundles, cursor.bundles = _drain(
        lambda since: client.fetch_bundles(topic, since), cursor.bundles
    )
    for row in bundles:
        _open(row)

    # Fetch-missing-by-hash: chase (a) parents of fresh events and
    # (b) announced heads that no drained bundle carried — ancestry
    # deposited before our cursor, or deposited but under-announced.
    while True:
        need = {
            p
            for event in fresh.values()
            for p in event.parents
            if p not in store.ledger and p not in fresh
        }
        need.update(h for h in announced if h not in store.ledger and h not in fresh)
        if not need:
            break
        before = len(fresh)
        for row in client.fetch_by_hash(topic, sorted(need)):
            _open(row)
        if len(fresh) == before:
            detail = f"; skipped {len(poisoned)} undecryptable bundle(s)" if poisoned else ""
            raise BrokerError(
                f"mailbox lacks {len(need)} needed event(s){detail}; "
                "sync against a direct peer instead"
            )

    if fresh:
        store.append_bundle(list(fresh.values()))
    return len(fresh), cursor
