#!/usr/bin/env python3
"""Exercise a real session:8000 through the hardened Compose Caddy gateway.

Run from the Compose host after launching a disposable bridge-network session.
The harness uses the normal Dashboard publication API, creates its acceptance
certificate only inside the dashboard's ramfs, starts Caddy with the test-only
8443 override, loads the trusted route through the Unix socket, and retains a
sanitized proof bundle. Cleanup is the default.
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import secrets
import socket
import ssl
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx


class ProofFailure(RuntimeError):
    pass


def _cookies_from_jar(path: Path) -> httpx.Cookies:
    jar = http.cookiejar.MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    cookies = httpx.Cookies()
    for cookie in jar:
        if cookie.domain_specified:
            cookies.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)
        else:
            cookies.set(cookie.name, cookie.value, path=cookie.path)
    return cookies


def _run(argv: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )


def acceptance_certificate_command(hostname: str) -> list[str]:
    """Build the ephemeral acceptance certificate command.

    The public origin belongs in subjectAltName.  X.509 Common Name is a
    legacy presentation field capped at 64 characters, while a valid Service
    origin can be longer once its app and persona labels are combined.
    """
    return [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "ec",
        "-pkeyopt",
        "ec_paramgen_curve:P-256",
        "-nodes",
        "-days",
        "1",
        "-subj",
        "/CN=Autonomy Service Gateway Acceptance",
        "-addext",
        f"subjectAltName=DNS:{hostname}",
        "-keyout",
        "/run/autonomy-keycache/service-gateway/tls.key",
        "-out",
        "/run/autonomy-keycache/service-gateway/tls.crt",
    ]


def _require(result: subprocess.CompletedProcess[str], label: str) -> str:
    if result.returncode != 0:
        raise ProofFailure(
            f"{label} failed ({result.returncode}): {result.stderr[-1000:]}"
        )
    return result.stdout


def _docker_exec(
    container: str,
    argv: list[str],
    *,
    timeout: float = 30.0,
    user: str | None = None,
) -> str:
    command = ["docker", "exec"]
    if user is not None:
        command.extend(["--user", user])
    command.extend([container, *argv])
    return _require(
        _run(command, timeout=timeout),
        f"docker exec {container}",
    )


def _wait_healthy(container_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        result = _run(["docker", "inspect", container_id], timeout=5)
        if result.returncode == 0:
            documents = json.loads(result.stdout)
            latest = documents[0] if documents else {}
            if latest.get("State", {}).get("Health", {}).get("Status") == "healthy":
                return latest
        time.sleep(0.25)
    raise ProofFailure(
        "Service gateway did not become healthy: "
        + json.dumps(latest.get("State", {}), sort_keys=True)[:1000]
    )


def _curl(
    hostname: str,
    port: int,
    path: str,
    output_dir: Path,
    name: str,
    *extra: str,
) -> dict:
    headers_path = output_dir / f"{name}.headers"
    body_path = output_dir / f"{name}.body"
    argv = [
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
        "--resolve",
        f"{hostname}:{port}:127.0.0.1",
        "--dump-header",
        str(headers_path),
        "--output",
        str(body_path),
        "--write-out",
        "%{http_code}",
        *extra,
        f"https://{hostname}:{port}{path}",
    ]
    result = _run(argv, timeout=20)
    status_text = result.stdout.strip()
    status = int(status_text) if status_text.isdigit() else 0
    return {
        "name": name,
        "method": extra[extra.index("-X") + 1] if "-X" in extra else "GET",
        "path": path,
        "status": status,
        "curl_exit": result.returncode,
        "stderr": result.stderr[-500:],
        "headers": headers_path.read_text(errors="replace") if headers_path.exists() else "",
        "body": body_path.read_text(errors="replace") if body_path.exists() else "",
    }


def _recv_exact(connection: ssl.SSLSocket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise ProofFailure("WebSocket closed before the expected frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _websocket_echo(hostname: str, port: int, message: str) -> dict:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    connection = context.wrap_socket(raw, server_hostname=hostname)
    connection.settimeout(5)
    try:
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        request = (
            f"GET /ws HTTP/1.1\r\nHost: {hostname}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        connection.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response and len(response) < 16384:
            response += connection.recv(4096)
        if not response.startswith(b"HTTP/1.1 101"):
            raise ProofFailure(f"WebSocket handshake failed: {response[:500]!r}")

        payload = message.encode()
        if len(payload) > 125:
            raise ValueError("acceptance WebSocket message is too large")
        mask = secrets.token_bytes(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        connection.sendall(bytes((0x81, 0x80 | len(payload))) + mask + masked)
        header = _recv_exact(connection, 2)
        if header[0] & 0x0F != 1:
            raise ProofFailure("WebSocket response was not a text frame")
        length = header[1] & 0x7F
        echoed = _recv_exact(connection, length).decode()
        if echoed != message:
            raise ProofFailure(f"WebSocket echo mismatch: {echoed!r}")
        return {"status": 101, "sent": message, "received": echoed}
    finally:
        connection.close()


def _expect_http(result: dict, status: int, body_contains: str = "") -> None:
    if result["curl_exit"] != 0 or result["status"] != status:
        raise ProofFailure(
            f"{result['name']}: expected HTTP {status}, got "
            f"exit={result['curl_exit']} status={result['status']} "
            f"stderr={result['stderr']!r}"
        )
    if body_contains and body_contains not in result["body"]:
        raise ProofFailure(f"{result['name']}: response omitted {body_contains!r}")


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    transcript: list[dict[str, Any]] = []
    reservation_id = ""
    hostname = ""
    gateway_id = ""
    compose = [
        "docker",
        "compose",
        "--project-name",
        args.compose_project,
        "--project-directory",
        str(Path(args.compose_dir).resolve()),
        "-f",
        str(Path(args.compose_dir).resolve() / "docker-compose.yml"),
        "-f",
        str(
            Path(args.compose_dir).resolve()
            / "tools/network/acceptance/compose.service-gateway.yml"
        ),
        "--profile",
        "service-gateway",
    ]

    cookies = (
        _cookies_from_jar(Path(args.cookie_jar))
        if args.cookie_jar
        else httpx.Cookies()
    )
    dashboard_headers = {"X-Graph-Org": args.org}
    if args.bearer_env:
        bearer = os.environ.get(args.bearer_env)
        if not bearer:
            raise ProofFailure(f"bearer environment {args.bearer_env!r} is unset")
        dashboard_headers["Authorization"] = f"Bearer {bearer}"

    client = httpx.Client(
        base_url=args.base_url.rstrip("/"),
        headers=dashboard_headers,
        cookies=cookies,
        verify=args.verify_dashboard_tls,
        timeout=15,
    )

    def api(method: str, path: str, body: dict | None = None, expected=(200,)) -> dict:
        response = client.request(method, path, json=body)
        try:
            value: Any = response.json() if response.content else {}
        except ValueError:
            value = response.text[:1000]
        transcript.append(
            {
                "plane": "dashboard",
                "method": method,
                "path": path,
                "request": body,
                "status": response.status_code,
                "response": value,
            }
        )
        if response.status_code not in expected:
            raise ProofFailure(
                f"Dashboard {method} {path}: {response.status_code} {value!r}"
            )
        return value if isinstance(value, dict) else {}

    try:
        _require(
            _run(
                [
                    "docker",
                    "exec",
                    "-d",
                    args.session,
                    "python3",
                    "/workspace/repo/tools/network/acceptance/service_gateway_canary.py",
                    "--port",
                    "8000",
                    "--log",
                    "/tmp/service-gateway-canary.jsonl",
                ]
            ),
            "start session canary",
        )

        reservation = api(
            "POST",
            "/api/network/service-reservations",
            {"app_label": args.app_label},
            (200, 201),
        )["reservation"]
        reservation_id = reservation["reservation_id"]
        hostname = reservation["origin"].removeprefix("https://")
        target_path = f"/api/network/service-targets/{reservation_id}"
        api(
            "PUT",
            target_path,
            {"session_id": args.session, "port": 8000},
            (200, 201),
        )
        api("POST", target_path + "/check", expected=(200,))

        _docker_exec(
            args.dashboard_container,
            acceptance_certificate_command(hostname),
            user="1000:1000",
        )
        # The operator applies the dashboard's new control-volume mount once.
        # This proof must not silently recreate that stateful service itself.
        _require(
            _run(
                [*compose, "up", "-d", "--no-deps", "service-gateway"],
                timeout=120,
            ),
            "start Caddy",
        )
        gateway_id = _require(
            _run([*compose, "ps", "-q", "service-gateway"]), "locate Caddy"
        ).strip()
        if not gateway_id:
            raise ProofFailure("Compose returned no Service gateway container id")
        inspect = _wait_healthy(gateway_id)

        loaded = json.loads(
            _docker_exec(
                args.dashboard_container,
                [
                    "python3",
                    "-m",
                    "tools.dashboard.service_gateway",
                    "load",
                    "--org",
                    args.org,
                    "--reservation-id",
                    reservation_id,
                ],
                user="1000:1000",
            )
        )
        if loaded.get("hostname") != hostname:
            raise ProofFailure("loaded gateway hostname disagrees with reservation")
        active_config = json.loads(
            _docker_exec(
                args.dashboard_container,
                [
                    "curl",
                    "-fsS",
                    "--unix-socket",
                    "/run/autonomy-service-gateway/admin.sock",
                    "http://localhost/config/",
                ],
                user="1000:1000",
            )
        )
        active_config_text = json.dumps(active_config, sort_keys=True)
        if "PRIVATE KEY" in active_config_text or "BEGIN CERTIFICATE" in active_config_text:
            raise ProofFailure("Caddy config embedded certificate/key bytes")
        if hostname not in active_config_text or f"{args.session}:8000" not in active_config_text:
            raise ProofFailure("active Caddy config omitted the exact trusted route")

        calls = [
            _curl(hostname, args.port, "/", output_dir, "get-root"),
            _curl(hostname, args.port, "/assets/site.css", output_dir, "relative-asset"),
            _curl(
                hostname,
                args.port,
                "/form",
                output_dir,
                "post-form",
                "-X",
                "POST",
                "-H",
                "Content-Type: application/x-www-form-urlencoded",
                "--data",
                "value=sovereign",
            ),
            _curl(
                hostname,
                args.port,
                "/resource",
                output_dir,
                "put-resource",
                "-X",
                "PUT",
                "--data-binary",
                "relay-bytes",
            ),
            _curl(hostname, args.port, "/redirect", output_dir, "redirect"),
            _curl(hostname, args.port, "/stream", output_dir, "stream"),
        ]
        for result, status, marker in (
            (calls[0], 200, "<form"),
            (calls[1], 200, "rgb(1,2,3)"),
            (calls[2], 200, "value=sovereign"),
            (calls[3], 200, "relay-bytes"),
            (calls[4], 302, ""),
            (calls[5], 200, "stream-three"),
        ):
            _expect_http(result, status, marker)
        if "Location: /final" not in calls[4]["headers"]:
            raise ProofFailure("redirect did not preserve the relative Location")
        websocket = _websocket_echo(hostname, args.port, "sovereign-websocket")

        before_unknown = _docker_exec(
            args.session, ["sh", "-c", "wc -l < /tmp/service-gateway-canary.jsonl"]
        ).strip()
        unknown = _curl(
            "unknown.persona-00000000000000000000.serve.auto.network",
            args.port,
            "/",
            output_dir,
            "unknown-host",
        )
        after_unknown = _docker_exec(
            args.session, ["sh", "-c", "wc -l < /tmp/service-gateway-canary.jsonl"]
        ).strip()
        if unknown["status"] in range(200, 400) or before_unknown != after_unknown:
            raise ProofFailure("unknown hostname reached the session canary")

        unavailable = json.loads(
            _docker_exec(
                args.dashboard_container,
                [
                    "python3",
                    "-m",
                    "tools.dashboard.service_gateway",
                    "unavailable",
                    "--org",
                    args.org,
                    "--reservation-id",
                    reservation_id,
                ],
                user="1000:1000",
            )
        )
        removed = _curl(hostname, args.port, "/", output_dir, "removed-route")
        _expect_http(removed, 503, "Service unavailable")

        canary_rows = [
            json.loads(line)
            for line in _docker_exec(
                args.session, ["cat", "/tmp/service-gateway-canary.jsonl"]
            ).splitlines()
            if line.strip()
        ]
        observed = {(row["method"], row["path"], row["body"]) for row in canary_rows}
        required = {
            ("GET", "/", ""),
            ("GET", "/assets/site.css", ""),
            ("POST", "/form", "value=sovereign"),
            ("PUT", "/resource", "relay-bytes"),
            ("GET", "/redirect", ""),
            ("GET", "/stream", ""),
            ("GET", "/ws", ""),
        }
        if not required <= observed:
            raise ProofFailure(f"canary omitted requests: {sorted(required - observed)!r}")

        session_boundary = _run(
            [
                "docker",
                "exec",
                args.session,
                "sh",
                "-c",
                "test ! -e /run/autonomy-service-gateway/admin.sock && "
                "test ! -e /var/run/docker.sock",
            ]
        )
        _require(session_boundary, "session socket isolation")

        log_result = _run(["docker", "logs", gateway_id])
        _require(log_result, "collect Caddy logs")
        caddy_logs = log_result.stdout + log_result.stderr
        mounts = inspect.get("Mounts", [])
        host_config = inspect.get("HostConfig", {})
        session_inspect = json.loads(
            _require(_run(["docker", "inspect", args.session]), "inspect session")
        )[0]
        dashboard_inspect = json.loads(
            _require(
                _run(["docker", "inspect", args.dashboard_container]),
                "inspect dashboard",
            )
        )[0]
        gateway_networks = sorted(
            inspect.get("NetworkSettings", {}).get("Networks", {})
        )
        session_networks = sorted(
            session_inspect.get("NetworkSettings", {}).get("Networks", {})
        )
        dashboard_networks = sorted(
            dashboard_inspect.get("NetworkSettings", {}).get("Networks", {})
        )
        security = {
            "container_id": gateway_id,
            "user": inspect.get("Config", {}).get("User"),
            "readonly_rootfs": host_config.get("ReadonlyRootfs"),
            "cap_drop": host_config.get("CapDrop"),
            "security_opt": host_config.get("SecurityOpt"),
            "memory": host_config.get("Memory"),
            "nano_cpus": host_config.get("NanoCpus"),
            "pids_limit": host_config.get("PidsLimit"),
            "networks": gateway_networks,
            "session_networks": session_networks,
            "dashboard_networks": dashboard_networks,
            "port_bindings": host_config.get("PortBindings"),
            "mounts": [
                {
                    "type": mount.get("Type"),
                    "destination": mount.get("Destination"),
                    "rw": mount.get("RW"),
                }
                for mount in mounts
            ],
        }
        if any(mount.get("Destination") == "/var/run/docker.sock" for mount in mounts):
            raise ProofFailure("Caddy received the Docker socket")
        if (
            security["user"] != "1000:1000"
            or not security["readonly_rootfs"]
            or security["cap_drop"] != ["ALL"]
            or "no-new-privileges:true" not in (security["security_opt"] or [])
            or security["memory"] != 128 * 1024 * 1024
            or security["nano_cpus"] != 500_000_000
            or security["pids_limit"] != 64
        ):
            raise ProofFailure("Caddy inspect disagrees with the hardening contract")
        if (
            len(gateway_networks) != 1
            or gateway_networks[0] not in session_networks
            or gateway_networks[0] not in dashboard_networks
        ):
            raise ProofFailure("Caddy, dashboard, and session do not share one node network")
        expected_binding = {"9443/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(args.port)}]}
        if security["port_bindings"] != expected_binding:
            raise ProofFailure("acceptance override exposed a port beyond loopback 8443")

        evidence = {
            "ok": True,
            "organization": args.org,
            "session": args.session,
            "reservation": reservation,
            "hostname": hostname,
            "gateway_load": loaded,
            "gateway_unavailable": unavailable,
            "http": calls,
            "websocket": websocket,
            "unknown_host": unknown,
            "removed_route": removed,
            "canary_requests": canary_rows,
            "security": security,
            "caddy_config_redacted": active_config,
            "caddy_logs": caddy_logs.splitlines(),
            "dashboard_transcript": transcript,
        }
        (output_dir / "evidence.json").write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return evidence
    finally:
        (output_dir / "dashboard-transcript.json").write_text(
            json.dumps(transcript, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if not args.keep_running:
            if reservation_id:
                try:
                    api(
                        "DELETE",
                        f"/api/network/service-targets/{reservation_id}",
                        expected=(204,),
                    )
                    api(
                        "PUT",
                        f"/api/network/service-reservations/{reservation_id}/state",
                        {"state": "released"},
                        (200,),
                    )
                except Exception:
                    pass
            _run([*compose, "rm", "-f", "-s", "service-gateway"], timeout=60)
            _run(
                [
                    "docker",
                    "exec",
                    args.dashboard_container,
                    "rm",
                    "-f",
                    "/run/autonomy-keycache/service-gateway/tls.crt",
                    "/run/autonomy-keycache/service-gateway/tls.key",
                ]
            )
            _run(
                [
                    "docker",
                    "exec",
                    args.session,
                    "pkill",
                    "-f",
                    "service_gateway_canary.py",
                ]
            )
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--org", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--app-label", required=True)
    parser.add_argument("--dashboard-container", default="autonomy-dashboard-1")
    parser.add_argument("--compose-dir", default="/opt/autonomy/code")
    parser.add_argument("--compose-project", default="autonomy")
    parser.add_argument("--port", type=int, default=8443)
    auth = parser.add_mutually_exclusive_group(required=True)
    auth.add_argument("--cookie-jar")
    auth.add_argument("--bearer-env")
    parser.add_argument(
        "--output-dir", default="/opt/autonomy/data/service-gateway-acceptance"
    )
    parser.add_argument("--verify-dashboard-tls", action="store_true")
    parser.add_argument("--keep-running", action="store_true")
    args = parser.parse_args()
    evidence = run(args)
    print(
        json.dumps(
            {
                "ok": True,
                "hostname": evidence["hostname"],
                "session": evidence["session"],
                "evidence": str(Path(args.output_dir) / "evidence.json"),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
