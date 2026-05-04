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
    # auto-10lsv: fingerprint dropped — identity is now keyed by org uuid
    # plus the operator-facing alias from the filename, not a hash of the
    # refresh token.
    assert "fingerprint" not in bundle


def test_load_claude_credential_bundle_from_setup_token_file(tmp_path):
    primary = tmp_path / ".setup-token.primary"
    primary.write_text("sk-ant-oat01-primary-token\n")

    bundle = hus.load_claude_credential_bundle(primary)

    assert bundle is not None
    assert bundle["source_kind"] == "setup_token"
    assert bundle["access_token"] == "sk-ant-oat01-primary-token"
    assert "fingerprint" not in bundle


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


_CLAUDE_USAGE_BODY = {
    "five_hour": {"utilization": 12.0, "resets_at": "2026-05-04T20:00:00+00:00"},
    "seven_day": {"utilization": 40.0, "resets_at": "2026-05-10T20:00:00+00:00"},
}


def _seed_token_files(creds_dir, monkeypatch, files: dict[str, str]) -> None:
    """Drop ``.setup-token*`` files into ``creds_dir`` and point the
    launcher at it. ``files`` maps filename → token text."""
    creds_dir.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (creds_dir / name).write_text(body)
    monkeypatch.setenv("CLAUDE_CREDENTIALS_DIR", str(creds_dir))


def test_collect_claude_usage_writes_one_row_per_token_file(tmp_path, monkeypatch):
    """auto-10lsv: each ``.setup-token*`` produces its own row, keyed by
    org+alias. Different orgs → distinct ``claude:org:<uuid>:<alias>`` keys."""
    _seed_token_files(tmp_path, monkeypatch, {
        ".setup-token": "tok-default",
        ".setup-token.primary": "tok-primary",
    })

    fetch_calls: list[str] = []

    def _fake_fetch(token):
        fetch_calls.append(token)
        # Distinct org per token so the rows don't collapse onto each
        # other — matches the operator's "two different accounts" case.
        org = "org-DEFAULT" if token == "tok-default" else "org-PRIMARY"
        return _CLAUDE_USAGE_BODY, {"anthropic-organization-id": org}

    monkeypatch.setattr(server, "_fetch_claude_oauth_usage", _fake_fetch)

    payloads = server._collect_claude_usage_payloads([], "2026-05-04T13:00:00Z")

    assert sorted(fetch_calls) == ["tok-default", "tok-primary"]
    keys = [k for k, _ in payloads]
    assert keys == sorted([
        "claude:org:org-DEFAULT:default",
        "claude:org:org-PRIMARY:primary",
    ])
    by_alias = {p["alias"]: p for _, p in payloads}
    assert by_alias["default"]["account_id"] == "org-DEFAULT"
    assert by_alias["primary"]["account_id"] == "org-PRIMARY"


def test_collect_claude_usage_returns_nothing_when_no_token_files(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CREDENTIALS_DIR", str(tmp_path))  # exists but empty
    monkeypatch.setattr(
        server,
        "_fetch_claude_oauth_usage",
        lambda token: (_ for _ in ()).throw(AssertionError("must not call OAuth")),
    )

    payloads = server._collect_claude_usage_payloads([], "2026-05-04T13:00:00Z")

    assert payloads == []


def test_collect_claude_usage_swallows_oauth_errors_per_token(tmp_path, monkeypatch):
    """One token's /usage call exploding doesn't suppress the other token's row."""
    _seed_token_files(tmp_path, monkeypatch, {
        ".setup-token": "tok-default",
        ".setup-token.primary": "tok-primary",
    })

    def _fake_fetch(token):
        if token == "tok-default":
            raise RuntimeError("Claude usage API returned HTTP 401")
        return _CLAUDE_USAGE_BODY, {"anthropic-organization-id": "org-uuid-PRIMARY"}

    monkeypatch.setattr(server, "_fetch_claude_oauth_usage", _fake_fetch)

    payloads = server._collect_claude_usage_payloads([], "2026-05-04T13:00:00Z")

    keys = [k for k, _ in payloads]
    assert keys == ["claude:org:org-uuid-PRIMARY:primary"]


def test_collect_claude_usage_skips_when_org_header_missing(tmp_path, monkeypatch):
    """No org id in /usage response → no row (org id is required)."""
    _seed_token_files(tmp_path, monkeypatch, {".setup-token": "tok-default"})
    monkeypatch.setattr(
        server, "_fetch_claude_oauth_usage", lambda token: (_CLAUDE_USAGE_BODY, {}),
    )

    payloads = server._collect_claude_usage_payloads([], "2026-05-04T13:00:00Z")

    assert payloads == []


def test_collect_claude_usage_no_walk_up_dependency(tmp_path, monkeypatch):
    """Acceptance criterion #6: poller works regardless of session resolution_dir.

    With two ``.setup-token*`` files on disk and zero live sessions on the
    dashboard (rows=[]), the poller still produces telemetry."""
    _seed_token_files(tmp_path, monkeypatch, {
        ".setup-token": "tok-default",
    })
    monkeypatch.setattr(
        server,
        "_fetch_claude_oauth_usage",
        lambda token: (_CLAUDE_USAGE_BODY, {"anthropic-organization-id": "org-X"}),
    )

    payloads = server._collect_claude_usage_payloads([], "2026-05-04T13:00:00Z")

    keys = [k for k, _ in payloads]
    assert keys == ["claude:org:org-X:default"]


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

