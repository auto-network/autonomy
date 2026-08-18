"""Contract tests for the explicit production-TURN acceptance runner."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from deploy.harness.external_turn_fixture import FixtureError, setup
from deploy.harness.production_turn import (
    ProductionTurnConfig,
    ProductionTurnError,
    ProductionTurnHarness,
    _candidate_types,
)


REPO = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_endpoints_are_explicit_secure_and_payload_crosses_record_boundary(tmp_path):
    good = ProductionTurnConfig(
        registry_url="https://registry.example",
        relay_url="wss://relay.example",
        artifacts_dir=tmp_path,
    )
    good.validate()
    assert good.link_base_url == "https://relay.example"
    assert good.connector_url == "wss://registry.example"

    for config, message in (
        (
            ProductionTurnConfig("http://registry.example", "wss://relay.example"),
            "HTTPS",
        ),
        (
            ProductionTurnConfig("https://registry.example", "ws://relay.example"),
            "WSS",
        ),
        (
            ProductionTurnConfig(
                "https://registry.example", "wss://relay.example", ttl=3599
            ),
            "3600",
        ),
        (
            ProductionTurnConfig(
                "https://registry.example",
                "wss://relay.example",
                site_bytes=60 * 1024,
            ),
            "60 KiB",
        ),
    ):
        with pytest.raises(ProductionTurnError, match=message):
            config.validate()


def test_fixture_rejects_implicit_or_incomplete_production_setup():
    with pytest.raises(FixtureError, match="exactly"):
        setup({"slug": "missing-everything-else"})


def test_fixture_uses_real_registry_registration_publish_and_revoke(tmp_path):
    port = _free_port()
    registry = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tools.network.registry",
            "--db",
            str(tmp_path / "registry.db"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--base-url",
            "https://relay.fixture.invalid",
        ],
        cwd=REPO,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    root = tmp_path / "node"
    root.mkdir()
    slug = "turnfixture"
    password = "fixture-password"
    env = {
        **os.environ,
        "AUTONOMY_DATA_ROOT": str(root),
        "AUTONOMY_REFUSE_REAL_DATA_FALLBACK": "1",
        "MISSION_CONTROL_DB": str(root / "mission_control.db"),
        "GRAPH_ORG": slug,
        "PYTHONPATH": str(REPO),
    }

    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=1
                ) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("temporary Registry did not start")

        def fixture(command: str, payload: dict) -> dict:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "deploy.harness.external_turn_fixture",
                    command,
                ],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                env=env,
                timeout=30,
                check=False,
            )
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout.strip().splitlines()[-1])

        state = fixture(
            "setup",
            {
                "slug": slug,
                "password": password,
                "registry_url": f"http://127.0.0.1:{port}",
                "link_base_url": "https://relay.fixture.invalid",
                "ttl": 3600,
                "site_bytes": 61 * 1024,
            },
        )
        assert state["slug"] == slug
        assert len(state["token"]) == 32
        assert Path(state["key_file"]).stat().st_mode & 0o777 == 0o600
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/v1/links/{state['token']}/envelope"
        ) as response:
            assert response.status == 200

        assert fixture(
            "revoke",
            {
                "slug": slug,
                "password": password,
                "registry_url": f"http://127.0.0.1:{port}",
                "token": state["token"],
            },
        ) == {"revoked": True}
        with pytest.raises(urllib.error.HTTPError) as revoked:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v1/links/{state['token']}/envelope"
            )
        assert revoked.value.code == 404
    finally:
        registry.terminate()
        with contextlib.suppress(Exception):
            registry.wait(timeout=5)


def test_selected_pair_evidence_requires_both_relay_candidates():
    class Candidate:
        def __init__(self, kind):
            self.type = kind

    class Pair:
        local_candidate = Candidate("relay")
        remote_candidate = Candidate("relay")

    class Connection:
        nominated = {"data": Pair()}

    class Ice:
        _connection = Connection()

    class Dtls:
        transport = Ice()

    class Sctp:
        transport = Dtls()

    class Peer:
        sctp = Sctp()

    assert _candidate_types(Peer()) == ("relay", "relay")


def test_durable_evidence_contains_no_runtime_secret_fields(tmp_path, monkeypatch):
    harness = ProductionTurnHarness(
        ProductionTurnConfig(
            "https://registry.example",
            "wss://relay.example",
            artifacts_dir=tmp_path,
        ),
        announce=lambda _message: None,
    )
    nodes = [object(), object()]
    monkeypatch.setattr(harness, "_start_node", lambda _ordinal: nodes.pop(0))

    async def prove(_first, _second):
        harness._evidence["directions"] = [
            {
                "direction": "node-b-to-node-a",
                "policy": "relay_only",
                "local_candidate": "relay",
                "remote_candidate": "relay",
                "artifact_bytes": 70_001,
                "feed": "verified",
                "post_feed": "ok",
            },
            {
                "direction": "node-a-to-node-b",
                "policy": "relay_only",
                "local_candidate": "relay",
                "remote_candidate": "relay",
                "artifact_bytes": 70_001,
                "feed": "verified",
                "post_feed": "ok",
            },
        ]
        harness._evidence["renewal"] = {
            "make_before_break": True,
            "post_break": "ok",
        }

    cleaned = []
    monkeypatch.setattr(harness, "_preflight", lambda: None)
    monkeypatch.setattr(harness, "_run_network", prove)
    def cleanup(*, strict):
        cleaned.append(strict)
        return {
            "links_revoked": 2,
            "connectors_stopped": 2,
            "secret_roots_removed": True,
            "state_retained": False,
            "errors": [],
        }

    monkeypatch.setattr(harness, "_cleanup", cleanup)

    evidence = harness.run()
    persisted = json.loads(
        (tmp_path / harness.run_id / "evidence.json").read_text(encoding="utf-8")
    )
    assert evidence == persisted
    assert cleaned == [True]
    assert persisted["cleanup"] == {
        "links_revoked": 2,
        "connectors_stopped": 2,
        "secret_roots_removed": True,
        "state_retained": False,
        "errors": [],
    }
    text = json.dumps(persisted, sort_keys=True)
    for forbidden in ("token", "password", "credential", "private", "stream_key"):
        assert forbidden not in text


def test_missing_native_runtime_fails_before_artifact_or_registry_work(
    tmp_path, monkeypatch
):
    from tools.network.relaykit import aiortc_responder

    harness = ProductionTurnHarness(
        ProductionTurnConfig(
            "https://registry.example",
            "wss://relay.example",
            artifacts_dir=tmp_path,
        )
    )

    def missing():
        raise RuntimeError("not installed")

    monkeypatch.setattr(aiortc_responder, "load_aiortc_modules", missing)
    with pytest.raises(ProductionTurnError, match="deploy/requirements.txt"):
        harness.run()
    assert not (tmp_path / harness.run_id).exists()


def test_fixture_and_runner_never_embed_live_production_hosts():
    root = Path(__file__).resolve().parents[2]
    source = (root / "deploy" / "harness" / "production_turn.py").read_text(
        encoding="utf-8"
    )
    fixture = (root / "deploy" / "harness" / "external_turn_fixture.py").read_text(
        encoding="utf-8"
    )
    assert "registry.auto.network" not in source + fixture
    assert "relay.auto.network" not in source + fixture
