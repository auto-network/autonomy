"""jira_write as a Central approval (auto-fkhq0.9).

Against the real ApprovalService (in-memory store) and the real desk, with a
fake Jira client, an in-memory journal and staging in tmp_path (the Jira
calls themselves are covered in test_jira_broker.py):

- the org is settled by the shared rule; no body field chooses authority;
- content is staged machine-locally and Central carries its sha256; the
  review holds it whole when it fits and a prefix otherwise; the whole
  content is served only by the accepting machine, sha256-checked on every
  read, and never as active content;
- staging is bounded per requester and in total;
- a Grant executes once, whenever it reaches the accepting machine; an
  interrupted claim is settled per op (re-applied once, reconciled, or
  unknown); decline/cancel/expiry never call Jira.
"""

from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace

import pytest

from agents.capabilities.jira.backend import api
from tools.dashboard import api_auth
from tools.dashboard import jira_central as jc
from tools.dashboard.approval_kind_registry import build_production_registry
from tools.dashboard.approval_service import (
    ApprovalService,
    ApprovalServiceError,
    HumanApprovalActor,
    InMemoryApprovalStore,
)
from tools.graph.schemas.central_attention import ApprovalRequestV1
from tools.network.idkit.keys import KeyPair

ROOT = KeyPair.generate()
HERE = "h" * 43
ELSEWHERE = "e" * 43
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


class Machine:
    def __init__(self):
        self.destination = HERE

    def __call__(self):
        return self.destination


class FakeJira:
    JiraError = api.JiraError

    def __init__(self):
        self.calls = []
        self.found = None
        self.status = ""
        self.fail = None

    def _call(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self.fail:
            raise api.JiraError(self.fail)
        return {"op": name}

    def add_comment(self, cfg, key, body, tag=None):
        return self._call("add_comment", key, body, tag=tag)

    def set_editable_field(self, cfg, key, ref, value):
        return self._call("set_editable_field", key, ref, value)

    def create_issue(self, cfg, fields, tag=None):
        return self._call("create_issue", fields, tag=tag)

    def add_attachment(self, cfg, key, filename, data, media):
        return self._call("add_attachment", key, filename, data, media)

    def transition_issue(self, cfg, key, name, fields=None):
        return self._call("transition_issue", key, name, fields)

    def change_issue_type(self, cfg, key, issue_type):
        return self._call("change_issue_type", key, issue_type)

    def set_story_points(self, cfg, key, value, board_id=None):
        return self._call("set_story_points", key, value, board_id)

    def find_comment(self, cfg, key, prop, value):
        self.calls.append(("find_comment", (key, prop, value), {}))
        return self.found

    def find_created_issue(self, cfg, project, summary, prop, value):
        self.calls.append(("find_created_issue", (project, summary, prop, value), {}))
        return self.found

    def issue_status(self, cfg, key):
        self.calls.append(("issue_status", (key,), {}))
        return self.status

    def list_transitions(self, cfg, key):
        self.calls.append(("list_transitions", (key,), {}))
        return [{"name": "Start Progress", "to_status": "In Progress"}]


@pytest.fixture
def env(tmp_path):
    clock, machine = Clock(), Machine()
    staging = jc.Staging(root=lambda: tmp_path / "jira-staging")
    registry = build_production_registry(runtimes={jc.KIND: jc.build_approval_runtime(
        destination_resolver=machine, machine_label=lambda: "Home", staging=staging)})
    approvals = ApprovalService(registry=registry, store=InMemoryApprovalStore(),
                                personal_root_resolver=lambda: ROOT.public_hex,
                                session_label_resolver=lambda s: f"{s} · Jira", clock=clock)
    journal: dict = {}
    jira = FakeJira()
    desk = jc.JiraWriteDesk(approvals=approvals, destination_resolver=machine,
                            staging=staging, jira=jira, config=lambda org: f"cfg:{org}",
                            journal=journal.get, record=journal.__setitem__, clock=clock)
    return SimpleNamespace(approvals=approvals, desk=desk, jira=jira, journal=journal,
                           clock=clock, machine=machine, staging=staging,
                           tmp=tmp_path)


def _org_session(org="acme", subject="auto-1"):
    return api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.ORG_SESSION, subject=subject, org=org)


def _create(env, request, principal=None):
    return env.approvals.create_from_principal(jc.KIND, principal or _org_session(),
                                               request).approval_id


def _grant(env, approval_id, outcome="granted"):
    env.approvals.decide(approval_id, HumanApprovalActor._verified(ROOT.public_hex),
                         outcome=outcome, decision={})
    return env.approvals.status(approval_id)


def _run(env, approval_id):
    status = _grant(env, approval_id)
    env.desk.materialize(status)
    return env.desk.requester_result(env.approvals.status(approval_id))


COMMENT = {"op": "comment", "key": "ENT-1", "body_markdown": "line one\nline two"}


# ── creation ─────────────────────────────────────────────────────────


def test_the_org_is_the_sessions_own_and_the_review_carries_the_whole_text(env):
    rid = _create(env, COMMENT)
    payload = env.approvals.status(rid).request.payload
    ApprovalRequestV1.validate(payload)
    assert payload["request"]["org"] == "acme"
    review = payload["safe_review"]
    assert review["content"]["complete"] is True
    assert review["content"]["lines"] == ["line one", "line two"]
    assert review["title"] == "Jira comment" and review["target"] == "ENT-1"
    assert "body_markdown" not in json.dumps(payload["request"])


def test_an_org_session_cannot_name_another_org_and_a_local_session_must_name_one(env):
    with pytest.raises(ApprovalServiceError):
        _create(env, {**COMMENT, "org_slug": "other"})
    local = api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.LOCAL_SESSION, subject="host-1")
    with pytest.raises(ApprovalServiceError):
        _create(env, COMMENT, local)
    rid = _create(env, {**COMMENT, "org_slug": "beta"}, local)
    assert env.approvals.status(rid).request.payload["request"]["org"] == "beta"
    with pytest.raises(ApprovalServiceError):   # Central reserves "org" in a body
        _create(env, {**COMMENT, "org": "acme"})


@pytest.mark.parametrize("request_body", [
    {"op": "delete", "key": "ENT-1"},
    {"op": "comment", "key": "not a key", "body_markdown": "x"},
    {"op": "comment", "key": "ENT-1", "body_markdown": "  "},
    {"op": "comment", "key": "ENT-1", "body_markdown": "x", "audience": "sessions"},
    {"op": "create", "fields": {"summary": "no project"}},
    # A description the operator could not read as text is never posted unseen.
    {"op": "create", "fields": {"project": {"key": "ENT"}, "summary": "s",
                                "description": {"type": "doc", "version": 1, "content": []}}},
    {"op": "attach", "key": "ENT-1", "filename": "a", "mime_type": "text/plain",
     "content_b64": "not base64!"},
    {"op": "set_field", "key": "ENT-1", "field_id": "a", "field_name": "b",
     "body_markdown": "x"},
])
def test_a_write_that_could_never_run_is_refused_at_creation(env, request_body):
    with pytest.raises(ApprovalServiceError):
        _create(env, request_body)
    assert not list((env.tmp / "jira-staging").glob("*.json"))


def test_a_long_body_reviews_as_a_prefix_and_is_fetched_whole_here_only(env):
    body = "\n".join(f"line {n} " + "x" * 80 for n in range(200))
    rid = _create(env, {"op": "comment", "key": "ENT-1", "body_markdown": body})
    review = env.approvals.status(rid).request.payload["safe_review"]
    assert review["content"]["complete"] is False
    assert 0 < len(review["content"]["lines"]) < 200
    form, fetched, headers = env.desk.content(rid)
    assert form == "json" and fetched["lines"] == body.split("\n")
    assert headers["X-Content-Type-Options"] == "nosniff"
    env.machine.destination = ELSEWHERE
    with pytest.raises(jc.JiraWriteError) as exc:
        env.desk.content(rid)
    assert exc.value.code == "elsewhere"


def test_staged_content_is_checked_on_every_read(env):
    rid = _create(env, COMMENT)
    (env.tmp / "jira-staging" / f"{rid}.bin").write_bytes(b"swapped after review")
    with pytest.raises(jc.JiraWriteError) as exc:
        env.desk.content(rid)
    assert exc.value.code == "content_mismatch"
    result = _run(env, rid)
    assert result["execution"]["ok"] is False and "content_mismatch" in result["execution"]["error"]
    assert env.jira.calls == []


@pytest.mark.parametrize("media,data,inline", [
    ("image/png", PNG, True),
    ("image/svg+xml", b"<svg><script>alert(1)</script></svg>", False),
    ("image/png", b"<html><script>alert(1)</script></html>", False),   # html disguised
    ("image/gif", PNG, False),                                           # mismatched type
    ("text/html", b"<html></html>", False),
])
def test_an_attachment_is_inline_only_as_a_verified_raster_image(env, media, data, inline):
    rid = _create(env, {"op": "attach", "key": "ENT-1", "filename": "x.png", "mime_type": media,
                        "content_b64": base64.b64encode(data).decode()})
    form, body, headers = env.desk.content(rid)
    assert form == "bytes" and body == data
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Content-Security-Policy"] == "sandbox; default-src 'none'"
    if inline:
        assert headers["Content-Type"] == "image/png" and headers["Content-Disposition"] == "inline"
    else:
        assert headers["Content-Type"] == "application/octet-stream"
        assert headers["Content-Disposition"].startswith("attachment;")
    assert env.approvals.status(rid).request.payload["safe_review"]["content"]["complete"] is False


def test_staging_is_bounded_per_requester_and_in_total(env, monkeypatch):
    monkeypatch.setattr(jc, "MAX_STAGED_BYTES_PER_REQUESTER", 40)
    _create(env, {"op": "comment", "key": "ENT-1", "body_markdown": "x" * 30})
    with pytest.raises(ApprovalServiceError):
        _create(env, {"op": "comment", "key": "ENT-1", "body_markdown": "y" * 30})
    _create(env, {"op": "comment", "key": "ENT-1", "body_markdown": "z" * 30},
            _org_session(subject="auto-2"))
    monkeypatch.setattr(jc, "MAX_STAGED_BYTES_TOTAL", 70)
    with pytest.raises(ApprovalServiceError):
        _create(env, {"op": "comment", "key": "ENT-1", "body_markdown": "w" * 20},
                _org_session(subject="auto-3"))


# ── execution ────────────────────────────────────────────────────────


def test_a_grant_executes_once_with_the_frozen_org_and_tag(env):
    rid = _create(env, COMMENT)
    assert _run(env, rid) == {"approved": True, "execution": {"ok": True, "op": "add_comment"}}
    env.desk.materialize(env.approvals.status(rid))
    assert env.jira.calls == [("add_comment", ("ENT-1", "line one\nline two"),
                               {"tag": {jc.TAG_PROPERTY: rid}})]
    assert env.desk.operator_result(env.approvals.status(rid))["state"] == jc.DONE


@pytest.mark.parametrize("outcome", ["declined", "canceled"])
def test_decline_and_cancel_never_call_jira(env, outcome):
    rid = _create(env, COMMENT)
    if outcome == "declined":
        status = _grant(env, rid, outcome="declined")
    else:
        env.approvals.cancel_from_principal(rid, _org_session())
        status = env.approvals.status(rid)
    env.desk.materialize(status)
    assert env.jira.calls == [] and env.journal == {}


def test_a_grant_that_arrives_late_still_runs(env):
    rid = _create(env, COMMENT)
    status = _grant(env, rid)
    env.clock.t += 86400      # the decision reached this machine a day later
    env.desk.materialize(status)
    assert len(env.jira.calls) == 1
    assert env.desk.operator_result(status)["state"] == jc.DONE

def test_another_machine_executes_nothing(env):
    rid = _create(env, COMMENT)
    status = _grant(env, rid)
    env.machine.destination = ELSEWHERE
    env.desk.materialize(status)
    assert env.jira.calls == []
    assert env.desk.operator_result(status)["state"] == jc.ELSEWHERE


def test_a_jira_refusal_is_failed(env):
    env.jira.fail = "bad field"
    rid = _create(env, COMMENT)
    assert _run(env, rid)["execution"] == {"ok": False, "error": "bad field"}


# ── an interrupted claim ─────────────────────────────────────────────


def _interrupted(env, request, **journal):
    rid = _create(env, request)
    status = _grant(env, rid)
    env.journal[rid] = {"state": "claimed", "claimed_at": env.clock.t, **journal}
    env.desk.materialize(status)
    return rid, env.desk.requester_result(env.approvals.status(rid))


def test_a_value_op_is_reapplied_once_and_says_so(env):
    rid, result = _interrupted(env, {"op": "change_type", "key": "ENT-1", "issue_type": "Bug"})
    assert [c[0] for c in env.jira.calls] == ["change_issue_type"]
    assert result["execution"]["ok"] is True and "overwritten" in result["execution"]["note"]
    assert env.desk.operator_result(env.approvals.status(rid))["reapplied_after_restart"] is True


def test_a_second_interruption_is_unknown(env):
    _rid, result = _interrupted(env, {"op": "change_type", "key": "ENT-1", "issue_type": "Bug"},
                                reapplied_after_restart=True)
    assert env.jira.calls == [] and result["execution"]["ok"] is False
    assert "interrupted" in result["execution"]["error"]


def test_an_interrupted_attachment_is_unknown_and_not_retried(env):
    _rid, result = _interrupted(env, {"op": "attach", "key": "ENT-1", "filename": "a.log",
                                      "mime_type": "text/plain",
                                      "content_b64": base64.b64encode(b"x").decode()})
    assert env.jira.calls == [] and "interrupted" in result["execution"]["error"]


def test_a_transition_records_its_destination_before_the_call(env):
    rid = _create(env, {"op": "transition", "key": "ENT-1", "transition": "Start Progress"})
    _run(env, rid)
    assert [c[0] for c in env.jira.calls] == ["list_transitions", "transition_issue"]
    assert env.journal[rid]["target_status"] == "In Progress"


def test_a_transition_that_now_leads_elsewhere_than_reviewed_is_refused(env):
    rid = _create(env, {"op": "transition", "key": "ENT-1", "transition": "Start Progress",
                        "to_status": "In review"})
    result = _run(env, rid)
    assert result["execution"] == {"ok": False, "error": "this transition now leads to "
                                   "In Progress, not the reviewed In review"}
    assert [c[0] for c in env.jira.calls] == ["list_transitions"]


def test_an_interrupted_transition_that_landed_is_done_without_posting(env):
    """The transition is a verb ("Start Progress"); the status it lands in is
    a state ("In Progress"): reconcile against the recorded destination."""
    env.jira.status = "In Progress"
    _rid, result = _interrupted(env, {"op": "transition", "key": "ENT-1",
                                      "transition": "Start Progress"},
                                target_status="In Progress")
    assert [c[0] for c in env.jira.calls] == ["issue_status"]
    assert result["execution"]["reconciled"] is True


def test_a_claim_held_by_a_live_process_is_left_alone(env):
    env.desk._owner_alive = lambda row: row.get("owner_pid") == 4242
    rid = _create(env, COMMENT)
    status = _grant(env, rid)
    env.journal[rid] = {"state": "claimed", "claimed_at": env.clock.t, "owner_pid": 4242,
                        "owner_start": "running"}
    env.desk.materialize(status)
    assert env.jira.calls == [] and env.journal[rid]["state"] == "claimed"
    env.journal[rid]["owner_pid"] = 99      # that process is gone now
    env.desk.materialize(status)
    assert [c[0] for c in env.jira.calls] == ["find_comment", "add_comment"]
    assert env.journal[rid]["state"] == jc.DONE and "owner_pid" not in env.journal[rid]


def test_an_interrupted_comment_that_landed_is_found_not_reposted(env):
    env.jira.found = {"id": "77"}
    rid, result = _interrupted(env, COMMENT)
    assert [c[0] for c in env.jira.calls] == ["find_comment"]
    assert env.jira.calls[0][1] == ("ENT-1", jc.TAG_PROPERTY, rid)
    assert result["execution"] == {"ok": True, "id": "77"}


def test_an_interrupted_comment_that_did_not_land_is_posted_once(env):
    _rid, result = _interrupted(env, COMMENT)
    assert [c[0] for c in env.jira.calls] == ["find_comment", "add_comment"]
    assert result["execution"]["ok"] is True and "had not landed" in result["execution"]["note"]


def test_an_interrupted_create_that_landed_is_found(env):
    env.jira.found = {"key": "ENT-9"}
    _rid, result = _interrupted(env, {"op": "create", "fields": {
        "project": {"key": "ENT"}, "summary": "A bug", "issuetype": {"name": "Bug"}}})
    assert [c[0] for c in env.jira.calls] == ["find_created_issue"]
    assert result["execution"] == {"ok": True, "key": "ENT-9"}


def test_production_composition_claims_the_kind_and_the_legacy_executor_is_gone():
    from tools.dashboard import approvals_routes, attention_routes, jira_routes

    runtime = attention_routes.build_production_runtime()
    assert runtime.approval_http.claims_kind(jc.KIND)
    assert runtime.approval_http.migrated_kind(jc.KIND)
    assert isinstance(runtime.jira_write_desk, jc.JiraWriteDesk)
    assert jc.KIND not in approvals_routes.EXECUTORS
    assert not hasattr(jira_routes, "_execute_jira_write")
    paths = {route.path for route in attention_routes.routes}
    assert "/api/attention/items/{attention_id:path}/jira-write-content" in paths


def test_staged_content_no_write_will_read_again_is_deleted_on_the_next_event(env):
    declined = _create(env, {**COMMENT, "body_markdown": "declined body"})
    performed = _create(env, {**COMMENT, "body_markdown": "performed body"})
    waiting = _create(env, {**COMMENT, "body_markdown": "waiting body"})
    _grant(env, declined, outcome="declined")
    env.desk.materialize(_grant(env, performed))
    assert sorted(env.staging.approval_ids()) == sorted([declined, performed, waiting])
    env.desk.forget_settled()
    assert env.staging.approval_ids() == [waiting]


def test_a_store_that_cannot_answer_never_loses_staged_content(env, monkeypatch):
    waiting = _create(env, {**COMMENT, "body_markdown": "waiting body"})
    def unavailable(_approval_id, **_):
        raise ApprovalServiceError("storage_unavailable")
    monkeypatch.setattr(env.approvals, "status", unavailable)
    env.desk.forget_settled()
    assert env.staging.approval_ids() == [waiting]
