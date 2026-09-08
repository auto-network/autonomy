"""The cross-machine comparison: does it catch the false positive that fooled
us on 2026-09-08, and does it distinguish delivery from local migration?
"""
from __future__ import annotations

from tools.network.fleet_sync_report import Target, _verdict, compare, render


def _machine(name, scope, *, events, rows, captured=None, ids=None):
    t = Target(name=name)
    t.report = {
        "verdict": {"top_line": "OK"},
        "org_sync": {
            scope: {
                "ledger_events": events,
                "published": rows,
                "captured": rows if captured is None else captured,
                "row_ids": ids or {},
            }
        },
    }
    return t


def test_identical_counts_with_local_ids_is_reported_as_stalled():
    """The exact false positive: both machines migrated the same history
    independently, both show 16, and nothing has crossed."""
    shared = [f"{i:064x}" for i in range(16)]
    home = _machine("home", "autonomy", events=16, rows=16,
                    ids={k: f"home-{k[:6]}" for k in shared})
    sjc = _machine("sjc-2", "autonomy", events=16, rows=16,
                   ids={k: f"sjc-{k[:6]}" for k in shared})
    data = compare([home, sjc])["scopes"]["autonomy"]
    state, sentence = _verdict(data)
    assert state == "stalled"
    assert "nothing has crossed" in sentence
    assert data["received_from"]["sjc-2"]["home"] == 0


def test_a_row_carrying_the_peers_id_is_delivery():
    shared = {f"{i:064x}": f"home-{i}" for i in range(4)}
    home = _machine("home", "autonomy", events=4, rows=4, ids=shared)
    # sjc holds the same rows with home's identifiers: it received them.
    sjc = _machine("sjc-2", "autonomy", events=4, rows=4, ids=dict(shared))
    data = compare([home, sjc])["scopes"]["autonomy"]
    state, sentence = _verdict(data)
    assert state == "ok"
    assert "crossing" in sentence
    assert data["received_from"]["sjc-2"]["home"] == 4


def test_uncaptured_rows_outrank_a_delivery_verdict():
    """Rows the catalog does not cover cannot cross; say that rather than
    blaming the transport."""
    home = _machine("home", "autonomy", events=16, rows=16, captured=0,
                    ids={f"{i:064x}": f"home-{i}" for i in range(16)})
    sjc = _machine("sjc-2", "autonomy", events=0, rows=0, ids={})
    data = compare([home, sjc])["scopes"]["autonomy"]
    state, sentence = _verdict(data)
    assert state == "uncaptured"
    assert "never cross" in sentence


def test_exclusive_events_are_named_as_the_clean_test():
    """An event one machine has and the other lacks is uncontested, so its
    absence is unambiguous — unlike a shared address, where an older arriving
    mutation is discarded."""
    home = _machine("home", "autonomy", events=5, rows=5,
                    ids={f"{i:064x}": f"home-{i}" for i in range(5)})
    sjc = _machine("sjc-2", "autonomy", events=4, rows=4,
                   ids={f"{i:064x}": f"sjc-{i}" for i in range(4)})
    data = compare([home, sjc])["scopes"]["autonomy"]
    missing = data["missing_from_peer"]["sjc-2"]["home"]
    assert missing == [f"{4:064x}"]
    state, sentence = _verdict(data)
    assert state == "stalled" and "missing 1+" in sentence


def test_one_machine_alone_is_not_a_failure():
    home = _machine("home", "autonomy", events=4, rows=4,
                    ids={f"{i:064x}": f"home-{i}" for i in range(4)})
    data = compare([home])["scopes"]["autonomy"]
    state, _sentence = _verdict(data)
    assert state == "alone"


def test_an_unreachable_target_fails_the_run(capsys):
    home = _machine("home", "autonomy", events=4, rows=4,
                    ids={f"{i:064x}": f"home-{i}" for i in range(4)})
    dead = Target(name="sjc-2", ssh="-p 22 root@nowhere")
    dead.error = "exit 255: Connection refused"
    comparison = compare([home, dead])
    assert comparison["unreachable"] == {"sjc-2": "exit 255: Connection refused"}
    assert render([home, dead], comparison) == 1
    out = capsys.readouterr().out
    assert "Connection refused" in out


def test_a_blind_process_scan_is_surfaced_per_machine(capsys):
    home = _machine("home", "autonomy", events=4, rows=4,
                    ids={f"{i:064x}": f"home-{i}" for i in range(4)})
    home.report["process_scan_unavailable"] = "no readable process entries"
    render([home], compare([home]))
    assert "process scan unavailable" in capsys.readouterr().out


def _with_frontier(name, scope, origin, newest_ns, age_s, *, empty=None):
    t = Target(name=name)
    t.report = {
        "verdict": {"top_line": "OK"},
        "sync_frontiers": {
            scope: {"origins": [{
                "origin": origin, "newest_ns": newest_ns,
                "age_s": age_s, "transactions": 10,
            }]}
        },
        "empty_pull_channels": empty or [],
    }
    return t


def test_an_idle_scope_is_not_reported_as_a_fault(capsys):
    """Both machines at the same frontier for an origin that has not written:
    correct, and must not be flagged. This is the false positive the operator
    named — absence of updates is not staleness."""
    from tools.network.fleet_sync_report import compare_frontiers, render_frontiers

    old = 1_000_000_000_000_000_000
    a = _with_frontier("home", "dynbench", "aa" * 6, old, 61200)
    b = _with_frontier("sjc-2", "dynbench", "aa" * 6, old, 61200,
                       empty=["dynbench/direct"])
    rc = render_frontiers(compare_frontiers([a, b]))
    assert rc == 0
    assert "FAIL" not in capsys.readouterr().out


def test_a_real_lag_is_reported_with_the_empty_pull_that_explains_it(capsys):
    from tools.network.fleet_sync_report import (
        _EMPTY_BY_MACHINE, compare_frontiers, render_frontiers,
    )

    now = 1_000_000_000_000_000_000
    day = 24 * 3600 * 1_000_000_000
    a = _with_frontier("home", "autonomy", "bb" * 6, now, 0)
    b = _with_frontier("sjc-2", "autonomy", "bb" * 6, now - day, 88300,
                       empty=["autonomy/direct"])
    _EMPTY_BY_MACHINE.clear()
    _EMPTY_BY_MACHINE["sjc-2"] = ["autonomy/direct"]
    rc = render_frontiers(compare_frontiers([a, b]))
    out = capsys.readouterr().out
    assert rc == 1
    assert "sjc-2 24h behind" in out
    assert "got NOTHING" in out and "autonomy/direct" in out


def test_a_single_machine_holding_an_origin_is_not_compared(capsys):
    from tools.network.fleet_sync_report import compare_frontiers, render_frontiers

    a = _with_frontier("home", "autonomy", "cc" * 6, 1_000_000_000_000_000_000, 0)
    assert render_frontiers(compare_frontiers([a])) == 0
    assert "FAIL" not in capsys.readouterr().out
