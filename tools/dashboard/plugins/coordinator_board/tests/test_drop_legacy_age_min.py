"""Migration tests for ``drop_legacy_age_min`` (bead auto-fwwfu).

Asserts the helper correctly upconverts tile / thread / sprint rows
from the now-prior ``ageMin``-bearing revision to the new revision,
strips ``ageMin`` from the stored payload, and is idempotent.

These tests live with the plugin per the operator's structural rule
(graph://f6c6c43e-24a). The main pytest ``testpaths`` does not collect
them; run explicitly with::

    pytest tools/dashboard/plugins/coordinator_board/tests/
"""
from __future__ import annotations

import importlib
import os
import tempfile

import pytest


COORD_TILE_SET_ID = "dashboard.coordinator-tile"
COORD_THREAD_SET_ID = "dashboard.coordinator-thread"
COORD_SPRINT_SET_ID = "dashboard.coordinator-sprint"


@pytest.fixture
def isolated_db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "drop-age-min.db")
        old_db = os.environ.get("GRAPH_DB")
        os.environ["GRAPH_DB"] = db_path
        importlib.import_module(
            "tools.dashboard.plugins.coordinator_board.entrypoints.schemas"
        )
        yield db_path
        if old_db is None:
            os.environ.pop("GRAPH_DB", None)
        else:
            os.environ["GRAPH_DB"] = old_db


class TestDropLegacyAgeMin:
    def _seed_tile_v2(self, key: str = "auto-peer-A", *, with_age: bool = True) -> str:
        from tools.graph import settings_ops
        payload = {
            "label": "Peer A",
            "role": "implementer",
            "thing": "Peer's framing",
            "asks": "fyi",
            "detail": {"context": "ctx", "choices": ["a", "b"]},
        }
        if with_age:
            payload["ageMin"] = 7
        return settings_ops.add_setting(
            COORD_TILE_SET_ID, 2, key, payload,
            org=settings_ops.CALLER_ORG,
        )

    def _seed_thread_v2(self, key: str = "auto-peer-A") -> str:
        from tools.graph import settings_ops
        return settings_ops.add_setting(
            COORD_THREAD_SET_ID, 2, key,
            {
                "label": "Peer A thread",
                "role": "implementer",
                "status": "blocked",
                "lead": "stuck",
                "ageMin": 12,
                "totalTurns": 33,
                "needs": "Make a call",
            },
            org=settings_ops.CALLER_ORG,
        )

    def _seed_sprint_v1(self, key: str = "sprint-X") -> str:
        from tools.graph import settings_ops
        return settings_ops.add_setting(
            COORD_SPRINT_SET_ID, 1, key,
            {
                "title": "Sprint X",
                "status": "active",
                "ageMin": 30,
                "participants": ["auto-coord-1"],
                "commitCount": 4,
            },
            org=settings_ops.CALLER_ORG,
        )

    def test_drops_age_min_from_tile_rows(self, isolated_db):
        from tools.graph import settings_ops
        from tools.dashboard.plugins.coordinator_board.entrypoints \
            import migrate as coord_migrate

        self._seed_tile_v2()
        reports = coord_migrate.drop_legacy_age_min(isolated_db)
        tile_report = next(
            r for r in reports if r.set_id == COORD_TILE_SET_ID
        )
        assert tile_report.rewritten == 1, tile_report.to_dict()
        # Resolves at v3 with no ageMin.
        result = settings_ops.read_set(
            COORD_TILE_SET_ID, target_revision=3,
            org=settings_ops.CALLER_ORG,
        )
        assert len(result.members) == 1
        payload = result.members[0].payload
        assert "ageMin" not in payload, payload
        # Other fields preserved verbatim through the upconvert.
        assert payload["label"] == "Peer A"
        assert payload["detail"] == {"context": "ctx", "choices": ["a", "b"]}

    def test_drops_age_min_from_thread_rows(self, isolated_db):
        from tools.graph import settings_ops
        from tools.dashboard.plugins.coordinator_board.entrypoints \
            import migrate as coord_migrate

        self._seed_thread_v2()
        reports = coord_migrate.drop_legacy_age_min(isolated_db)
        thread_report = next(
            r for r in reports if r.set_id == COORD_THREAD_SET_ID
        )
        assert thread_report.rewritten == 1, thread_report.to_dict()
        result = settings_ops.read_set(
            COORD_THREAD_SET_ID, target_revision=3,
            org=settings_ops.CALLER_ORG,
        )
        payload = result.members[0].payload
        assert "ageMin" not in payload
        assert payload["totalTurns"] == 33
        assert payload["needs"] == "Make a call"

    def test_drops_age_min_from_sprint_rows(self, isolated_db):
        from tools.graph import settings_ops
        from tools.dashboard.plugins.coordinator_board.entrypoints \
            import migrate as coord_migrate

        self._seed_sprint_v1()
        reports = coord_migrate.drop_legacy_age_min(isolated_db)
        sprint_report = next(
            r for r in reports if r.set_id == COORD_SPRINT_SET_ID
        )
        assert sprint_report.rewritten == 1, sprint_report.to_dict()
        result = settings_ops.read_set(
            COORD_SPRINT_SET_ID, target_revision=2,
            org=settings_ops.CALLER_ORG,
        )
        payload = result.members[0].payload
        assert "ageMin" not in payload
        assert payload["title"] == "Sprint X"
        assert payload["commitCount"] == 4

    def test_idempotent(self, isolated_db):
        from tools.dashboard.plugins.coordinator_board.entrypoints \
            import migrate as coord_migrate

        self._seed_tile_v2()
        self._seed_thread_v2()
        self._seed_sprint_v1()

        first = coord_migrate.drop_legacy_age_min(isolated_db)
        # Each set rewrote exactly one row.
        for r in first:
            assert r.rewritten == 1, r.to_dict()

        # Second pass: zero rewrites; rows already at target revision.
        second = coord_migrate.drop_legacy_age_min(isolated_db)
        for r in second:
            assert r.rewritten == 0, (
                f"migration not idempotent for {r.set_id}: {r.to_dict()}"
            )
            assert r.already_at_target == 1, r.to_dict()

    def test_dry_run_does_not_write(self, isolated_db):
        from tools.graph import settings_ops
        from tools.dashboard.plugins.coordinator_board.entrypoints \
            import migrate as coord_migrate

        self._seed_tile_v2()
        reports = coord_migrate.drop_legacy_age_min(isolated_db, dry_run=True)
        tile_report = next(
            r for r in reports if r.set_id == COORD_TILE_SET_ID
        )
        assert tile_report.rewritten == 1, tile_report.to_dict()
        # Row still at v2 with ageMin.
        result_v2 = settings_ops.read_set(
            COORD_TILE_SET_ID, target_revision=2,
            org=settings_ops.CALLER_ORG,
        )
        assert result_v2.members[0].payload.get("ageMin") == 7

    def test_skips_rows_already_at_target_revision(self, isolated_db):
        from tools.graph import settings_ops
        from tools.dashboard.plugins.coordinator_board.entrypoints \
            import migrate as coord_migrate

        # Write a tile directly at v3 (no ageMin) to simulate a row that
        # was created post-migration.
        settings_ops.add_setting(
            COORD_TILE_SET_ID, 3, "already-v3",
            {
                "label": "x", "role": "y", "thing": "z", "asks": "fyi",
            },
            org=settings_ops.CALLER_ORG,
        )
        reports = coord_migrate.drop_legacy_age_min(isolated_db)
        tile_report = next(
            r for r in reports if r.set_id == COORD_TILE_SET_ID
        )
        assert tile_report.rewritten == 0, tile_report.to_dict()
        assert tile_report.already_at_target == 1, tile_report.to_dict()

    def test_main_drop_age_min_flag(self, isolated_db, capsys):
        from tools.dashboard.plugins.coordinator_board.entrypoints \
            import migrate as coord_migrate

        self._seed_tile_v2()
        rc = coord_migrate.main([
            "--db", isolated_db, "--drop-age-min", "--dry-run",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "v2→v3" in out, out
        assert COORD_TILE_SET_ID in out
        assert "rewritten=1" in out
        assert "dry_run=True" in out
