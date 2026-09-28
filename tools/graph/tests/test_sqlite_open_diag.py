"""auto-bkv3p: the SQLite open counter names its openers and sees closes."""

import gc
import sqlite3

from tools.graph import sqlite_open_diag


def _open_here(path):
    return sqlite3.connect(path)


def _rows(name):
    return {row["opener"].split(" < ")[0].rsplit(" ", 1)[1]: row
            for row in sqlite_open_diag.snapshot(name)}


def test_counts_opens_by_opener_and_what_stays_open(tmp_path):
    sqlite_open_diag.install()
    path = tmp_path / "counted.db"
    kept = [_open_here(str(path)) for _ in range(3)]
    closed = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    closed.close()
    rows = _rows("counted.db")
    assert rows["_open_here"]["opened"] == 3
    assert rows["_open_here"]["open_now"] == 3
    assert rows["test_counts_opens_by_opener_and_what_stays_open"] == {
        "database": "counted.db",
        "opener": rows["test_counts_opens_by_opener_and_what_stays_open"]["opener"],
        "opened": 1,
        "open_now": 0,
    }
    kept[0].close()
    del kept[1:]
    gc.collect()
    assert _rows("counted.db")["_open_here"]["open_now"] == 0


def test_a_callers_factory_is_kept_and_memory_databases_are_not_counted(tmp_path):
    sqlite_open_diag.install()

    class Mine(sqlite3.Connection):
        pass

    conn = sqlite3.connect(str(tmp_path / "own.db"), factory=Mine)
    assert isinstance(conn, Mine)
    conn.close()
    sqlite3.connect(":memory:").close()
    assert [row["database"] for row in sqlite_open_diag.snapshot()
            if row["database"] in ("own.db", ":memory:")] == ["own.db"]


def test_a_positional_factory_is_wrapped_in_place(tmp_path):
    sqlite_open_diag.install()

    class Mine(sqlite3.Connection):
        pass

    conn = sqlite3.connect(str(tmp_path / "positional.db"), 5.0, 0, "", True, Mine)
    assert isinstance(conn, Mine)
    assert [row["open_now"] for row in sqlite_open_diag.snapshot("positional.db")] == [1]
    conn.close()
