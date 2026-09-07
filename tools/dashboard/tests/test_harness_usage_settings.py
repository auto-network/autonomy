from __future__ import annotations

import json
import sqlite3

import pytest

from tools.dashboard import server
from tools.dashboard import session_monitor
from tools.graph import ops
from tools.dashboard import harness_usage_settings as hus
from tools.dashboard.session_harness import CLAUDE_HARNESS, CODEX_HARNESS


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    # Orgs-tree hermeticity, no GRAPH_DB pin: the collector reads credentials
    # at explicit org='personal' and the server boot touches 'autonomy', both
    # of which a pin silently swallows (73bad14e) and the fail-loud resolver
    # refuses. Caller-scope writes (org=CALLER_ORG with no GRAPH_ORG) resolve
    # to the same personal.db, so the yielded path stays count-able directly.
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    db_path = orgs_dir / "personal.db"
    if not db_path.exists():
        GraphDB.create_org_db("personal", type_="personal", path=db_path).close()
    yield db_path
    GraphDB.close_all_pooled()


@pytest.fixture(autouse=True)
def _clear_harness_usage_write_cache():
    hus.clear_published_payload_cache()
    yield
    hus.clear_published_payload_cache()


def _count_setting_rows(db_path, set_id: str, key: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM settings WHERE set_id = ? AND key = ?",
            (set_id, key),
        ).fetchone()
        return int(row[0] if row else 0)
    finally:
        conn.close()


def test_normalize_claude_usage_payload_uses_org_identity():
    payload = hus.normalize_claude_usage_payload(
        bundle={
            "subscription_type": "max",
            "rate_limit_tier": "default_scale_tier",
        },
        usage_body={
            "five_hour": {
                "utilization": 78.0,
                "resets_at": "2026-05-02T20:40:00+00:00",
            },
            "seven_day": {
                "utilization": 25.0,
                "resets_at": "2026-05-05T15:00:00+00:00",
            },
        },
        org_id="478d4828-69e3-4aec-837d-ab25b5c799c4",
        updated_at="2026-05-02T19:03:00Z",
        alias="primary",
    )

    assert payload["identity_id"] == "org:478d4828-69e3-4aec-837d-ab25b5c799c4"
    assert payload["identity_label"] == "org 478d4828"
    assert payload["account_id"] == "478d4828-69e3-4aec-837d-ab25b5c799c4"
    assert payload["alias"] == "primary"
    assert payload["windows"]["short"]["used_percent"] == 78.0
    assert payload["windows"]["short"]["window_minutes"] == 300
    assert payload["windows"]["short"]["resets_at"] == 1777754400
    assert payload["windows"]["long"]["used_percent"] == 25.0
    assert payload["windows"]["long"]["window_minutes"] == 10080
    assert payload["windows"]["long"]["resets_at"] == 1777993200


def test_harness_usage_setting_visible_via_api(graph_db_env, test_client):
    payload = hus.normalize_codex_usage_payload({
        "source": "transcript",
        "updated_at": "2026-05-02T04:16:20.630Z",
        "plan_type": "pro",
        "limit_id": "codex",
        "windows": {
            "short": {
                "used_percent": 2.0,
                "window_minutes": 300,
                "resets_at": 1777709435,
            },
            "long": {
                "used_percent": 10.0,
                "window_minutes": 10080,
                "resets_at": 1777959419,
            },
        },
    })
    ops.upsert_by_key(
        hus.HARNESS_USAGE_SET_ID,
        hus.HARNESS_USAGE_SCHEMA_REVISION,
        hus.make_harness_usage_key("codex", "default"),
        payload,
     org=ops.CALLER_ORG)

    resp = test_client.get(f"/api/graph/settings/{hus.HARNESS_USAGE_SET_ID}")

    assert resp.status_code == 200
    members = resp.json()["members"]
    assert len(members) == 1
    assert members[0]["key"] == "codex:default"
    assert members[0]["payload"]["identity_label"] == "default"
    assert members[0]["payload"]["windows"]["short"]["used_percent"] == 2.0


def test_upsert_by_key_updates_existing_setting_in_place(graph_db_env):
    key = hus.make_harness_usage_key("codex", "default")
    payload = hus.normalize_codex_usage_payload({
        "source": "transcript",
        "updated_at": "2026-05-02T04:16:20.630Z",
        "plan_type": "pro",
        "limit_id": "codex",
        "windows": {
            "short": {
                "used_percent": 2.0,
                "window_minutes": 300,
                "resets_at": 1777709435,
            },
        },
    })

    first = ops.upsert_by_key(
        hus.HARNESS_USAGE_SET_ID,
        hus.HARNESS_USAGE_SCHEMA_REVISION,
        key,
        payload,
     org=ops.CALLER_ORG)
    second = ops.upsert_by_key(
        hus.HARNESS_USAGE_SET_ID,
        hus.HARNESS_USAGE_SCHEMA_REVISION,
        key,
        {**payload, "updated_at": "2026-05-02T04:17:20.630Z"},
     org=ops.CALLER_ORG)

    assert first == second
    assert _count_setting_rows(graph_db_env, hus.HARNESS_USAGE_SET_ID, key) == 1


def test_upsert_by_key_replaces_payload_in_place(graph_db_env):
    key = hus.make_harness_usage_key("codex", "default")
    payload = hus.normalize_codex_usage_payload({
        "source": "transcript",
        "updated_at": "2026-05-02T04:16:20.630Z",
        "plan_type": "pro",
        "limit_id": "codex",
        "windows": {
            "short": {
                "used_percent": 2.0,
                "window_minutes": 300,
                "resets_at": 1777709435,
            },
        },
    })

    ops.upsert_by_key(
        hus.HARNESS_USAGE_SET_ID,
        hus.HARNESS_USAGE_SCHEMA_REVISION,
        key,
        payload,
     org=ops.CALLER_ORG)
    ops.upsert_by_key(
        hus.HARNESS_USAGE_SET_ID,
        hus.HARNESS_USAGE_SCHEMA_REVISION,
        key,
        {
            **payload,
            "updated_at": "2026-05-02T04:25:20.630Z",
            "windows": {
                "short": {
                    "used_percent": 9.0,
                    "window_minutes": 300,
                    "resets_at": 1777709435,
                },
            },
        },
     org=ops.CALLER_ORG)

    assert _count_setting_rows(graph_db_env, hus.HARNESS_USAGE_SET_ID, key) == 1
    members = ops.read_set(hus.HARNESS_USAGE_SET_ID, org=ops.CALLER_ORG)
    assert members.members[0].payload["windows"]["short"]["used_percent"] == 9.0


def test_publish_codex_harness_usage_setting_writes_personal_once_when_unchanged(
    monkeypatch,
):
    row = {"harness": "codex"}
    state = {
        "kind": "rate_limits",
        "source": "transcript",
        "updated_at": "2026-05-02T21:01:00Z",
        "limit_id": "codex",
        "windows": {
            "short": {
                "used_percent": 4.0,
                "window_minutes": 300,
                "resets_at": 1777755350,
            },
        },
    }

    writes = []
    monkeypatch.setattr(
        session_monitor.graph_ops,
        "upsert_by_key",
        lambda set_id, schema_revision, key, payload, *, org, state="raw":
            writes.append({
                "set_id": set_id,
                "schema_revision": schema_revision,
                "key": key,
                "payload": payload,
                "org": org,
                "state": state,
            }) or "sid",
    )

    first = session_monitor._publish_codex_harness_usage_setting(row, state)
    second = session_monitor._publish_codex_harness_usage_setting(row, state)

    assert first is True
    assert second is False
    assert len(writes) == 1
    assert writes[0]["org"] == "personal"
    assert writes[0]["state"] == "raw"
    assert writes[0]["key"] == "codex:default"
    assert writes[0]["payload"]["windows"]["short"]["used_percent"] == 4.0


def test_publish_codex_harness_usage_setting_rewrites_changed_payload(monkeypatch):
    writes = []
    monkeypatch.setattr(
        session_monitor.graph_ops,
        "upsert_by_key",
        lambda set_id, schema_revision, key, payload, *, org, state="raw":
            writes.append(payload) or "sid",
    )
    state = {
        "kind": "rate_limits",
        "source": "transcript",
        "updated_at": "2026-05-02T21:01:00Z",
        "limit_id": "codex",
        "windows": {
            "short": {
                "used_percent": 4.0,
                "window_minutes": 300,
                "resets_at": 1777755350,
            },
        },
    }

    assert session_monitor._publish_codex_harness_usage_setting(
        {"harness": "codex"}, state,
    ) is True
    changed = {
        **state,
        "updated_at": "2026-05-02T21:02:00Z",
        "windows": {
            "short": {
                **state["windows"]["short"],
                "used_percent": 5.0,
            },
        },
    }
    assert session_monitor._publish_codex_harness_usage_setting(
        {"harness": "codex"}, changed,
    ) is True

    assert len(writes) == 2
    assert writes[1]["windows"]["short"]["used_percent"] == 5.0


def test_publish_codex_harness_usage_ignores_timestamp_only_refresh(monkeypatch):
    writes = []
    monkeypatch.setattr(
        session_monitor.graph_ops,
        "upsert_by_key",
        lambda *args, **kwargs: writes.append((args, kwargs)) or "sid",
    )
    state = {
        "kind": "rate_limits",
        "source": "transcript",
        "updated_at": "2026-05-02T21:01:00Z",
        "limit_id": "codex",
        "windows": {
            "short": {
                "used_percent": 4.0,
                "window_minutes": 300,
                "resets_at": 1777755350,
            },
        },
    }

    assert session_monitor._publish_codex_harness_usage_setting(
        {"harness": "codex"}, state,
    ) is True
    assert session_monitor._publish_codex_harness_usage_setting(
        {"harness": "codex"},
        {**state, "updated_at": "2026-05-02T21:02:00Z"},
    ) is False
    assert len(writes) == 1


def test_claude_harness_state_tracks_last_user_message_at():
    state = CLAUDE_HARNESS.extract_harness_state({
        "type": "user",
        "timestamp": "2026-05-02T21:00:00Z",
        "isSidechain": False,
        "message": {"role": "user", "content": "hello"},
    }, {})

    assert state == {"last_user_message_at": "2026-05-02T21:00:00Z"}


def test_codex_harness_state_preserves_last_user_message_at():
    state = CODEX_HARNESS.extract_harness_state({
        "type": "event_msg",
        "timestamp": "2026-05-02T21:00:00Z",
        "payload": {"type": "user_message", "message": "hello"},
    }, {})
    state = CODEX_HARNESS.extract_harness_state({
        "timestamp": "2026-05-02T21:01:00Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "limit_id": "codex",
                "primary": {
                    "used_percent": 4.0,
                    "window_minutes": 300,
                    "resets_at": 1777755350,
                },
            },
        },
    }, state)

    assert state["last_user_message_at"] == "2026-05-02T21:00:00Z"
    assert state["windows"]["short"]["used_percent"] == 4.0


def test_publish_harness_usage_snapshot_skips_codex_when_no_new_user_messages(monkeypatch):
    """Codex telemetry comes from session transcripts, so the publisher
    dedups on (session signature × latest user message) — replaying the
    same state twice produces one write.

    auto-08n3f: Claude usage is now substrate-backed and runs every
    tick unconditionally, so this dedup applies to codex only.
    """
    rows = [{
        "tmux_name": "codex-a",
        "harness": "codex",
        "harness_state": json.dumps({
            "last_user_message_at": "2026-05-02T21:00:00Z",
        }),
    }]
    writes: list[str] = []

    monkeypatch.setattr(server.dashboard_db, "get_live_sessions", lambda: rows)
    monkeypatch.setattr(
        server,
        "_collect_codex_usage_payloads",
        lambda rows, updated_at: [(
            "codex:default",
            hus.make_unavailable_usage_payload(
                harness="codex",
                identity_id="default",
                identity_label="default",
                source="transcript",
                note="test",
                updated_at=updated_at,
            ),
        )],
    )
    monkeypatch.setattr(
        server,
        "_collect_claude_usage_payloads",
        lambda rows, updated_at: [],
    )
    monkeypatch.setattr(
        server.graph_ops,
        "upsert_by_key",
        lambda set_id, schema_revision, key, payload, org=None, state="raw": writes.append(key) or "sid",
    )
    monkeypatch.setattr(server, "operator_is_idle", lambda threshold_minutes=15: False)
    server._harness_usage_last_refresh_context.clear()

    server._publish_harness_usage_snapshot()
    server._publish_harness_usage_snapshot()

    assert writes == ["codex:default"]


def test_publish_harness_usage_snapshot_collects_claude_but_writes_only_changes(
    monkeypatch,
):
    """auto-08n3f: Claude path is decoupled from live sessions, so it
    runs every tick regardless of the session-signature dedup that
    governs codex.
    """
    writes: list[str] = []

    monkeypatch.setattr(server.dashboard_db, "get_live_sessions", lambda: [])
    monkeypatch.setattr(
        server,
        "_collect_codex_usage_payloads",
        lambda rows, updated_at: [],
    )
    monkeypatch.setattr(
        server,
        "_collect_claude_usage_payloads",
        lambda rows, updated_at: [(
            "claude:org:org-X",
            hus.make_unavailable_usage_payload(
                harness="claude",
                identity_id="org:org-X",
                identity_label="org X",
                source="oauth_usage",
                note="test",
                updated_at=updated_at,
                account_id="org-X",
                alias="gmail",
            ),
        )],
    )
    monkeypatch.setattr(
        server.graph_ops,
        "upsert_by_key",
        lambda set_id, schema_revision, key, payload, org=None, state="raw": writes.append(key) or "sid",
    )
    monkeypatch.setattr(server, "operator_is_idle", lambda threshold_minutes=15: False)
    server._harness_usage_last_refresh_context.clear()

    server._publish_harness_usage_snapshot()
    server._publish_harness_usage_snapshot()

    # Substrate enumeration still runs every tick, but timestamp-only refreshes
    # do not rewrite the Personal/raw Setting.
    assert writes == ["claude:org:org-X"]


def test_operator_is_idle_delegates_to_operator_activity(monkeypatch):
    calls = []

    class _FakeOperatorActivity:
        @staticmethod
        def is_idle(*, threshold):
            calls.append(threshold)
            return True

    monkeypatch.setattr("tools.graph.surface.OperatorActivity", _FakeOperatorActivity)

    assert server.operator_is_idle(threshold_minutes=15) is True
    assert len(calls) == 1
    assert int(calls[0].total_seconds()) == 900


def test_publish_harness_usage_snapshot_skips_when_operator_is_idle(monkeypatch):
    monkeypatch.setattr(server, "operator_is_idle", lambda threshold_minutes=15: True)
    monkeypatch.setattr(
        server.dashboard_db,
        "get_live_sessions",
        lambda: (_ for _ in ()).throw(AssertionError("poller should skip before reading sessions")),
    )
    server._harness_usage_last_refresh_context.clear()

    server._publish_harness_usage_snapshot()


_CLAUDE_USAGE_BODY = {
    "five_hour": {"utilization": 12.0, "resets_at": "2026-05-04T20:00:00+00:00"},
    "seven_day": {"utilization": 40.0, "resets_at": "2026-05-10T20:00:00+00:00"},
}


def _install_credentials(graph_db_env, *, alias: str, org_uuid: str,
                         access_token: str = "fake-access") -> None:
    """Install one ``dashboard.claude.credentials`` row directly via
    ``ops.upsert_by_key`` so the substrate-backed collector can read it.

    Mirrors what ``graph claude install`` writes after a real consumer
    OAuth flow — the harness-usage poller doesn't care how the row got
    there, only that it carries the rotating bundle.
    """
    from tools.graph.schemas.claude_credentials import (
        CLAUDE_CREDENTIALS_REVISION,
        CLAUDE_CREDENTIALS_SET_ID,
    )
    ops.upsert_by_key(
        CLAUDE_CREDENTIALS_SET_ID,
        CLAUDE_CREDENTIALS_REVISION,
        org_uuid,
        {
            "alias": alias,
            "organization_name": f"{alias}-org",
            "account_email": f"{alias}@example.com",
            "access_token": access_token,
            "refresh_token": f"refresh-{alias}",
            "expires_at_ms": 9999999999999,
            "scopes": ["user:profile"],
        },
        org=ops.CALLER_ORG,
    )


def test_collect_claude_usage_writes_one_row_per_credential(graph_db_env, monkeypatch):
    """auto-08n3f: substrate-backed enumeration. Each
    ``dashboard.claude.credentials`` row produces one harness-usage row,
    keyed by the bare ``claude:org:<uuid>`` (no alias suffix)."""
    _install_credentials(graph_db_env, alias="default",
                         org_uuid="org-DEFAULT", access_token="tok-default")
    _install_credentials(graph_db_env, alias="primary",
                         org_uuid="org-PRIMARY", access_token="tok-primary")

    fetch_calls: list[str] = []

    def _fake_fetch(token):
        fetch_calls.append(token)
        return _CLAUDE_USAGE_BODY, {}

    monkeypatch.setattr(server, "_fetch_claude_oauth_usage", _fake_fetch)

    payloads = server._collect_claude_usage_payloads([], "2026-05-04T13:00:00Z")

    assert sorted(fetch_calls) == ["tok-default", "tok-primary"]
    keys = [k for k, _ in payloads]
    assert keys == sorted([
        "claude:org:org-DEFAULT",
        "claude:org:org-PRIMARY",
    ])
    by_alias = {p["alias"]: p for _, p in payloads}
    assert by_alias["default"]["account_id"] == "org-DEFAULT"
    assert by_alias["primary"]["account_id"] == "org-PRIMARY"


def test_collect_claude_usage_returns_nothing_when_no_credentials(
    graph_db_env, monkeypatch,
):
    monkeypatch.setattr(
        server,
        "_fetch_claude_oauth_usage",
        lambda token: (_ for _ in ()).throw(AssertionError("must not call OAuth")),
    )

    payloads = server._collect_claude_usage_payloads([], "2026-05-04T13:00:00Z")

    assert payloads == []


def test_collect_claude_usage_writes_unavailable_on_failure(
    graph_db_env, monkeypatch,
):
    """One credential's /usage call failing produces an
    ``unavailable``-status row for that org while other credentials
    still get an ``ok`` row."""
    _install_credentials(graph_db_env, alias="default",
                         org_uuid="org-DEFAULT", access_token="tok-default")
    _install_credentials(graph_db_env, alias="primary",
                         org_uuid="org-PRIMARY", access_token="tok-primary")

    def _fake_fetch(token):
        if token == "tok-default":
            raise RuntimeError("Claude usage API returned HTTP 401")
        return _CLAUDE_USAGE_BODY, {}

    monkeypatch.setattr(server, "_fetch_claude_oauth_usage", _fake_fetch)

    payloads = dict(
        server._collect_claude_usage_payloads([], "2026-05-04T13:00:00Z"),
    )

    assert set(payloads) == {"claude:org:org-DEFAULT", "claude:org:org-PRIMARY"}
    assert payloads["claude:org:org-DEFAULT"]["status"] == "unavailable"
    assert "HTTP 401" in (payloads["claude:org:org-DEFAULT"].get("note") or "")
    assert payloads["claude:org:org-DEFAULT"]["account_id"] == "org-DEFAULT"
    assert payloads["claude:org:org-PRIMARY"]["status"] == "ok"


def test_collect_claude_usage_no_session_dependency(graph_db_env, monkeypatch):
    """Acceptance criterion: poller works with zero live sessions.

    The substrate-backed enumeration is decoupled from
    ``tmux_sessions`` entirely, so handing in ``rows=[]`` still
    produces telemetry."""
    _install_credentials(graph_db_env, alias="default",
                         org_uuid="org-X", access_token="tok-default")
    monkeypatch.setattr(
        server,
        "_fetch_claude_oauth_usage",
        lambda token: (_CLAUDE_USAGE_BODY, {}),
    )

    payloads = server._collect_claude_usage_payloads([], "2026-05-04T13:00:00Z")

    keys = [k for k, _ in payloads]
    assert keys == ["claude:org:org-X"]


def _stored_reading(updated_at: str, *, long_resets_at: int) -> dict:
    """A persisted ok reading whose 7d window is still open."""
    return {
        "harness": "claude", "identity_id": "org:org-X", "status": "ok",
        "source": "oauth_usage", "updated_at": updated_at,
        "windows": {
            "short": {"used_percent": 74.0, "window_minutes": 300,
                      "resets_at": long_resets_at - 6 * 86400},
            "long": {"used_percent": 48.0, "window_minutes": 10080,
                     "resets_at": long_resets_at},
        },
    }


def test_collect_claude_usage_refetches_when_stored_reading_is_older_than_interval(
    graph_db_env, monkeypatch,
):
    """Regression for 2026-09-06: skipping the /usage call while the stored
    reading was merely *valid* (7d window not yet reset) froze both accounts
    at one reading for the entire week. Validity is a lower bound for the
    strip to keep showing; it is not a reason to stop polling."""
    import datetime as _dt
    import time as _time

    _install_credentials(graph_db_env, alias="default",
                         org_uuid="org-X", access_token="tok-default")
    now = int(_time.time())
    taken = _dt.datetime.fromtimestamp(now - 23 * 3600, _dt.timezone.utc)
    stored = _stored_reading(taken.strftime("%Y-%m-%dT%H:%M:%SZ"),
                             long_resets_at=now + 20 * 3600)
    assert hus.reading_still_valid(stored, now_epoch=now)
    monkeypatch.setattr(server, "_existing_usage_payload", lambda key: stored)

    fetch_calls: list[str] = []

    def _fake_fetch(token):
        fetch_calls.append(token)
        return _CLAUDE_USAGE_BODY, {}

    monkeypatch.setattr(server, "_fetch_claude_oauth_usage", _fake_fetch)

    payloads = server._collect_claude_usage_payloads([], "2026-09-07T19:00:00Z")

    assert fetch_calls == ["tok-default"]
    assert [k for k, _ in payloads] == ["claude:org:org-X"]


def test_collect_claude_usage_skips_fetch_when_stored_reading_is_fresh(
    graph_db_env, monkeypatch,
):
    """The restart-storm guard: a reading younger than one poll interval is
    reused rather than re-fetched."""
    import datetime as _dt
    import time as _time

    _install_credentials(graph_db_env, alias="default",
                         org_uuid="org-X", access_token="tok-default")
    now = int(_time.time())
    taken = _dt.datetime.fromtimestamp(now - 60, _dt.timezone.utc)
    stored = _stored_reading(taken.strftime("%Y-%m-%dT%H:%M:%SZ"),
                             long_resets_at=now + 20 * 3600)
    monkeypatch.setattr(server, "_existing_usage_payload", lambda key: stored)
    monkeypatch.setattr(
        server, "_fetch_claude_oauth_usage",
        lambda token: (_ for _ in ()).throw(AssertionError("must not call /usage")),
    )

    payloads = server._collect_claude_usage_payloads([], "2026-09-07T19:00:00Z")

    assert payloads == []


def test_reading_is_fresh_is_age_based_not_validity_based():
    now = 1_788_808_743
    recent = {"status": "ok", "updated_at": "2026-09-07T19:10:00Z",
              "windows": {"long": {"resets_at": now + 86400}}}
    old = {"status": "ok", "updated_at": "2026-09-06T19:52:30Z",
           "windows": {"long": {"resets_at": now + 86400}}}
    assert hus.reading_is_fresh(recent, max_age_seconds=900, now_epoch=now)
    assert not hus.reading_is_fresh(old, max_age_seconds=900, now_epoch=now)
    assert hus.reading_still_valid(old, now_epoch=now)
    unavailable = dict(recent, status="unavailable")
    assert not hus.reading_is_fresh(unavailable, max_age_seconds=900, now_epoch=now)
    assert not hus.reading_is_fresh(None, max_age_seconds=900, now_epoch=now)
    assert not hus.reading_is_fresh({"status": "ok"}, max_age_seconds=900, now_epoch=now)


# ── auto-10lsv: declarative schema migration ───────────────────


def test_schema_migration_declarative_field_metadata():
    """Acceptance criterion #2: ``graph set schema dashboard.harness.usage``
    prints the declared field set with descriptions."""
    schema = hus.DashboardHarnessUsageV1
    meta = schema._field_metadata
    # The full set of declared fields (no quietly-inherited ``set_id``
    # / ``schema_revision`` leakage).
    expected_fields = {
        "harness", "identity_id", "identity_label", "alias", "account_id",
        "status", "source", "updated_at", "plan_type", "tier", "limit_id",
        "limit_name", "rate_limit_reached_type", "note", "windows",
    }
    assert set(meta.keys()) == expected_fields
    # Required fields carry descriptions.
    assert meta["harness"]["required"] is True
    assert meta["harness"]["enum"] == ["claude", "codex"]
    assert "Harness this row reports for" in meta["harness"]["description"]
    # ``alias`` is optional with a None default so the launcher can omit it
    # for legacy compat paths.
    assert meta["alias"].get("required") is not True
    assert meta["alias"]["type"] == "string"
    # The exported JSON schema carries the same shape.
    exported = schema.export_json_schema()
    assert exported["set_id"] == hus.HARNESS_USAGE_SET_ID
    assert "alias" in exported["properties"]
    assert exported["properties"]["alias"]["description"]


def test_schema_migration_accepts_alias_field():
    """Acceptance criterion #7 (positive): payload with alias='primary' validates."""
    payload = hus.normalize_claude_usage_payload(
        bundle={"subscription_type": "max"},
        usage_body=_CLAUDE_USAGE_BODY,
        org_id="org-uuid-XYZ",
        updated_at="2026-05-04T13:00:00Z",
        alias="primary",
    )
    # No exception → schema accepts the new field.
    hus.DashboardHarnessUsageV1.validate(payload)
    assert payload["alias"] == "primary"


def test_schema_migration_rejects_unknown_field():
    """Acceptance criterion #7 (negative): unknown field → SchemaValidationError."""
    from tools.graph.schemas.registry import SchemaValidationError

    payload = hus.normalize_claude_usage_payload(
        bundle={"subscription_type": "max"},
        usage_body=_CLAUDE_USAGE_BODY,
        org_id="org-uuid-XYZ",
        updated_at="2026-05-04T13:00:00Z",
        alias="primary",
    )
    payload["unknown_field"] = "boom"
    with pytest.raises(SchemaValidationError):
        hus.DashboardHarnessUsageV1.validate(payload)


def test_schema_migration_rejects_missing_required_field():
    from tools.graph.schemas.registry import SchemaValidationError

    payload = hus.normalize_claude_usage_payload(
        bundle={"subscription_type": "max"},
        usage_body=_CLAUDE_USAGE_BODY,
        org_id="org-uuid-XYZ",
        updated_at="2026-05-04T13:00:00Z",
    )
    del payload["harness"]
    with pytest.raises(SchemaValidationError):
        hus.DashboardHarnessUsageV1.validate(payload)


def test_schema_migration_rejects_invalid_enum_value():
    from tools.graph.schemas.registry import SchemaValidationError

    payload = hus.normalize_claude_usage_payload(
        bundle={"subscription_type": "max"},
        usage_body=_CLAUDE_USAGE_BODY,
        org_id="org-uuid-XYZ",
        updated_at="2026-05-04T13:00:00Z",
    )
    payload["harness"] = "definitely-not-a-real-harness"
    with pytest.raises(SchemaValidationError):
        hus.DashboardHarnessUsageV1.validate(payload)


def test_schema_migration_preserves_existing_window_substructure():
    from tools.graph.schemas.registry import SchemaValidationError

    payload = hus.normalize_claude_usage_payload(
        bundle={"subscription_type": "max"},
        usage_body=_CLAUDE_USAGE_BODY,
        org_id="org-uuid-XYZ",
        updated_at="2026-05-04T13:00:00Z",
    )
    payload["windows"]["short"]["unexpected_subfield"] = "boom"
    with pytest.raises(SchemaValidationError):
        hus.DashboardHarnessUsageV1.validate(payload)
