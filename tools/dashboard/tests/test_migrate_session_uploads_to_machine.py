"""The upload migration claims a machine's own rows and nothing else.

The interesting property is not "it copies rows" — it is that attribution is
decided by the FILE, since the row carries no machine identity. A tool that got
this wrong would claim every row on the first machine that ran it and leave the
other machine's uploads pointing at a store that does not have them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.dashboard import migrate_session_uploads_to_machine as mig


def _payload(session="auto-0101-000000", rel=".uploads/shot.png"):
    return {"target_session": session, "filename": "shot.png",
            "rel_path": rel, "mime": "image/png", "size": 3,
            "timestamp": "2026-01-01T00:00:00Z"}


def test_a_row_whose_file_is_here_is_claimed(tmp_path):
    run = tmp_path / "auto-0101-000000-20260101" / ".uploads"
    run.mkdir(parents=True)
    (run / "shot.png").write_bytes(b"png")

    assert mig._file_is_here(tmp_path, _payload()) == run / "shot.png"


def test_a_row_whose_file_is_elsewhere_is_left_alone(tmp_path):
    """THE ONE THAT MATTERS. This machine must not claim another machine's
    upload: the row would then live in a machine store whose disk cannot serve
    it, which is the same falsely-present tile the schema change removed."""
    (tmp_path / "auto-0101-000000").mkdir()

    assert mig._file_is_here(tmp_path, _payload()) is None


def test_the_bare_run_dir_is_matched_as_well_as_timestamped_ones(tmp_path):
    run = tmp_path / "auto-0101-000000" / ".uploads"
    run.mkdir(parents=True)
    (run / "shot.png").write_bytes(b"png")

    assert mig._file_is_here(tmp_path, _payload()) == run / "shot.png"


def test_a_similarly_named_session_is_not_matched(tmp_path):
    """`startswith` on a bare name would match `auto-0101-0000001`; the dash is
    what makes the prefix a run-dir stamp rather than a longer session name."""
    run = tmp_path / "auto-0101-0000009" / ".uploads"
    run.mkdir(parents=True)
    (run / "shot.png").write_bytes(b"png")

    assert mig._file_is_here(tmp_path, _payload()) is None


@pytest.mark.parametrize("rel", ["/etc/passwd", "../../etc/passwd",
                                 ".uploads/../../../etc/passwd"])
def test_an_escaping_rel_path_is_refused_not_resolved(tmp_path, rel):
    """`rel_path` is relative by contract. A row that violates it is refused
    here rather than resolved — this tool walks the filesystem on the strength
    of a stored string."""
    assert mig._file_is_here(tmp_path, _payload(rel=rel)) is None


def test_an_incomplete_row_is_refused(tmp_path):
    assert mig._file_is_here(tmp_path, {}) is None
    assert mig._file_is_here(tmp_path, _payload(session="")) is None
    assert mig._file_is_here(tmp_path, _payload(rel="")) is None


def test_a_missing_agent_runs_root_reports_nothing_rather_than_raising(tmp_path):
    assert mig._run_dirs(tmp_path / "absent", "auto-0101-000000") == []
