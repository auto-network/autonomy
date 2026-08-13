"""Headless validation for the B6 production-path Docker harness.

Docker itself is intentionally not faked into a green acceptance.  These tests
prove the orchestration model, phase sequence, secret boundary, fixture's real
crypto/ledger state, and teardown scope.  Linux CI runs the public command for
the genuine container acceptance.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path

import pytest

from deploy.harness import driver
from deploy.harness.driver import CommandResult, Harness, HarnessConfig
from deploy.harness.fixture_ops import found_node, seed_registry
from deploy.harness.topology import (
    TopologyConfig,
    TopologyError,
    compose_model,
    write_compose,
)
from tools.data_paths import REFUSE_REAL_DATA_FALLBACK_ENV, STORE_MANIFEST


PROJECT = "autonomy-harness-test"


def test_compose_topology_is_manifest_rooted_and_has_no_host_data_mounts(tmp_path):
    config = TopologyConfig(
        project=PROJECT,
        nodes=5,
        relay_port=20477,
        node_port_base=20880,
    )
    model = compose_model(config)

    # Five counted siblings plus the fresh restore target C and the relay.
    assert sorted(model["services"]) == [
        "node-3", "node-4", "node-5", "node-a", "node-b", "node-c", "relay",
    ]
    # internal:false since ee85b1c5 — an internal bridge blocked host port
    # publishing on the real-Docker run, timing out every node wait.
    assert model["networks"]["harness"]["internal"] is False
    assert len(model["volumes"]) == 8  # registry + artifacts + A/B/C + 3 peers

    expected_envs = {
        store.env for store in STORE_MANIFEST if store.env and store.key != "graph"
    }
    for name, service in model["services"].items():
        rendered = json.dumps(service, sort_keys=True)
        assert "/var/run/docker.sock" not in rendered
        if name == "relay":
            assert service["volumes"] == [f"{PROJECT}-registry:/registry"]
            continue
        environment = service["environment"]
        assert environment[REFUSE_REAL_DATA_FALLBACK_ENV] == "1"
        assert expected_envs <= set(environment)
        # The single-database test pin must never ship on a node: it
        # collapses every settings scope into one file while the ledger and
        # open_org_db stay per-org, so the join channel and note serving
        # both refuse (auto-sb0g8).
        assert "GRAPH_DB" not in environment
        for store in STORE_MANIFEST:
            if store.env and store.key != "graph":
                assert environment[store.env] == f"/app/data/{store.relative}"
        for mount in service["volumes"]:
            source, target = mount.split(":", 1)
            assert source.startswith(PROJECT + "-")
            assert not source.startswith(("/", ".", "~"))
            assert target in {"/app/data", "/artifacts"}

    compose_path = write_compose(tmp_path / "compose.json", config)
    text = compose_path.read_text()
    assert "AUTONOMY_HARNESS_INVITE_B" in text
    assert "claim_token" not in text
    assert "harness-personal-password" in text  # path only, never the value


@pytest.mark.parametrize(
    "kwargs",
    [
        {"project": "unsafe"},
        {"project": PROJECT, "nodes": 2},
        {"project": PROJECT, "relay_port": 18880, "node_port_base": 18880},
        {"project": PROJECT, "node_port_base": 70000},
    ],
)
def test_topology_refuses_unsafe_scope(kwargs):
    with pytest.raises(TopologyError):
        compose_model(TopologyConfig(**kwargs))


def test_digest_image_mode_has_no_implicit_build_path():
    model = compose_model(
        TopologyConfig(
            project=PROJECT,
            image="registry.example/autonomy@sha256:" + "a" * 64,
            from_source=False,
        )
    )
    assert all("build" not in service for service in model["services"].values())


class FakeDocker:
    """A command-level double; it never stands in for the transport."""

    def __init__(self):
        self.calls: list[dict] = []
        self.finalized = False
        self._restart_b = 0

    def run(self, argv, *, env=None, input_text=None, check=True):
        self.calls.append(
            {"argv": list(argv), "env": dict(env or {}), "input": input_text}
        )
        joined = " ".join(argv)
        if "restart node-b" in joined:
            self._restart_b += 1
            self.finalized = True
        if "fixture_ops found" in joined:
            value = {
                "org": "demo",
                "org_uuid": "11111111-1111-4111-8111-111111111111",
                "root_pub": "a" * 64,
                "personal_root_pub": "b" * 64,
                "genesis_id": "c" * 64,
                "founder_persona_pub": "d" * 64,
                "invite_ref": "e" * 64,
                "invite_expiry": 4_000_000_000_000,
                "content_id": "22222222-2222-4222-8222-222222222222",
                "serve_delegate_pub": "f" * 64,
                "serve_delegate_sha256": "1" * 64,
            }
            return CommandResult(json.dumps(value) + "\n")
        if "fixture_ops seed-registry" in joined:
            request = json.loads(input_text)
            return CommandResult(json.dumps({
                "seeded": True,
                "join_url": (
                    "https://relay.harness.invalid/l/"
                    + request["join_channel_token"]
                ),
            }) + "\n")
        if "fixture_ops pending" in joined:
            if self.finalized:
                return CommandResult('{"state":"not-single","count":0}\n')
            value = {
                "state": "pending",
                "org": "11111111-1111-4111-8111-111111111111",
                "invite_ref": "e" * 64,
                "persona_pub": "2" * 64,
                "claim_key": "3" * 64,
                "have": 0,
                "need": 2,
            }
            return CommandResult(json.dumps(value) + "\n")
        if "fixture_ops approvals" in joined:
            return CommandResult(json.dumps({
                "approvals": [
                    {"key": "4" * 64, "sig": "5" * 128},
                    {"key": "6" * 64, "sig": "7" * 128},
                ]
            }) + "\n")
        if "fixture_ops inspect" in joined:
            return CommandResult(json.dumps({
                "org_root_pub": "a" * 64,
                "personal_root_pub": "b" * 64,
                "genesis_id": "c" * 64,
                "member_present": True,
                "member_roles": ["member"],
                "serve_delegate_sha256": "1" * 64,
                "serve_key_mode": 0o600,
            }) + "\n")
        return CommandResult()


class FastHarness(Harness):
    """Runs the complete phase machine while replacing external waits only."""

    def _wait_http(self, url, *, description):
        return None

    def _wait_join_context(self):
        return {"status": "ok"}

    def _post_json(self, url, payload, *, org):
        self._approval_count = getattr(self, "_approval_count", 0) + 1
        return {
            "status": "pending" if self._approval_count == 1 else "ready",
            "have": self._approval_count,
            "need": 2,
        }

    async def _fetch_content(self):
        return b'{"v":1,"status":"ok"}\\nThis note crossed the real relay from restored node C.'


def test_full_phase_machine_is_redrivable_secret_safe_and_tears_down(
    tmp_path, monkeypatch
):
    fake = FakeDocker()
    announced = []
    harness = FastHarness(
        HarnessConfig(
            project=PROJECT,
            artifacts_dir=tmp_path,
            build=False,
            timeout=0.1,
            relay_port=21477,
            node_port_base=21880,
        ),
        runner=fake,
        announce=announced.append,
    )
    password = harness._password
    claim_token = harness._claim_token
    monkeypatch.setattr(driver.shutil, "which", lambda _name: "/usr/bin/docker")

    harness.run()

    assert [line for line in announced if line.startswith("\n=== ")] == [
        f"\n=== [{index}/6] {phase.title} ({phase.name}) ==="
        for index, phase in enumerate(driver.PHASES, 1)
    ]
    all_argv = "\n".join(" ".join(call["argv"]) for call in fake.calls)
    assert password not in all_argv
    assert claim_token not in all_argv
    assert any(call["input"] == password + "\n" for call in fake.calls)
    invitation_calls = [
        call for call in fake.calls
        if "AUTONOMY_HARNESS_INVITE_B" in call["env"]
    ]
    assert len(invitation_calls) == 1
    registry_seed = next(
        call for call in fake.calls
        if "exec -T relay" in " ".join(call["argv"])
        and "fixture_ops seed-registry" in " ".join(call["argv"])
    )
    assert claim_token not in (registry_seed["input"] or "")
    assert "AUTONOMY_HARNESS_INVITE_B" not in (
        tmp_path / PROJECT / "compose.json"
    ).read_text().replace("${AUTONOMY_HARNESS_INVITE_B:-}", "")

    assert any("stop node-a" in " ".join(call["argv"]) for call in fake.calls)
    assert any(
        "snapshot /app/data /artifacts/node-a.tar.gz --quiesced"
        in " ".join(call["argv"])
        for call in fake.calls
    )
    assert any(
        "restore /artifacts/node-a.tar.gz /app/data" in " ".join(call["argv"])
        for call in fake.calls
    )
    assert any(
        "down --volumes --remove-orphans" in " ".join(call["argv"])
        for call in fake.calls
    )
    with pytest.raises(NotImplementedError):
        harness.partition("node-a", "node-b")
    with pytest.raises(NotImplementedError):
        harness.heal("node-a", "node-b")
    with pytest.raises(NotImplementedError):
        harness.assert_converged("node-a", "node-b")


def test_no_docker_is_an_explicit_ci_requirement(tmp_path, monkeypatch):
    harness = Harness(
        HarnessConfig(project=PROJECT, artifacts_dir=tmp_path, build=False),
        runner=FakeDocker(),
    )
    monkeypatch.setattr(driver.shutil, "which", lambda _name: None)
    with pytest.raises(driver.HarnessError, match="Linux Docker host|required CI"):
        harness.run()
    assert not (tmp_path / PROJECT).exists()


def test_failure_preserves_logs_then_tears_down_exact_project(
    tmp_path, monkeypatch
):
    class FailingHarness(FastHarness):
        def phase_join(self):
            raise driver.HarnessError("deliberate phase failure")

    fake = FakeDocker()
    harness = FailingHarness(
        HarnessConfig(
            project=PROJECT,
            artifacts_dir=tmp_path,
            build=False,
            timeout=0.1,
        ),
        runner=fake,
        announce=lambda _line: None,
    )
    monkeypatch.setattr(driver.shutil, "which", lambda _name: "/usr/bin/docker")
    with pytest.raises(driver.HarnessError, match="deliberate phase failure"):
        harness.run()

    assert harness.log_file.is_file()
    commands = [" ".join(call["argv"]) for call in fake.calls]
    assert any("logs --no-color" in command for command in commands)
    teardown = [
        command for command in commands
        if "down --volumes --remove-orphans" in command
    ]
    assert teardown
    assert all(
        f"--project-name {PROJECT}" in command for command in teardown
    )


def test_linux_ci_runs_the_public_real_docker_command():
    workflow = (
        Path(__file__).resolve().parents[2]
        / ".github" / "workflows" / "multi-node-harness.yml"
    ).read_text()
    assert "docker compose version" in workflow
    assert "python3 -m deploy.harness" in workflow
    assert "ubuntu-latest" in workflow
    assert "mock" not in workflow.lower()


def _root_all_stores(monkeypatch, root: Path) -> None:
    # Mirrors the node environment topology generates: every store rooted
    # EXCEPT the graph pin — GRAPH_DB collapses org scoping (auto-sb0g8).
    for store in STORE_MANIFEST:
        if store.env and store.key != "graph":
            monkeypatch.setenv(store.env, str(root / store.relative))
    monkeypatch.setenv(REFUSE_REAL_DATA_FALLBACK_ENV, "1")
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.delenv("GRAPH_DB", raising=False)


def test_fixture_founds_real_crypto_ledger_and_registry_rows(tmp_path, monkeypatch):
    from tools.graph import settings_ops
    from tools.graph.db import GraphDB
    from tools.graph.schemas.network_identity import (
        NETWORK_LINK_GRANT_SET_ID,
        NETWORK_SERVE_CERT_SET_ID,
    )
    from tools.network.ledger import LedgerStore, org_ledger_db_path
    from tools.network.registry.store import RegistryStore

    GraphDB.close_all_pooled()
    _root_all_stores(monkeypatch, tmp_path)
    orgs = tmp_path / "orgs"
    GraphDB.create_org_db(
        "personal", type_="personal", path=orgs / "personal.db"
    ).close()
    GraphDB.create_org_db(
        "demo",
        type_="shared",
        path=orgs / "demo.db",
        org_id="11111111-1111-4111-8111-111111111111",
    ).close()
    payload = {
        "org": "demo",
        "password": "harness password",
        "claim_token": "claim bearer never sent to relay",
        "join_channel_token": "1" * 32,
        "content_channel_token": "2" * 32,
    }
    founded = found_node(payload)

    assert founded["root_pub"] != founded["personal_root_pub"]
    assert founded["invite_ref"] != founded["genesis_id"]
    with LedgerStore(org_ledger_db_path("demo")) as store:
        state = store.fold()
        assert state.members[founded["founder_persona_pub"]].roles == ("owner",)
        assert state.role_defs["member"].approver_threshold == 2
        assert (
            store.get(founded["invite_ref"]).payload["token_hash"]
            != payload["claim_token"]
        )
    grants = settings_ops.read_owned_set(
        NETWORK_LINK_GRANT_SET_ID, org="demo"
    ).members
    assert {row.key for row in grants} == {"1" * 32, "2" * 32}
    assert payload["claim_token"] not in repr([row.payload for row in grants])
    cert = settings_ops.read_owned_set(
        NETWORK_SERVE_CERT_SET_ID, org="demo"
    ).members[0].payload
    assert Path(cert["key_path"]).name == cert["key_path"]
    key_path = tmp_path / "network" / cert["key_path"]
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600

    registry_db = tmp_path / "registry.db"
    monkeypatch.setenv("AUTONOMY_HARNESS_REGISTRY_DB", str(registry_db))
    seed_registry({
        "org_uuid": founded["org_uuid"],
        "root_pub": founded["root_pub"],
        "join_channel_token": "1" * 32,
        "invite_ref": founded["invite_ref"],
        "invite_expiry": founded["invite_expiry"],
        "content_channel_token": "2" * 32,
        "content_id": founded["content_id"],
    })
    registry = RegistryStore(registry_db)
    try:
        assert registry.get_org(founded["org_uuid"]).root_pub == founded["root_pub"]
        assert registry.get_link("1" * 32).invite_ref == founded["invite_ref"]
        assert registry.get_link("2" * 32).target_uuid == founded["content_id"]
    finally:
        registry.close()
        GraphDB.close_all_pooled()
