"""Phase-driven B6 Docker acceptance ladder."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from tools.network.invitation import (
    Invitation,
    encode_invitation,
    invitation_from_join_url,
)

from .topology import TopologyConfig, write_compose


class HarnessError(RuntimeError):
    """The ladder failed an acceptance assertion."""


@dataclass(frozen=True)
class HarnessConfig:
    project: str = field(
        default_factory=lambda: f"autonomy-harness-{secrets.token_hex(4)}"
    )
    nodes: int = 3
    relay_port: int = 18477
    node_port_base: int = 18880
    image: str = "autonomy-dashboard:harness"
    artifacts_dir: Path = Path("harness-artifacts")
    build: bool = True
    timeout: float = 120.0
    secure_dashboard: bool = False
    docker_command: tuple[str, ...] = ("docker",)
    compose_command: tuple[str, ...] = ("docker", "compose")

    @property
    def relay_http(self) -> str:
        return f"http://127.0.0.1:{self.relay_port}"

    @property
    def relay_ws(self) -> str:
        return f"ws://127.0.0.1:{self.relay_port}"

    def node_http(self, index: int) -> str:
        scheme = "https" if self.secure_dashboard else "http"
        return f"{scheme}://127.0.0.1:{self.node_port_base + index}"


@dataclass
class CommandResult:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


class Runner(Protocol):
    def run(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
        check: bool = True,
    ) -> CommandResult: ...


class SubprocessRunner:
    """The real command runner; command logging never includes secret input."""

    def __init__(self, announce: Callable[[str], None] = print):
        self._announce = announce

    def run(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
        check: bool = True,
    ) -> CommandResult:
        self._announce("+ " + " ".join(argv))
        completed = subprocess.run(
            argv,
            env=env,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
        result = CommandResult(
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )
        if check and result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise HarnessError(
                f"command failed ({result.returncode}): {' '.join(argv)}"
                + (f"\n{detail}" if detail else "")
            )
        return result


@dataclass(frozen=True)
class Phase:
    name: str
    title: str
    action: str


PHASES = (
    Phase("topology", "Isolated nodes", "phase_topology"),
    Phase("found", "Found and serve", "phase_found"),
    Phase("join", "Join over the real relay", "phase_join"),
    Phase("admit", "Two-party admission", "phase_admit"),
    Phase("port", "Stop A, restore C", "phase_portability"),
    Phase("prove", "C serves A's content and identity", "phase_prove"),
)


class Harness:
    """One reusable ladder.  Presentation code may call each phase directly."""

    def __init__(
        self,
        config: HarnessConfig,
        *,
        runner: Runner | None = None,
        announce: Callable[[str], None] = print,
    ):
        self.config = config
        self.runner = runner or SubprocessRunner(announce)
        self.announce = announce
        self.topology = TopologyConfig(
            project=config.project,
            nodes=config.nodes,
            relay_port=config.relay_port,
            node_port_base=config.node_port_base,
            image=config.image,
            from_source=config.build,
            secure_dashboard=config.secure_dashboard,
        )
        self.topology.validate()
        self.run_dir = config.artifacts_dir.resolve() / config.project
        self.compose_file = self.run_dir / "compose.json"
        self.log_file = self.run_dir / "compose.log"
        self._password = secrets.token_urlsafe(24)
        self._claim_token = secrets.token_urlsafe(32)
        self._join_channel_token = secrets.token_hex(16)
        self._content_channel_token = secrets.token_hex(16)
        self._found: dict = {}
        self._pending: dict = {}
        self._invitation: Invitation | None = None
        self._torn_down = False
        self._node_ssl_contexts: dict[int, ssl.SSLContext] = {}

    @property
    def compose(self) -> list[str]:
        return [
            *self.config.compose_command,
            "--project-name", self.config.project,
            "--file", str(self.compose_file),
        ]

    @property
    def content_source_id(self) -> str:
        content_id = self._found.get("content_id")
        if not isinstance(content_id, str) or not content_id:
            raise HarnessError("found phase has not exposed its content source")
        return content_id

    @property
    def relay_content_url(self) -> str:
        if not self._found:
            raise HarnessError("found phase has not published relay content")
        return (
            f"{self.config.relay_http}/l/{self._content_channel_token}"
        )

    def _phase(self, index: int, phase: Phase) -> None:
        self.announce(
            f"\n=== [{index}/{len(PHASES)}] {phase.title} ({phase.name}) ==="
        )

    def _compose(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
        check: bool = True,
    ) -> CommandResult:
        return self.runner.run(
            [*self.compose, *args],
            env=env,
            input_text=input_text,
            check=check,
        )

    def _fixture(self, service: str, command: str, payload: dict) -> dict:
        result = self._compose(
            "exec", "-T", service,
            "python", "-m", "deploy.harness.fixture_ops", command,
            input_text=json.dumps(payload),
        )
        try:
            value = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError) as exc:
            raise HarnessError(
                f"{service} fixture {command} returned malformed JSON"
            ) from exc
        if not isinstance(value, dict):
            raise HarnessError(f"{service} fixture {command} returned a non-object")
        return value

    def _ssl_context_for(self, url: str) -> ssl.SSLContext | None:
        if not url.startswith("https://"):
            return None
        port = urllib.parse.urlsplit(url).port
        if port is None:
            return None
        return self._node_ssl_contexts.get(port - self.config.node_port_base)

    @staticmethod
    def _pinned_ssl_context(cert_path: Path) -> ssl.SSLContext:
        """Trust only the node's captured self-signed certificate."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cafile=str(cert_path))
        return context

    def _capture_node_certificate(self, index: int, service: str) -> None:
        if not self.config.secure_dashboard:
            return
        deadline = time.monotonic() + self.config.timeout
        certificate = ""
        while time.monotonic() < deadline:
            result = self._compose(
                "exec", "-T", service, "cat", "/app/data/tls.crt",
                check=False,
            )
            if (
                result.returncode == 0
                and "-----BEGIN CERTIFICATE-----" in result.stdout
                and "-----END CERTIFICATE-----" in result.stdout
            ):
                certificate = result.stdout
                break
            time.sleep(0.5)
        if not certificate:
            raise HarnessError(
                f"timed out capturing {service}'s generated TLS certificate"
            )
        cert_dir = self.run_dir / "tls"
        cert_dir.mkdir(parents=True, exist_ok=True)
        cert_path = cert_dir / f"{service}.crt"
        cert_path.write_text(certificate, encoding="ascii")
        self._node_ssl_contexts[index] = self._pinned_ssl_context(cert_path)

    def _wait_http(self, url: str, *, description: str) -> None:
        deadline = time.monotonic() + self.config.timeout
        last = ""
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    url,
                    timeout=2,
                    context=self._ssl_context_for(url),
                ) as response:
                    if 200 <= response.status < 300:
                        return
            except (OSError, urllib.error.URLError) as exc:
                last = str(exc)
            time.sleep(0.5)
        raise HarnessError(f"timed out waiting for {description}: {last}")

    def _wait_fixture(
        self,
        service: str,
        command: str,
        predicate: Callable[[dict], bool],
    ) -> dict:
        deadline = time.monotonic() + self.config.timeout
        last: dict = {}
        while time.monotonic() < deadline:
            try:
                last = self._fixture(service, command, {})
                if predicate(last):
                    return last
            except HarnessError:
                pass
            time.sleep(0.5)
        raise HarnessError(
            f"timed out waiting for {service} {command}: {last!r}"
        )

    def _post_json(self, url: str, payload: dict, *, org: str) -> dict:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Graph-Org": org,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=10,
                context=self._ssl_context_for(url),
            ) as response:
                value = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise HarnessError(f"HTTP {exc.code} from {url}: {detail}") from exc
        if not isinstance(value, dict):
            raise HarnessError(f"{url} returned a non-object")
        return value

    def phase_topology(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        write_compose(self.compose_file, self.topology)
        # A killed prior run may have left this exact, validated project
        # behind. Re-driving the named demo replaces only that project's
        # labelled resources; it can never prune a broader Docker scope.
        self._compose(
            "down", "--volumes", "--remove-orphans", "--timeout", "15",
            check=False,
        )
        if self.config.build:
            self._compose("build", "relay", "node-a")
        active = ["relay", "node-a", *(
            f"node-{index}" for index in range(3, self.config.nodes + 1)
        )]
        self._compose("up", "-d", *active)
        self._wait_http(
            f"{self.config.relay_http}/healthz", description="real relay"
        )
        self._capture_node_certificate(0, "node-a")
        self._wait_http(
            f"{self.config.node_http(0)}/api/ping", description="node A"
        )
        for index in range(3, self.config.nodes + 1):
            self._capture_node_certificate(index - 1, f"node-{index}")
            self._wait_http(
                f"{self.config.node_http(index - 1)}/api/ping",
                description=f"node {index}",
            )

    def phase_found(self) -> None:
        self._found = self._fixture(
            "node-a",
            "found",
            {
                "org": "demo",
                "password": self._password,
                "claim_token": self._claim_token,
                "join_channel_token": self._join_channel_token,
                "content_channel_token": self._content_channel_token,
            },
        )
        registry_payload = {
            key: self._found[key]
            for key in (
                "org_uuid", "root_pub", "invite_ref", "invite_expiry",
                "content_id",
            )
        }
        registry_payload.update(
            {
                "join_channel_token": self._join_channel_token,
                "content_channel_token": self._content_channel_token,
            }
        )
        seeded = self._fixture("relay", "seed-registry", registry_payload)
        join_url = seeded.get("join_url")
        if not isinstance(join_url, str):
            raise HarnessError("registry fixture omitted its minted join URL")
        # The driver assembles v2 from the same two URL domains as the real
        # minter: registry token in the path, ledger bearer in the fragment.
        # Only the path token was sent to the relay fixture above.
        self._invitation = invitation_from_join_url(
            org=self._found["org_uuid"],
            root_pub=self._found["root_pub"],
            invite_ref=self._found["invite_ref"],
            join_url=(
                join_url
                + "#t="
                + urllib.parse.quote(self._claim_token, safe="")
            ),
        )
        # Startup reconciliation is the production path by which a restored
        # node reconnects. Restart A after fixture setup so the initial proof
        # exercises that same path.
        self._compose("restart", "node-a")
        self._wait_http(
            f"{self.config.node_http(0)}/api/ping",
            description="node A after serving setup",
        )
        context = self._wait_join_context()
        # Print the asserted values, don't just assert them: a passing run
        # must carry its own evidence in stdout (auto-qqlz5).
        self.announce(
            "join context served: status=ok"
            f" granted_role={context.get('granted_role')}"
            f" binding={context.get('binding')}"
            f" heads={len(context.get('heads') or [])}"
        )
        stats = self._fixture(
            "relay", "relay-stats", {"org_uuid": self._found["org_uuid"]}
        )
        self.announce(
            "relay serving evidence:"
            f" link_sessions live={stats.get('link_sessions_live')}"
            f" ever={stats.get('link_sessions_ever')}"
            f" node_hints live={stats.get('node_hints_live')}"
        )

    def _wait_join_context(self) -> dict:
        from tools.init.join import ViewerJoinTransport

        if self._invitation is None:
            raise HarnessError("invitation is not initialized")
        deadline = time.monotonic() + self.config.timeout
        last = ""
        while time.monotonic() < deadline:
            try:
                reply = ViewerJoinTransport(
                    self._invitation,
                    relay_url=self.config.relay_ws,
                    timeout=5,
                ).request({"v": 1, "op": "context"})
                # A reachable channel that REFUSES (status "unavailable") is
                # not an open join channel — accepting it here deferred the
                # auto-sb0g8 serving failure into phase_join with a
                # misleading symptom. Only a served context counts.
                if isinstance(reply, dict) and reply.get("status") == "ok":
                    return reply
                status = (
                    reply.get("status") if isinstance(reply, dict)
                    else type(reply).__name__
                )
                last = f"context status={status!r}"
            except Exception as exc:
                last = type(exc).__name__
            time.sleep(0.5)
        raise HarnessError(f"A never opened the production join channel: {last}")

    def phase_join(self) -> None:
        if self._invitation is None:
            raise HarnessError("found phase did not create an invitation")
        # The password crosses stdin into a mode-0600 file in B's named
        # volume. It is absent from Compose, argv, and preserved logs.
        self._compose(
            "run", "--rm", "--no-deps", "--entrypoint", "sh", "node-b",
            "-c", "umask 077; cat > /app/data/harness-personal-password",
            input_text=self._password + "\n",
        )
        env = dict(os.environ)
        env["AUTONOMY_HARNESS_INVITE_B"] = encode_invitation(self._invitation)
        self._compose("up", "-d", "node-b", env=env)
        self._capture_node_certificate(1, "node-b")
        self._wait_http(
            f"{self.config.node_http(1)}/api/ping", description="joining node B"
        )
        self._pending = self._wait_fixture(
            "node-b",
            "pending",
            lambda row: row.get("state") == "pending"
            and row.get("have") == 0
            and row.get("need") == 2,
        )
        self.announce(
            "node-b staged pending claim:"
            f" state={self._pending.get('state')}"
            f" have={self._pending.get('have')}"
            f" need={self._pending.get('need')}"
        )
        if self._pending["invite_ref"] != self._found["invite_ref"]:
            raise HarnessError("B staged a different invitation")

    def phase_admit(self) -> None:
        signed = self._fixture(
            "node-a",
            "approvals",
            {
                "org": "demo",
                "password": self._password,
                "claim_key": self._pending["claim_key"],
            },
        )
        entries = signed.get("approvals")
        if not isinstance(entries, list) or len(entries) != 2:
            raise HarnessError("A did not mint exactly two approvals")
        endpoint = (
            f"{self.config.node_http(0)}/api/network/ledger/claim/"
            f"{self._pending['claim_key']}/approval"
        )
        responses = []
        for entry in entries:
            responses.append(
                self._post_json(
                    endpoint,
                    {
                        "org": "demo",
                        "invite_ref": self._pending["invite_ref"],
                        "persona_pub": self._pending["persona_pub"],
                        "approval": entry,
                    },
                    org="demo",
                )
            )
        if responses[0].get("status") != "pending":
            raise HarnessError(f"first approval did not remain pending: {responses[0]}")
        if responses[1].get("status") != "ready":
            raise HarnessError(f"second approval did not make claim ready: {responses[1]}")
        self.announce(
            "admission approvals:"
            f" first={responses[0].get('status')}"
            f" second={responses[1].get('status')} (threshold of 2 met)"
        )
        self._compose("restart", "node-b")
        self._wait_http(
            f"{self.config.node_http(1)}/api/ping",
            description="B after status-first finalize",
        )
        self._wait_fixture(
            "node-b",
            "pending",
            lambda row: row == {"state": "not-single", "count": 0},
        )

    def phase_portability(self) -> None:
        self._compose("stop", "node-a")
        self._compose(
            "run", "--rm", "--no-deps", "--entrypoint", "python", "node-a",
            "-m", "tools.portability",
            "snapshot", "/app/data", "/artifacts/node-a.tar.gz",
            "--quiesced",
        )
        self._compose(
            "run", "--rm", "--no-deps", "--entrypoint", "python", "node-c",
            "-m", "tools.portability",
            "restore", "/artifacts/node-a.tar.gz", "/app/data",
        )
        self._compose("up", "-d", "node-c")
        self._capture_node_certificate(self.config.nodes, "node-c")
        self._wait_http(
            f"{self.config.node_http(self.config.nodes)}/api/ping",
            description="restored node C",
        )

    async def _fetch_content(self) -> bytes:
        from tools.network.idkit import canonical_json
        from tools.network.relaykit.viewer import ViewerChannel

        channel = await ViewerChannel.connect(
            self.config.relay_ws,
            self._content_channel_token,
            root_pub=self._found["root_pub"],
            org=self._found["org_uuid"],
            open_timeout=5,
        )
        async with channel:
            await channel.send_message(canonical_json({"v": 1, "op": "fetch"}))
            return await channel.recv_message()

    def phase_prove(self) -> None:
        deadline = time.monotonic() + self.config.timeout
        raw = b""
        while time.monotonic() < deadline:
            try:
                raw = asyncio.run(self._fetch_content())
                break
            except Exception:
                time.sleep(0.5)
        if b"This note crossed the real relay from restored node C." not in raw:
            raise HarnessError(
                "C did not serve A's note over the restored production connector"
            )
        inspected = self._fixture(
            "node-c",
            "inspect",
            {
                "org": "demo",
                "password": self._password,
                "persona_pub": self._pending["persona_pub"],
            },
        )
        expected = {
            "org_root_pub": self._found["root_pub"],
            "personal_root_pub": self._found["personal_root_pub"],
            "genesis_id": self._found["genesis_id"],
            "serve_delegate_sha256": self._found["serve_delegate_sha256"],
            "serve_key_mode": 0o600,
        }
        for key, value in expected.items():
            if inspected.get(key) != value:
                raise HarnessError(
                    f"restored identity mismatch for {key}: "
                    f"{inspected.get(key)!r} != {value!r}"
                )
        if not inspected.get("member_present"):
            raise HarnessError("restored ledger lost B's admitted membership")

    # Sync-sprint extension points are explicit refusals, not fake successes.
    def partition(self, *_nodes: str) -> None:
        raise NotImplementedError(
            "network partition control is reserved for the sync sprint"
        )

    def heal(self, *_nodes: str) -> None:
        raise NotImplementedError(
            "network heal control is reserved for the sync sprint"
        )

    def assert_converged(self, *_nodes: str) -> None:
        raise NotImplementedError(
            "convergence assertions require the sync engine"
        )

    def _capture_logs(self) -> None:
        result = self._compose("logs", "--no-color", check=False)
        self.log_file.write_text(
            result.stdout + result.stderr,
            encoding="utf-8",
        )

    def teardown(self) -> None:
        if self._torn_down or not self.compose_file.exists():
            return
        self._compose(
            "down", "--volumes", "--remove-orphans", "--timeout", "15",
            check=False,
        )
        filters = ["label=com.docker.compose.project=" + self.config.project]
        checks = (
            [*self.config.docker_command, "ps", "-aq", "--filter", filters[0]],
            [
                *self.config.docker_command,
                "volume", "ls", "-q", "--filter", filters[0],
            ],
            [
                *self.config.docker_command,
                "network", "ls", "-q", "--filter", filters[0],
            ],
        )
        leftovers = []
        for command in checks:
            result = self.runner.run(command, check=False)
            if result.stdout.strip():
                leftovers.extend(result.stdout.split())
        self._torn_down = True
        if leftovers:
            raise HarnessError(
                "deterministic teardown left project-labelled resources: "
                + ", ".join(sorted(leftovers))
            )

    def run(self) -> None:
        if shutil.which(self.config.docker_command[0]) is None:
            raise HarnessError(
                "Docker is required for the real multi-node ladder; run this "
                "command on a Linux Docker host or in the required CI job"
            )
        failure: BaseException | None = None
        try:
            for index, phase in enumerate(PHASES, 1):
                self._phase(index, phase)
                getattr(self, phase.action)()
        except BaseException as exc:
            failure = exc
            raise
        finally:
            # Capture container logs pass OR fail (auto-qqlz5) — a passing
            # run's evidence must not be thinner than a failure's. Must
            # happen before teardown removes the containers; best-effort so
            # a log hiccup cannot turn a pass into a failure.
            if self.compose_file.exists():
                try:
                    self._capture_logs()
                except Exception as log_exc:
                    print(
                        f"WARNING: compose log capture failed: {log_exc}",
                        file=sys.stderr,
                    )
            try:
                self.teardown()
            except Exception:
                if failure is None:
                    raise
        self.announce(
            "\nPASS: found -> real-relay join -> 2 approvals -> "
            "snapshot/restore -> restored C reconnect and content fetch"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the real multi-node Docker acceptance ladder"
    )
    parser.add_argument("--project")
    parser.add_argument("--nodes", type=int, default=3)
    parser.add_argument("--relay-port", type=int, default=18477)
    parser.add_argument("--node-port-base", type=int, default=18880)
    parser.add_argument("--image", default="autonomy-dashboard:harness")
    parser.add_argument("--artifacts-dir", type=Path, default=Path("harness-artifacts"))
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--timeout", type=float, default=120)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = HarnessConfig(
        project=args.project or f"autonomy-harness-{secrets.token_hex(4)}",
        nodes=args.nodes,
        relay_port=args.relay_port,
        node_port_base=args.node_port_base,
        image=args.image,
        artifacts_dir=args.artifacts_dir,
        build=not args.no_build,
        timeout=args.timeout,
    )
    try:
        Harness(config).run()
    except (HarnessError, ValueError) as exc:
        print(f"HARNESS FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
