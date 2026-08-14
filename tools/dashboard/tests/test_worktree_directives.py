"""Tests for Worktrees directive schemas.

Covers:

* inbound ``dashboard.session.crosstalk.worktree.rebase#1`` transport
  and server-side prompt rendering
* outbound ``dashboard.session.worktree.rebase_status#1`` validation
  and registry wiring
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tools.graph import ops, schemas, settings_ops
from tools.graph.schemas.registry import (
    SchemaValidationError,
    get_schema,
    validate_payload,
)
from tools.dashboard.event_bus import EventBus
from tools.dashboard.settings_mediator import (
    Row,
    Services,
    start_action_loop,
    stop_action_loop,
)
from tools.dashboard.settings_mediator.loop import _HANDLERS
from tools.dashboard.worktree_directives import (
    WORKTREE_REBASE_DIRECTIVE_REVISION,
    VALID_REBASE_STATES,
    WORKTREE_REBASE_STATUS_REVISION,
    WORKTREE_REBASE_STATUS_SET_ID,
    RebaseDirectiveV1,
    WorktreeDirective,
    WorktreeStatusSchema,
    WorktreeRebaseStatusV1,
    render_rebase_prompt,
)


@pytest.fixture(autouse=True)
def _clear_emit_hook():
    settings_ops.set_emit_hook(None)
    yield
    settings_ops.set_emit_hook(None)


@pytest.fixture(autouse=True)
def _isolate_action_registry():
    snap = {k: list(v) for k, v in _HANDLERS.items()}
    try:
        yield
    finally:
        _HANDLERS.clear()
        for k, v in snap.items():
            _HANDLERS[k] = list(v)


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    # Orgs-tree hermeticity, no GRAPH_DB pin: the failure path writes the
    # rebase status at the directive row's explicit org ('autonomy'), which
    # a pin silently swallows (73bad14e) and the fail-loud resolver refuses.
    # Caller-scope directive writes resolve to personal.db in the same tree.
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.create_org_db("autonomy", type_="shared",
                          path=orgs_dir / "autonomy.db").close()
    GraphDB.create_org_db("personal", type_="personal",
                          path=orgs_dir / "personal.db").close()
    yield orgs_dir
    GraphDB.close_all_pooled()


def test_rebase_directive_set_id_composes_under_worktree_namespace():
    assert WorktreeDirective.set_id == "dashboard.session.crosstalk.worktree"
    assert RebaseDirectiveV1.set_id == "dashboard.session.crosstalk.worktree.rebase"
    assert RebaseDirectiveV1.schema_revision == WORKTREE_REBASE_DIRECTIVE_REVISION
    assert RebaseDirectiveV1._access_pattern == "append_only_log"
    validate_payload(
        RebaseDirectiveV1.set_id,
        WORKTREE_REBASE_DIRECTIVE_REVISION,
        {"target_session": "auto-x", "repo": "autonomy"},
    )


def test_worktree_status_namespace_root_is_abstract():
    assert WorktreeStatusSchema.set_id == "dashboard.session.worktree"
    assert "schema_revision" not in WorktreeStatusSchema.__dict__
    assert WorktreeRebaseStatusV1.set_id == "dashboard.session.worktree.rebase_status"
    assert WORKTREE_REBASE_STATUS_SET_ID == WorktreeRebaseStatusV1.set_id


def test_render_rebase_prompt_matches_existing_copy():
    message = render_rebase_prompt({
        "target_branch": "master",
        "commits_behind": 2,
        "fork_sha": "2d10a47deadbeef",
        "is_dirty": False,
    })
    assert "Rebase required before your commit can be merged via the dashboard." in message
    assert "master has advanced 2 commits beyond your fork point (2d10a47)." in message
    assert "git rebase master" in message
    assert "Then refresh the Worktrees page" in message
    assert "stash or commit them before rebasing" not in message


def test_render_rebase_prompt_includes_dirty_hint():
    message = render_rebase_prompt({
        "target_branch": "main",
        "commits_behind": 1,
        "fork_sha": "deadbeef12345678",
        "is_dirty": True,
    })
    assert "main has advanced 1 commit beyond your fork point (deadbee)." in message
    assert "If you have uncommitted changes, stash or commit them before rebasing." in message


@pytest.mark.asyncio
async def test_rebase_directive_deliver_renders_and_sends(monkeypatch):
    from tools.dashboard import worktree_directives as wd

    info_calls = []
    sent = []

    def fake_info(session_name, repo_name, *, sync_managed_clone_target=False):
        info_calls.append((session_name, repo_name, sync_managed_clone_target))
        return {
            "target_branch": "master",
            "commits_behind": 2,
            "fork_sha": "2d10a47deadbeef",
            "session_live": True,
            "is_dirty": False,
        }

    async def session_send(session, text):
        sent.append((session, text))

    monkeypatch.setattr(wd, "get_session_worktree_rebase_info", fake_info)

    actions = _HANDLERS.get(RebaseDirectiveV1.set_id, [])
    assert len(actions) == 1
    assert actions[0].name == "RebaseDirectiveV1.deliver"

    row = Row(
        id="evt-1",
        set_id=RebaseDirectiveV1.set_id,
        key="evt-1",
        payload={"target_session": "auto-x", "repo": "autonomy"},
        created_at="",
        updated_at="",
        org="autonomy",
    )
    await actions[0].fn(row, Services(session_send=session_send))
    assert info_calls == [("auto-x", "autonomy", True)]
    assert len(sent) == 1
    assert sent[0][0] == "auto-x"
    assert "git rebase master" in sent[0][1]


@pytest.mark.asyncio
async def test_rebase_directive_dispatched_via_settings_mediator(
    graph_db_env,
    monkeypatch,
):
    from tools.dashboard import worktree_directives as wd

    bus = EventBus()
    delivered = asyncio.Event()
    sent = []

    def hook(*, operation, snapshot, org):
        bus.broadcast_sync("setting.changed", {
            "set_id": snapshot["set_id"],
            "schema_revision": snapshot["schema_revision"],
            "key": snapshot["key"],
            "org": org,
            "publication_state": snapshot["publication_state"],
            "deprecated": snapshot["deprecated"],
            "operation": operation,
        }, dedup=False)

    def fake_info(session_name, repo_name, *, sync_managed_clone_target=False):
        return {
            "target_branch": "master",
            "commits_behind": 2,
            "fork_sha": "2d10a47deadbeef",
            "session_live": True,
            "is_dirty": False,
        }

    async def session_send(session, text):
        sent.append((session, text))
        delivered.set()

    monkeypatch.setattr(wd, "get_session_worktree_rebase_info", fake_info)
    settings_ops.set_emit_hook(hook)
    start_action_loop(Services(session_send=session_send), event_bus=bus)
    try:
        await asyncio.sleep(0.05)
        ops.add_setting(
            RebaseDirectiveV1.set_id,
            WORKTREE_REBASE_DIRECTIVE_REVISION,
            "evt-1",
            {"target_session": "auto-x", "repo": "autonomy"},
            org=ops.CALLER_ORG,
        )
        await asyncio.wait_for(delivered.wait(), timeout=2.0)
        assert sent == [("auto-x", render_rebase_prompt({
            "target_branch": "master",
            "commits_behind": 2,
            "fork_sha": "2d10a47deadbeef",
            "session_live": True,
            "is_dirty": False,
        }))]
    finally:
        await stop_action_loop()


@pytest.mark.asyncio
async def test_rebase_directive_failure_writes_failed_status(graph_db_env, monkeypatch):
    from agents.workspace_manager import WorkspaceError
    from tools.dashboard import worktree_directives as wd

    def fake_info(session_name, repo_name, *, sync_managed_clone_target=False):
        raise WorkspaceError("session is not live: auto-x")

    async def session_send(_session, _text):
        raise AssertionError("session_send should not be called on failure")

    monkeypatch.setattr(wd, "get_session_worktree_rebase_info", fake_info)

    actions = _HANDLERS.get(RebaseDirectiveV1.set_id, [])
    row = Row(
        id="evt-2",
        set_id=RebaseDirectiveV1.set_id,
        key="evt-2",
        payload={"target_session": "auto-x", "repo": "autonomy"},
        created_at="",
        updated_at="",
        org="autonomy",
    )
    await actions[0].fn(row, Services(session_send=session_send))
    failed = settings_ops.resolve_set_key(
        WORKTREE_REBASE_STATUS_SET_ID,
        "auto-x/autonomy",
        org="autonomy",
    )
    assert failed is not None
    assert json.loads(failed["payload"]) == {
        "state": "failed",
        "error": "session is not live: auto-x",
    }


def test_registry_round_trip():
    cls = get_schema(
        WORKTREE_REBASE_STATUS_SET_ID,
        WORKTREE_REBASE_STATUS_REVISION,
    )
    assert cls is WorktreeRebaseStatusV1
    validate_payload(
        WORKTREE_REBASE_STATUS_SET_ID,
        WORKTREE_REBASE_STATUS_REVISION,
        {"state": "in_progress", "error": ""},
    )


def test_keyed_per_entity_decorator_applied():
    assert WorktreeRebaseStatusV1._access_pattern == "keyed_per_entity"


def test_set_id_is_under_dashboard_namespace():
    """The schema lives under the dashboard.session.worktree.* namespace."""
    assert WorktreeRebaseStatusV1.set_id.startswith(
        "dashboard.session.worktree."
    )
    assert WorktreeRebaseStatusV1.set_id.endswith("rebase_status")


@pytest.mark.parametrize("state", list(VALID_REBASE_STATES))
def test_accepts_each_valid_state(state):
    WorktreeRebaseStatusV1.validate({"state": state, "error": ""})


def test_accepts_empty_payload():
    """All fields default to empty — an empty payload is valid (initial seed)."""
    WorktreeRebaseStatusV1.validate({})


def test_accepts_failed_with_error_text():
    WorktreeRebaseStatusV1.validate({
        "state": "failed",
        "error": "conflict in tools/dashboard/server.py at line 42",
    })


def test_rejects_unknown_state():
    with pytest.raises(SchemaValidationError, match="state"):
        WorktreeRebaseStatusV1.validate({"state": "completed"})


def test_rejects_non_string_state():
    with pytest.raises(SchemaValidationError, match="state"):
        WorktreeRebaseStatusV1.validate({"state": 1})


def test_rejects_non_string_error():
    with pytest.raises(SchemaValidationError, match="error"):
        WorktreeRebaseStatusV1.validate({"state": "failed", "error": 5})


def test_rejects_extra_fields():
    """Forward-compat: unknown fields are rejected so payload schema drift
    is loud, not silent."""
    with pytest.raises(SchemaValidationError, match="unknown"):
        WorktreeRebaseStatusV1.validate({
            "state": "in_progress",
            "error": "",
            "agent_pid": 12345,
        })
