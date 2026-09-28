"""email_send as a Settings-native Central approval (tools/dashboard/mailbox_central.py).

Proves, against the real ApprovalService with an in-memory store:
- the request is planned only for a proven session of a workspace with the
  mailbox capability, freezes the sender from the org install, and fits the
  Central row limits (the body becomes lines: no newline may be stored);
- the operator's decision carries nothing (operator-session authority);
- an approved email is sent exactly once, only on the machine that accepted
  the request; a replay never resends; a claim left by a stopped process
  becomes "unknown" and is not retried; a decline never sends;
- the requester's envelope and the operator's result carry the outcome;
- the production composition claims and migrates the kind.
"""
from __future__ import annotations

import pytest

from agents.capabilities.mailbox.backend import api as mail
from tools.dashboard import api_auth
from tools.dashboard import mailbox_central as central
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
MESSAGE = {"to": "Someone <a@example.com>", "cc": "", "subject": "Your code",
           "body": "Hello,\r\n\tthe code is 482913.\n\nThanks\n"}


def _principal(subject="auto-0927-000001", org="acme"):
    return api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.ORG_SESSION, subject=subject, org=org)


def _service(*, enabled_workspaces=("ws1",), ids=None):
    ids = iter(ids or [f"central-email-{n:04d}" for n in range(1, 50)])

    def workspace_resolver(session):
        if "ws1" not in enabled_workspaces:
            raise ValueError("not enabled")
        return "ws1", "acme"

    runtime = central.build_approval_runtime(
        destination_resolver=lambda: HERE,
        workspace_resolver=workspace_resolver,
        sender_resolver=lambda org: "agent@auto.network" if org == "acme" else "",
    )
    return ApprovalService(
        registry=build_production_registry(runtimes={central.KIND: runtime}),
        store=InMemoryApprovalStore(),
        personal_root_resolver=lambda: ROOT.public_hex,
        session_label_resolver=lambda subject: f"{subject} · Writing an email",
        clock=lambda: 1000.0,
        id_factory=lambda: next(ids),
    )


def _actor():
    return HumanApprovalActor._verified(ROOT.public_hex)


class Journal(dict):
    pass


@pytest.fixture
def journal(monkeypatch):
    rows = Journal()
    monkeypatch.setattr(central.EmailSendConsumer, "journal", staticmethod(lambda i: rows.get(i)))
    monkeypatch.setattr(central.EmailSendConsumer, "_record",
                        staticmethod(lambda i, p: rows.__setitem__(i, dict(p))))
    central.EmailSendConsumer._inflight.clear()
    return rows


def _consumer(sent, *, destination=HERE, enabled=True, fail=None):
    def sender(org, **message):
        if fail:
            raise mail.MailboxError(fail)
        sent.append((org, message))
        return {"message_id": f"<m{len(sent)}@auto.network>"}
    return central.EmailSendConsumer(destination_resolver=lambda: destination, sender=sender,
                                     enabled=lambda ws, org: enabled, clock=lambda: 2000.0)


# ── planning ───────────────────────────────────────────────────────────────

def test_request_freezes_sender_and_fits_central_limits():
    approvals = _service()
    record = approvals.create_from_principal(central.KIND, _principal(), MESSAGE)
    payload = record.payload
    ApprovalRequestV1.validate(payload)
    assert payload["request"] == {"to": "a@example.com", "cc": "", "subject": "Your code",
                                  "body_lines": ["Hello,", "    the code is 482913.", "", "Thanks"]}
    review = payload["safe_review"]
    assert review["from_addr"] == "agent@auto.network"
    assert review["requester_label"] == "auto-0927-000001 · Writing an email"
    assert payload["staged"]["result_destination_id"] == HERE
    assert payload["staged"]["workspace"] == "ws1"


def test_the_largest_allowed_message_still_fits():
    approvals = _service()
    line = "x" * 48
    body = "\n".join([line] * mail.MAX_BODY_LINES)
    assert len(body.encode()) <= mail.MAX_BODY_BYTES
    record = approvals.create_from_principal(central.KIND, _principal(),
                                             {**MESSAGE, "subject": "S" * 200, "body": body})
    ApprovalRequestV1.validate(record.payload)


@pytest.mark.parametrize("body", [
    {**MESSAGE, "body": "x" * (mail.MAX_BODY_BYTES + 1)},
    {**MESSAGE, "body": "\n".join(["x"] * (mail.MAX_BODY_LINES + 1))},
    {**MESSAGE, "subject": "hi\r\nBcc: evil@example.com"},
    {**MESSAGE, "to": ""},
    {**MESSAGE, "from": "ceo@example.com"},
    {**MESSAGE, "session": "auto-someone-else"},
])
def test_invalid_or_forged_requests_are_refused(body):
    with pytest.raises(ApprovalServiceError):
        _service().create_from_principal(central.KIND, _principal(), body)


def test_a_workspace_without_the_capability_cannot_request():
    with pytest.raises(ApprovalServiceError):
        _service(enabled_workspaces=()).create_from_principal(central.KIND, _principal(), MESSAGE)


def test_decisions_carry_nothing():
    approvals = _service()
    record = approvals.create_from_principal(central.KIND, _principal(), MESSAGE)
    with pytest.raises(ApprovalServiceError):
        approvals.decide(record.approval_id, _actor(), outcome="granted", decision={"x": 1})
    approvals.decide(record.approval_id, _actor(), outcome="granted", decision={})


# ── sending ────────────────────────────────────────────────────────────────

def _granted(approvals):
    record = approvals.create_from_principal(central.KIND, _principal(), MESSAGE)
    approvals.decide(record.approval_id, _actor(), outcome="granted", decision={})
    return approvals.status(record.approval_id)


def test_sent_exactly_once_on_the_accepting_machine(journal):
    approvals, sent = _service(), []
    status = _granted(approvals)
    consumer = _consumer(sent)
    assert consumer.materialize(status) is True
    assert consumer.materialize(status) is True        # replay
    assert _consumer(sent).materialize(status) is True  # another consumer, same machine
    assert len(sent) == 1
    org, message = sent[0]
    assert org == "acme" and message["to"] == "a@example.com"
    assert message["body"] == "Hello,\n    the code is 482913.\n\nThanks\n"
    assert journal[status.request.approval_id]["state"] == "sent"
    result = consumer.project(status)
    assert result == {"approved": True, "execution": {
        "ok": True, "message_id": "<m1@auto.network>", "from": "agent@auto.network",
        "to": "a@example.com", "subject": "Your code"}}


def test_another_machine_never_sends(journal):
    approvals, sent = _service(), []
    status = _granted(approvals)
    consumer = _consumer(sent, destination=ELSEWHERE)
    assert consumer.materialize(status) is False
    assert consumer.project(status) is None
    assert sent == [] and journal == {}


def test_a_decline_never_sends(journal):
    approvals, sent = _service(), []
    record = approvals.create_from_principal(central.KIND, _principal(), MESSAGE)
    approvals.decide(record.approval_id, _actor(), outcome="declined", decision={})
    consumer = _consumer(sent)
    assert consumer.materialize(approvals.status(record.approval_id)) is False
    assert sent == []


def test_an_interrupted_send_becomes_unknown_and_is_not_retried(journal):
    approvals, sent = _service(), []
    status = _granted(approvals)
    journal[status.request.approval_id] = {"state": "claimed", "claimed_at": 1500.0}
    consumer = _consumer(sent)
    consumer.materialize(status)
    assert sent == []
    assert journal[status.request.approval_id]["state"] == "unknown"
    assert consumer.project(status)["execution"]["ok"] is False


def test_failure_and_disabled_capability_are_reported_not_retried(journal):
    approvals, sent = _service(), []
    status = _granted(approvals)
    consumer = _consumer(sent, fail="SMTP mail.example.org: 550 relay denied")
    consumer.materialize(status)
    consumer.materialize(status)
    assert journal[status.request.approval_id]["state"] == "failed"
    assert consumer.project(status)["execution"] == {"ok": False, "error": "SMTP mail.example.org: 550 relay denied"}
    status2 = _granted(approvals)
    _consumer(sent, enabled=False).materialize(status2)
    assert sent == [] and "no longer enabled" in journal[status2.request.approval_id]["error"]


def test_legacy_decision_mapping():
    adapter = central.build_http_adapter(_consumer([]))
    assert adapter.legacy_decision_mapper({"approved": False}).outcome == "declined"
    assert adapter.legacy_decision_mapper({"approved": True}).outcome == "granted"


# ── composition ────────────────────────────────────────────────────────────

def test_production_composition_claims_and_migrates_email_send():
    from tools.dashboard import attention_routes
    runtime = attention_routes.build_production_runtime()
    assert runtime.approval_http.migrated_kind(central.KIND) is True
    assert central.KIND in runtime.operator_result_projectors


def test_mail_send_limits_match_the_backend():
    from pathlib import Path
    tool = (Path(central.__file__).resolve().parents[2] / "agents" / "capabilities" / "mailbox"
            / "tools" / "mail-send").read_text()
    assert f"MAX_BODY_BYTES = {mail.MAX_BODY_BYTES}" in tool
    assert f"MAX_BODY_LINES = {mail.MAX_BODY_LINES}" in tool
    assert '"session"' not in tool


def test_the_real_journal_round_trips_through_the_machine_store(tmp_path, monkeypatch):
    """The send journal is written to the machine store before and after SMTP.
    Every other test replaces it, so this one writes through the real schema:
    a float timestamp once failed validation after the email had left, and the
    request then answered "unavailable" for good."""
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    from tools.graph import schemas  # noqa: F401 (registers the set)
    approval_id = "central-journal-round-trip"
    central.EmailSendConsumer._record(approval_id, {"state": "claimed", "claimed_at": 1500.0})
    claim = central.EmailSendConsumer.journal(approval_id)
    central.EmailSendConsumer._record(approval_id, {
        **claim, "state": "sent", "message_id": "<m1@auto.network>", "finished_at": 1501.5})
    assert central.EmailSendConsumer.journal(approval_id) == {
        "state": "sent", "claimed_at": 1500.0, "message_id": "<m1@auto.network>", "finished_at": 1501.5}
    central.EmailSendConsumer._record(approval_id, {
        "state": "unknown", "claimed_at": 1500.0, "finished_at": 1502.0,
        "error": "the send was interrupted; it was not retried"})
    assert central.EmailSendConsumer.journal(approval_id)["state"] == "unknown"
