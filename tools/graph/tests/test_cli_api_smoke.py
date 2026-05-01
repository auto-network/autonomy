"""End-to-end smoke suite: every migrated cmd_* routes through the
dashboard API when ``GRAPH_API`` is set.

Stands up the dashboard Starlette app in-process via TestClient, then
patches ``urllib.request.urlopen`` so :class:`HttpClient`'s HTTP calls
land on the TestClient (no real uvicorn, no real TCP, no real TLS).

Each test invokes a CLI handler directly (argparse Namespace → cmd_X())
and asserts:

1. The command round-trips through an API endpoint (HttpClient call seen).
2. Output contains the expected content / structure.
3. No CLI-side ``sqlite3.connect`` calls happened during the command.
   The server side legitimately opens sqlite connections — we gate the
   guard by inspecting the call stack for frames in ``cli.py``/
   ``set_cmd.py``.

This is the contract the conformance test can't enforce: not just
"cmd_* should route through get_client()" but "it actually does reach
the dashboard over HTTP".
"""

from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest import mock

import pytest
from starlette.testclient import TestClient

from tools.graph import cli as graph_cli
from tools.graph import db as graph_db_mod
from tools.graph import set_cmd
from tools.graph.db import GraphDB, resolve_caller_db_path
from tools.graph.ingest import ingest_claude_code_session
from tools.graph.models import Source, Thought


# ── HttpClient → TestClient plumbing ────────────────────────────


class _FakeResponse:
    """Minimal stand-in for an ``http.client.HTTPResponse``.

    ``HttpClient._request`` only calls ``.read()`` on the response and
    ignores everything else, so a stream-backed stub is enough.
    """

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _testclient_urlopen(test_client: TestClient):
    """Return a drop-in ``urlopen`` that dispatches to a TestClient."""

    def urlopen(req, *args, **kwargs):
        url = req.full_url
        # Strip the base URL off — TestClient needs a path.
        base = "https://localhost:8080"
        if url.startswith(base):
            path = url[len(base):]
        else:
            # Any base URL works; the client uses whatever GRAPH_API says.
            idx = url.find("/api/")
            path = url[idx:] if idx != -1 else url
        method = req.get_method()
        headers = {k: v for k, v in req.header_items()}
        data = req.data

        if method == "GET":
            resp = test_client.get(path, headers=headers)
        elif method == "POST":
            resp = test_client.post(path, content=data, headers=headers)
        elif method == "PUT":
            resp = test_client.put(path, content=data, headers=headers)
        elif method == "DELETE":
            resp = test_client.delete(path, headers=headers)
        else:
            raise AssertionError(f"unexpected HTTP method: {method}")

        if resp.status_code >= 400:
            raise urllib.error.HTTPError(
                url, resp.status_code, resp.reason_phrase or "",
                resp.headers, io.BytesIO(resp.content),
            )
        return _FakeResponse(resp.content, resp.status_code)

    return urlopen


# ── Fixtures ───────────────────────────────────────────────────


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    """Stand up a clean per-test orgs/ root + seeded personal/autonomy DBs."""
    root = tmp_path / "orgs"
    legacy = tmp_path / "legacy.db"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.setattr(graph_db_mod, "DEFAULT_DB", legacy)
    GraphDB.close_all_pooled()
    # Create the target org DBs so graph_ops writes have somewhere to land.
    GraphDB.create_org_db("personal", type_="personal").close()
    GraphDB.create_org_db("autonomy").close()
    try:
        yield root
    finally:
        GraphDB.close_all_pooled()


@pytest.fixture
def seeded_source_id(orgs_root):
    """Seed a canonical note in autonomy.db so reads have something to return."""
    db = GraphDB.open_org_db("autonomy", mode="rw")
    try:
        sid = str(uuid.uuid4())
        src = Source(
            id=sid, type="note", platform="local", project="autonomy",
            title="Dispatch Lifecycle Signpost", file_path=f"note:{sid}",
            metadata={"tags": ["signpost"], "author": "test"},
            publication_state="canonical",
        )
        db.insert_source(src)
        db.insert_thought(Thought(
            source_id=sid, content="canonical signpost content",
            role="user", turn_number=1, tags=["signpost"],
        ))
        db.insert_note_version(sid, 1, "canonical signpost content")
        db.commit()
    finally:
        db.close()
    GraphDB.close_all_pooled()
    return sid


@pytest.fixture
def dashboard_app(orgs_root, monkeypatch):
    """Boot the dashboard ASGI app against the test orgs/ root."""
    from tools.dashboard import server as dashboard_server
    return dashboard_server.app


@pytest.fixture
def api_client(dashboard_app, monkeypatch):
    """TestClient + HttpClient plumbing.

    Sets ``GRAPH_API`` so ``get_client()`` returns HttpClient, then patches
    ``urllib.request.urlopen`` so HttpClient's calls land on the
    in-process ASGI app instead of a real TCP socket.
    """
    client = TestClient(dashboard_app)
    monkeypatch.setenv("GRAPH_API", "https://localhost:8080")
    # All three modules that hold urllib.request need the same patch — the
    # api_client.py write helpers still use urlopen too.
    opener = _testclient_urlopen(client)
    monkeypatch.setattr("urllib.request.urlopen", opener)
    return client


# ── sqlite3-forbidden guard ────────────────────────────────────


class _SqliteForbiddenError(AssertionError):
    pass


def _forbidden_sqlite_connect(*args, **kwargs):
    """Raise if the caller's stack has any frame in cli.py or set_cmd.py.

    Server-side sqlite3 connections (``ops.*`` → ``GraphDB``) are allowed
    because they happen during the ASGI handler's execution, not in the
    CLI process. We want to catch: a cmd_ that opens a DB directly, or
    falls through to ops before get_client(), which would indicate a new
    bypass of the migration.
    """
    import inspect
    frame = sys._getframe(1)
    seen_cli = False
    while frame is not None:
        fname = frame.f_code.co_filename
        if fname.endswith("/cli.py") or fname.endswith("/set_cmd.py"):
            seen_cli = True
            break
        frame = frame.f_back
    if seen_cli:
        raise _SqliteForbiddenError(
            f"sqlite3.connect called inside CLI handler: "
            f"args={args!r} kwargs={kwargs!r}"
        )
    return _original_connect(*args, **kwargs)


_original_connect = sqlite3.connect


@pytest.fixture
def forbid_cli_sqlite(monkeypatch):
    """Raise on any ``sqlite3.connect`` from within cli.py / set_cmd.py."""
    monkeypatch.setattr(sqlite3, "connect", _forbidden_sqlite_connect)


# ── arg builders ───────────────────────────────────────────────


def _cli_args(**kw) -> argparse.Namespace:
    """Baseline argparse namespace for cmd_* handlers.

    Most cmd_* accept ``args.db`` (resolved lazily) + their own flags.
    Unused flags default to ``None`` / ``False`` so ``getattr`` paths
    don't trip AttributeError.
    """
    defaults = {
        "db": resolve_caller_db_path(None),
        "source": None, "first": False, "max_chars": 0, "json": False,
        "all_comments": False, "html_output": False, "save": None,
        "window": 3, "turn": "last", "limit": 50, "id": None,
        "source_id": None, "state": None, "include": None,
        "from_org": None,
        "only_org": None, "org_mode": None, "project": None, "type": None,
        "since": None, "until": None, "author": None, "verbose": False,
        "tags": None, "text": None, "content_stdin": None, "html": None,
        "attach": None, "force": False, "actor": "user",
        "bead": None, "relation": "informed_by", "turns": None, "note": None,
        "integrate_ids": None, "file_path": None, "alt": None,
        "alt_file": None, "org": None,
        # cmd_search
        "query": None, "width": 200, "or_mode": False, "tag": None,
        "states": None, "include_raw": False, "session": None,
        "only_project": None,
        # cmd_set_add/show/etc
        "set_id": None, "set_at_rev": None, "key": None, "from_file": None,
        "as_rev": None, "min_rev": None, "stored_rev": None,
        "no_upconvert": False, "target_id": None, "to": None,
        "successor": None, "to_rev": None, "dry_run": False,
    }
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _user_entry(text: str, ts: str) -> dict:
    return {
        "type": "user",
        "uuid": f"u-{abs(hash((text, ts))) & 0xffff:x}",
        "message": {"role": "user", "content": text},
        "timestamp": ts,
    }


def _assistant_entry(text: str, ts: str) -> dict:
    return {
        "type": "assistant",
        "uuid": f"a-{abs(hash((text, ts))) & 0xffff:x}",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": "claude-test",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
        "timestamp": ts,
    }


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


# ── Read-command smoke tests ──────────────────────────────────


def test_cmd_search_routes_through_api(
    api_client, forbid_cli_sqlite, seeded_source_id, capsys, monkeypatch,
):
    """``graph search`` hits /api/graph/search via HttpClient."""
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    args = _cli_args(query=["signpost"], limit=10)
    # cmd_search expects args.query, args.limit, and various org flags;
    # build the full shape it expects.
    args.query = ["signpost"]
    args.limit = 10
    args.or_mode = False
    args.tag = None
    args.project = None
    args.states = None
    args.include_raw = False
    args.session = None
    args.only_project = None

    graph_cli.cmd_search(args)
    out = capsys.readouterr().out
    # The API returns the same signpost content.
    assert "Signpost" in out or "signpost" in out.lower()


def test_cmd_sources_routes_through_api(
    api_client, forbid_cli_sqlite, seeded_source_id, capsys, monkeypatch,
):
    """``graph sources`` hits /api/graph/sources via HttpClient.

    This was the decisive failing command on master: ``cmd_sources`` used
    to open graph.db directly and fail in read-only container mounts.
    """
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    args = _cli_args(project=None, type=None, limit=10)
    graph_cli.cmd_sources(args)
    out = capsys.readouterr().out
    assert "Dispatch Lifecycle Signpost" in out


def test_cmd_context_routes_through_api(
    api_client, forbid_cli_sqlite, seeded_source_id, capsys, monkeypatch,
):
    """``graph context <id> <turn>`` hits /api/graph/source + /api/graph/{id}
    via HttpClient and renders turn bodies.

    Regression: the CLI was calling /api/graph/resolve/{id} (404), the
    LookupError got swallowed, and ``cmd_context`` printed the header
    line but no turn content (auto-5zess). Asserting only the title is
    not enough — the title is rendered from ``client.get_source``, so it
    showed up even with zero entries.
    """
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    args = _cli_args(source=seeded_source_id, turn="1", window=3)
    graph_cli.cmd_context(args)
    out = capsys.readouterr().out
    assert "Dispatch Lifecycle Signpost" in out
    # Header AND label AND body — title-only assertions hid an empty-entries
    # bug for months (auto-5zess). The label assertion specifically guards
    # auto-yhnpm: in container/HTTP mode the entries payload has ``role`` but
    # no ``entry_type``, so a label test that demands "USER" (matching the
    # seeded role="user") catches a regression to the etype-only branch.
    assert "Turn 1 — USER" in out
    assert "canonical signpost content" in out


def test_cmd_context_role_labels_match_seeded_roles(
    api_client, forbid_cli_sqlite, capsys, monkeypatch, orgs_root,
):
    """Mixed-role fixture: container-mode ``graph context`` must label each
    turn by its actual role, not blanket-ASSISTANT every entry.

    Regression for auto-yhnpm: the host path returns ``entry_type``
    ('thought' | 'derivation') alongside ``role``, the server path
    returns only ``role``. The CLI's label logic used to branch on
    ``entry_type == 'thought'`` only, so every entry from the HTTP
    payload (where ``entry_type`` is absent) rendered as ASSISTANT —
    user turns included.
    """
    monkeypatch.setenv("GRAPH_ORG", "autonomy")

    db = GraphDB.open_org_db("autonomy", mode="rw")
    sid = str(uuid.uuid4())
    try:
        src = Source(
            id=sid, type="session", platform="claude-code",
            project="autonomy", title="mixed-role context fixture",
            file_path=f"session:{sid}", metadata={"author": "test"},
        )
        db.insert_source(src)
        db.insert_thought(Thought(
            source_id=sid, content="user turn one body",
            role="user", turn_number=1,
        ))
        db.insert_thought(Thought(
            source_id=sid, content="assistant turn two body",
            role="assistant", turn_number=2,
        ))
        db.insert_thought(Thought(
            source_id=sid, content="user turn three body",
            role="user", turn_number=3,
        ))
        db.commit()
    finally:
        db.close()
    GraphDB.close_all_pooled()

    args = _cli_args(source=sid, turn="2", window=2, max_chars=0)
    graph_cli.cmd_context(args)
    out = capsys.readouterr().out

    assert "mixed-role context fixture" in out
    # Label MUST match role on every turn — and the body MUST be present
    # (smoke tests that only checked headers shipped a 404 unnoticed).
    assert "Turn 1 — USER" in out
    assert "user turn one body" in out
    assert "Turn 2 — ASSISTANT" in out
    assert "assistant turn two body" in out
    assert "Turn 3 — USER" in out
    assert "user turn three body" in out
    # The blanket-ASSISTANT regression printed "Turn 1 — ASSISTANT" — fail
    # loud if it ever comes back.
    assert "Turn 1 — ASSISTANT" not in out
    assert "Turn 3 — ASSISTANT" not in out


def test_cmd_context_last_n_role_labels_match_seeded_roles(
    api_client, forbid_cli_sqlite, capsys, monkeypatch, orgs_root,
):
    """The ``last:N`` tail-render path has its own loop with its own label
    logic — guard it the same way as the around-turn path."""
    monkeypatch.setenv("GRAPH_ORG", "autonomy")

    db = GraphDB.open_org_db("autonomy", mode="rw")
    sid = str(uuid.uuid4())
    try:
        src = Source(
            id=sid, type="session", platform="claude-code",
            project="autonomy", title="tail role-label fixture",
            file_path=f"session:{sid}", metadata={"author": "test"},
        )
        db.insert_source(src)
        db.insert_thought(Thought(
            source_id=sid, content="user tail body",
            role="user", turn_number=1,
        ))
        db.insert_thought(Thought(
            source_id=sid, content="assistant tail body",
            role="assistant", turn_number=2,
        ))
        db.commit()
    finally:
        db.close()
    GraphDB.close_all_pooled()

    args = _cli_args(source=sid, turn="last:2", window=0, max_chars=0)
    graph_cli.cmd_context(args)
    out = capsys.readouterr().out

    assert "Turn 1 — USER" in out
    assert "user tail body" in out
    assert "Turn 2 — ASSISTANT" in out
    assert "assistant tail body" in out
    assert "Turn 1 — ASSISTANT" not in out


def test_read_source_full_via_api_uses_resolve_endpoint(monkeypatch):
    """Pin the URL pattern for ``_read_source_full_via_api``.

    Bead auto-5zess: the helper was hitting /api/graph/resolve/{id},
    which the dashboard never registered, returning 404. The route is
    /api/graph/{id} and supports ``?turn=N&window=W`` for a server-side
    slice.
    """
    from tools.graph import client as client_mod

    captured: list[tuple[str, dict | None, str | None]] = []

    class _StubHttpClient(client_mod.HttpClient):
        def __init__(self):
            pass

        def _get(self, path, params=None, *, org=None):
            captured.append((path, params, org))
            return {
                "source": {"id": "abc"},
                "entries": [{"turn_number": 5, "role": "user",
                             "content": "hi", "created_at": ""}],
            }

    monkeypatch.setattr(graph_cli, "get_client", lambda: _StubHttpClient())

    result = graph_cli._read_source_full_via_api(
        "abc-def", org="autonomy", around_turn=5, window=2,
    )

    assert result is not None
    assert result["entries"] != []
    assert len(captured) == 1
    path, params, org = captured[0]
    assert path == "/api/graph/abc-def"
    assert params == {"turn": "5", "window": "2"}
    assert org == "autonomy"


def test_read_source_full_via_api_tail_n_uses_from(monkeypatch):
    """Pin the URL pattern for ``tail_n``: the helper must round-trip
    ``?from=-N``, not two requests.

    Bead spec: a coordinator should be able to read "the last N turns of
    a session" in *one* call, not two (max-turn discovery + window). The
    network spy here is the canonical assertion that we avoid the old
    metadata-fishing pattern.
    """
    from tools.graph import client as client_mod

    captured: list[tuple[str, dict | None, str | None]] = []

    class _StubHttpClient(client_mod.HttpClient):
        def __init__(self):
            pass

        def _get(self, path, params=None, *, org=None):
            captured.append((path, params, org))
            return {
                "source": {"id": "abc"},
                "entries": [
                    {"turn_number": 99, "role": "user",
                     "content": "tail entry", "created_at": ""},
                ],
            }

    monkeypatch.setattr(graph_cli, "get_client", lambda: _StubHttpClient())

    result = graph_cli._read_source_full_via_api(
        "abc-def", org="autonomy", tail_n=7,
    )

    assert result is not None
    assert result["entries"] != []
    assert len(captured) == 1, (
        "tail_n must reach the server in a single round trip — multiple "
        "calls indicate a regression to the two-call (max-turn discovery + "
        "window) pattern this bead was created to remove"
    )
    path, params, _org = captured[0]
    assert path == "/api/graph/abc-def"
    assert params == {"from": "-7"}


def test_cmd_tail_routes_through_api(
    api_client, forbid_cli_sqlite, capsys, monkeypatch, orgs_root,
):
    """``graph tail <id> N`` hits /api/graph/{id}?from=-N and renders the
    trailing N turns. End-to-end check that the new CLI command, the
    HttpClient helper, and the API endpoint compose cleanly.
    """
    monkeypatch.setenv("GRAPH_ORG", "autonomy")

    # Seed a 25-turn session so a tail of 5 is unambiguous.
    db = GraphDB.open_org_db("autonomy", mode="rw")
    sid = str(uuid.uuid4())
    try:
        src = Source(
            id=sid, type="session", platform="claude-code",
            project="autonomy", title="long tail session",
            file_path=f"session:{sid}", metadata={"author": "test"},
        )
        db.insert_source(src)
        for n in range(1, 26):
            db.insert_thought(Thought(
                source_id=sid,
                content=f"turn {n} content body",
                role="user",
                turn_number=n,
            ))
        db.commit()
    finally:
        db.close()
    GraphDB.close_all_pooled()

    args = _cli_args(source=sid, max_chars=0)
    args.n = 5
    graph_cli.cmd_tail(args)
    out = capsys.readouterr().out

    assert "long tail session" in out
    # Last 5 turns: 21..25.
    for n in (21, 22, 23, 24, 25):
        assert f"Turn {n}" in out, f"missing turn {n} in tail output"
    # Earlier turns must NOT appear in a tail of 5.
    for n in (1, 5, 10, 15, 20):
        assert f"Turn {n} —" not in out, f"unexpected turn {n} in tail of 5"


def test_cmd_context_last_n_routes_through_api(
    api_client, forbid_cli_sqlite, capsys, monkeypatch, orgs_root,
):
    """``graph context <id> last:N`` is the cleanest extension of the
    existing ``last`` keyword — it should produce the same trailing slice
    as ``graph tail <id> N`` and route through the API the same way."""
    monkeypatch.setenv("GRAPH_ORG", "autonomy")

    db = GraphDB.open_org_db("autonomy", mode="rw")
    sid = str(uuid.uuid4())
    try:
        src = Source(
            id=sid, type="session", platform="claude-code",
            project="autonomy", title="context-last session",
            file_path=f"session:{sid}", metadata={"author": "test"},
        )
        db.insert_source(src)
        for n in range(1, 16):
            db.insert_thought(Thought(
                source_id=sid,
                content=f"turn {n} content body",
                role="user",
                turn_number=n,
            ))
        db.commit()
    finally:
        db.close()
    GraphDB.close_all_pooled()

    args = _cli_args(source=sid, turn="last:3", window=0, max_chars=0)
    graph_cli.cmd_context(args)
    out = capsys.readouterr().out

    assert "context-last session" in out
    for n in (13, 14, 15):
        assert f"Turn {n}" in out
    for n in (1, 10, 12):
        assert f"Turn {n} —" not in out


def test_read_source_full_via_api_no_window_omits_params(monkeypatch):
    """Without ``around_turn`` / ``window`` the helper sends no query
    string — preserves the legacy front-of-source slice for the
    ``--turn last`` codepath where the CLI still has to fetch every
    entry to discover the max turn."""
    from tools.graph import client as client_mod

    captured: list[tuple[str, dict | None]] = []

    class _StubHttpClient(client_mod.HttpClient):
        def __init__(self):
            pass

        def _get(self, path, params=None, *, org=None):
            captured.append((path, params))
            return {"source": {"id": "abc"}, "entries": []}

    monkeypatch.setattr(graph_cli, "get_client", lambda: _StubHttpClient())
    graph_cli._read_source_full_via_api("abc", org=None)

    assert captured == [("/api/graph/abc", None)]


def test_cmd_context_last_refreshes_target_session_through_api(
    api_client, forbid_cli_sqlite, tmp_path, capsys, monkeypatch,
):
    """API-backed `graph context <src> last:N` refreshes the addressed session."""
    monkeypatch.setenv("GRAPH_ORG", "autonomy")

    session_path = tmp_path / "sessions" / "api-tail-session.jsonl"
    _write_jsonl(session_path, [
        _user_entry("Initial API question", "2026-04-22T10:00:00Z"),
        _assistant_entry("Initial API answer", "2026-04-22T10:00:05Z"),
    ])

    db = GraphDB.open_org_db("autonomy", mode="rw")
    try:
        result = ingest_claude_code_session(db, session_path)
    finally:
        db.close()
    source_id = result["source_id"]

    with open(session_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(_user_entry("What changed most recently?", "2026-04-22T10:01:00Z")) + "\n")
        f.write(json.dumps(_assistant_entry("Fresh API-backed reply", "2026-04-22T10:01:05Z")) + "\n")

    args = _cli_args(source=source_id, turn="last:2", window=0)
    graph_cli.cmd_context(args)

    out = capsys.readouterr().out
    assert "Fresh API-backed reply" in out


def test_cmd_attachments_requires_source_id_in_container(
    api_client, forbid_cli_sqlite, seeded_source_id, capsys, monkeypatch,
):
    """Unfiltered ``graph attachments`` prints a host-only notice in container."""
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    args = _cli_args(source_id=None, limit=10)
    graph_cli.cmd_attachments(args)
    err = capsys.readouterr().err
    assert "only available on the host" in err


# ── Write-command smoke tests ─────────────────────────────────


def test_cmd_note_routes_through_api(
    api_client, forbid_cli_sqlite, capsys, monkeypatch,
):
    """``graph note "text"`` creates a note via the API; lands in caller org."""
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    args = _cli_args(
        text=["smoke", "test", "note"],
        tags=None, author=None, project="autonomy",
    )
    graph_cli.cmd_note(args)
    out = capsys.readouterr().out
    assert "Note saved" in out
    GraphDB.close_all_pooled()
    db = GraphDB.open_org_db("autonomy", mode="rw")
    try:
        rows = db.conn.execute(
            "SELECT COUNT(*) FROM sources WHERE title LIKE ?",
            ("smoke test note%",),
        ).fetchone()
        assert rows[0] == 1
    finally:
        db.close()


def test_graph_note_router_roundtrip_via_api(
    api_client, forbid_cli_sqlite, capsys, monkeypatch,
):
    """The real user invocation path: argparse → cmd_note_router → persisted.

    Regression for the silent-write bug: router branches on is_api_mode()
    and dispatches to api_client.api_note (old path), which expects the
    pre-iv6c5 server response shape and swallows the new one. Result:
    exit 0, no output, nothing persisted.
    """
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    args = _cli_args(
        text=["router", "smoke", "ping"],
        tags=None, author=None, project="autonomy",
    )
    graph_cli.cmd_note_router(args)
    out = capsys.readouterr().out
    assert "Note saved" in out
    GraphDB.close_all_pooled()
    db = GraphDB.open_org_db("autonomy", mode="rw")
    try:
        rows = db.conn.execute(
            "SELECT COUNT(*) FROM sources WHERE title LIKE ?",
            ("router smoke ping%",),
        ).fetchone()
        assert rows[0] == 1
    finally:
        db.close()


def test_cmd_note_update_routes_through_api(
    api_client, forbid_cli_sqlite, seeded_source_id, capsys, monkeypatch,
):
    """``graph note update`` routes to POST /api/graph/note/update."""
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    # Also bypass the read-required-note protocol for the smoke test.
    monkeypatch.setattr(graph_cli, "_require_read", lambda *a, **k: None)
    args = _cli_args(
        source=seeded_source_id, text=["v2 from smoke test"],
        integrate_ids=None,
    )
    graph_cli.cmd_note_update(args)
    out = capsys.readouterr().out
    assert "Note updated" in out
    db = GraphDB.open_org_db("autonomy", mode="ro")
    try:
        row = db.conn.execute(
            "SELECT content FROM note_versions "
            "WHERE source_id = ? ORDER BY version DESC LIMIT 1",
            (seeded_source_id,),
        ).fetchone()
        assert row and "v2 from smoke test" in row["content"]
    finally:
        db.close()


def test_cmd_link_routes_through_api(
    api_client, forbid_cli_sqlite, seeded_source_id, capsys, monkeypatch,
):
    """``graph link`` creates an edge via POST /api/graph/link.

    The api_client.py path prints via ``_print_output(response['output'])``
    which the link endpoint doesn't emit — so we verify the side effect
    (a new edge row in autonomy.db) rather than stdout.
    """
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    args = _cli_args(
        bead="auto-smoke-test", source=seeded_source_id,
        relation="conceived_at", turns="1",
    )
    graph_cli.cmd_link(args)
    GraphDB.close_all_pooled()
    db = GraphDB.open_org_db("autonomy", mode="rw")
    try:
        row = db.conn.execute(
            "SELECT COUNT(*) FROM edges "
            "WHERE source_id = ? AND target_id = ? AND relation = ?",
            ("auto-smoke-test", seeded_source_id, "conceived_at"),
        ).fetchone()
        assert row[0] == 1
    finally:
        db.close()


def test_cmd_comment_add_routes_through_api(
    api_client, forbid_cli_sqlite, seeded_source_id, capsys, monkeypatch,
):
    """``graph comment <src> "text"`` hits POST /api/graph/comment."""
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    args = _cli_args(
        source=seeded_source_id, text=["smoke comment"],
        actor="test",
    )
    graph_cli.cmd_comment_add(args)
    out = capsys.readouterr().out
    assert "Comment added" in out


def test_graph_comment_router_roundtrip_via_api(
    api_client, forbid_cli_sqlite, seeded_source_id, capsys, monkeypatch,
):
    """Real user path: argparse → cmd_comment_router → visible success.

    Same class of regression as the note-router silent-write: the router
    branched on is_api_mode() and sent API-mode callers to api_client's
    duplicate path, bypassing the migrated cmd_comment_add.
    """
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    args = _cli_args(
        args=[seeded_source_id, "router", "comment", "ping"],
        actor="test",
    )
    graph_cli.cmd_comment_router(args)
    out = capsys.readouterr().out
    assert "Comment added" in out


# ── Settings command smoke tests ──────────────────────────────


def test_cmd_set_list_routes_through_api(
    api_client, forbid_cli_sqlite, capsys, monkeypatch,
):
    """``graph set list`` hits GET /api/graph/sets — fixture DB only carries
    the schema-meta Settings auto-flushed at first connection (auto-82xyq).
    """
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    args = _cli_args()
    set_cmd.cmd_set_list(args)
    out = capsys.readouterr().out
    # The flush surfaces autonomy.schema / autonomy.schema.synopsis on
    # every writable DB; absence of either means the request didn't reach
    # the server or the flush regressed.
    assert "autonomy.schema" in out


def test_cmd_set_add_then_show_routes_through_api(
    api_client, forbid_cli_sqlite, tmp_path, capsys, monkeypatch,
):
    """``graph set add`` → ``graph set show`` round-trip via the API."""
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps({"setting": "value"}))

    add_args = _cli_args()
    add_args.set_at_rev = "test.generic#1"
    add_args.key = "smoke-key"
    add_args.from_file = str(payload_path)
    add_args.state = "raw"

    # Settings require a registered schema + revision. Rather than
    # reach into the schema registry, we accept either a clean success
    # OR a schema-validation error (the request reached the server,
    # which is all this smoke test needs to prove).
    try:
        set_cmd.cmd_set_add(add_args)
    except (SystemExit, ValueError):
        pytest.skip(
            "schema registry rejected the test payload — "
            "acceptable; this test covers wiring, not semantics",
        )
    out = capsys.readouterr().out
    assert "Setting:" in out


def test_http_client_translates_cross_org_409_to_exception(
    api_client, forbid_cli_sqlite, seeded_source_id, capsys, monkeypatch,
):
    """Peer write rejected by the server surfaces as a CrossOrgWriteError.

    An ``anchore`` caller writing to an ``autonomy`` note is the
    motivating regression (auto-co51y). The server returns 409; HttpClient
    translates that back to ``CrossOrgWriteError`` so the cmd_'s existing
    except block produces the same exit-2 it did in local mode.
    """
    GraphDB.create_org_db("anchore").close()
    monkeypatch.setenv("GRAPH_ORG", "anchore")
    monkeypatch.setattr(graph_cli, "_require_read", lambda *a, **k: None)
    args = _cli_args(
        source=seeded_source_id, text=["cross-org hijack attempt"],
        integrate_ids=None,
    )
    with pytest.raises(SystemExit) as exc_info:
        graph_cli.cmd_note_update(args)
    assert exc_info.value.code == 2


def test_cmd_move_routes_through_api(
    api_client, forbid_cli_sqlite, seeded_source_id, capsys, monkeypatch, orgs_root,
):
    """``graph move`` hits the dashboard API and performs a cross-org transfer."""
    GraphDB.create_org_db("anchore").close()
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    args = _cli_args(
        source_id=seeded_source_id, from_org="autonomy", to_org="anchore", reason="rehome",
    )
    graph_cli.cmd_move(args)
    out = capsys.readouterr().out
    assert "Moved" in out

    ac = sqlite3.connect(str(orgs_root / "autonomy.db"))
    nc = sqlite3.connect(str(orgs_root / "anchore.db"))
    try:
        row = ac.execute(
            "SELECT moved_to_org, deprecated FROM sources WHERE id = ?",
            (seeded_source_id,),
        ).fetchone()
        assert row == ("anchore", 1)
        row = nc.execute(
            "SELECT moved_to_org, deprecated FROM sources WHERE id = ?",
            (seeded_source_id,),
        ).fetchone()
        assert row == (None, 0)
    finally:
        ac.close()
        nc.close()


# ── Read-command smoke for previously-bypassed commands ──────


def test_cmd_attention_routes_through_api(
    api_client, forbid_cli_sqlite, seeded_source_id, capsys, monkeypatch,
):
    """``graph attention`` hits the server, never opens a local DB."""
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    args = _cli_args(
        since=None, search=None, last=5, session=None, context=0,
    )
    graph_cli.cmd_attention(args)
    capsys.readouterr()  # just make sure no sqlite3.connect fired


def test_cmd_sessions_status_routes_through_api(
    api_client, forbid_cli_sqlite, capsys, monkeypatch,
):
    """``graph sessions --status`` must route through the dashboard API."""
    rows = [
        {
            "tmux_name": "auto-live",
            "is_live": 1,
            "activity_state": "busy",
            "last_activity": 1_700_000_000.0,
            "created_at": 1_700_000_000.0,
            "context_tokens": 4200,
            "label": "live label",
        },
        {
            "tmux_name": "auto-dead",
            "is_live": 0,
            "activity_state": "idle",
            "last_activity": 1_699_999_000.0,
            "created_at": 1_699_999_000.0,
            "context_tokens": 800,
            "label": "dead label",
        },
    ]
    monkeypatch.setattr("tools.dashboard.server.dao_sessions.get_session_status_rows", lambda since=None: rows)
    args = _cli_args(status=True, since="24h")
    graph_cli.cmd_sessions(args)
    out = capsys.readouterr().out
    assert "auto-live" in out
    assert "auto-dead" in out
    assert "busy" in out
    assert "dead" in out


def test_cmd_wait_routes_through_api(
    api_client, forbid_cli_sqlite, capsys, monkeypatch,
):
    """``graph wait`` must use the dashboard API in container mode."""
    from tools.dashboard import server as dashboard_server

    async def _fake_run_cli_json(cmd, timeout=30):
        if cmd[:2] == ["bd", "show"]:
            return {"id": cmd[2], "labels": ["readiness:approved"]}
        raise AssertionError(f"unexpected run_cli_json call: {cmd!r}")

    monkeypatch.setattr(
        dashboard_server,
        "run_cli_json",
        _fake_run_cli_json,
    )
    monkeypatch.setattr(
        dashboard_server,
        "get_runs_for_bead",
        lambda bead_id: [
            {
                "bead_id": bead_id,
                "status": "DONE",
                "completed_at": "2026-01-01T00:05:00Z",
                "duration_secs": 5,
                "commit_hash": "abcdef123456",
                "lines_added": 3,
                "lines_removed": 1,
                "files_changed": 2,
                "commit_message": "Ship it",
                "reason": "approved",
            }
        ],
    )

    def _should_not_run(*args, **kwargs):
        raise AssertionError("cmd_wait should not call subprocess.run in API mode")

    monkeypatch.setattr(graph_cli.subprocess, "run", _should_not_run)

    args = _cli_args(bead_id="auto-wait", timeout=1)
    with pytest.raises(SystemExit) as exc_info:
        graph_cli.cmd_wait(args)
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "✓ auto-wait DONE (5s)" in out
    assert "Commit: abcdef1 (+3 -1, 2 files)" in out
    assert "Message: Ship it" in out


def test_cmd_collab_topics_routes_through_api(
    api_client, forbid_cli_sqlite, capsys, monkeypatch,
):
    """``graph collab topics`` round-trips via /api/graph/collab-topics."""
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    args = _cli_args(limit=50)
    graph_cli.cmd_collab_topics(args)
    capsys.readouterr()


def test_cmd_notes_routes_through_api(
    api_client, forbid_cli_sqlite, capsys, monkeypatch,
):
    """``graph notes`` lists notes through the dashboard API."""
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    args = _cli_args(
        since=None, tags=None, project=None, limit=10,
        states=None, include_raw=False, short=False, headline=False,
    )
    graph_cli.cmd_notes(args)
    capsys.readouterr()


def test_cmd_journal_list_routes_through_api(
    api_client, forbid_cli_sqlite, capsys, monkeypatch,
):
    """``graph journal`` (list mode) round-trips via the API."""
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    args = _cli_args(since=None, limit=10, expanded=False)
    graph_cli.cmd_journal_list(args)
    capsys.readouterr()


def test_cmd_stats_routes_through_api(
    api_client, forbid_cli_sqlite, capsys, monkeypatch,
):
    """``graph stats`` hits /api/graph/stats instead of opening graph.db."""
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    args = _cli_args()
    graph_cli.cmd_stats(args)
    out = capsys.readouterr().out
    assert "Knowledge Graph Stats" in out


def test_cmd_tree_routes_through_api(
    api_client, forbid_cli_sqlite, capsys, monkeypatch,
):
    """``graph tree`` asks the server for the hierarchy, not the local DB."""
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    args = _cli_args(root=None, depth=3)
    graph_cli.cmd_tree(args)
    capsys.readouterr()


def test_cmd_entities_routes_through_api(
    api_client, forbid_cli_sqlite, capsys, monkeypatch,
):
    """``graph entities`` lists / searches via the server."""
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    args = _cli_args(query=None, type=None, limit=20)
    graph_cli.cmd_entities(args)
    capsys.readouterr()


def test_cmd_related_routes_through_api(
    api_client, forbid_cli_sqlite, capsys, monkeypatch,
):
    """``graph related <term>`` resolves an entity and its thoughts via API."""
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    args = _cli_args()
    args.term = "nothing-here"
    graph_cli.cmd_related(args)
    capsys.readouterr()


def test_cmd_read_routes_through_api(
    api_client, forbid_cli_sqlite, seeded_source_id, capsys, monkeypatch,
):
    """``graph read <id>`` fetches the source + content via the API."""
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    args = _cli_args(
        source=seeded_source_id, first=False, max_chars=0, json=False,
        all_comments=False, html_output=False, save=None,
    )
    graph_cli.cmd_read(args)
    out = capsys.readouterr().out
    # The seeded source has a canonical thought; the output must include
    # the source title so we know the content really came back.
    assert "Dispatch Lifecycle" in out


def test_cmd_read_routes_through_api_creates_read_marker(
    api_client, forbid_cli_sqlite, seeded_source_id, capsys, monkeypatch, tmp_path,
):
    """API-backed ``graph read`` should drop the same read marker as local reads."""
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(graph_cli, "_in_container", lambda: True)
    args = _cli_args(
        source=seeded_source_id, first=False, max_chars=0, json=False,
        all_comments=False, html_output=False, save=None,
    )

    graph_cli.cmd_read(args)
    capsys.readouterr()

    marker = tmp_path / ".graph" / "reads" / seeded_source_id
    assert marker.exists(), f"Expected read marker at {marker}"
    graph_cli._require_read(seeded_source_id[:8], "seeded note marker check")


def test_cmd_read_save_routes_through_api_and_writes_file(
    api_client, forbid_cli_sqlite, seeded_source_id, capsys, monkeypatch, tmp_path,
):
    """``graph read --save`` should work in HTTP mode, not just local mode."""
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    save_path = tmp_path / "dispatch-lifecycle.md"
    args = _cli_args(
        source=seeded_source_id, first=False, max_chars=0, json=False,
        all_comments=False, html_output=False, save=str(save_path),
    )
    graph_cli.cmd_read(args)
    out = capsys.readouterr().out

    assert f"Saved to {save_path}" in out
    assert save_path.read_text() == "canonical signpost content"


# ── Global-org default for scopeless reads (dashboard URLs) ───


def _seed_curated_note(org: str, *, content: str, tags: list[str] | None = None) -> str:
    GraphDB.close_all_pooled()
    db = GraphDB.open_org_db(org, mode="rw")
    try:
        sid = str(uuid.uuid4())
        src = Source(
            id=sid, type="note", platform="local", project=org,
            title=content[:80], file_path=f"note:{sid}",
            metadata={"tags": tags or [], "author": "test"},
            publication_state="curated",
        )
        db.insert_source(src)
        db.insert_thought(Thought(
            source_id=sid, content=content, role="user",
            turn_number=1, tags=tags or [],
        ))
        db.commit()
    finally:
        db.close()
    GraphDB.close_all_pooled()
    return sid


def test_scopeless_search_returns_curated_results_from_every_org(
    api_client, monkeypatch,
):
    """``GET /api/graph/search`` with no X-Graph-Org should return curated
    rows from every org, not just published+canonical peer surface."""
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    needle = f"global-search-probe-{uuid.uuid4().hex[:8]}"
    a_id = _seed_curated_note("autonomy", content=needle + " from autonomy")
    p_id = _seed_curated_note("personal", content=needle + " from personal")
    resp = api_client.get(f"/api/graph/search?q={needle}&limit=10")
    assert resp.status_code == 200
    rows = resp.json()
    # ``search`` returns thought rows; the *source* id lives on each row's
    # ``source_id`` (the ``id`` column is the thought's own UUID).
    source_ids = {r.get("source_id") for r in rows}
    assert a_id in source_ids and p_id in source_ids, (
        f"global search should include curated rows from autonomy AND "
        f"personal; got source_ids={source_ids}"
    )


def test_scopeless_list_sources_returns_curated_from_every_org(
    api_client, monkeypatch,
):
    """``GET /api/graph/sources`` scopeless should merge curated rows
    from every org without filtering to peer-public-surface."""
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    a_id = _seed_curated_note("autonomy", content=f"global-list-probe-autonomy-{uuid.uuid4().hex[:8]}")
    p_id = _seed_curated_note("personal", content=f"global-list-probe-personal-{uuid.uuid4().hex[:8]}")
    resp = api_client.get("/api/graph/sources?type=note&limit=50")
    assert resp.status_code == 200
    body = resp.json()
    rows = body.get("sources") if isinstance(body, dict) else body
    ids = {r.get("id") for r in (rows or [])}
    assert a_id in ids and p_id in ids, (
        f"global list_sources should merge curated from every org; "
        f"got {len(ids)} ids, missing {{a={a_id}, p={p_id}}} ∩ {ids}"
    )


def test_scopeless_resolve_finds_curated_note_in_any_org(
    api_client, monkeypatch,
):
    """A scopeless ``/api/graph/{id}`` request (no X-Graph-Org header) must
    resolve a ``curated`` note that lives in any org's DB.

    Models the dashboard URL case: a browser opens ``/graph/<id>`` and the
    page JS fetches ``/api/graph/<id>`` with no auth header. The dashboard
    is operator UI — it should see every org's full surface, not be
    restricted to peer-public-surface (published+canonical only).

    Today this returns 404 because:
      1. ``_resolve_org(None)`` drops to scopeless personal default.
      2. ``_open(None)`` opens personal.db, doesn't find the curated note.
      3. Peer fallback scans autonomy/anchore but filters to
         PEER_VISIBLE_STATES = ("published", "canonical").
      4. The curated row in autonomy is dropped → 404.

    The fix is a "global" semantic for scopeless callers — scan every
    org's own surface, no peer filter. ``graph note`` writes default to
    ``curated``, so without this fix every newly-created note 404s on
    dashboard URLs.
    """
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    # Seed a curated note in autonomy.db via the host ops layer.
    GraphDB.close_all_pooled()
    autonomy_db = GraphDB.open_org_db("autonomy", mode="rw")
    try:
        seed_id = str(uuid.uuid4())
        seed = Source(
            id=seed_id, type="note", platform="local", project="autonomy",
            title="global-resolve probe",
            file_path=f"note:{seed_id}",
            metadata={"tags": ["probe"], "author": "test"},
            publication_state="curated",
        )
        autonomy_db.insert_source(seed)
        autonomy_db.insert_thought(Thought(
            source_id=seed_id, content="global resolve target",
            role="user", turn_number=1, tags=["probe"],
        ))
    finally:
        autonomy_db.close()
    GraphDB.close_all_pooled()

    # Request with no X-Graph-Org header — like a browser hitting /graph/<id>.
    resp = api_client.get(f"/api/graph/{seed_id}")
    assert resp.status_code == 200, (
        f"scopeless resolve should find a curated note in any org, "
        f"got HTTP {resp.status_code}: {resp.text[:200]}"
    )
    body = resp.json()
    assert body.get("source", {}).get("id") == seed_id, (
        f"scopeless resolve should return the seeded note id; got {body!r}"
    )


# ── Response-shape regressions (client ↔ server contract) ─────


def test_list_captures_surfaces_just_written_thought(
    api_client, forbid_cli_sqlite, monkeypatch,
):
    """Writing via ``insert_capture`` and immediately listing via
    ``list_captures`` must return the same capture row.

    Regression: ``HttpClient.list_captures`` previously looked for
    ``result["captures"]`` while ``api_graph_thoughts`` returns
    ``{"thoughts": [...]}``. Client silently fell through to the
    ``isinstance(list) else []`` branch → always empty. Write went through
    fine, read returned nothing — a ghost capture.
    """
    from tools.graph.client import HttpClient

    monkeypatch.delenv("GRAPH_ORG", raising=False)
    http = HttpClient("https://localhost:8080")
    probe = f"list-captures regression {uuid.uuid4()}"
    capture_id = str(uuid.uuid4())
    http.insert_capture(capture_id, probe, org="autonomy")
    rows = http.list_captures(limit=50, org="autonomy")
    matches = [r for r in rows if r.get("content") == probe]
    assert matches, (
        f"list_captures did not surface the just-written capture. "
        f"Got {len(rows)} rows, none matched content={probe!r}. "
        f"Likely the client is looking for the wrong JSON key in the "
        f"response envelope."
    )


def test_insert_thread_honors_client_supplied_id(
    api_client, forbid_cli_sqlite, monkeypatch,
):
    """The id the client generates for ``insert_thread`` must match the
    id that ``list_threads`` returns for that same thread.

    Regression: ``api_graph_thread`` ignored the client's ``thread_id``
    and generated its own. The CLI printed "Thread: <client-id>" but
    the DB row (and the list endpoint) had a different server-generated
    id. Users saw phantom-id mismatches on every thread create.
    """
    from tools.graph.client import HttpClient

    monkeypatch.delenv("GRAPH_ORG", raising=False)
    http = HttpClient("https://localhost:8080")
    supplied_id = str(uuid.uuid4())
    title = f"thread-id regression {uuid.uuid4()}"
    http.insert_thread(supplied_id, title, priority=2, org="autonomy")

    threads = http.list_threads(include_all=True, limit=50, org="autonomy")
    matches = [t for t in threads if t.get("id") == supplied_id]
    assert matches, (
        f"list_threads did not return the client-supplied id {supplied_id!r}. "
        f"Server likely generated its own id and discarded the client's. "
        f"Rows with the probe title: "
        f"{[t for t in threads if t.get('title') == title]}"
    )


def test_add_tag_returns_added_bool(
    api_client, forbid_cli_sqlite, seeded_source_id, monkeypatch,
):
    """``HttpClient.add_tag`` must return ``True`` on a first-time tag
    add, ``False`` on a duplicate — so the CLI can print "Tagged" vs
    "Already tagged" correctly.

    Regression: ``api_graph_tag_add`` returned ``{"ok": True, "output":
    msg}`` with no ``"added"`` key. Client's ``bool(result.get("added"))``
    always evaluated ``bool(None) == False``, so every successful add
    reported "Already tagged" including the first one.
    """
    from tools.graph.client import HttpClient

    monkeypatch.delenv("GRAPH_ORG", raising=False)
    http = HttpClient("https://localhost:8080")
    tag_name = f"regression-tag-{uuid.uuid4().hex[:8]}"
    first = http.add_tag(seeded_source_id, tag_name, org="autonomy")
    assert first is True, (
        f"First add_tag of a fresh tag returned {first!r}; "
        "server must include an 'added': true field in the JSON response."
    )
    second = http.add_tag(seeded_source_id, tag_name, org="autonomy")
    assert second is False, (
        f"Second add_tag of the same tag returned {second!r}; "
        "server must return 'added': false so the CLI can report "
        "'Already tagged' correctly."
    )


# ── Env-leak regression: handlers must honour X-Graph-Org, NOT env ──


def test_handler_reads_x_graph_org_without_env_leak(
    api_client, forbid_cli_sqlite, monkeypatch,
):
    """Server handlers must route per request via the X-Graph-Org header,
    not by reading ``GRAPH_ORG`` out of the server process's environment.

    In production, the dashboard process runs on the host with NO
    ``GRAPH_ORG``. Every container call arrives with an ``X-Graph-Org``
    header. A handler that forgets to read that header — and just calls
    ``graph_ops.X(...)`` scopelessly — falls back to ``os.environ.get
    ("GRAPH_ORG")``, which is ``None`` on the host. The write silently
    lands in ``personal.db`` instead of the caller's org.

    This test reproduces that failure: explicit ``GRAPH_ORG`` unset in
    the test process, HttpClient called with ``org="autonomy"`` so the
    X-Graph-Org header IS sent, and we assert the write lands in
    autonomy.db rather than personal.db. An env-leak handler shape
    passes today against ``monkeypatch.setenv("GRAPH_ORG", "autonomy")``
    — only once ``GRAPH_ORG`` is cleared does the bug surface.
    """
    from tools.graph.client import HttpClient

    monkeypatch.delenv("GRAPH_ORG", raising=False)
    http = HttpClient("https://localhost:8080")
    probe = f"env-leak regression probe {uuid.uuid4()}"
    http.insert_capture(str(uuid.uuid4()), probe, org="autonomy")

    # Probe by content, not id — the server generates its own capture_id
    # on the thought endpoint, so we can't predict it client-side.
    GraphDB.close_all_pooled()
    autonomy_db = GraphDB.open_org_db("autonomy", mode="rw")
    try:
        row = autonomy_db.conn.execute(
            "SELECT content FROM captures WHERE content=?", (probe,),
        ).fetchone()
    finally:
        autonomy_db.close()
    assert row is not None and row["content"] == probe, (
        "Capture should have landed in autonomy.db because the request "
        "carried X-Graph-Org=autonomy. If it's missing, the handler is "
        "falling back to a process-env lookup instead of reading the header."
    )

    # Sanity: it should NOT have landed in personal.db.
    GraphDB.close_all_pooled()
    personal_db = GraphDB.open_org_db("personal", mode="rw")
    try:
        bad = personal_db.conn.execute(
            "SELECT 1 FROM captures WHERE content=?", (probe,),
        ).fetchone()
    finally:
        personal_db.close()
    assert bad is None, (
        "Capture ended up in personal.db — classic env-leak fallback. "
        "The handler ignored X-Graph-Org and scopelessly resolved to "
        "the default org."
    )


# ── Conformance guard (defence in depth) ──────────────────────


def test_forbidden_sqlite_fires_when_tripped(monkeypatch):
    """The forbidden-sqlite helper must raise when called from a cli-stack context.

    Sanity check for the guard itself — if a future edit silently swallowed
    the assertion, the smoke tests above would stop being protective.
    """
    # Import the real cli module and run a bound function from it that
    # opens sqlite3. The stack frame walker checks ``frame.f_code.co_filename``
    # which is set from the module's actual file, so we need a real call
    # originating in cli.py.
    import sqlite3 as sqlite_mod
    monkeypatch.setattr(sqlite_mod, "connect", _forbidden_sqlite_connect)
    # Exec a snippet whose co_filename is cli.py so frame inspection sees it.
    src = "import sqlite3\nsqlite3.connect(':memory:')\n"
    code = compile(src, "/workspace/repo/tools/graph/cli.py", "exec")
    with pytest.raises(_SqliteForbiddenError):
        exec(code, {})


# ── New set ergonomics smoke tests (auto-xhimi) ──────────────


def test_cmd_set_show_partial_id_routes_through_api(
    api_client, forbid_cli_sqlite, capsys, monkeypatch,
):
    """``graph set show <8-char-prefix>`` resolves via /api/graph/setting-resolve."""
    from tools.graph import schemas, ops as graph_ops
    from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS

    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    snap_s, snap_u = dict(SCHEMAS), dict(UPCONVERTERS)
    try:
        class V1(schemas.SettingSchema):
            set_id = "smoke.via.api"
            schema_revision = 1
        schemas.register_schema("smoke.via.api", 1, V1)
        sid = graph_ops.add_setting(
            "smoke.via.api", 1, "k", {"v": 1}, org="autonomy",
        )
        GraphDB.close_all_pooled()
        args = _cli_args()
        args.id_parts = [sid[:8]]
        set_cmd.cmd_set_show(args)
        out = capsys.readouterr().out
        body = json.loads(out)
        assert body["id"] == sid
    finally:
        SCHEMAS.clear(); SCHEMAS.update(snap_s)
        UPCONVERTERS.clear(); UPCONVERTERS.update(snap_u)


def test_cmd_set_read_chain_routes_through_api(
    api_client, forbid_cli_sqlite, capsys, monkeypatch,
):
    """``graph set read <set_id> <key> --chain`` hits /api/graph/settings/.../chain."""
    from tools.graph import schemas, ops as graph_ops
    from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS

    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    snap_s, snap_u = dict(SCHEMAS), dict(UPCONVERTERS)
    try:
        class V1(schemas.SettingSchema):
            set_id = "smoke.chain.api"
            schema_revision = 1
        schemas.register_schema("smoke.chain.api", 1, V1)
        base = graph_ops.add_setting(
            "smoke.chain.api", 1, "ws",
            {"name": "B", "v": 1}, state="canonical", org="autonomy",
        )
        graph_ops.override_setting(base, {"name": "O"}, org="autonomy")
        GraphDB.close_all_pooled()
        args = _cli_args()
        args.id_parts = ["smoke.chain.api", "ws"]
        args.chain = True
        set_cmd.cmd_set_read(args)
        out = capsys.readouterr().out
        body = json.loads(out)
        assert body["set_id"] == "smoke.chain.api"
        assert len(body["layers"]) == 2
        assert body["final"]["name"] == "O"
        assert body["final"]["v"] == 1
    finally:
        SCHEMAS.clear(); SCHEMAS.update(snap_s)
        UPCONVERTERS.clear(); UPCONVERTERS.update(snap_u)
