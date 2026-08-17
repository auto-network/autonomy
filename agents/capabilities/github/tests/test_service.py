"""Tests for the REST-by-id ops added in auto-nrqbs.

Covers:

* ``source_control_review_read_by_id_v1`` builds the right docker/gh
  argv (with and without ETag), classifies 304 responses as
  ``FAILURE_NOT_MODIFIED``.
* ``source_control_check_runs_read_for_sha_v1`` builds the right
  docker/gh argv.
* ``parse_pull_response`` normalizes REST JSON into the flat
  cache shape; merges merged_at into ``state="merged"``; reads
  ETag from the response headers.
* ``parse_check_runs_response`` maps REST JSON to the four-status
  vocabulary and skips skipped runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.capabilities.github import service
from agents.capabilities.github.service import (
    FAILURE_NOT_MODIFIED,
    FAILURE_RATE_LIMITED,
    OP_CHECK_RUNS_READ_FOR_SHA,
    OP_REVIEW_READ_BY_ID,
    parse_check_runs_response,
    parse_pull_response,
    source_control_check_runs_read_for_sha_v1,
    source_control_review_read_by_id_v1,
)
from agents.workspace_manager import WorktreeState


def _live_row() -> WorktreeState:
    return WorktreeState(
        session_name="auto-x",
        repo_name="autonomy",
        worktree_path=Path("/tmp/worktrees/auto-x/autonomy"),
        managed_clone=Path("/tmp/repos/autonomy.git"),
        branch="session/auto-x",
        commits_ahead=1,
        is_dirty=False,
        ff_eligible=True,
        clone_stale=False,
        rebase_required=False,
        session_live=True,
    )


# ── parse_pull_response ───────────────────────────────────────────────


def test_parse_pull_response_basic():
    stdout = (
        "HTTP/2.0 200 OK\r\n"
        "ETag: W/\"abc123\"\r\n"
        "Content-Type: application/json\r\n"
        "\r\n"
        '{"title": "Add binding", "body": "see bead", "state": "open", '
        '"node_id": "PR_kwDOA1", '
        '"draft": false, "html_url": "https://github.com/o/r/pull/1", '
        '"head": {"sha": "head1"}, '
        '"base": {"sha": "base1", "ref": "main"}}'
    )
    parsed = parse_pull_response(stdout)
    assert parsed["title"] == "Add binding"
    assert parsed["body"] == "see bead"
    assert parsed["node_id"] == "PR_kwDOA1"
    assert parsed["state"] == "open"
    assert parsed["head_sha"] == "head1"
    assert parsed["base_sha"] == "base1"
    assert parsed["base_branch"] == "main"
    assert parsed["is_draft"] is False
    assert parsed["url"] == "https://github.com/o/r/pull/1"
    assert parsed["etag"] == 'W/"abc123"'


def test_parse_pull_response_promotes_merged_to_state():
    """GitHub keeps state='closed' for merged PRs; promote to 'merged'."""
    stdout = (
        "HTTP/2.0 200 OK\r\n"
        "\r\n"
        '{"title": "X", "body": "", "state": "closed", '
        '"merged_at": "2026-04-01T00:00:00Z", '
        '"head": {"sha": "h"}, "base": {"sha": "b", "ref": "main"}}'
    )
    parsed = parse_pull_response(stdout)
    assert parsed["state"] == "merged"


def test_parse_pull_response_falls_back_to_caller_etag():
    """On 304 the body is empty; the caller passes the cached etag through."""
    stdout = "HTTP/2.0 304 Not Modified\r\n\r\n"
    parsed = parse_pull_response(stdout, etag='W/"prev"')
    assert parsed["etag"] == 'W/"prev"'
    assert parsed["title"] == ""
    assert parsed["state"] == "open"  # safe default for empty body


def test_parse_pull_response_handles_lf_separators():
    """gh on some platforms uses \\n\\n instead of \\r\\n\\r\\n; both must work."""
    stdout = (
        "HTTP/2.0 200 OK\n"
        "ETag: \"xyz\"\n"
        "\n"
        '{"title": "T", "body": "B", "state": "open", '
        '"head": {"sha": "h"}, "base": {"sha": "b", "ref": "main"}}'
    )
    parsed = parse_pull_response(stdout)
    assert parsed["etag"] == '"xyz"'


# ── parse_check_runs_response ─────────────────────────────────────────


def test_parse_check_runs_response_maps_statuses():
    stdout = (
        '{"check_runs": ['
        '{"id": 1, "name": "build", "status": "completed", "conclusion": "success"},'
        '{"id": 2, "name": "test",  "status": "completed", "conclusion": "failure"},'
        '{"id": 3, "name": "lint",  "status": "in_progress"},'
        '{"id": 4, "name": "deploy", "status": "queued"}'
        ']}'
    )
    runs = parse_check_runs_response(stdout)
    assert {r["label"]: r["status"] for r in runs} == {
        "build": "pass",
        "test": "fail",
        "lint": "running",
        "deploy": "pending",
    }


def test_parse_check_runs_response_drops_skipped():
    stdout = (
        '{"check_runs": ['
        '{"id": 1, "name": "build", "status": "completed", "conclusion": "skipped"},'
        '{"id": 2, "name": "test",  "status": "completed", "conclusion": "success"}'
        ']}'
    )
    runs = parse_check_runs_response(stdout)
    assert [r["label"] for r in runs] == ["test"]


def test_parse_check_runs_response_no_icon_field():
    """Cache must not store ``icon`` — UI computes it from ``label``."""
    stdout = '{"check_runs": [{"id": 1, "name": "build", "status": "completed", "conclusion": "success"}]}'
    runs = parse_check_runs_response(stdout)
    assert "icon" not in runs[0]


def test_parse_check_runs_response_empty_input():
    assert parse_check_runs_response("") == []
    assert parse_check_runs_response("not json") == []
    assert parse_check_runs_response('{"check_runs": []}') == []


def test_parse_check_runs_response_carries_detail():
    stdout = (
        '{"check_runs": ['
        '{"id": 1, "name": "test", "status": "completed", "conclusion": "failure", '
        ' "output": {"title": "1 of 12 failed", "summary": "..."}}'
        ']}'
    )
    runs = parse_check_runs_response(stdout)
    assert runs[0]["detail"] == "1 of 12 failed"


# ── source_control_review_read_by_id_v1 ───────────────────────────────


@pytest.mark.asyncio
async def test_review_read_by_id_builds_correct_argv(monkeypatch):
    """Maps to ``docker exec <c> gh api -i /repos/<slug>/pulls/<id>``."""
    captured: dict = {}

    async def fake_run_cli(cmd, *, timeout):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        return ('HTTP/2.0 200 OK\r\n\r\n{"title":"T","body":"B","state":"open","head":{"sha":"h"},"base":{"sha":"b","ref":"main"}}', "", 0, False)

    async def fake_resolve_container(session_name, *, timeout=5):
        return session_name

    monkeypatch.setattr(service, "run_cli", fake_run_cli)
    monkeypatch.setattr(service, "resolve_live_container", fake_resolve_container)
    monkeypatch.setattr(service, "derive_repo_slug", lambda _p: "owner/repo")

    rows = [_live_row()]
    result = await source_control_review_read_by_id_v1(
        "auto-x", "autonomy",
        org="autonomy",
        review_id="123",
        rows=rows,
    )
    assert result.ok
    assert result.operation == OP_REVIEW_READ_BY_ID
    assert captured["cmd"] == [
        "docker", "exec", "auto-x",
        "gh", "api", "-i", "/repos/owner/repo/pulls/123",
    ]


@pytest.mark.asyncio
async def test_review_read_by_id_passes_etag_header(monkeypatch):
    """``etag=`` arg surfaces as ``--header 'If-None-Match: <etag>'``."""
    captured: dict = {}

    async def fake_run_cli(cmd, *, timeout):
        captured["cmd"] = cmd
        return ('HTTP/2.0 200 OK\r\n\r\n{"title":"T","body":"B","state":"open","head":{"sha":"h"},"base":{"sha":"b","ref":"main"}}', "", 0, False)

    async def fake_resolve_container(session_name, *, timeout=5):
        return session_name

    monkeypatch.setattr(service, "run_cli", fake_run_cli)
    monkeypatch.setattr(service, "resolve_live_container", fake_resolve_container)
    monkeypatch.setattr(service, "derive_repo_slug", lambda _p: "owner/repo")

    await source_control_review_read_by_id_v1(
        "auto-x", "autonomy",
        org="autonomy",
        review_id="9",
        rows=[_live_row()],
        etag='W/"abc"',
    )
    assert captured["cmd"][-3:] == [
        "/repos/owner/repo/pulls/9",
        "--header", 'If-None-Match: W/"abc"',
    ]


@pytest.mark.asyncio
async def test_review_read_by_id_classifies_304(monkeypatch):
    """A 304 response is surfaced as ``failure=not_modified``."""

    async def fake_run_cli(cmd, *, timeout):
        return ("HTTP/2.0 304 Not Modified\r\n\r\n", "", 0, False)

    async def fake_resolve_container(session_name, *, timeout=5):
        return session_name

    monkeypatch.setattr(service, "run_cli", fake_run_cli)
    monkeypatch.setattr(service, "resolve_live_container", fake_resolve_container)
    monkeypatch.setattr(service, "derive_repo_slug", lambda _p: "owner/repo")

    result = await source_control_review_read_by_id_v1(
        "auto-x", "autonomy",
        org="autonomy",
        review_id="42",
        rows=[_live_row()],
        etag='W/"prev"',
    )
    assert result.ok is False
    assert result.failure == FAILURE_NOT_MODIFIED


@pytest.mark.asyncio
async def test_review_read_by_id_classifies_rate_limit(monkeypatch):
    """A 429-shaped failure still classifies as rate_limited."""

    async def fake_run_cli(cmd, *, timeout):
        return ("", "API rate limit exceeded", 1, False)

    async def fake_resolve_container(session_name, *, timeout=5):
        return session_name

    monkeypatch.setattr(service, "run_cli", fake_run_cli)
    monkeypatch.setattr(service, "resolve_live_container", fake_resolve_container)
    monkeypatch.setattr(service, "derive_repo_slug", lambda _p: "owner/repo")

    result = await source_control_review_read_by_id_v1(
        "auto-x", "autonomy",
        org="autonomy",
        review_id="42",
        rows=[_live_row()],
    )
    assert result.failure == FAILURE_RATE_LIMITED


# ── source_control_check_runs_read_for_sha_v1 ─────────────────────────


@pytest.mark.asyncio
async def test_check_runs_read_for_sha_builds_correct_argv(monkeypatch):
    captured: dict = {}

    async def fake_run_cli(cmd, *, timeout):
        captured["cmd"] = cmd
        return ('{"check_runs":[]}', "", 0, False)

    async def fake_resolve_container(session_name, *, timeout=5):
        return session_name

    monkeypatch.setattr(service, "run_cli", fake_run_cli)
    monkeypatch.setattr(service, "resolve_live_container", fake_resolve_container)
    monkeypatch.setattr(service, "derive_repo_slug", lambda _p: "owner/repo")

    result = await source_control_check_runs_read_for_sha_v1(
        "auto-x", "autonomy",
        org="autonomy",
        head_sha="deadbeef",
        rows=[_live_row()],
    )
    assert result.ok
    assert result.operation == OP_CHECK_RUNS_READ_FOR_SHA
    assert captured["cmd"] == [
        "docker", "exec", "auto-x",
        "gh", "api", "/repos/owner/repo/commits/deadbeef/check-runs",
    ]
