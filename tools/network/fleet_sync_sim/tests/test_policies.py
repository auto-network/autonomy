from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_sync_sim.policies import (
    EXCLUDED_SETTING_SET_IDS,
    PolicyKind,
    TABLE_POLICIES,
    audit_schema,
)


def test_current_graph_schema_is_completely_classified(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        classified = audit_schema(db.conn)
    finally:
        db.close()

    assert set(TABLE_POLICIES).issubset(classified)
    assert classified["sources"] is PolicyKind.LWW
    assert classified["note_versions"] is PolicyKind.SPECIAL
    assert classified["attachments"] is PolicyKind.EXTERNAL_BLOB
    assert classified["vault_content_bodies"] is PolicyKind.IMMUTABLE
    assert classified["vault_content_objects"] is PolicyKind.IMMUTABLE
    assert classified["vault_state_object_counts"] is PolicyKind.DERIVED
    assert classified["keycontrol_state"] is PolicyKind.IMMUTABLE
    assert classified["keycontrol_credential"] is PolicyKind.IMMUTABLE_PRUNABLE
    assert classified["keycontrol_bridge"] is PolicyKind.IMMUTABLE_PRUNABLE
    assert classified["keycontrol_pending"] is PolicyKind.LOCAL
    assert classified["orgs"] is PolicyKind.LOCAL
    assert classified["sources_fts_data"] is PolicyKind.DERIVED


def test_schema_audit_refuses_an_unclassified_durable_table(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        db.conn.execute("CREATE TABLE future_graph_state(id TEXT PRIMARY KEY)")
        with pytest.raises(ValueError, match="future_graph_state"):
            audit_schema(db.conn)
    finally:
        db.close()


def test_machine_local_columns_and_identity_rows_are_explicit() -> None:
    assert "file_path" in TABLE_POLICIES["sources"].excluded_columns
    assert "file_path" in TABLE_POLICIES["attachments"].excluded_columns
    assert TABLE_POLICIES["note_versions"].excluded_columns == ("id", "version")
    assert EXCLUDED_SETTING_SET_IDS == {
        "autonomy.identity.personal",
        "autonomy.identity.passkey",
    }
