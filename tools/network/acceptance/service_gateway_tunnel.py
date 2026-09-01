#!/usr/bin/env python3
"""Prove real TLS/SNI publication through RelayKit into local Caddy.

Run inside the composed Dashboard container after the existing Service gateway
acceptance harness has left a reservation, target, certificate, Caddy route,
and port-8000 canary running with ``--keep-running``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx

from tools.dashboard import service_publication
from tools.dashboard.service_gateway_stream import LocalCaddyStreamHandler
from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.registry.signing import sign_request
from tools.network.relaykit.connector import TunnelConnector


CAPS = ("host-lease/1", "tls-stream/1")


class ProofFailure(RuntimeError):
    pass


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_registry(port: int, process: subprocess.Popen, log_path: Path) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    tail = log_path.read_text(errors="replace")[-2000:] if log_path.exists() else ""
    raise ProofFailure(f"registry did not become healthy: {tail}")


def _register_org(port: int, org_id: str, root: KeyPair) -> None:
    path = "/v1/orgs"
    body = {
        "org_uuid": org_id,
        "root_pub": root.public_hex,
        "recovery_policy": "none",
    }
    response = httpx.post(
        f"http://127.0.0.1:{port}{path}",
        json=sign_request(root, "POST", path, body, ts=int(time.time())),
        timeout=5,
    )
    if response.status_code != 201:
        raise ProofFailure(f"org registration failed: {response.status_code} {response.text}")


async def _curl(ingress_port: int, hostname: str, path: str, *extra: str) -> dict:
    process = await asyncio.create_subprocess_exec(
        "curl",
        "--silent",
        "--show-error",
        "--insecure",
        "--connect-timeout",
        "5",
        "--max-time",
        "15",
        "--noproxy",
        "*",
        "--connect-to",
        f"{hostname}:443:127.0.0.1:{ingress_port}",
        "--dump-header",
        "-",
        *extra,
        f"https://{hostname}{path}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    rendered = stdout.decode(errors="replace")
    head, separator, body = rendered.partition("\r\n\r\n")
    if not separator:
        head, separator, body = rendered.partition("\n\n")
    status_line = head.splitlines()[0] if head else ""
    status = int(status_line.split()[1]) if len(status_line.split()) >= 2 else 0
    return {
        "path": path,
        "status": status,
        "exit_code": process.returncode,
        "headers": head,
        "body": body,
        "stderr": stderr.decode(errors="replace")[-500:],
    }


async def _run(args, registry_port: int, ingress_port: int, root: KeyPair) -> dict:
    member = service_publication._reservation_for_target(
        args.graph_org, args.reservation, serving=False
    )
    payload = member.payload
    expected_host = (
        f"{payload['app_label']}.{payload['persona_label']}.serve.auto.network"
    )
    if args.hostname != expected_host:
        raise ProofFailure(f"hostname mismatch: expected {expected_host}")

    tunnel_org = str(uuid.uuid4())
    _register_org(registry_port, tunnel_org, root)
    serve_key, machine_key = KeyPair.generate(), KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(
        root,
        serve_key.public_hex,
        scope=("tunnel:serve",),
        org=tunnel_org,
        subject=Subject("persona", payload["persona_pub"]),
        not_before=now - 300,
        not_after=now + 86_400,
    )
    connector = TunnelConnector(
        f"ws://127.0.0.1:{registry_port}",
        tunnel_org,
        serve_key,
        cert,
        min_backoff=0.1,
        max_backoff=0.5,
        machine_key=machine_key,
        caps=CAPS,
        stream_handler=LocalCaddyStreamHandler(args.graph_org),
    )
    connector_task = asyncio.create_task(connector.run())
    try:
        await asyncio.wait_for(connector.connected.wait(), 10)
        if "tls-stream/1" not in connector.accepted_caps:
            raise ProofFailure(f"tls-stream capability not negotiated: {connector.accepted_caps}")
        lease = await connector.serve_host(args.reservation, args.hostname)
        if lease.get("ok") is not True:
            raise ProofFailure(f"hostname lease failed: {lease}")

        calls = [
            await _curl(ingress_port, args.hostname, "/"),
            await _curl(
                ingress_port,
                args.hostname,
                "/form",
                "-X",
                "POST",
                "-H",
                "Content-Type: application/x-www-form-urlencoded",
                "--data",
                "value=through-relaykit",
            ),
            await _curl(ingress_port, args.hostname, "/redirect"),
        ]
        expected = ((200, "<form"), (200, "value=through-relaykit"), (302, ""))
        for call, (status, marker) in zip(calls, expected):
            if call["exit_code"] != 0 or call["status"] != status or marker not in call["body"]:
                raise ProofFailure(f"tunnel request failed: {call}")

        return {
            "ok": True,
            "hostname": args.hostname,
            "reservation": args.reservation,
            "negotiated_caps": list(connector.accepted_caps),
            "lease": lease,
            "path": [
                "registry-stream-ingress",
                "relaykit-tls-stream/1",
                "LocalCaddyStreamHandler",
                "service-gateway:9443",
                "session:8000",
            ],
            "calls": calls,
        }
    finally:
        connector.stop()
        connector_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await connector_task


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph-org", required=True)
    parser.add_argument("--reservation", required=True)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    registry_port, ingress_port = _free_port(), _free_port()
    root = KeyPair.generate()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="service-gateway-tunnel-") as temp:
        temp_path = Path(temp)
        log_path = temp_path / "registry.log"
        with log_path.open("wb") as log:
            registry = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "tools.network.registry",
                    "--db",
                    str(temp_path / "registry.db"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(registry_port),
                    "--stream-ingress-port",
                    str(ingress_port),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONPATH": "/app"},
            )
        try:
            _wait_registry(registry_port, registry, log_path)
            evidence = asyncio.run(_run(args, registry_port, ingress_port, root))
            evidence["registry_log"] = log_path.read_text(errors="replace").splitlines()
            output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
            print(json.dumps({"ok": True, "evidence": str(output)}, sort_keys=True))
        finally:
            registry.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                registry.wait(timeout=5)


if __name__ == "__main__":
    main()
