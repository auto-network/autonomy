"""Generate the isolated Docker Compose topology used by the B6 ladder."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from tools.data_paths import REFUSE_REAL_DATA_FALLBACK_ENV, STORE_MANIFEST


_PROJECT_RE = re.compile(r"^autonomy-harness-[a-z0-9][a-z0-9-]{0,39}$")
REPO_ROOT = Path(__file__).resolve().parents[2]


class TopologyError(ValueError):
    """A topology could escape the harness's isolation boundary."""


@dataclass(frozen=True)
class TopologyConfig:
    project: str
    nodes: int = 3
    relay_port: int = 18477
    node_port_base: int = 18880
    image: str = "autonomy-dashboard:harness"
    source_root: Path = REPO_ROOT
    from_source: bool = True
    secure_dashboard: bool = False

    def validate(self) -> None:
        if not _PROJECT_RE.fullmatch(self.project):
            raise TopologyError(
                "project must match autonomy-harness-[a-z0-9-] and be at "
                "most 57 characters"
            )
        if self.nodes < 3 or self.nodes > 20:
            raise TopologyError("nodes must be in [3, 20]")
        ports = [self.relay_port, *(self.node_port_base + i for i in range(self.nodes + 1))]
        if any(type(port) is not int or port < 1024 or port > 65535 for port in ports):
            raise TopologyError("all host ports must be integers in [1024, 65535]")
        if len(ports) != len(set(ports)):
            raise TopologyError("relay and node host ports must be distinct")
        if not self.source_root.is_dir() or not (
            self.source_root / "deploy" / "Dockerfile"
        ).is_file():
            raise TopologyError("source_root must be an Autonomy checkout")


def _rooted_environment(*, secure_dashboard: bool = False) -> dict[str, str]:
    """Every manifest store is explicitly rooted inside the node volume."""
    environment = {
        REFUSE_REAL_DATA_FALLBACK_ENV: "1",
        "DASHBOARD_HOST": "0.0.0.0",
        "DASHBOARD_PORT": "8080",
        "AUTONOMY_NETWORK_REGISTRY_URL": "http://relay:8477",
    }
    if secure_dashboard:
        # The existing entrypoint provisions a self-signed keypair.  localhost
        # is present in its SAN, so the presentation driver can pin and verify
        # the exact generated certificate from the host.
        environment["DASHBOARD_DOMAIN"] = "localhost"
    else:
        environment["DASHBOARD_TLS"] = "off"
    for store in STORE_MANIFEST:
        if store.env:
            environment[store.env] = f"/app/data/{store.relative}"
    return environment


def _node_service(
    config: TopologyConfig,
    *,
    volume: str,
    host_port: int,
    first_org: str | None = None,
    invitation: bool = False,
) -> dict:
    environment = _rooted_environment(
        secure_dashboard=config.secure_dashboard,
    )
    if first_org:
        environment["AUTONOMY_FIRST_ORG"] = first_org
        environment["AUTONOMY_FIRST_ORG_NAME"] = f"Harness {first_org}"
        # Pin the serving scope to the founded slug. Without this, GRAPH_DB is
        # set but GRAPH_ORG is empty, so _discover_startup_orgs() manages only
        # org=None: the connector then answers join ops with org=None, and the
        # claim service (which opens the org LEDGER, keyed by real slug) raises
        # "org must be a non-empty local slug" -> the join context is refused
        # as "unavailable". Serving under the real slug lets the claim service
        # open the founded org's ledger, so the join context resolves.
        environment["GRAPH_ORG"] = first_org
    if invitation:
        # The value is supplied only to the `up node-b` subprocess.  It is
        # never rendered into the generated file or written to artifacts.
        environment["AUTONOMY_INVITE"] = "${AUTONOMY_HARNESS_INVITE_B:-}"
        environment["AUTONOMY_PERSONAL_PASSWORD_FILE"] = (
            "/app/data/harness-personal-password"
        )
    service = {
        "image": config.image,
        "environment": environment,
        "volumes": [
            f"{volume}:/app/data",
            f"{config.project}-artifacts:/artifacts",
        ],
        "ports": [f"127.0.0.1:{host_port}:8080"],
        "networks": ["harness"],
        "depends_on": {"relay": {"condition": "service_healthy"}},
        "healthcheck": {
            "test": (
                [
                    "CMD", "curl", "--cacert", "/app/data/tls.crt",
                    "-fs", "https://localhost:8080/api/ping",
                ]
                if config.secure_dashboard
                else ["CMD", "curl", "-fs", "http://127.0.0.1:8080/api/ping"]
            ),
            "interval": "2s",
            "timeout": "2s",
            "retries": 45,
            "start_period": "5s",
        },
    }
    if config.from_source:
        service["build"] = {
            "context": str(config.source_root),
            "dockerfile": "deploy/Dockerfile",
        }
    return service


def compose_model(config: TopologyConfig) -> dict:
    """Return a complete JSON-compatible Compose model.

    ``nodes`` counts the concurrently available test nodes A, B, and
    node-3..N.  The fresh restore target C is deliberately an additional
    service: once A is quiesced, B + C + node-3..N still leaves N live,
    isolated nodes on screen while A remains stopped.
    """
    config.validate()
    relay = {
        "image": config.image,
        "entrypoint": ["python", "-m", "tools.network.registry"],
        "command": [
            "--db", "/registry/registry.db",
            "--host", "0.0.0.0",
            "--port", "8477",
            "--base-url", "https://relay.harness.invalid",
        ],
        "volumes": [f"{config.project}-registry:/registry"],
        "ports": [f"127.0.0.1:{config.relay_port}:8477"],
        "networks": ["harness"],
        "healthcheck": {
            "test": ["CMD", "curl", "-fs", "http://127.0.0.1:8477/healthz"],
            "interval": "2s",
            "timeout": "2s",
            "retries": 30,
            "start_period": "2s",
        },
    }
    if config.from_source:
        relay["build"] = {
            "context": str(config.source_root),
            "dockerfile": "deploy/Dockerfile",
        }
    services: dict[str, dict] = {
        "relay": relay,
        "node-a": _node_service(
            config,
            volume=f"{config.project}-node-a",
            host_port=config.node_port_base,
            first_org="demo",
        ),
        "node-b": _node_service(
            config,
            volume=f"{config.project}-node-b",
            host_port=config.node_port_base + 1,
            invitation=True,
        ),
    }
    for index in range(3, config.nodes + 1):
        services[f"node-{index}"] = _node_service(
            config,
            volume=f"{config.project}-node-{index}",
            host_port=config.node_port_base + index - 1,
            first_org=f"peer-{index}",
        )
    services["node-c"] = _node_service(
        config,
        volume=f"{config.project}-node-c",
        host_port=config.node_port_base + config.nodes,
    )

    volumes = {
        f"{config.project}-registry": {},
        f"{config.project}-artifacts": {},
        f"{config.project}-node-a": {},
        f"{config.project}-node-b": {},
        f"{config.project}-node-c": {},
    }
    volumes.update(
        {f"{config.project}-node-{index}": {} for index in range(3, config.nodes + 1)}
    )
    return {
        "name": config.project,
        "services": services,
        "volumes": volumes,
        "networks": {"harness": {"driver": "bridge", "internal": False}},
    }


def write_compose(path: Path, config: TopologyConfig) -> Path:
    """Write Compose as JSON (a valid Compose YAML document)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(compose_model(config), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path
