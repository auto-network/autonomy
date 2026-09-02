from __future__ import annotations

import pytest

from tools.dashboard import service_certificate as certs
from tools.dashboard import service_certificate_manager as manager


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
    monkeypatch.setattr(certs, "certificate_metadata", lambda *_args: None)
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


@pytest.mark.asyncio
async def test_manager_exercises_staging_then_activates_production(monkeypatch):
    calls = []
    monkeypatch.setattr(certs, "certificate_metadata", lambda *_args: None)
    monkeypatch.setattr(manager, "_import_legacy_pair", lambda *_args: None)

    async def obtain(org, persona, *, staging):
        calls.append(("obtain", org, persona, staging))
        return metadata(staging=True), b"staging-cert", b"staging-key"

    async def issue(org, persona, *, staging):
        calls.append(("issue", org, persona, staging))
        return metadata(not_after=10_000)

    monkeypatch.setattr(certs, "obtain", obtain)
    monkeypatch.setattr(certs, "issue", issue)
    lifecycle = manager.ServiceCertificateManager(
        now=lambda: 1000,
        desired_fn=lambda: {("anchore", "persona-abc")},
    )

    healthy = await lifecycle.reconcile_once()

    assert calls == [
        ("obtain", "anchore", "persona-abc", True),
        ("issue", "anchore", "persona-abc", False),
    ]
    assert healthy is True


@pytest.mark.asyncio
async def test_manager_isolates_one_personas_failure(monkeypatch):
    monkeypatch.setattr(certs, "certificate_metadata", lambda *_args: None)
    monkeypatch.setattr(manager, "_import_legacy_pair", lambda *_args: None)

    async def obtain(_org, persona, *, staging):
        if persona == "persona-bad":
            raise RuntimeError("CA unavailable")
        return metadata(persona_label=persona), b"cert", b"key"

    async def issue(org, persona, *, staging):
        return metadata(org=org, persona_label=persona, not_after=10_000)

    monkeypatch.setattr(certs, "obtain", obtain)
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
