"""A decided secured vault read wakes its requesting session by notification.

Covers the A-5 async-notify seam: the per-kind notifier spec, the deduped
best-effort deliverer, and the ``_finalize_decision`` emit that fires exactly
once when the operator's decision is committed.
"""

from __future__ import annotations

from tools.dashboard import approvals_routes
from tools.dashboard import session_notify
from tools.dashboard.vault_open_approvals import notify_session


def _approved_row():
    return {
        "id": "open-9",
        "kind": "vault_open",
        "session": "auto-test",
        "request": {"setting": {"key": "gh.token"}},
        "result": {
            "approved": True,
            "execution": {"ok": True, "receipt": {"path": "/run/secrets/gh.token"}},
        },
    }


def test_notifier_spec_carries_path_on_approval_and_never_the_value():
    spec = notify_session(_approved_row())
    assert spec["notification_id"] == "vault-open:open-9"
    assert spec["kind"] == "vault-open"
    assert spec["status"] == "released"
    assert "gh.token" in spec["summary"]
    assert "/run/secrets/gh.token" in spec["body"]


def test_notifier_spec_reports_a_declined_release():
    row = _approved_row()
    row["result"] = {"approved": False}
    spec = notify_session(row)
    assert spec["status"] == "declined"
    assert "declined" in spec["body"].lower()


def test_deliver_is_idempotent_for_one_notification_id(monkeypatch):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(session_notify, "_tmux_session_exists", lambda _s: True)
    monkeypatch.setattr(session_notify, "tmux_send_sync", lambda s, t: sent.append((s, t)))
    session_notify._WAKE_IDS.clear()

    first = session_notify.deliver_task_notification_sync(
        "auto-test", "vault-open:open-9", kind="vault-open",
        status="released", summary="Vault secret released: gh.token", body="at /run/secrets/gh.token",
    )
    second = session_notify.deliver_task_notification_sync(
        "auto-test", "vault-open:open-9", kind="vault-open",
        status="released", summary="Vault secret released: gh.token", body="at /run/secrets/gh.token",
    )
    assert first == "accepted"
    assert second == "duplicate"
    assert len(sent) == 1
    assert "<task-notification>" in sent[0][1]


def test_deliver_reports_absent_session_without_raising(monkeypatch):
    monkeypatch.setattr(session_notify, "_tmux_session_exists", lambda _s: False)
    session_notify._WAKE_IDS.clear()
    assert session_notify.deliver_task_notification_sync(
        "gone", "vault-open:x", kind="vault-open", status="released", summary="s",
    ) == "absent"


def test_finalize_decision_wakes_vault_open_requester_once(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(approvals_routes.web_push, "cancel_approval", lambda *a, **k: None)
    monkeypatch.setattr(approvals_routes.event_bus, "broadcast_sync", lambda *a, **k: None)
    monkeypatch.setattr(approvals_routes.ar, "get", lambda rid: _approved_row())
    monkeypatch.setattr(
        approvals_routes.session_notify, "deliver_task_notification_sync",
        lambda session, nid, **kw: calls.append({"session": session, "nid": nid, **kw}) or "accepted",
    )

    approvals_routes._finalize_decision("open-9", "vault_open", "auto-test")

    assert len(calls) == 1
    assert calls[0]["session"] == "auto-test"
    assert calls[0]["nid"] == "vault-open:open-9"
    assert calls[0]["summary"].startswith("Vault secret released")


def test_finalize_decision_does_not_wake_a_kind_without_a_notifier(monkeypatch):
    calls: list = []
    monkeypatch.setattr(approvals_routes.web_push, "cancel_approval", lambda *a, **k: None)
    monkeypatch.setattr(approvals_routes.event_bus, "broadcast_sync", lambda *a, **k: None)
    monkeypatch.setattr(
        approvals_routes.session_notify, "deliver_task_notification_sync",
        lambda *a, **k: calls.append(1) or "accepted",
    )
    approvals_routes._finalize_decision("x", "commit_sign", "auto-test")
    assert calls == []
