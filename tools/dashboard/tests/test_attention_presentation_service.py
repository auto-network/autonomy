"""Contract tests for the Central Attention presentation mediator."""

from __future__ import annotations

import inspect
import threading

import pytest

from tools.dashboard import attention_presentation_service as presentation
from tools.dashboard.attention_presentation_service import (
    AttentionItemRecord,
    AttentionPresentationError,
    AttentionPresentationRecord,
    AttentionPresentationService,
    SettingsAttentionItemResolver,
    SettingsAttentionPresentationStore,
)
from tools.graph.schemas.central_attention import (
    ATTENTION_ITEM_SET_ID,
    ATTENTION_PRESENTATION_SET_ID,
    CENTRAL_ATTENTION_REVISION,
)


ATTENTION_ID = "attention_01j8v7ng5h6ya"


def _item_payload():
    return {
        "application_scope": "fleet",
        "notification_class": "approval.fleet_machine_admission.requested",
        "object_ref": "approval:approval_01j8v7ng5h6ya",
        "participant_role": "recipient",
        "attention_state": "needs_attention",
        "safe_title": "Fleet approval requested",
        "safe_summary": "A new machine wants to join.",
        "occurred_at": 1000.0,
        "source_version": 1,
    }


def _reference(attention_id=ATTENTION_ID, payload=None):
    return AttentionItemRecord(attention_id, payload or _item_payload())


class MemoryStore:
    def __init__(self, initial=None):
        self.rows = {} if initial is None else {
            key: dict(value) for key, value in initial.items()
        }
        self.writes = []
        self.fail_read = False
        self.fail_write = False

    def get(self, attention_id):
        if self.fail_read:
            raise RuntimeError("read failed")
        payload = self.rows.get(attention_id)
        if payload is None:
            return None
        return AttentionPresentationRecord(attention_id, dict(payload))

    def upsert(self, attention_id, payload):
        if self.fail_write:
            raise RuntimeError("write failed")
        clean = dict(payload)
        self.rows[attention_id] = clean
        self.writes.append((attention_id, clean))
        return AttentionPresentationRecord(attention_id, dict(clean))


def _service(*, store=None, clock=lambda: 1000.0, resolver=None):
    return AttentionPresentationService(
        store=store or MemoryStore(),
        clock=clock,
        reference_resolver=resolver or (lambda attention_id: _reference(attention_id)),
    )


def test_seen_open_snooze_and_clear_merge_exact_payload():
    times = iter([1000.0, 1001.0, 1002.0, 1003.0])
    store = MemoryStore()
    service = _service(store=store, clock=lambda: next(times))

    assert service.mark_seen(ATTENTION_ID).payload == {"seen_at": 1000.0}
    assert service.mark_opened(ATTENTION_ID).payload == {
        "seen_at": 1000.0,
        "last_opened_at": 1001.0,
    }
    assert service.snooze(ATTENTION_ID, 60).payload == {
        "seen_at": 1000.0,
        "last_opened_at": 1001.0,
        "snoozed_until": 1062.0,
    }
    assert service.clear_snooze(ATTENTION_ID).payload == {
        "seen_at": 1000.0,
        "last_opened_at": 1001.0,
        "snoozed_until": 1003.0,
    }
    assert all(key == ATTENTION_ID for key, _payload in store.writes)


def test_seen_and_opened_never_rewind():
    store = MemoryStore({ATTENTION_ID: {
        "seen_at": 2000.0,
        "last_opened_at": 2001.0,
    }})
    service = _service(store=store, clock=lambda: 1000.0)
    assert service.mark_seen(ATTENTION_ID).payload["seen_at"] == 2000.0
    assert service.mark_opened(ATTENTION_ID).payload["last_opened_at"] == 2001.0


def test_two_mediator_instances_share_one_process_lock_and_preserve_fields():
    store = MemoryStore()
    first = _service(store=store, clock=lambda: 1000.0)
    second = _service(store=store, clock=lambda: 1001.0)
    barrier = threading.Barrier(2)
    failures = []

    def run(operation):
        try:
            barrier.wait()
            operation(ATTENTION_ID)
        except Exception as exc:  # pragma: no cover - assertion reports detail
            failures.append(exc)

    threads = [
        threading.Thread(target=run, args=(first.mark_seen,)),
        threading.Thread(target=run, args=(second.mark_opened,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    assert store.rows[ATTENTION_ID] == {
        "seen_at": 1000.0,
        "last_opened_at": 1001.0,
    }


@pytest.mark.parametrize("duration", [60, presentation.MAX_SNOOZE_SECONDS])
def test_snooze_accepts_exact_duration_boundaries(duration):
    answer = _service().snooze(ATTENTION_ID, duration)
    assert answer.payload == {"snoozed_until": 1000.0 + duration}


@pytest.mark.parametrize(
    "duration",
    [59, presentation.MAX_SNOOZE_SECONDS + 1, True, 60.0, "60", None],
)
def test_snooze_rejects_invalid_duration_without_reference_or_write(duration):
    store = MemoryStore()
    resolver_calls = []
    service = _service(
        store=store,
        resolver=lambda attention_id: resolver_calls.append(attention_id),
    )
    with pytest.raises(AttentionPresentationError, match="invalid_request"):
        service.snooze(ATTENTION_ID, duration)
    assert resolver_calls == []
    assert store.writes == []


@pytest.mark.parametrize(
    "clock",
    [
        lambda: True,
        lambda: "1000",
        lambda: float("nan"),
        lambda: float("inf"),
        lambda: 10**1000,
        lambda: -1.0,
        lambda: (_ for _ in ()).throw(RuntimeError("clock failed")),
    ],
)
def test_broken_clock_is_bounded_and_writes_nothing(clock):
    store = MemoryStore()
    with pytest.raises(AttentionPresentationError, match="invalid_request"):
        _service(store=store, clock=clock).mark_seen(ATTENTION_ID)
    assert store.writes == []


@pytest.mark.parametrize("attention_id", ["a", "é" * 128])
def test_attention_id_accepts_exact_utf8_byte_boundaries(attention_id):
    answer = _service().mark_seen(attention_id)
    assert answer.attention_id == attention_id


@pytest.mark.parametrize(
    "attention_id",
    ["", "a" * 257, " leading", "trailing ", "line\nbreak", "format\u200bmark", None],
)
def test_attention_id_rejects_empty_overlong_whitespace_and_unicode_controls(
    attention_id,
):
    store = MemoryStore()
    with pytest.raises(AttentionPresentationError, match="invalid_request"):
        _service(store=store).mark_seen(attention_id)
    assert store.writes == []


def test_reference_not_found_at_check_refuses_write():
    store = MemoryStore()
    with pytest.raises(AttentionPresentationError, match="not_found"):
        _service(store=store, resolver=lambda _attention_id: None).mark_seen(ATTENTION_ID)
    assert store.writes == []


@pytest.mark.parametrize(
    "resolver",
    [
        lambda _attention_id: (_ for _ in ()).throw(RuntimeError("unavailable")),
        lambda _attention_id: {"key": ATTENTION_ID, "payload": _item_payload()},
        lambda _attention_id: _reference("another-attention-id"),
        lambda _attention_id: _reference(payload={"application_scope": "fleet"}),
    ],
)
def test_malformed_wrong_key_or_invalid_reference_is_unavailable(resolver):
    store = MemoryStore()
    with pytest.raises(AttentionPresentationError, match="unavailable"):
        _service(store=store, resolver=resolver).mark_seen(ATTENTION_ID)
    assert store.writes == []


def test_store_read_write_and_current_validation_failures_are_bounded():
    read_failure = MemoryStore()
    read_failure.fail_read = True
    with pytest.raises(AttentionPresentationError, match="unavailable"):
        _service(store=read_failure).mark_seen(ATTENTION_ID)

    write_failure = MemoryStore()
    write_failure.fail_write = True
    with pytest.raises(AttentionPresentationError, match="unavailable"):
        _service(store=write_failure).mark_seen(ATTENTION_ID)

    invalid = MemoryStore({ATTENTION_ID: {"decision": "granted"}})
    with pytest.raises(AttentionPresentationError, match="invalid_request"):
        _service(store=invalid).mark_seen(ATTENTION_ID)
    assert read_failure.writes == write_failure.writes == invalid.writes == []


@pytest.mark.parametrize("field", ["seen_at", "last_opened_at"])
def test_huge_existing_seen_or_opened_timestamp_is_bounded(field):
    store = MemoryStore({ATTENTION_ID: {field: 10**1000}})
    operation = (
        _service(store=store).mark_seen
        if field == "seen_at"
        else _service(store=store).mark_opened
    )
    with pytest.raises(AttentionPresentationError, match="invalid_request"):
        operation(ATTENTION_ID)
    assert store.writes == []


def test_settings_reference_resolver_reads_only_exact_personal_item(monkeypatch):
    calls = []

    def read_set_key(set_id, key, *, org, peers):
        calls.append((set_id, key, org, peers))
        return {"key": key, "payload": _item_payload()}

    monkeypatch.setattr(presentation.settings_ops, "read_set_key", read_set_key)
    answer = SettingsAttentionItemResolver()(ATTENTION_ID)
    assert answer == _reference()
    assert calls == [(ATTENTION_ITEM_SET_ID, ATTENTION_ID, None, [])]


@pytest.mark.parametrize(
    "row",
    [
        {"key": "other", "payload": _item_payload()},
        {"key": ATTENTION_ID, "payload": {"application_scope": "fleet"}},
        {"key": ATTENTION_ID, "payload": "not-a-payload"},
        "not-a-row",
    ],
)
def test_settings_reference_resolver_rejects_malformed_rows(monkeypatch, row):
    monkeypatch.setattr(
        presentation.settings_ops, "read_set_key", lambda *_args, **_kwargs: row,
    )
    with pytest.raises(RuntimeError):
        SettingsAttentionItemResolver()(ATTENTION_ID)


def test_settings_presentation_store_uses_exact_personal_raw_key(monkeypatch):
    calls = []

    def upsert_by_key(set_id, revision, key, payload, *, org, state):
        calls.append((set_id, revision, key, dict(payload), org, state))
        return "setting-id"

    monkeypatch.setattr(presentation.settings_ops, "upsert_by_key", upsert_by_key)
    answer = SettingsAttentionPresentationStore().upsert(
        ATTENTION_ID, {"seen_at": 1000.0},
    )
    assert answer.payload == {"seen_at": 1000.0}
    assert calls == [(
        ATTENTION_PRESENTATION_SET_ID,
        CENTRAL_ATTENTION_REVISION,
        ATTENTION_ID,
        {"seen_at": 1000.0},
        None,
        "raw",
    )]


def test_module_has_no_legacy_lifecycle_delivery_or_selector_surface():
    source = inspect.getsource(presentation)
    assert "operator_dismissed" not in source
    assert "approval:changed" not in source
    assert "AttentionDelivery" not in source
    signatures = " ".join(
        str(inspect.signature(method))
        for method in (
            AttentionPresentationService.mark_seen,
            AttentionPresentationService.mark_opened,
            AttentionPresentationService.snooze,
            AttentionPresentationService.clear_snooze,
        )
    )
    for selector in (
        "operator", "audience", "org", "persona", "application", "decision", "ack",
    ):
        assert selector not in signatures
