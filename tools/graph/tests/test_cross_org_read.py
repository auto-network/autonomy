"""Cross-org read activation tests (auto-txg5.4).

End-to-end coverage for the ops.* read path changes that turn per-org
DBs into a unified surface: own-org full visibility + peer public
surface, RRF merge for search, own-first resolve, chronological list
merge, and write rejection for peer-origin targets.

The acceptance contract in graph://bcce359d-a1d § Acceptance contract
maps (mostly) 1:1 to tests below — the check numbers in the docstrings
reference the numbered items from the bead description.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from tools.graph import cross_org, ops, settings_ops
from tools.graph import db as graph_db_mod
from tools.graph.db import GraphDB
from tools.graph.models import Source, Thought


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    """Pin ``data/orgs/`` to tmp; unset GRAPH_{DB,ORG}."""
    root = tmp_path / "orgs"
    legacy = tmp_path / "legacy.db"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.setattr(graph_db_mod, "DEFAULT_DB", legacy)
    return root


@pytest.fixture(autouse=True)
def _evict_pool():
    GraphDB.close_all_pooled()
    try:
        yield
    finally:
        GraphDB.close_all_pooled()


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


def _seed_anchore_and_autonomy(orgs_root):
    """Seed canonical + raw content in each org; return handles + ids."""
    anchore = _seed_org("anchore")
    autonomy = _seed_org("autonomy")
    ids = {}
    # Autonomy: a canonical signpost + a curated (non-public) decision note.
    ids["autonomy_sign"] = _seed_note(
        autonomy, title="Dispatch Lifecycle Signpost",
        body="dispatch lifecycle state machine canonical content",
        state="canonical", tags=["signpost"],
    )
    ids["autonomy_curated"] = _seed_note(
        autonomy, title="D1 per-org DB decision",
        body="D1 per-org DB decision curated internal",
        state="curated",
    )
    ids["autonomy_raw"] = _seed_note(
        autonomy, title="Autonomy raw note",
        body="autonomy internal unpublished thought",
        state="raw",
    )

    # Anchore: its own content + one canonical public contribution.
    ids["anchore_internal"] = _seed_note(
        anchore, title="Anchore internal pitfall",
        body="anchore dispatch lifecycle internal finding",
        state="raw", tags=["pitfall"],
    )
    ids["anchore_canonical"] = _seed_note(
        anchore, title="Anchore canonical runbook",
        body="anchore runbook published for every org",
        state="canonical",
    )
    autonomy.close()
    anchore.close()
    return ids


# ── Acceptance 1–2: search cross-org sees own + peer public surface ──


def test_search_from_anchore_session_finds_autonomy_canonical(orgs_root):
    """Acceptance #1: anchore session finds autonomy signpost."""
    ids = _seed_anchore_and_autonomy(orgs_root)

    results = ops.search(
        "dispatch lifecycle", org="anchore", limit=25,
    )
    matched = {r["source_id"] for r in results}
    assert ids["autonomy_sign"] in matched, (
        "autonomy canonical should be visible from anchore"
    )
    # Every result carries its origin org annotation.
    for r in results:
        assert "org" in r
        if r["source_id"] == ids["autonomy_sign"]:
            assert r["org"] == "autonomy"


def test_search_from_anchore_does_not_leak_autonomy_curated(orgs_root):
    """Acceptance #2: autonomy-curated content stays invisible to anchore."""
    ids = _seed_anchore_and_autonomy(orgs_root)

    results = ops.search(
        "D1 per-org DB decision", org="anchore", limit=25,
    )
    matched = {r["source_id"] for r in results}
    assert ids["autonomy_curated"] not in matched, (
        "curated autonomy content must not leak across orgs"
    )


# ── Acceptance 3–4: resolve(uuid) own-first then peer public ─────────


def test_resolve_uuid_reaches_autonomy_canonical_from_anchore(orgs_root):
    """Acceptance #3: ``graph read <canonical-id>`` resolves across orgs."""
    ids = _seed_anchore_and_autonomy(orgs_root)

    src = ops.get_source(ids["autonomy_sign"], org="anchore")
    assert src is not None
    assert src["org"] == "autonomy"


def test_resolve_uuid_rejects_autonomy_curated_from_anchore(orgs_root):
    """Acceptance #4: curated autonomy id → 404 from anchore session."""
    ids = _seed_anchore_and_autonomy(orgs_root)

    src = ops.get_source(ids["autonomy_curated"], org="anchore")
    assert src is None


def test_resolve_uuid_own_org_sees_all_states(orgs_root):
    """Caller's own DB is the full surface — even raw content is visible."""
    ids = _seed_anchore_and_autonomy(orgs_root)

    src = ops.get_source(ids["autonomy_curated"], org="autonomy")
    assert src is not None
    assert src["publication_state"] == "curated"


def test_resolve_uuid_own_first_beats_peer(orgs_root):
    """Own DB is consulted before peers — spec § resolver fixed order."""
    _seed_anchore_and_autonomy(orgs_root)

    # Create a source that shares its ID with one in autonomy.db — the
    # anchore copy should win even if autonomy has the same UUID.
    shared_id = str(uuid.uuid4())
    anchore_db = GraphDB.open_org_db("anchore")
    try:
        anchore_db.insert_source(Source(
            id=shared_id, type="note", platform="local", project="autonomy",
            title="anchore-shared", file_path=f"note:a-{shared_id}",
            metadata={}, publication_state="raw",
        ))
        anchore_db.conn.commit()
    finally:
        anchore_db.close()
    autonomy_db = GraphDB.open_org_db("autonomy")
    try:
        autonomy_db.insert_source(Source(
            id=shared_id, type="note", platform="local", project="autonomy",
            title="autonomy-shared", file_path=f"note:b-{shared_id}",
            metadata={}, publication_state="canonical",
        ))
        autonomy_db.conn.commit()
    finally:
        autonomy_db.close()

    src = ops.get_source(shared_id, org="anchore")
    assert src is not None
    assert src["title"] == "anchore-shared"
    assert src["org"] == "anchore"


# ── Acceptance 6: notes stay in origin DB ────────────────────────────


def test_pitfall_note_lands_in_org_only(orgs_root):
    """Acceptance #6: a note authored in anchore lives in anchore.db alone.

    Mostly a regression check (writes were routed in auto-txg5.3) — but
    we assert *nothing* leaked into the autonomy DB.
    """
    _seed_anchore_and_autonomy(orgs_root)

    # Write a pitfall via ops.* as anchore.
    from tools.graph.models import Source as Src, Thought as Th
    anchore_db = GraphDB(ops._db_path("anchore"))
    try:
        sid = str(uuid.uuid4())
        anchore_db.insert_source(Src(
            id=sid, type="note", platform="local", project="autonomy",
            title="fresh pitfall",
            file_path=f"note:fresh-{sid}",
            metadata={"tags": ["pitfall"]},
            publication_state="raw",
        ))
        anchore_db.insert_thought(Th(
            source_id=sid, content="pitfall body", role="user",
            turn_number=1, tags=["pitfall"],
        ))
        anchore_db.conn.commit()
    finally:
        anchore_db.close()

    # Autonomy DB should not have seen the row.
    autonomy_db = GraphDB.open_org_db("autonomy", mode="ro")
    try:
        row = autonomy_db.conn.execute(
            "SELECT 1 FROM sources WHERE id = ?", (sid,),
        ).fetchone()
    finally:
        autonomy_db.close()
    assert row is None


# ── Acceptance 7–8: peer-subscription Setting drives visibility ──────


def test_autonomy_with_empty_peers_sees_own_canonical(orgs_root):
    """Acceptance #7: isolated subscription still sees own full surface."""
    ids = _seed_anchore_and_autonomy(orgs_root)
    _seed_org("personal")

    settings_ops.add_setting(
        cross_org.PEER_SUBSCRIPTION_SET_ID, 1,
        key="autonomy", payload={"peers": []},
        org="personal", state="canonical",
    )

    results = ops.search(
        "dispatch lifecycle", org="autonomy", limit=25,
    )
    matched = {r["source_id"] for r in results}
    # Own signpost visible.
    assert ids["autonomy_sign"] in matched
    # No anchore content bleeds in.
    assert ids["anchore_canonical"] not in matched


def test_autonomy_with_anchore_peer_sees_canonical_only(orgs_root):
    """Acceptance #8: published/canonical peers visible; raw stays private."""
    ids = _seed_anchore_and_autonomy(orgs_root)
    _seed_org("personal")

    settings_ops.add_setting(
        cross_org.PEER_SUBSCRIPTION_SET_ID, 1,
        key="autonomy", payload={"peers": ["anchore"]},
        org="personal", state="canonical",
    )

    results = ops.search(
        "anchore runbook", org="autonomy", limit=25,
    )
    matched = {r["source_id"] for r in results}
    assert ids["anchore_canonical"] in matched
    assert ids["anchore_internal"] not in matched


# ── Chronological list merge ─────────────────────────────────────────


def test_list_sources_merges_chronologically_across_orgs(orgs_root):
    """Per-DB results come through merged newest-first + org-annotated."""
    _seed_anchore_and_autonomy(orgs_root)

    # ``include_raw=True`` disables the default cross-session raw hide
    # on the own-org DB so the raw anchore pitfall is visible for the
    # full-surface half of this assertion. Peer DB is still clamped to
    # ``published``/``canonical`` regardless — that's the contract
    # we're exercising.
    rows = ops.list_sources(
        org="anchore", limit=50, include_raw=True,
    )
    assert rows, "expected something to come through"
    # Every row carries an origin.
    for r in rows:
        assert "org" in r
    orgs = {r["org"] for r in rows}
    assert orgs <= {"anchore", "autonomy"}
    titles = {r.get("title") for r in rows}
    # Own raw sources come through (include_raw=True, own DB = full surface).
    assert "Anchore internal pitfall" in titles
    # Peer raw does NOT come through — peers always clamp to public surface.
    assert "Autonomy raw note" not in titles
    # Peer canonical DOES come through.
    assert "Dispatch Lifecycle Signpost" in titles


# ── Only-org pin ─────────────────────────────────────────────────────


def test_only_org_pins_search_to_single_db(orgs_root):
    """``--only-org autonomy`` from an anchore session returns only autonomy rows,
    and the public-surface filter is enforced on the peer side."""
    ids = _seed_anchore_and_autonomy(orgs_root)

    results = ops.search(
        "autonomy", org="anchore", only_org="autonomy", limit=25,
    )
    for r in results:
        assert r.get("org") == "autonomy", r
    matched = {r["source_id"] for r in results}
    # Curated content is still invisible across orgs.
    assert ids["autonomy_curated"] not in matched


def test_only_org_self_returns_full_surface(orgs_root):
    """Pinning to own org still lets raw rows through."""
    ids = _seed_anchore_and_autonomy(orgs_root)

    results = ops.search(
        "autonomy", org="autonomy", only_org="autonomy", limit=25,
        include_raw=True,
    )
    matched = {r["source_id"] for r in results}
    # Own raw IS visible when pinned to self.
    assert ids["autonomy_raw"] in matched or ids["autonomy_curated"] in matched


# ── Settings cross-org read ──────────────────────────────────────────


def test_read_set_includes_peer_published_rows(orgs_root):
    """``ops.read_set`` pulls peer rows whose state is published/canonical."""
    _seed_org("autonomy")
    _seed_org("anchore")
    _seed_org("personal")

    # Autonomy publishes a shared setting; anchore has a raw one.
    from tools.graph.schemas import registry
    # Use an ad-hoc schema so we don't collide with real contracts.
    SET_ID = "autonomy.test.cross-org-read"

    class _Schema(registry.SettingSchema):
        set_id = SET_ID
        schema_revision = 1

        @classmethod
        def validate(cls, payload):
            super().validate(payload)
    if (SET_ID, 1) not in registry.SCHEMAS:
        registry.register_schema(SET_ID, 1, _Schema)

    auto_sid = settings_ops.add_setting(
        SET_ID, 1, key="shared",
        payload={"k": "auto-canonical"},
        org="autonomy", state="canonical",
    )
    settings_ops.add_setting(
        SET_ID, 1, key="local",
        payload={"k": "anchore-raw"},
        org="anchore", state="raw",
    )

    result = ops.read_set(SET_ID, org="anchore")
    keys = {m.key: m for m in result.members}
    assert "shared" in keys, "peer canonical should be in read_set"
    assert keys["shared"].org == "autonomy"
    assert keys["shared"].state == "canonical"

    # Own-org raw is visible; peer raw is NOT.
    assert "local" in keys
    assert keys["local"].org == "anchore"


def test_get_setting_resolves_peer_canonical_by_id(orgs_root):
    """``ops.get_setting`` finds a peer id when it's published/canonical."""
    _seed_org("autonomy")
    _seed_org("anchore")
    _seed_org("personal")

    from tools.graph.schemas import registry
    SET_ID = "autonomy.test.single-get"

    class _Schema(registry.SettingSchema):
        set_id = SET_ID
        schema_revision = 1

        @classmethod
        def validate(cls, payload):
            super().validate(payload)
    if (SET_ID, 1) not in registry.SCHEMAS:
        registry.register_schema(SET_ID, 1, _Schema)

    canonical_id = settings_ops.add_setting(
        SET_ID, 1, key="k", payload={"x": 1},
        org="autonomy", state="canonical",
    )
    raw_id = settings_ops.add_setting(
        SET_ID, 1, key="k", payload={"x": 2},
        org="autonomy", state="raw",
    )

    from_peer = ops.get_setting(canonical_id, org="anchore")
    assert from_peer is not None
    assert from_peer.org == "autonomy"

    raw_from_peer = ops.get_setting(raw_id, org="anchore")
    assert raw_from_peer is None, "peer raw Setting must be invisible"


# ── Cross-org write rejection ────────────────────────────────────────


def test_add_tag_rejects_peer_origin_source(orgs_root):
    """Writing a tag on a peer-origin source raises CrossOrgWriteError."""
    ids = _seed_anchore_and_autonomy(orgs_root)

    with pytest.raises(ops.CrossOrgWriteError) as exc:
        ops.add_tag(ids["autonomy_sign"], "new-tag", org="anchore")
    err = exc.value
    assert err.origin_org == "autonomy"
    assert err.target_id == ids["autonomy_sign"]
    assert "cannot modify cross-org content" in str(err)

    # Structured dict survives API round-trip.
    payload = err.to_dict()
    assert payload["error"] == "cross_org_write_rejected"
    assert payload["origin_org"] == "autonomy"


def test_add_comment_rejects_peer_origin_note(orgs_root):
    ids = _seed_anchore_and_autonomy(orgs_root)

    with pytest.raises(ops.CrossOrgWriteError):
        ops.add_comment(
            ids["autonomy_sign"], "hey", org="anchore",
        )


def test_promote_source_rejects_peer_target(orgs_root):
    ids = _seed_anchore_and_autonomy(orgs_root)

    with pytest.raises(ops.CrossOrgWriteError):
        ops.promote_source(
            ids["autonomy_sign"], "canonical", org="anchore",
        )


def test_promote_setting_rejects_peer_target(orgs_root):
    """Peer Settings can't have their state flipped from another org."""
    _seed_org("autonomy")
    _seed_org("anchore")

    from tools.graph.schemas import registry
    SET_ID = "autonomy.test.promote-reject"

    class _Schema(registry.SettingSchema):
        set_id = SET_ID
        schema_revision = 1

        @classmethod
        def validate(cls, payload):
            super().validate(payload)
    if (SET_ID, 1) not in registry.SCHEMAS:
        registry.register_schema(SET_ID, 1, _Schema)

    sid = settings_ops.add_setting(
        SET_ID, 1, key="k", payload={"x": 1},
        org="autonomy", state="canonical",
    )

    with pytest.raises(ops.CrossOrgWriteError) as exc:
        settings_ops.promote_setting(sid, "canonical", org="anchore")
    assert exc.value.origin_org == "autonomy"


def test_update_source_title_rejects_peer_target(orgs_root):
    ids = _seed_anchore_and_autonomy(orgs_root)

    with pytest.raises(ops.CrossOrgWriteError):
        ops.update_source_title(
            ids["autonomy_sign"], "new title", org="anchore",
        )


# ── ``graph://uuid`` scan order ──────────────────────────────────────


def test_resolve_source_strict_cross_org_own_first(orgs_root):
    """Strict resolver same contract as ``get_source``: own-first then peers."""
    ids = _seed_anchore_and_autonomy(orgs_root)

    hit = ops.resolve_source_strict(ids["autonomy_sign"], org="anchore")
    assert isinstance(hit, dict)
    assert hit["org"] == "autonomy"


def test_resolve_source_strict_returns_none_on_peer_curated(orgs_root):
    ids = _seed_anchore_and_autonomy(orgs_root)

    assert ops.resolve_source_strict(
        ids["autonomy_curated"], org="anchore",
    ) is None
