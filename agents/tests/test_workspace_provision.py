"""resolve_provision / materialize_startup_script — the launcher's read
of ``autonomy.workspace.provision#1``.

The contract under test: org row is the shared definition, a
personal-store row shadows it per field via the deliberate second read
(never federation), the materialized script wins over the legacy
repo-relative path field, and no rows means the legacy behavior exactly.
"""

from __future__ import annotations

from types import SimpleNamespace

from agents import workspace_settings


def _stub_rows(monkeypatch, rows: dict):
    """rows maps store-org -> payload dict (or None for no row)."""
    calls: list[tuple[str, str]] = []

    def read_set_key(set_id, key, *, org, peers):
        assert set_id == workspace_settings.PROVISION_SET_ID
        assert peers == [], "provision reads must never federate"
        calls.append((org, key))
        payload = rows.get(org)
        return {"key": key, "payload": payload} if payload is not None else None

    monkeypatch.setattr(workspace_settings.ops, "read_set_key", read_set_key)
    return calls


def _proj(**kw):
    return SimpleNamespace(
        id=kw.get("id", "ai-recon"),
        graph_project=kw.get("graph_project", "autonomy"),
        startup=kw.get("startup"),
    )


def test_org_row_alone(monkeypatch):
    _stub_rows(monkeypatch, {"autonomy": {"startup_script": "echo org"}})
    merged = workspace_settings.resolve_provision("ai-recon", org="autonomy")
    assert merged == {"startup_script": "echo org"}


def test_personal_row_shadows_per_field(monkeypatch):
    _stub_rows(monkeypatch, {
        "autonomy": {"startup_script": "echo org", "dockerfile": "FROM a"},
        "personal": {"startup_script": "echo mine"},
    })
    merged = workspace_settings.resolve_provision("ai-recon", org="autonomy")
    # present personal field wins; absent field falls through to the org's
    assert merged["startup_script"] == "echo mine"
    assert merged["dockerfile"] == "FROM a"


def test_personal_org_reads_once(monkeypatch):
    calls = _stub_rows(monkeypatch, {"personal": {"startup_script": "x"}})
    workspace_settings.resolve_provision("mine", org="personal")
    assert calls == [("personal", "mine")]


def test_materialize_writes_run_dir_and_wins_over_path(monkeypatch, tmp_path):
    _stub_rows(monkeypatch, {"autonomy": {"startup_script": "#!/bin/bash\nhi"}})
    proj = _proj(startup="agents/projects/ai-recon/startup.sh")
    path = workspace_settings.materialize_startup_script(
        proj, tmp_path, repo_root=tmp_path / "repo")
    assert path == tmp_path / "startup.sh"
    assert path.read_text() == "#!/bin/bash\nhi"
    assert path.stat().st_mode & 0o111, "materialized script is executable"


def test_no_rows_falls_back_to_legacy_path(monkeypatch, tmp_path):
    _stub_rows(monkeypatch, {})
    proj = _proj(startup="agents/projects/legacy/startup.sh")
    path = workspace_settings.materialize_startup_script(
        proj, tmp_path, repo_root=tmp_path / "repo")
    assert path == tmp_path / "repo" / "agents/projects/legacy/startup.sh"
    assert not (tmp_path / "startup.sh").exists()


def test_no_rows_no_path_is_none(monkeypatch, tmp_path):
    _stub_rows(monkeypatch, {})
    assert workspace_settings.materialize_startup_script(
        _proj(), tmp_path, repo_root=tmp_path) is None
