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
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db_path


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


def test_load_claude_credential_bundle_from_credentials_json(tmp_path):
    creds = tmp_path / ".credentials.json"
    creds.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-access-token-value",
            "refreshToken": "sk-ant-oat01-refresh-token-value",
            "expiresAt": 1777766417068,
            "subscriptionType": "max",
            "rateLimitTier": "default_scale_tier",
        },
    }))

    bundle = hus.load_claude_credential_bundle(creds)

    assert bundle is not None
    assert bundle["source_kind"] == "credentials_json"
    assert bundle["access_token"] == "sk-ant-oat01-access-token-value"
    assert bundle["refresh_token"] == "sk-ant-oat01-refresh-token-value"
    assert bundle["subscription_type"] == "max"
    assert bundle["rate_limit_tier"] == "default_scale_tier"
    assert bundle["expires_at_ms"] == 1777766417068
    assert bundle["fingerprint"] == hus.fingerprint_secret(
        "sk-ant-oat01-refresh-token-value",
    )


def test_candidate_claude_credential_paths_walks_up_to_run_dir(tmp_path):
    run_dir = tmp_path / "agent-run"
    resolution_dir = run_dir / "sessions" / "uuid-1234"
    resolution_dir.mkdir(parents=True)
    row = {"resolution_dir": str(resolution_dir)}

    candidates = hus.candidate_claude_credential_paths(row)

    assert run_dir / ".credentials.json" in candidates
    assert run_dir / ".setup-token" in candidates
    assert resolution_dir / ".credentials.json" in candidates


def test_normalize_claude_usage_payload_uses_org_identity():
    payload = hus.normalize_claude_usage_payload(
        bundle={
            "fingerprint": "abc123def456",
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
    )

    assert payload["identity_id"] == "org:478d4828-69e3-4aec-837d-ab25b5c799c4"
    assert payload["identity_label"] == "org 478d4828"
    assert payload["account_id"] == "478d4828-69e3-4aec-837d-ab25b5c799c4"
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


def test_publish_codex_harness_usage_setting_writes_directly(graph_db_env):
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

    wrote = session_monitor._publish_codex_harness_usage_setting(row, state)

    assert wrote is True
    members = ops.read_set(hus.HARNESS_USAGE_SET_ID, org=ops.CALLER_ORG)
    assert len(members.members) == 1
    assert members.members[0].key == "codex:default"
    assert members.members[0].payload["windows"]["short"]["used_percent"] == 4.0


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


def test_publish_harness_usage_snapshot_skips_when_no_new_user_messages(monkeypatch):
    rows = [{
        "tmux_name": "claude-a",
        "harness": "claude",
        "harness_state": json.dumps({
            "last_user_message_at": "2026-05-02T21:00:00Z",
        }),
    }]
    writes: list[str] = []

    monkeypatch.setattr(server.dashboard_db, "get_live_sessions", lambda: rows)
    monkeypatch.setattr(
        server,
        "_collect_codex_usage_payloads",
        lambda rows, updated_at: [],
    )
    monkeypatch.setattr(
        server,
        "_collect_claude_usage_payloads",
        lambda rows, updated_at: [(
            "claude:test",
            hus.make_unavailable_usage_payload(
                harness="claude",
                identity_id="test",
                identity_label="test",
                source="oauth_usage",
                note="test",
                updated_at=updated_at,
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

    assert writes == ["claude:test"]


def test_operator_is_idle_delegates_to_presence(monkeypatch):
    calls = []

    class _FakePresence:
        @staticmethod
        def is_idle(*, threshold):
            calls.append(threshold)
            return True

    monkeypatch.setattr("tools.graph.surface.Presence", _FakePresence)

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


def _claude_bundle(fingerprint: str, *, expires_at_ms: int | None) -> dict:
    return {
        "source_kind": "credentials_json",
        "path": f"/fake/{fingerprint}/.credentials.json",
        "access_token": f"access-{fingerprint}",
        "refresh_token": f"refresh-{fingerprint}",
        "fingerprint": fingerprint,
        "subscription_type": "max",
        "rate_limit_tier": "default_scale_tier",
        "expires_at_ms": expires_at_ms,
    }


_CLAUDE_USAGE_BODY = {
    "five_hour": {"utilization": 12.0, "resets_at": "2026-05-04T20:00:00+00:00"},
    "seven_day": {"utilization": 40.0, "resets_at": "2026-05-10T20:00:00+00:00"},
}


def test_collect_claude_usage_uses_first_live_bundle_then_stops(monkeypatch):
    # Two live bundles for the same account → only ONE /usage call,
    # one org row, no acct rows.
    bundles = [
        _claude_bundle("fp-A", expires_at_ms=2_000_000_000_000),
        _claude_bundle("fp-B", expires_at_ms=2_000_000_000_000),
    ]
    rows = [
        {"tmux_name": f"claude-{i}", "harness": "claude"}
        for i in range(len(bundles))
    ]

    monkeypatch.setattr(
        server,
        "_resolve_claude_credential_bundle",
        lambda row: bundles[int(row["tmux_name"].split("-")[1])],
    )
    fetch_calls: list[str] = []

    def _fake_fetch(token):
        fetch_calls.append(token)
        return _CLAUDE_USAGE_BODY, {"anthropic-organization-id": "org-uuid-XYZ"}

    monkeypatch.setattr(server, "_fetch_claude_oauth_usage", _fake_fetch)

    payloads = server._collect_claude_usage_payloads(rows, "2026-05-04T13:00:00Z")

    assert len(fetch_calls) == 1
    assert fetch_calls[0] == "access-fp-A"
    keys = [k for k, _ in payloads]
    assert keys == ["claude:org:org-uuid-XYZ"]


def test_collect_claude_usage_skips_expired_bundles_silently(monkeypatch):
    # First bundle is expired → skipped (no acct row written), second is
    # used. Net: one org row, no acct rows for the expired fingerprint.
    bundles = [
        _claude_bundle("fp-EXPIRED", expires_at_ms=1),
        _claude_bundle("fp-LIVE", expires_at_ms=2_000_000_000_000),
    ]
    rows = [
        {"tmux_name": f"claude-{i}", "harness": "claude"}
        for i in range(len(bundles))
    ]
    monkeypatch.setattr(
        server,
        "_resolve_claude_credential_bundle",
        lambda row: bundles[int(row["tmux_name"].split("-")[1])],
    )

    fetch_tokens: list[str] = []

    def _fake_fetch(token):
        fetch_tokens.append(token)
        return _CLAUDE_USAGE_BODY, {"anthropic-organization-id": "org-uuid-XYZ"}

    monkeypatch.setattr(server, "_fetch_claude_oauth_usage", _fake_fetch)

    payloads = server._collect_claude_usage_payloads(rows, "2026-05-04T13:00:00Z")

    assert fetch_tokens == ["access-fp-LIVE"]
    keys = [k for k, _ in payloads]
    assert keys == ["claude:org:org-uuid-XYZ"]
    assert not any(k.startswith("claude:acct:") for k in keys)


def test_collect_claude_usage_writes_nothing_when_all_bundles_expired(monkeypatch):
    bundles = [_claude_bundle("fp-A", expires_at_ms=1)]
    rows = [{"tmux_name": "claude-0", "harness": "claude"}]
    monkeypatch.setattr(
        server, "_resolve_claude_credential_bundle", lambda row: bundles[0],
    )
    monkeypatch.setattr(
        server,
        "_fetch_claude_oauth_usage",
        lambda token: (_ for _ in ()).throw(AssertionError("must not call OAuth")),
    )

    payloads = server._collect_claude_usage_payloads(rows, "2026-05-04T13:00:00Z")

    # No row written: prior org row keeps its last-known-good telemetry.
    assert payloads == []


def test_collect_claude_usage_swallows_oauth_errors_without_acct_row(monkeypatch):
    bundles = [_claude_bundle("fp-A", expires_at_ms=2_000_000_000_000)]
    rows = [{"tmux_name": "claude-0", "harness": "claude"}]
    monkeypatch.setattr(
        server, "_resolve_claude_credential_bundle", lambda row: bundles[0],
    )

    def _boom(token):
        raise RuntimeError("Claude usage API returned HTTP 401")

    monkeypatch.setattr(server, "_fetch_claude_oauth_usage", _boom)

    payloads = server._collect_claude_usage_payloads(rows, "2026-05-04T13:00:00Z")

    assert payloads == []


def test_collect_claude_usage_writes_unresolved_only_when_no_bundle_resolves(monkeypatch):
    rows = [{"tmux_name": "claude-0", "harness": "claude"}]
    monkeypatch.setattr(server, "_resolve_claude_credential_bundle", lambda row: None)
    monkeypatch.setattr(
        server,
        "_fetch_claude_oauth_usage",
        lambda token: (_ for _ in ()).throw(AssertionError("must not call OAuth")),
    )

    payloads = server._collect_claude_usage_payloads(rows, "2026-05-04T13:00:00Z")

    keys = [k for k, _ in payloads]
    assert keys == ["claude:unresolved"]


def test_collect_claude_usage_does_not_write_unresolved_when_bundle_succeeds(monkeypatch):
    # Mix: one row resolves to a live bundle, one row doesn't. Successful
    # /usage call covers the account; unresolved row is suppressed.
    bundle = _claude_bundle("fp-LIVE", expires_at_ms=2_000_000_000_000)
    rows = [
        {"tmux_name": "claude-resolves", "harness": "claude"},
        {"tmux_name": "claude-no-creds", "harness": "claude"},
    ]
    monkeypatch.setattr(
        server,
        "_resolve_claude_credential_bundle",
        lambda row: bundle if row["tmux_name"] == "claude-resolves" else None,
    )
    monkeypatch.setattr(
        server,
        "_fetch_claude_oauth_usage",
        lambda token: (_CLAUDE_USAGE_BODY, {"anthropic-organization-id": "org-uuid-XYZ"}),
    )

    payloads = server._collect_claude_usage_payloads(rows, "2026-05-04T13:00:00Z")

    keys = [k for k, _ in payloads]
    assert keys == ["claude:org:org-uuid-XYZ"]
    assert "claude:unresolved" not in keys
