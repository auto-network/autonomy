"""The doctor's persona write floor line (auto-mmwgu, reopened 2026-09-28).

Home carried a five-day-old persona write floor that the report called
"covered (advertised to the relay)" with no warning, while every share link
published after the floor closed 4431. A covered floor that stopped moving
is a fault, and the line must say which roster machine pins it.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from tools.network import fleet_doctor
from tools.network.fleet_sync.write_floors import store_persona_write_floor

HOME, SJC = "aa" * 32, "bb" * 32


def _store_with_persona_floor(path: Path, floor_ns: int, positions: dict) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE fleet_sync_origins(id INTEGER PRIMARY KEY, incarnation TEXT)")
    conn.execute("CREATE TABLE fleet_sync_transactions(origin_id INTEGER, timestamp_ns INTEGER, transaction_id TEXT)")
    conn.execute("CREATE TABLE fleet_sync_origin_cursor(origin_id INTEGER PRIMARY KEY, timestamp_ns INTEGER, transaction_id TEXT)")
    for n, (machine, position) in enumerate(sorted(positions.items()), start=1):
        conn.execute("INSERT INTO fleet_sync_origins(id, incarnation) VALUES(?,?)", (n, machine))
        conn.execute("INSERT INTO fleet_sync_transactions VALUES(?,?,?)", (n, position, f"t{n}"))
        conn.execute("INSERT INTO fleet_sync_origin_cursor VALUES(?,?,?)", (n, position, f"t{n}"))
    conn.commit()
    store_persona_write_floor(conn, {
        "persona": "cc" * 32, "org": "autonomy", "write_floor_ns": floor_ns,
        "machines": {m: min(p, floor_ns) if m == SJC else p for m, p in positions.items()},
    })
    conn.close()


def _report(tmp_path, monkeypatch, capsys, *, floor_age_s: int, home_behind_floor_s: int | None = None) -> tuple[str, dict]:
    """A store whose persona floor is ``floor_age_s`` old by the clock. HOME's
    own position is ``home_behind_floor_s`` after the floor (default: now,
    i.e. HOME kept writing); SJC's position IS the floor."""
    from tools.graph import db as graph_db

    orgs = tmp_path / "orgs"
    orgs.mkdir()
    now = time.time_ns()
    floor = now - floor_age_s * 1_000_000_000
    home = now if home_behind_floor_s is None else floor + home_behind_floor_s * 1_000_000_000
    _store_with_persona_floor(orgs / "autonomy.db", floor, {HOME: home, SJC: floor})
    monkeypatch.setattr(graph_db, "_orgs_dir", lambda root=None: orgs)
    monkeypatch.setattr(graph_db, "_org_db_path", lambda slug, root=None: tmp_path / "missing-personal.db")
    monkeypatch.setattr(fleet_doctor, "_QUIET", False)
    report: dict = {}
    fleet_doctor.check_sync_frontiers(report)
    return capsys.readouterr().out, report


def test_a_fresh_covered_floor_is_reported_ok(tmp_path, monkeypatch, capsys):
    out, report = _report(tmp_path, monkeypatch, capsys, floor_age_s=90)
    [line] = [l for l in out.splitlines() if "autonomy persona" in l]
    assert line.startswith("  [ok  ]") and "covered (advertised to the relay)" in line and "BEHIND" not in line
    [persona] = report["sync_frontiers"]["autonomy"]["personas"]
    assert persona["pinned_by"] == [SJC] and persona["behind_on"] == [] and persona["behind_s"] == 90


def test_an_old_floor_on_a_quiet_scope_is_correct_not_behind(tmp_path, monkeypatch, capsys):
    """anchore and dynbench on Home and SJC-2, 2026-09-29 15:41Z: floors 15 min
    old because nothing was written since, both cursors at MAX. The floor is
    behind nothing; the clock is not the measure."""
    out, report = _report(tmp_path, monkeypatch, capsys, floor_age_s=15 * 60, home_behind_floor_s=0)
    [line] = [l for l in out.splitlines() if "autonomy persona" in l]
    assert line.startswith("  [ok  ]") and "BEHIND" not in line and "write floor 9" in line
    [persona] = report["sync_frontiers"]["autonomy"]["personas"]
    assert persona["behind_s"] == 0


def test_a_floor_far_behind_the_writes_held_warns_and_names_the_pinning_machine(tmp_path, monkeypatch, capsys):
    """Home, 2026-09-24 to 09-29: Home kept writing while SJC-2's position
    stayed at the floor for five days."""
    out, report = _report(tmp_path, monkeypatch, capsys, floor_age_s=5 * 86400)
    [line] = [l for l in out.splitlines() if "autonomy persona" in l]
    assert line.startswith("  [WARN]")
    assert "covered (advertised to the relay); BEHIND: " in line
    assert "behind the newest write held here, pinned by roster machine(s) " + SJC[:12] in line
    assert HOME[:12] not in line.split("BEHIND", 1)[1]
    [persona] = report["sync_frontiers"]["autonomy"]["personas"]
    assert 5 * 86400 - 5 <= persona["behind_s"] <= 5 * 86400
