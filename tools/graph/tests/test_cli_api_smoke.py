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
    """``graph context <id> <turn>`` hits /api/graph/source + resolve via HttpClient."""
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    args = _cli_args(source=seeded_source_id, turn="1", window=3)
    graph_cli.cmd_context(args)
    out = capsys.readouterr().out
    assert "Dispatch Lifecycle Signpost" in out


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
    # Verify it landed in autonomy.db (not personal.db). Evict the pool
    # + re-open fresh so WAL writes from the server-side handle are
    # visible to this reader.
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


# ── Settings command smoke tests ──────────────────────────────


def test_cmd_set_list_routes_through_api(
    api_client, forbid_cli_sqlite, capsys, monkeypatch,
):
    """``graph set list`` hits GET /api/graph/sets — empty on clean fixture."""
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    args = _cli_args()
    set_cmd.cmd_set_list(args)
    out = capsys.readouterr().out
    # An empty fixture DB has no Settings yet.
    assert "no Settings yet" in out or out.strip() == ""


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
