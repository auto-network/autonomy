"""Fleet lag per origin, both directions, on REAL catalog stores (auto-pmw2v).

This machine L is behind its peers by max over origins o of
``best[o] - W_L[o]`` (best[o]: the highest cursor any peer reported for o,
folded from every pull's watermark map on arrival), and peer P is behind L by
max over o of ``W_L[o] - W_P[o]``, reduced once when P's map arrives.
graph://6aa9bffc-ca9 Record 3; terms graph://1155b8f4-8cf; pitfall
graph://e6dba57c-f8b.

Two wrong versions shipped on 2026-09-29: a hard-set 0 (SJC-2 "Up to date"
16k transactions behind), then min-vs-min (8ee69a22: Home "5.3 days behind"
SJC-2 in blindhash with every cursor at MAX). These tests build the stores,
exchange transactions, seal and adopt write floors, and run the production
record_frontier and projection -- no invented numbers.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.dashboard.plugins.fleet.entrypoints import projection as proj
from tools.graph.db import GraphDB
from tools.network import fleet_sync_peer_scope as peer_scope
from tools.network.fleet_sync import write_floors
from tools.network.fleet_sync.catalog import MutationCatalog
from tools.network.idkit import KeyPair

HOME_KEY = KeyPair.from_private_hex("11" * 32)
SJC_KEY = KeyPair.from_private_hex("22" * 32)
LAPTOP_KEY = KeyPair.from_private_hex("33" * 32)
HOME, SJC, LAPTOP = HOME_KEY.public_hex, SJC_KEY.public_hex, LAPTOP_KEY.public_hex
DAY_NS = 86_400 * 1_000_000_000
DAY_MS = DAY_NS // 1_000_000
NOW = 1_800_000_000 * 1_000_000_000


class _Backend:
    """settings_ops, for the two machine-local sets, backed by a dict."""

    def __init__(self, store):
        self.store = store
        self.patch = pytest.MonkeyPatch()

    def __enter__(self):
        store = self.store
        ops = peer_scope.settings_ops
        self.patch.setattr(ops, "read_set_key", lambda set_id, key, **_: (
            {"payload": store[(set_id, key)]} if (set_id, key) in store else None))
        self.patch.setattr(ops, "upsert_by_key", lambda set_id, _rev, key, payload, **_:
                           store.__setitem__((set_id, key), dict(payload)))
        self.patch.setattr(ops, "read_owned_set", lambda set_id, **_: SimpleNamespace(
            members=[SimpleNamespace(key=key, payload=payload)
                     for (sid, key), payload in store.items() if sid == set_id]))
        return self

    def __exit__(self, *exc):
        self.patch.undo()


class Machine:
    """One machine's real store for one scope, and its machine-local
    settings (the two sets record_frontier writes), held in memory."""

    def __init__(self, path: Path, key: KeyPair):
        self.path = path
        self.key = key
        self.origin = key.public_hex
        self.db = GraphDB(path)
        self.catalog = MutationCatalog(self.db.conn, self.origin)
        self.catalog.install()
        self.settings: dict[tuple[str, str], dict] = {}

    def write(self, ts: int) -> None:
        with self.catalog.transaction(ts, f"{self.origin[:4]}-{ts}"):
            self.db.conn.execute(
                "INSERT INTO sources (id, type, title) VALUES (?,?,?)",
                (str(uuid.uuid4()), "note", "row"))
        self.seal_floor(ts)

    def seal_floor(self, now_ns: int) -> None:
        """Production seals this machine's write floor every round: its own
        cursor on its own writes."""
        write_floors.seal_machine_write_floor(self.db.conn, self.key, self.origin, now_ns)

    def watermarks(self) -> dict[str, int]:
        return self.catalog.origin_watermarks()

    def pull_from(self, server: "Machine", origins=None) -> None:
        """Apply what the server holds that we do not, adopt the server's
        floors for those origins (as the served floor frames do), and have
        the server record the map we advertised -- the production serve path."""
        advertised = self.watermarks()
        for origin in origins or (set(server.watermarks()) | {server.origin}):
            held = self.watermarks().get(origin, 0)
            position: tuple = (held, None)
            while page := server.catalog.next_transactions_for_origin(
                    origin, position[0], position[1], limit=50):
                for _ref, ts, tid, items in page:
                    self.catalog.apply_remote_batch(items)
                    position = (ts, tid)
            floor = server.watermarks().get(origin)
            if floor:
                self.catalog.claim_write_floor(origin, floor)
        with server.backend():
            peer_scope.record_frontier(
                self.origin, scope="autonomy", watermarks=advertised,
                local=server.watermarks(), at_ns=NOW)

    def backend(self) -> _Backend:
        return _Backend(self.settings)

    def local_row(self, scope: str = "autonomy") -> dict:
        with self.backend():
            rows = peer_scope.read_peer_scopes()
            best = peer_scope.read_best_known()
        [row] = proj._local_scope_rows(
            rows, {}, {}, proj._local_cursors({scope: self.path}), best)
        return row

    def peer_row(self, peer: "Machine") -> dict:
        with self.backend():
            rows = peer_scope.read_peer_scopes()
        [row] = proj._scope_rows(rows.get(peer.origin), server_time=0, peer=peer.origin)
        return row

    def close(self) -> None:
        self.db.close()


@pytest.fixture
def fleet(tmp_path):
    machines = {name: Machine(tmp_path / f"{name}.db", key) for name, key in
                (("home", HOME_KEY), ("sjc", SJC_KEY), ("laptop", LAPTOP_KEY))}
    yield SimpleNamespace(**machines)
    for machine in machines.values():
        machine.close()


def test_a_quiet_peer_origin_held_in_full_is_not_behind(fleet):
    """The operator's case: SJC-2 last wrote 5 days ago, Home holds it (cursor
    at MAX), Home has written since. Home's lag is 0."""
    home, sjc = fleet.home, fleet.sjc
    sjc.write(NOW - 5 * DAY_NS)
    home.pull_from(sjc)
    home.write(NOW - DAY_NS)
    sjc.pull_from(home)
    sjc.seal_floor(NOW)                      # SJC-2's own cursor runs to now
    home.pull_from(sjc)
    sjc.pull_from(home)                      # Home learns SJC-2's current map
    row = home.local_row()
    assert (row["lag"], row["behind"]) == (0, [])
    # What 8ee69a22 computed from these same stores: SJC-2's map minimum
    # minus the minimum over origins of the newest transaction Home holds.
    with sqlite3.connect(home.path) as conn:
        ours_min = min(r[0] for r in conn.execute(
            "SELECT MAX(timestamp_ns) FROM fleet_sync_transactions GROUP BY origin_id"))
    assert min(sjc.watermarks().values()) - ours_min >= 3 * DAY_NS


def test_a_puller_behind_a_busy_peer_says_how_far_and_the_peer_says_0(fleet):
    home, sjc = fleet.home, fleet.sjc
    sjc.write(NOW - 6 * DAY_NS)
    home.pull_from(sjc)
    home.write(NOW - 5 * DAY_NS)
    sjc.pull_from(home)                       # SJC holds Home through 5 d ago
    home.write(NOW)                           # Home writes more; SJC stalls
    home.pull_from(sjc)                       # Home pulls: SJC learns Home's map
    row = sjc.local_row()
    assert row["lag"] == 5 * DAY_MS
    assert [b["peer"] for b in row["behind"]] == [HOME]
    assert home.local_row()["lag"] == 0
    sjc.pull_from(home)                       # caught up
    assert sjc.local_row()["lag"] == 0
    # The peer row is as of the peer's last pull REQUEST: its map is sent
    # before it receives. At that request SJC-2 still trailed by 5 d; its
    # next request carries the caught-up map.
    assert home.peer_row(sjc)["lag"] == 5 * DAY_MS
    sjc.pull_from(home)
    assert home.peer_row(sjc)["lag"] == 0


def test_the_peer_row_says_how_far_the_peer_trails_us(fleet):
    home, sjc = fleet.home, fleet.sjc
    home.write(NOW - 5 * DAY_NS)
    sjc.pull_from(home)
    home.write(NOW)
    sjc.pull_from(home, origins={SJC})        # SJC pulls but takes nothing new
    assert home.peer_row(sjc)["lag"] == 5 * DAY_MS
    assert home.peer_row(sjc)["behindOn"] == [HOME]


def test_data_that_reached_a_peer_from_a_third_machine_counts(fleet):
    """The review's case: the laptop syncs only with Home. SJC-2 is behind
    Home on the laptop's origin, though it never hears from the laptop."""
    home, sjc, laptop = fleet.home, fleet.sjc, fleet.laptop
    laptop.write(NOW - 2 * DAY_NS)
    home.pull_from(laptop)
    sjc.pull_from(home)
    laptop.write(NOW - DAY_NS)
    home.pull_from(laptop)                    # only Home has the second write
    home.write(NOW)
    sjc.pull_from(home, origins={HOME})       # SJC takes Home's own writes only
    home.pull_from(sjc)                       # SJC learns Home's whole map
    row = sjc.local_row()
    assert row["lag"] == DAY_MS
    assert (row["behind"][0]["peer"], row["behind"][0]["origin"]) == (HOME, LAPTOP)
    # Home's row for SJC-2 is as of SJC-2's last pull request, sent before
    # it held any of Home's own writes: "not yet received". Its next request
    # names the laptop gap.
    assert home.peer_row(sjc)["lag"] is None
    sjc.pull_from(home, origins={SJC})
    assert home.peer_row(sjc)["lag"] == DAY_MS
    assert home.peer_row(sjc)["behindOn"] == [LAPTOP]


def test_an_origin_held_not_at_all_is_not_yet_received(fleet):
    home, sjc = fleet.home, fleet.sjc
    home.write(NOW)
    home.pull_from(sjc)                        # SJC learns Home's map; holds nothing
    row = sjc.local_row()
    assert row["lag"] is None
    assert row["behind"][0]["peer"] == HOME and row["behind"][0]["lag"] is None


def test_nothing_reported_is_unknown_not_current(fleet):
    fleet.home.write(NOW)
    assert fleet.home.local_row()["lag"] is None
    [row] = proj._scope_rows([{"scope": "autonomy", "frontier_ns": 0}], server_time=0)
    assert row["lag"] is None


def test_storage_is_reduced_per_origin_not_a_map_per_peer(fleet):
    """One best-known entry per origin per scope, one small record per peer:
    O(origins) per machine, never a stored map per peer."""
    home, sjc, laptop = fleet.home, fleet.sjc, fleet.laptop
    for machine in (sjc, laptop):
        machine.write(NOW - DAY_NS)
        home.pull_from(machine)
    sjc.pull_from(home)
    laptop.pull_from(home)
    peer_rows = [p for (sid, _k), p in home.settings.items()
                 if sid == "autonomy.machine.fleet-sync-peer-scope"]
    assert len(peer_rows) == 2 and all("watermarks" not in p for p in peer_rows)
    [best] = [p for (sid, _k), p in home.settings.items()
              if sid == "autonomy.machine.fleet-sync-best-known"]
    assert set(best["origins"]) <= {HOME, SJC, LAPTOP}


def _record(machine: Machine, peer_origin: str, watermarks: dict) -> None:
    with machine.backend():
        peer_scope.record_frontier(peer_origin, scope="autonomy",
                                   watermarks=watermarks, local=machine.watermarks(),
                                   at_ns=NOW)


def test_a_best_position_follows_its_peer_down(fleet):
    """Review of eab8ba66: the peer that vouched for best[o] reports lower
    (a rebuilt store) -- the false lag falls with it; an origin it stops
    reporting is dropped."""
    home = fleet.home
    home.write(NOW - DAY_NS)
    _record(home, SJC, {HOME: NOW - DAY_NS, LAPTOP: NOW})        # SJC claims laptop@NOW
    assert home.local_row()["lag"] is None                        # laptop not received
    _record(home, SJC, {HOME: NOW - DAY_NS, LAPTOP: NOW - 3 * DAY_NS})
    with home.backend():
        best = peer_scope.read_best_known()["autonomy"]
    assert best[LAPTOP]["ns"] == NOW - 3 * DAY_NS                 # followed it down
    _record(home, SJC, {HOME: NOW - DAY_NS})                      # stops reporting it
    with home.backend():
        best = peer_scope.read_best_known()["autonomy"]
    assert LAPTOP not in best
    assert home.local_row()["lag"] == 0


def test_a_higher_report_from_another_peer_still_wins(fleet):
    home = fleet.home
    home.write(NOW - DAY_NS)
    _record(home, SJC, {LAPTOP: NOW - 2 * DAY_NS})
    _record(home, LAPTOP, {LAPTOP: NOW - DAY_NS})
    _record(home, SJC, {LAPTOP: NOW - 3 * DAY_NS})                 # SJC no longer best
    with home.backend():
        best = peer_scope.read_best_known()["autonomy"]
    assert best[LAPTOP] == {"ns": NOW - DAY_NS, "peer": LAPTOP}


def test_positions_from_a_machine_off_the_roster_do_not_count():
    best = {"autonomy": {LAPTOP: {"ns": NOW, "peer": SJC},
                         HOME: {"ns": NOW, "peer": LAPTOP}}}
    assert proj._active_best_known(best, {HOME, LAPTOP}) == {
        "autonomy": {HOME: {"ns": NOW, "peer": LAPTOP}}}
