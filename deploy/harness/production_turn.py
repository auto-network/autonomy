"""Two isolated serving nodes over a caller-selected production TURN stack.

The command writes short-lived throwaway orgs and links through the normal
signed Registry API.  It uses the normal serving connector, TURN issuer,
RelayKit native initiator, Mission renderer, and sealed feed protocol.  The
only stand-in is a bounded local SSE event source, matching the Dashboard
endpoint consumed by the production connector.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import http.server
import json
import os
import queue
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from tools.network.idkit import canonical_json
from tools.network.relaykit.aiortc_initiator import NativeIceChannel, upgrade_via_ice
from tools.network.relaykit.channel import open_stream_frame
from tools.network.relaykit.viewer import ViewerChannel


class ProductionTurnError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProductionTurnConfig:
    registry_url: str
    relay_url: str
    artifacts_dir: Path = Path("harness-artifacts")
    ttl: int = 3600
    timeout: float = 120.0
    site_bytes: int = 70_000
    keep_state: bool = False

    def validate(self) -> None:
        registry = urllib.parse.urlsplit(self.registry_url)
        relay = urllib.parse.urlsplit(self.relay_url)
        if registry.scheme != "https" or not registry.hostname:
            raise ProductionTurnError("registry_url must be an explicit HTTPS URL")
        if relay.scheme != "wss" or not relay.hostname:
            raise ProductionTurnError("relay_url must be an explicit WSS URL")
        if self.ttl < 3600:
            raise ProductionTurnError("ttl must be at least the Registry minimum of 3600s")
        if self.timeout <= 0:
            raise ProductionTurnError("timeout must be positive")
        if self.site_bytes <= 60 * 1024:
            raise ProductionTurnError("site_bytes must exceed one 60 KiB record")

    @property
    def link_base_url(self) -> str:
        parsed = urllib.parse.urlsplit(self.relay_url)
        return urllib.parse.urlunsplit(("https", parsed.netloc, "", "", ""))

    @property
    def connector_url(self) -> str:
        """The org tunnel terminates on the Registry host, not the link host."""
        from tools.dashboard.link_probe import registry_to_relay_ws

        return registry_to_relay_ws(self.registry_url)


class _Events:
    """Small stand-in for the Dashboard's production ``/api/events`` SSE."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.port = port
        self._server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", port), self._handler()
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()

    def _handler(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):  # noqa: N802
                if self.path != "/api/events":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                while True:
                    try:
                        topic, data = outer._queue.get(timeout=0.5)
                        frame = f"event: {topic}\ndata: {json.dumps(data)}\n\n"
                    except queue.Empty:
                        frame = ": keepalive\n\n"
                    try:
                        self.wfile.write(frame.encode("utf-8"))
                        self.wfile.flush()
                    except Exception:
                        return

            def log_message(self, *_args):
                pass

        return Handler

    def emit(self, topic: str, data: dict) -> None:
        self._queue.put((topic, data))

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._server.shutdown()
        with contextlib.suppress(Exception):
            self._server.server_close()


@dataclass
class _Node:
    slug: str
    password: str
    root: Path
    state: dict
    events: _Events
    connector: subprocess.Popen


def _candidate_types(peer) -> tuple[str, str]:
    """Read aiortc's selected pair for acceptance evidence only."""
    try:
        connection = peer.sctp.transport.transport._connection
        pair = getattr(connection, "nominated", None) or getattr(
            connection, "_nominated", None
        )
        if isinstance(pair, dict):
            pair = next(iter(pair.values()), None)
        local = getattr(getattr(pair, "local_candidate", None), "type", None)
        remote = getattr(getattr(pair, "remote_candidate", None), "type", None)
    except Exception as exc:
        raise ProductionTurnError("selected ICE pair is unavailable") from exc
    if not isinstance(local, str) or not isinstance(remote, str):
        raise ProductionTurnError("selected ICE pair is incomplete")
    return local, remote


def _control_serving(path: Path) -> bool:
    try:
        descriptor = json.loads(path.read_text(encoding="utf-8"))
        request = json.dumps(
            {"auth": descriptor["auth"], "op": "connector-status", "args": {}}
        ).encode("utf-8") + b"\n"
        with socket.create_connection(
            ("127.0.0.1", int(descriptor["port"])), timeout=0.5
        ) as client:
            client.sendall(request)
            client.settimeout(0.5)
            response = b""
            while b"\n" not in response:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response += chunk
        value = json.loads(response.split(b"\n", 1)[0])
        return value.get("ok") is True and value.get("serving") is True
    except (OSError, ValueError, KeyError, TypeError):
        return False


class ProductionTurnHarness:
    def __init__(self, config: ProductionTurnConfig, *, announce=print):
        config.validate()
        self.config = config
        self.announce = announce
        self.run_id = f"turn-{secrets.token_hex(4)}"
        self.run_dir = config.artifacts_dir.resolve() / self.run_id
        self._scratch: Path | None = None
        self._nodes: list[_Node] = []
        self._evidence: dict = {
            "run_id": self.run_id,
            "registry_host": urllib.parse.urlsplit(config.registry_url).hostname,
            "relay_host": urllib.parse.urlsplit(config.relay_url).hostname,
            "directions": [],
            "renewal": {},
        }

    def _preflight(self) -> None:
        """Refuse before Registry writes when the native ICE runtime is absent."""
        from tools.network.relaykit.aiortc_responder import load_aiortc_modules

        try:
            load_aiortc_modules()
        except Exception as exc:
            raise ProductionTurnError(
                "the pinned aiortc runtime is unavailable; install deploy/requirements.txt"
            ) from exc

    def _node_env(self, root: Path, slug: str) -> dict[str, str]:
        return {
            **os.environ,
            "AUTONOMY_DATA_ROOT": str(root),
            "AUTONOMY_REFUSE_REAL_DATA_FALLBACK": "1",
            "MISSION_CONTROL_DB": str(root / "mission_control.db"),
            "GRAPH_ORG": slug,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        }

    def _fixture(self, command: str, payload: dict, env: dict[str, str]) -> dict:
        result = subprocess.run(
            [sys.executable, "-m", "deploy.harness.external_turn_fixture", command],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            timeout=self.config.timeout,
            check=False,
        )
        if result.returncode:
            detail = result.stderr.strip().splitlines()[-1:] or ["unknown failure"]
            raise ProductionTurnError(f"{command} fixture failed: {detail[0]}")
        try:
            value = json.loads(result.stdout.strip().splitlines()[-1])
        except ValueError as exc:
            raise ProductionTurnError(f"{command} fixture returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ProductionTurnError(f"{command} fixture returned a non-object")
        return value

    def _start_node(self, ordinal: int) -> _Node:
        assert self._scratch is not None
        slug = f"turnaccept{self.run_id.replace('-', '')}{ordinal}"
        root = self._scratch / slug
        root.mkdir(mode=0o700)
        password = secrets.token_urlsafe(32)
        env = self._node_env(root, slug)
        state = self._fixture(
            "setup",
            {
                "slug": slug,
                "password": password,
                "registry_url": self.config.registry_url,
                "link_base_url": self.config.link_base_url,
                "ttl": self.config.ttl,
                "site_bytes": self.config.site_bytes,
            },
            env,
        )
        events = _Events()
        log_path = self.run_dir / f"connector-{ordinal}.log"
        log = open(log_path, "ab", buffering=0)
        connector = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tools.dashboard.link_serving",
                "--relay",
                self.config.connector_url,
                "--org",
                state["org_uuid"],
                "--key-file",
                state["key_file"],
                "--cert-file",
                state["cert_file"],
                "--channel-cert-file",
                state["channel_cert_file"],
                "--control-file",
                state["control_file"],
                "--graph-org",
                slug,
                "--dashboard-url",
                f"http://127.0.0.1:{events.port}",
            ],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        node = _Node(slug, password, root, state, events, connector)
        self._nodes.append(node)
        deadline = time.monotonic() + self.config.timeout
        control = Path(state["control_file"])
        while time.monotonic() < deadline:
            if connector.poll() is not None:
                raise ProductionTurnError(f"serving connector {ordinal} exited")
            if _control_serving(control):
                return node
            time.sleep(0.25)
        raise ProductionTurnError(f"serving connector {ordinal} never became ready")

    async def _upgrade(self, node: _Node) -> NativeIceChannel:
        state = node.state
        signal = await ViewerChannel.connect(
            self.config.relay_url,
            state["token"],
            root_pub=state["root_pub"],
            org=state["org_uuid"],
            open_timeout=min(10, self.config.timeout),
        )
        native = await upgrade_via_ice(
            signal,
            token=state["token"],
            root_pub=state["root_pub"],
            org=state["org_uuid"],
        )
        local, remote = _candidate_types(native.peer)
        if native.policy != "relay_only" or (local, remote) != ("relay", "relay"):
            await native.close()
            raise ProductionTurnError(
                f"TURN path was not relay-only: policy={native.policy} "
                f"local={local} remote={remote}"
            )
        return native

    @staticmethod
    async def _op(channel: ViewerChannel, request: dict) -> tuple[dict, bytes]:
        await channel.send_message(canonical_json(request))
        raw = await asyncio.wait_for(channel.recv_message(), timeout=25)
        header, separator, body = raw.partition(b"\n")
        if not separator:
            raise ProductionTurnError("application reply omitted its header boundary")
        value = json.loads(header)
        if not isinstance(value, dict):
            raise ProductionTurnError("application reply header is not an object")
        return value, body

    async def _prove_direction(self, node: _Node, label: str) -> None:
        native = await self._upgrade(node)
        try:
            local, remote = _candidate_types(native.peer)
            header, artifact = await self._op(native.channel, {"v": 1, "op": "fetch"})
            if header.get("kind") != "mission" or len(artifact) <= 60 * 1024:
                raise ProductionTurnError("Mission fetch did not cross the chunk boundary")
            subscribed, _ = await self._op(
                native.channel, {"v": 1, "op": "subscribe"}
            )
            stream_key = bytes.fromhex(subscribed["stream_key"])
            answer = f"TURN feed {label}"
            node.events.emit(
                "mission_control:conversation",
                {
                    "event": "answered",
                    "mission_id": node.state["mission_id"],
                    "pillar_id": None,
                    "entry_id": f"turn-{label}",
                    "question": {"entry_id": f"turn-{label}", "answer": answer},
                    "update": None,
                },
            )
            sealed = await asyncio.wait_for(native.channel.recv_feed(), timeout=25)
            feed = json.loads(open_stream_frame(stream_key, sealed))
            if feed.get("question", {}).get("answer") != answer:
                raise ProductionTurnError("Mission feed body did not round-trip")
            post_feed, _ = await self._op(native.channel, {"v": 1, "op": "head"})
            if post_feed.get("status") != "ok":
                raise ProductionTurnError("channel failed after interleaved feed delivery")
            evidence = {
                "direction": label,
                "policy": native.policy,
                "local_candidate": local,
                "remote_candidate": remote,
                "artifact_bytes": len(artifact),
                "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
                "feed": "verified",
                "post_feed": "ok",
            }
            self._evidence["directions"].append(evidence)
            self.announce(
                f"{label}: relay/relay Mission fetch {len(artifact)} bytes, "
                "live feed verified, post-feed request ok"
            )
        finally:
            await native.close()

    async def _prove_renewal(self, node: _Node) -> None:
        first = await self._upgrade(node)
        second = None
        first_closed = False
        try:
            _, first_body = await self._op(first.channel, {"v": 1, "op": "fetch"})
            second = await self._upgrade(node)
            _, second_body = await self._op(second.channel, {"v": 1, "op": "fetch"})
            await first.close()
            first_closed = True
            _, after_break = await self._op(
                second.channel, {"v": 1, "op": "fetch"}
            )
            if not (len(first_body) == len(second_body) == len(after_break)):
                raise ProductionTurnError("renewed channel returned a different artifact")
            self._evidence["renewal"] = {
                "make_before_break": True,
                "fresh_expiry": second.expires_at >= first.expires_at,
                "artifact_bytes": len(after_break),
                "post_break": "ok",
            }
            self.announce(
                "renewal: second relay/relay channel served before and after first close"
            )
        finally:
            if second is not None:
                await second.close()
            if not first_closed:
                await first.close()

    def _cleanup(self, *, strict: bool) -> dict:
        revoked = 0
        stopped = 0
        errors = []
        for node in reversed(self._nodes):
            if not self.config.keep_state:
                try:
                    result = self._fixture(
                        "revoke",
                        {
                            "slug": node.slug,
                            "password": node.password,
                            "registry_url": self.config.registry_url,
                            "token": node.state["token"],
                        },
                        self._node_env(node.root, node.slug),
                    )
                    if result.get("revoked") is not True:
                        raise ProductionTurnError("throwaway link was not revoked")
                    revoked += 1
                except Exception as exc:
                    errors.append(f"link revoke: {type(exc).__name__}")
            node.connector.terminate()
            with contextlib.suppress(Exception):
                node.connector.wait(timeout=5)
            if node.connector.poll() is None:
                node.connector.kill()
                with contextlib.suppress(Exception):
                    node.connector.wait(timeout=5)
            if node.connector.poll() is None:
                errors.append("connector remained alive")
            else:
                stopped += 1
            node.events.close()
        if not self.config.keep_state and self._scratch is not None:
            shutil.rmtree(self._scratch, ignore_errors=True)
            if self._scratch.exists():
                errors.append("secret roots remained on disk")
        if errors and strict:
            raise ProductionTurnError("cleanup failed: " + ", ".join(errors))
        return {
            "links_revoked": revoked,
            "connectors_stopped": stopped,
            "secret_roots_removed": (
                not self.config.keep_state
                and self._scratch is not None
                and not self._scratch.exists()
            ),
            "state_retained": self.config.keep_state,
            "errors": errors,
        }

    def run(self) -> dict:
        self._preflight()
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self._scratch = Path(tempfile.mkdtemp(prefix=self.run_id + "-"))
        try:
            first = self._start_node(1)
            second = self._start_node(2)
            asyncio.run(
                asyncio.wait_for(
                    self._run_network(first, second), timeout=self.config.timeout * 4
                )
            )
        except BaseException:
            self._cleanup(strict=False)
            raise
        cleanup = self._cleanup(strict=True)
        self._evidence["cleanup"] = cleanup
        self._evidence["status"] = "pass"
        evidence_path = self.run_dir / "evidence.json"
        evidence_path.write_text(
            json.dumps(self._evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.announce(
            f"cleanup: {cleanup['links_revoked']} links revoked, "
            f"{cleanup['connectors_stopped']} connectors stopped, "
            f"secret_roots_removed={cleanup['secret_roots_removed']}"
        )
        self.announce(f"PASS: durable non-secret evidence: {evidence_path}")
        return self._evidence

    async def _run_network(self, first: _Node, second: _Node) -> None:
        await self._prove_direction(first, "node-b-to-node-a")
        await self._prove_direction(second, "node-a-to-node-b")
        await self._prove_renewal(first)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-url", required=True)
    parser.add_argument("--relay-url", required=True)
    parser.add_argument("--artifacts-dir", type=Path, default=Path("harness-artifacts"))
    parser.add_argument("--ttl", type=int, default=3600)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--site-bytes", type=int, default=70_000)
    parser.add_argument(
        "--keep-state",
        action="store_true",
        help="retain secret-bearing roots and TTL state for diagnosis",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = ProductionTurnConfig(
        registry_url=args.registry_url,
        relay_url=args.relay_url,
        artifacts_dir=args.artifacts_dir,
        ttl=args.ttl,
        timeout=args.timeout,
        site_bytes=args.site_bytes,
        keep_state=args.keep_state,
    )
    try:
        ProductionTurnHarness(config).run()
    except (ProductionTurnError, OSError, subprocess.SubprocessError) as exc:
        print(f"PRODUCTION TURN ACCEPTANCE FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
