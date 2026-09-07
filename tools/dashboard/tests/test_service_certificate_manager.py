from __future__ import annotations

import asyncio

import pytest

from tools.dashboard import service_certificate as certs
from tools.dashboard import service_certificate_manager as manager
from tools.dashboard.event_bus import EventBus


def metadata(**changes):
    apex = "persona-abc.serve.auto.network"
    value = {
        "org": "anchore",
        "persona_label": "persona-abc",
        "apex": apex,
        "sans": [f"*.{apex}", apex],
        "not_before": 100,
        "not_after": 200,
        "serial": "abc123",
        "staging": False,
        "activated_at": 110,
    }
    value.update(changes)
    return value


def test_activation_writes_public_pointer_only_after_bundle_materializes(
    monkeypatch, tmp_path
):
    order = []
    candidate_cert = tmp_path / "candidate.crt"
    candidate_key = tmp_path / "candidate.key"
    candidate_cert.write_text("cert")
    candidate_key.write_text("key")
    monkeypatch.setattr(
        certs.settings_ops, "personal_delegate_audited_is_warm", lambda: True
    )
    monkeypatch.setattr(certs, "certificate_metadata", lambda *_args: None)
    monkeypatch.setattr(
        certs.settings_ops, "read_set_key", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        certs,
        "_write_bundle",
        lambda *_args: order.append("seal") or "service.tls.bundle",
    )
    monkeypatch.setattr(
        certs,
        "_read_bundle",
        lambda _key: order.append("read") or {"bundle": True},
    )
    monkeypatch.setattr(
        certs,
        "_materialize_bundle",
        lambda *_args: order.append("materialize"),
    )
    monkeypatch.setattr(certs, "_retire_old_ramfs", lambda *_args: order.append("retire"))
    monkeypatch.setattr(
        certs.settings_ops,
        "write_by_key",
        lambda *_args, **_kwargs: order.append("metadata") or "setting-id",
    )

    activated = certs.activate_pair(
        "anchore", "persona-abc", candidate_cert, candidate_key, metadata()
    )

    assert activated["vault_key"] == "service.tls.bundle"
    assert order == ["seal", "read", "materialize", "metadata", "retire"]


def test_activation_reuses_matching_existing_bundle_without_another_write(
    monkeypatch, tmp_path
):
    candidate_cert = tmp_path / "candidate.crt"
    candidate_key = tmp_path / "candidate.key"
    candidate_cert.write_text("cert")
    candidate_key.write_text("key")
    value = metadata()
    expected = {
        "fullchain_pem": "cert",
        "private_key_pem": "key",
        "org": "anchore",
        "persona_label": "persona-abc",
        "serial": "abc123",
    }
    monkeypatch.setattr(
        certs.settings_ops, "personal_delegate_audited_is_warm", lambda: True
    )
    monkeypatch.setattr(certs, "certificate_metadata", lambda *_args: None)
    monkeypatch.setattr(
        certs.settings_ops,
        "read_set_key",
        lambda *_args, **_kwargs: {"payload": {"value": "sealed"}},
    )
    monkeypatch.setattr(certs, "_read_bundle", lambda _key: expected)
    monkeypatch.setattr(
        certs, "_write_bundle", lambda *_args: pytest.fail("rewrote bundle")
    )
    monkeypatch.setattr(certs, "_materialize_bundle", lambda *_args: None)
    monkeypatch.setattr(certs, "_retire_old_ramfs", lambda *_args: None)
    monkeypatch.setattr(
        certs.settings_ops, "write_by_key", lambda *_args, **_kwargs: "id"
    )

    activated = certs.activate_pair(
        "anchore", "persona-abc", candidate_cert, candidate_key, value
    )

    assert activated["vault_key"] == "service.tls.anchore.persona-abc.abc123"


def test_legacy_import_refuses_cold_vault_before_writing(monkeypatch, tmp_path):
    status = tmp_path / "tls-status.json"
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    status.write_text(
        '{"org":"anchore","apex":"persona-abc.serve.auto.network"}'
    )
    cert_path.write_text("cert")
    key_path.write_text("key")
    monkeypatch.setattr(certs, "STATUS_PATH", status)
    monkeypatch.setattr(certs, "GATEWAY_CERT", cert_path)
    monkeypatch.setattr(certs, "GATEWAY_KEY", key_path)
    monkeypatch.setattr(
        certs.settings_ops, "personal_delegate_audited_is_warm", lambda: False
    )
    writes = []
    monkeypatch.setattr(certs, "activate_pair", lambda *_args: writes.append(True))

    with pytest.raises(certs.ServiceCertificateError, match="vault is locked"):
        manager._import_legacy_pair("anchore", "persona-abc")

    assert writes == []


@pytest.mark.asyncio
async def test_manager_first_issuance_uses_only_production(monkeypatch):
    calls = []
    monkeypatch.setattr(certs, "certificate_metadata", lambda *_args: None)
    monkeypatch.setattr(manager, "_import_legacy_pair", lambda *_args: None)

    async def issue(org, persona, *, staging):
        calls.append(("issue", org, persona, staging))
        return metadata(not_after=10_000)

    monkeypatch.setattr(certs, "issue", issue)
    lifecycle = manager.ServiceCertificateManager(
        now=lambda: 1000,
        desired_fn=lambda: {("anchore", "persona-abc")},
    )

    healthy = await lifecycle.reconcile_once()

    assert calls == [("issue", "anchore", "persona-abc", False)]
    assert healthy is True


@pytest.mark.asyncio
async def test_manager_isolates_one_personas_failure(monkeypatch):
    monkeypatch.setattr(certs, "certificate_metadata", lambda *_args: None)
    monkeypatch.setattr(manager, "_import_legacy_pair", lambda *_args: None)

    async def issue(org, persona, *, staging):
        if persona == "persona-bad":
            raise RuntimeError("CA unavailable")
        return metadata(org=org, persona_label=persona, not_after=10_000)

    monkeypatch.setattr(certs, "issue", issue)
    lifecycle = manager.ServiceCertificateManager(
        now=lambda: 1000,
        desired_fn=lambda: {
            ("anchore", "persona-bad"),
            ("anchore", "persona-good"),
        },
    )

    healthy = await lifecycle.reconcile_once()

    assert ("anchore", "persona-bad") in lifecycle.errors
    assert ("anchore", "persona-good") not in lifecycle.errors
    assert healthy is False


def test_worker_reconcile_requests_coalesce():
    worker = manager.ServiceCertificateWorker()

    worker.request_reconcile()
    worker.request_reconcile()

    assert worker._wake.is_set()


@pytest.mark.asyncio
async def test_failed_worker_retry_is_not_starved_by_unrelated_events():
    class FailingManager:
        def __init__(self):
            self.calls = 0

        async def reconcile_once(self):
            self.calls += 1
            return False

    lifecycle = FailingManager()
    worker = manager.ServiceCertificateWorker(
        lifecycle, retry_interval=0.03, check_interval=60,
    )
    bus = EventBus()
    await worker.start(bus)
    try:
        # Keep irrelevant traffic arriving faster than the retry interval. The
        # old loop reset its timeout on every event and never called again.
        for sequence in range(12):
            await asyncio.sleep(0.006)
            await bus.broadcast("unrelated", {"sequence": sequence})
        await asyncio.sleep(0.02)
        assert lifecycle.calls >= 3
    finally:
        await worker.stop()


@pytest.mark.parametrize(
    ("metadata_value", "progress", "error", "expected"),
    [
        (None, False, None, "missing"),
        (None, True, None, "issuing"),
        (metadata(not_after=10_000_000), False, None, "current"),
        (metadata(not_after=200), False, None, "renewal_due"),
        (metadata(not_after=99), False, None, "expired"),
        (None, False, "ServiceCertificateError: refused", "issuance_failed"),
    ],
)
def test_manager_reports_each_certificate_state(
    monkeypatch, metadata_value, progress, error, expected
):
    identity = ("anchore", "persona-abc")
    monkeypatch.setattr(
        certs, "certificate_metadata", lambda *_args: metadata_value
    )
    lifecycle = manager.ServiceCertificateManager(
        now=lambda: 100,
        desired_fn=lambda: {identity},
    )
    if progress:
        lifecycle.in_progress.add(identity)
    if error:
        lifecycle.errors[identity] = error

    state = lifecycle.certificate_states()[0]

    assert state["state"] == expected
    assert state["reason"]


def test_issuance_failure_reason_shows_the_cause_not_the_preamble(monkeypatch):
    """Certbot's first line is 'Requesting a certificate for …'; the cause is
    last. The operator-facing reason must show the tail and the state must
    carry the whole detail (auto-0iwrd)."""
    identity = ("autonomy", "jeremy-77827e972ba4c37d4215")
    monkeypatch.setattr(certs, "certificate_metadata", lambda *_args: None)
    lifecycle = manager.ServiceCertificateManager(
        now=lambda: 100, desired_fn=lambda: {identity},
    )
    lifecycle.errors[identity] = (
        "Certbot failed (1): Requesting a certificate for x.serve.auto.network\n"
        "Hook '--manual-auth-hook' for x reported error code 1\n"
        "Hook '--manual-auth-hook' ran with error output:\n"
        " autonomy DNS-01 hook unavailable: FileNotFoundError: [Errno 2] (socket '/run/autonomy-acme/dns01.sock', action present)\n"
        " [attempt kept at /app/data/service-certs/attempts/20260907T053700Z-x]"
    )
    state = lifecycle.certificate_states()[0]
    assert state["state"] == "issuance_failed"
    assert "hook unavailable: FileNotFoundError" in state["reason"]
    assert "attempt kept at" in state["reason"]
    assert "Requesting a certificate" not in state["reason"]
    assert state["detail"].startswith("Certbot failed (1)")


def test_rate_limited_failure_holds_until_the_named_retry_time(monkeypatch):
    """An ACME 429 names its retry time; the manager must honour it instead of
    retrying every 60 s into the limit, and say so in the reason (auto-0iwrd)."""
    import asyncio
    identity = ("autonomy", "jeremy-77827e972ba4c37d4215")
    monkeypatch.setattr(certs, "certificate_metadata", lambda *_args: None)
    monkeypatch.setattr(manager, "_import_legacy_pair", lambda *_args: None)
    calls = []

    async def issue(org, persona, *, staging=False):
        calls.append((org, persona))
        raise certs.ServiceCertificateError(
            "Certbot failed (1): urn:ietf:params:acme:error:rateLimited: too many failed "
            "authorizations (5) for x.serve.auto.network in the last 1h, "
            "retry after 2026-09-07 05:48:01 UTC"
        )

    monkeypatch.setattr(certs, "issue", issue)
    import datetime as dt
    utc = lambda s: dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()
    clock = {"now": utc("2026-09-07 05:38:00")}
    lifecycle = manager.ServiceCertificateManager(
        now=lambda: clock["now"], desired_fn=lambda: {identity},
    )
    assert asyncio.run(lifecycle.reconcile_once()) is False
    assert len(calls) == 1
    hold = lifecycle.hold_until[identity]
    assert hold == utc("2026-09-07 05:48:01") + 30.0  # named retry time + margin
    assert "next attempt at 05:48:31 UTC" in lifecycle.certificate_states()[0]["reason"]
    clock["now"] += 60
    asyncio.run(lifecycle.reconcile_once())
    assert len(calls) == 1  # held: no second call one minute later
    clock["now"] = hold + 1
    asyncio.run(lifecycle.reconcile_once())
    assert len(calls) == 2  # released after the named time


def test_failure_hold_policy():
    now = 1_000_000.0
    assert manager.failure_hold_seconds("vault is locked; unlock", 3, now) == 0.0
    assert manager.failure_hold_seconds("Certbot failed (1): hook unavailable", 1, now) == 60.0
    assert manager.failure_hold_seconds("Certbot failed (1): hook unavailable", 3, now) == 240.0
    assert manager.failure_hold_seconds("Certbot failed (1): hook unavailable", 9, now) == 900.0
