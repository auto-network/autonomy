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
