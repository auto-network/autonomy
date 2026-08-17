"""The deployed registry reports its source provenance without making it trust."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from tools.network.registry.__main__ import _load_build_info, _load_turn_issuer
from tools.network.registry.app import create_app


REPO = Path(__file__).resolve().parents[4]
DEPLOY = REPO / "tools" / "network" / "registry" / "deploy"


def test_version_endpoint_reports_injected_build_without_cache():
    build = {
        "commit": "b" * 40,
        "dirty": True,
        "built_at": "2026-08-13T04:00:00Z",
    }
    client = TestClient(create_app(":memory:", build_info=build))
    response = client.get("/versionz")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "service": "auto.network-registry",
        **build,
    }


def test_load_build_info_accepts_exact_stamp(tmp_path):
    stamp = {
        "commit": "a" * 40,
        "dirty": False,
        "built_at": "2026-08-13T04:00:00Z",
    }
    path = tmp_path / "REVISION.json"
    path.write_text(json.dumps(stamp), encoding="utf-8")
    assert _load_build_info(str(path)) == stamp


def test_missing_or_malformed_stamp_is_explicit_unknown(tmp_path):
    unknown = {"commit": "unknown", "dirty": None, "built_at": None}
    assert _load_build_info(str(tmp_path / "missing")) == unknown
    for value in (
        {},
        {"commit": "a" * 40, "dirty": "no", "built_at": "now"},
        {"commit": "not-a-sha", "dirty": False, "built_at": "now"},
        {"commit": "a" * 40, "dirty": False, "built_at": "now", "extra": 1},
    ):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        assert _load_build_info(str(path)) == unknown


def test_deploy_writes_stamp_and_unit_consumes_it():
    script = (DEPLOY / "deploy.sh").read_text(encoding="utf-8")
    unit = (DEPLOY / "autonomy-registry.service").read_text(encoding="utf-8")
    assert "git -C \"$REPO_ROOT\" rev-parse HEAD" in script
    assert "REVISION.json.tmp" in script
    assert "mv $APP_DIR/REVISION.json.tmp $APP_DIR/REVISION.json" in script
    assert "--version-file /opt/autonomy-registry/REVISION.json" in unit
    # A SHA mismatch is diagnostic only: no connector or handshake consumes it.
    assert "REVISION.json" not in (REPO / "tools/network/relaykit/connector.py").read_text(
        encoding="utf-8"
    )


def test_registry_without_turn_credential_keeps_serving(monkeypatch):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    assert _load_turn_issuer() is None


def test_registry_discovers_turn_credential_in_systemd_directory(tmp_path, monkeypatch):
    (tmp_path / "turn-rest-secrets").write_text("a" * 64 + "\n", encoding="ascii")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    assert _load_turn_issuer() is not None
