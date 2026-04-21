"""CLI read-path cross-org regression tests (auto-bs3m3).

After per-org DB migration + txg5.4 ops.* cross-org reads, the CLI
read-path consumers (``cmd_read``, ``cmd_context``, ``cmd_attachment``,
``cmd_attachments``, primer provenance) were still opening only the
caller's own DB — single-ID resolution broke across host CLI and every
dashboard shell-out. These tests pin the rewire: single-ID reads MUST
resolve via ``ops.resolve_source_strict`` / ``ops.get_source`` and
subsequent reads MUST target the source's home-org DB.

Test matrix mirrors the spec's acceptance criteria:
- Own-org raw / curated / canonical all visible from own context.
- Peer canonical / published visible from any peer.
- Peer raw / curated NOT visible cross-org.
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

import pytest

from tools.graph import cli as graph_cli
from tools.graph import cross_org, ops
from tools.graph import db as graph_db_mod
from tools.graph import primer as graph_primer
from tools.graph.db import GraphDB
from tools.graph.models import Attachment, Source, Thought


# ── Fixtures ───────────────────────────────────────────────────


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    """Pin ``data/orgs/`` to tmp; unset ``GRAPH_{DB,ORG}``.

    Mirrors the harness in ``test_cross_org_read.py`` so we're exercising
    the same peer-resolution code path (no ``GRAPH_DB`` short-circuit).
    """
    root = tmp_path / "orgs"
    legacy = tmp_path / "legacy.db"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    # Forcing LocalClient: these tests drive the host-side cmd_* paths
    # directly; with GRAPH_API set in the container env, the newly-wired
    # ``get_client()`` dispatch would route through HTTP and miss the
    # seeded test DBs.
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.setattr(graph_db_mod, "DEFAULT_DB", legacy)
    return root


@pytest.fixture(autouse=True)
def _evict_pool():
    GraphDB.close_all_pooled()
    try:
        yield
    finally:
        GraphDB.close_all_pooled()


@pytest.fixture(autouse=True)
def _no_auto_ingest(monkeypatch):
    """Silence :func:`cmd_context`'s auto-ingest — irrelevant and slow for
    these unit tests.
    """
    monkeypatch.setattr(graph_cli, "_auto_ingest", lambda db: None)


# ── Seeders ────────────────────────────────────────────────────


def _seed_org(slug: str) -> GraphDB:
    return GraphDB.create_org_db(slug)


def _seed_note(
    db: GraphDB,
    *,
    title: str,
    body: str,
    state: str = "raw",
    tags: list[str] | None = None,
) -> str:
    sid = str(uuid.uuid4())
    src = Source(
        id=sid,
        type="note",
        platform="local",
        project="autonomy",
        title=title,
        file_path=f"note:{sid}",
        metadata={"tags": tags or [], "author": "test"},
        publication_state=state,
    )
    db.insert_source(src)
    db.insert_thought(Thought(
        source_id=sid, content=body, role="user", turn_number=1,
        tags=tags or [],
    ))
    db.conn.commit()
    return sid


def _seed_attachment(db: GraphDB, source_id: str) -> str:
    att = Attachment(
        id=str(uuid.uuid4()),
        hash="sha256:deadbeef",
        filename="shot.png",
        mime_type="image/png",
        size_bytes=42,
        file_path=f"attachments/{source_id}.png",
        source_id=source_id,
        turn_number=1,
        metadata={},
        alt_text="test attachment",
    )
    db.insert_attachment(att)
    return att.id


def _seed_anchore_and_autonomy(orgs_root):
    """Seed canonical + raw content in both orgs; return ID map."""
    anchore = _seed_org("anchore")
    autonomy = _seed_org("autonomy")
    ids: dict[str, str] = {}

    ids["autonomy_sign"] = _seed_note(
        autonomy, title="Dispatch Lifecycle Signpost",
        body="dispatch lifecycle state machine canonical content",
        state="canonical", tags=["signpost"],
    )
    ids["autonomy_raw"] = _seed_note(
        autonomy, title="Autonomy raw note",
        body="autonomy internal raw thought",
        state="raw",
    )
    ids["autonomy_sign_att"] = _seed_attachment(
        autonomy, ids["autonomy_sign"],
    )
    ids["autonomy_raw_att"] = _seed_attachment(
        autonomy, ids["autonomy_raw"],
    )
    autonomy.conn.commit()

    ids["anchore_raw"] = _seed_note(
        anchore, title="Anchore internal pitfall",
        body="anchore dispatch lifecycle internal finding",
        state="raw", tags=["pitfall"],
    )
    anchore.conn.commit()

    autonomy.close()
    anchore.close()
    return ids


def _make_args(**kwargs) -> argparse.Namespace:
    """Build an argparse-shaped namespace with defaults cmd_* expects."""
    from tools.graph.db import resolve_caller_db_path
    defaults = {
        "db": resolve_caller_db_path(None),
        "source": None,
        "first": False,
        "max_chars": 0,
        "json": False,
        "all_comments": False,
        "html_output": False,
        "save": None,
        "window": 3,
        "turn": "last",
        "limit": 50,
        "id": None,
        "source_id": None,
        "state": None,
        "include": None,
        "only_org": None,
        "org_mode": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ── cmd_read: single-ID resolve cross-org ────────────────────────


def test_cmd_read_own_org_canonical_reads_body(orgs_root, capsys, monkeypatch):
    """Own-org canonical source reads through the same resolver — no regression."""
    ids = _seed_anchore_and_autonomy(orgs_root)
    monkeypatch.setenv("GRAPH_ORG", "autonomy")

    args = _make_args(source=ids["autonomy_sign"])
    graph_cli.cmd_read(args)

    out = capsys.readouterr().out
    assert "Dispatch Lifecycle Signpost" in out
    assert "canonical content" in out


def test_cmd_read_cross_org_canonical_resolves_and_reads_body(orgs_root, capsys, monkeypatch):
    """Acceptance: anchore session reads the autonomy canonical signpost."""
    ids = _seed_anchore_and_autonomy(orgs_root)
    monkeypatch.setenv("GRAPH_ORG", "anchore")

    args = _make_args(source=ids["autonomy_sign"])
    graph_cli.cmd_read(args)

    out = capsys.readouterr().out
    assert "Dispatch Lifecycle Signpost" in out
    # The thought body must be printed — this is the regression we're pinning.
    assert "canonical content" in out


def test_cmd_read_cross_org_raw_rejected(orgs_root, capsys, monkeypatch):
    """Raw content stays invisible across orgs — `not found` on a peer's raw UUID."""
    ids = _seed_anchore_and_autonomy(orgs_root)
    monkeypatch.setenv("GRAPH_ORG", "anchore")

    args = _make_args(source=ids["autonomy_raw"])
    graph_cli.cmd_read(args)

    out = capsys.readouterr().out
    assert "No source found" in out
    assert "autonomy internal raw" not in out


def test_cmd_read_scopeless_caller_reaches_peer_canonical(orgs_root, capsys, monkeypatch):
    """Host CLI with no ``GRAPH_ORG`` still finds peer canonical content.

    The scopeless default routes writes to personal.db, but reads must
    cross-org-scan so every signpost remains addressable from the host.
    """
    # Personal DB must exist for the default-resolver to resolve peers.
    _seed_org("personal").close()
    ids = _seed_anchore_and_autonomy(orgs_root)

    args = _make_args(source=ids["autonomy_sign"])
    graph_cli.cmd_read(args)

    out = capsys.readouterr().out
    assert "canonical content" in out


def test_cmd_read_json_payload_carries_org_field(orgs_root, capsys, monkeypatch):
    """`--json` output includes the source's origin ``org`` so dashboards can
    display it without a second lookup."""
    ids = _seed_anchore_and_autonomy(orgs_root)
    monkeypatch.setenv("GRAPH_ORG", "anchore")

    args = _make_args(source=ids["autonomy_sign"], json=True)
    graph_cli.cmd_read(args)

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["source"]["org"] == "autonomy"
    assert payload["source"]["title"] == "Dispatch Lifecycle Signpost"
    assert payload["entries"]
    assert "canonical content" in payload["entries"][0]["content"]


# ── cmd_context ───────────────────────────────────────────────


def test_cmd_context_cross_org_canonical_prints_turn(orgs_root, capsys, monkeypatch):
    """`graph context` resolves peer canonical source and prints turns."""
    ids = _seed_anchore_and_autonomy(orgs_root)
    monkeypatch.setenv("GRAPH_ORG", "anchore")

    args = _make_args(source=ids["autonomy_sign"], turn="1", window=3)
    graph_cli.cmd_context(args)

    out = capsys.readouterr().out
    assert "Dispatch Lifecycle Signpost" in out
    assert "canonical content" in out


def test_cmd_context_cross_org_raw_not_found(orgs_root, capsys, monkeypatch):
    """`graph context` on a peer's raw UUID returns not-found, not a stack trace."""
    ids = _seed_anchore_and_autonomy(orgs_root)
    monkeypatch.setenv("GRAPH_ORG", "anchore")

    args = _make_args(source=ids["autonomy_raw"], turn="1", window=3)
    graph_cli.cmd_context(args)

    out = capsys.readouterr().out
    assert "Source not found" in out


# ── cmd_attachment / cmd_attachments ────────────────────────────


def test_cmd_attachment_cross_org_canonical(orgs_root, capsys, monkeypatch):
    """Peer attachment on a canonical source is visible cross-org."""
    ids = _seed_anchore_and_autonomy(orgs_root)
    monkeypatch.setenv("GRAPH_ORG", "anchore")

    args = _make_args(id=ids["autonomy_sign_att"])
    graph_cli.cmd_attachment(args)

    out = capsys.readouterr().out
    assert "shot.png" in out
    assert "org:" in out
    assert "autonomy" in out


def test_cmd_attachment_cross_org_raw_parent_rejected(orgs_root, capsys, monkeypatch):
    """Peer attachment whose parent source is raw must not surface."""
    ids = _seed_anchore_and_autonomy(orgs_root)
    monkeypatch.setenv("GRAPH_ORG", "anchore")

    args = _make_args(id=ids["autonomy_raw_att"])
    with pytest.raises(SystemExit) as exc:
        graph_cli.cmd_attachment(args)
    assert exc.value.code == 1


def test_cmd_attachments_by_peer_source_id(orgs_root, capsys, monkeypatch):
    """Listing attachments for a peer source routes to that peer's DB."""
    ids = _seed_anchore_and_autonomy(orgs_root)
    monkeypatch.setenv("GRAPH_ORG", "anchore")

    args = _make_args(source_id=ids["autonomy_sign"])
    graph_cli.cmd_attachments(args)

    out = capsys.readouterr().out
    assert "shot.png" in out


# ── ops.get_attachment ─────────────────────────────────────────


def test_ops_get_attachment_cross_org_canonical(orgs_root):
    """Direct ops.* check — peer attachment visible when parent is canonical."""
    ids = _seed_anchore_and_autonomy(orgs_root)

    att = ops.get_attachment(
        ids["autonomy_sign_att"], org="anchore",
    )
    assert att is not None
    assert att["org"] == "autonomy"
    assert att["filename"] == "shot.png"


def test_ops_get_attachment_peer_raw_parent_invisible(orgs_root):
    """Peer attachment is invisible when its parent source is raw."""
    ids = _seed_anchore_and_autonomy(orgs_root)

    att = ops.get_attachment(
        ids["autonomy_raw_att"], org="anchore",
    )
    assert att is None


# ── primer provenance cross-org ─────────────────────────────────


def test_primer_provenance_pulls_peer_canonical_turns(orgs_root, monkeypatch):
    """Primer assembly must fetch provenance content from the peer DB that
    actually hosts the referenced source.

    This is the load-bearing fix for ``graph primer <bead>`` when a bead's
    provenance edge references a peer-org canonical signpost.
    """
    ids = _seed_anchore_and_autonomy(orgs_root)
    monkeypatch.setenv("GRAPH_ORG", "anchore")

    # Caller's own DB gets a bead with an edge to autonomy's signpost.
    anchore_db = GraphDB.open_org_db("anchore")
    try:
        bead_id = "auto-test-bs3m3"
        anchore_db.conn.execute(
            """INSERT INTO edges(
                id, source_id, source_type, target_id, target_type,
                relation, weight, metadata, created_at
            ) VALUES (?, ?, 'bead', ?, 'source', 'conceived_at', 1.0, ?,
                      '2026-04-21T00:00:00Z')""",
            (
                str(uuid.uuid4()),
                bead_id,
                ids["autonomy_sign"],
                json.dumps({"turns": {"from": 1, "to": 1}}),
            ),
        )
        anchore_db.conn.commit()

        # _get_bead hits the dashboard DAO — stub it so we don't depend on
        # dashboard state.
        monkeypatch.setattr(graph_primer, "_get_bead", lambda _id: {
            "title": "test bead", "priority": 1, "status": "open",
            "description": "", "acceptance_criteria": "", "design": "",
            "comments": [], "notes": "", "labels": [],
        })
        # Skip the bd find-duplicates call.
        monkeypatch.setattr(graph_primer, "_run_bd", lambda *a, **kw: "")

        data = graph_primer.collect_primer_data(
            bead_id, db=anchore_db,
            include_pitfalls=False, include_related_beads=False,
        )
    finally:
        anchore_db.close()

    prov = data["provenance"]
    assert prov, "expected provenance entries"
    entry = next((p for p in prov if p["source_id"] == ids["autonomy_sign"]), None)
    assert entry is not None, "autonomy provenance row missing"
    assert entry["turns"], (
        "provenance turns must contain peer content — this is the regression"
    )
    assert "canonical content" in entry["turns"][0]["content"]
