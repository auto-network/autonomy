#!/usr/bin/env python3
"""Prove automatic Service-gateway lifecycle on a real Compose node.

The host supplies one live bridge-network session.  This harness uses only the
normal Dashboard publication API, writes an ephemeral wildcard certificate
inside the Dashboard ramfs, and observes the supervisor rather than manually
starting or loading Caddy.  Cleanup is the default.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from tools.network.acceptance.service_gateway import (
    ProofFailure,
    _cookies_from_jar,
    _dashboard_exec,
    _require,
    _run,
    acceptance_certificate_command,
)


def _p95(values: list[float]) -> float:
    if not values:
        raise ValueError("p95 requires observations")
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * 0.95) - 1]


def _observe_gateway_status(
    read: Callable[[], dict[str, Any]],
    observations: list[dict[str, Any]],
    *,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Read and journal gateway state so failure cleanup cannot erase it."""
    try:
        current = read()
    except Exception as exc:
        observations.append({"at": now(), "transport_error": str(exc)})
        return {}
    observations.append({"at": now(), "gateway": current})
    return current


def _wait(
    label: str,
    predicate: Callable[[], Any],
    *,
    timeout: float,
) -> tuple[Any, float]:
    started = time.monotonic()
    deadline = started + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
        except Exception as exc:  # transient while Dashboard/Caddy restarts
            last = {"error": str(exc)}
        if last:
            return last, time.monotonic() - started
        time.sleep(0.1)
    raise ProofFailure(f"{label} did not converge within {timeout}s; last={last!r}")


def _gateway_container_ids() -> list[str]:
    result = _run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=com.docker.compose.project=autonomy",
            "--filter",
            "label=com.docker.compose.service=service-gateway",
        ],
        timeout=5,
    )
    _require(result, "locate Service gateway")
    return [line for line in result.stdout.splitlines() if line.strip()]


def _docker_inspect(container_id: str) -> dict:
    result = _run(["docker", "inspect", container_id], timeout=5)
    _require(result, "inspect Service gateway")
    documents = json.loads(result.stdout)
    return documents[0] if documents else {}


def _active_caddy_config(dashboard_container: str) -> dict:
    output = _dashboard_exec(
        dashboard_container,
        [
            "curl",
            "-fsS",
            "--unix-socket",
            "/run/autonomy-service-gateway/admin.sock",
            "http://localhost/config/",
        ],
        timeout=8,
    )
    return json.loads(output)


def _canary_request(dashboard_container: str, hostname: str) -> str:
    return _dashboard_exec(
        dashboard_container,
        [
            "curl",
            "-fsS",
            "--insecure",
            "--connect-timeout",
            "5",
            "--max-time",
            "10",
            "--connect-to",
            f"{hostname}:9443:service-gateway:9443",
            f"https://{hostname}:9443/",
        ],
        timeout=15,
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    transcript: list[dict[str, Any]] = []
    status_observations: list[dict[str, Any]] = []
    timings: list[dict[str, Any]] = []
    created: dict[str, dict[str, str]] = {}
    started_epoch = int(time.time())
    cert_backup = ""

    cookies = (
        _cookies_from_jar(Path(args.cookie_jar))
        if args.cookie_jar
        else httpx.Cookies()
    )
    headers = {"X-Graph-Org": args.org}
    if args.bearer_env:
        bearer = os.environ.get(args.bearer_env)
        if not bearer:
            raise ProofFailure(f"bearer environment {args.bearer_env!r} is unset")
        headers["Authorization"] = f"Bearer {bearer}"
    client = httpx.Client(
        base_url=args.base_url.rstrip("/"),
        headers=headers,
        cookies=cookies,
        verify=args.verify_dashboard_tls,
        timeout=15,
    )

    def api(
        method: str,
        path: str,
        body: dict | None = None,
        expected: tuple[int, ...] = (200,),
        *,
        record: bool = True,
    ) -> dict:
        response = client.request(method, path, json=body)
        try:
            value: Any = response.json() if response.content else {}
        except ValueError:
            value = response.text[:1000]
        if record:
            transcript.append(
                {
                    "at": time.time(),
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

    def status() -> dict:
        return _observe_gateway_status(
            lambda: api(
                "GET", "/api/network/service-gateway", record=False
            ).get("gateway", {}),
            status_observations,
        )

    def wait_loaded(
        reservation_id: str, baseline_revision: int, label: str, timeout: float
    ) -> tuple[dict, float]:
        return _wait(
            label,
            lambda: (
                current
                if (current := status()).get("state") == "healthy"
                and reservation_id in current.get("advertised_routes", [])
                and current.get("config_revision", 0) > baseline_revision
                else None
            ),
            timeout=timeout,
        )

    def wait_stopped(label: str, timeout: float = 60.0) -> tuple[dict, float]:
        return _wait(
            label,
            lambda: (
                current
                if (current := status()).get("state") == "stopped"
                and not current.get("advertised_routes")
                and not _gateway_container_ids()
                else None
            ),
            timeout=timeout,
        )

    def create_route(app_label: str) -> dict[str, str]:
        reservation = api(
            "POST",
            "/api/network/service-reservations",
            {"app_label": app_label},
            (200, 201),
        )["reservation"]
        reservation_id = reservation["reservation_id"]
        hostname = reservation["origin"].removeprefix("https://")
        api(
            "PUT",
            f"/api/network/service-targets/{reservation_id}",
            {"session_id": args.session, "port": 8000},
            (200, 201),
        )
        value = {"reservation_id": reservation_id, "hostname": hostname}
        created[reservation_id] = value
        return value

    def release_route(route: dict[str, str]) -> None:
        reservation_id = route["reservation_id"]
        api(
            "DELETE",
            f"/api/network/service-targets/{reservation_id}",
            expected=(204,),
        )
        api(
            "PUT",
            f"/api/network/service-reservations/{reservation_id}/state",
            {"state": "released"},
        )

    def record_timing(kind: str, label: str, duration: float, **extra: Any) -> None:
        timings.append(
            {"kind": kind, "label": label, "seconds": round(duration, 6), **extra}
        )

    try:
        initial = status()
        if initial.get("state") != "stopped" or initial.get("advertised_routes"):
            raise ProofFailure(f"gateway was not dormant at preflight: {initial!r}")
        if _gateway_container_ids():
            raise ProofFailure("Service gateway container existed at preflight")

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
                    "/tmp/service-gateway-lifecycle-canary.jsonl",
                ]
            ),
            "start session canary",
        )

        # Preserve any real keypair entirely inside ramfs; no key bytes cross
        # the Dashboard boundary or enter evidence.
        cert_backup = _dashboard_exec(
            args.dashboard_container,
            [
                "sh",
                "-c",
                "set -eu; d=/run/autonomy-keycache/service-gateway; "
                "if test -f $d/tls.crt && test -f $d/tls.key; then "
                "cp $d/tls.crt $d/tls.crt.acceptance-backup; "
                "cp $d/tls.key $d/tls.key.acceptance-backup; echo present; "
                "else echo absent; fi",
            ],
        ).strip()

        cold: list[float] = []
        for index in range(10):
            label = f"{args.app_prefix}-cold-{index:02d}"
            baseline = int(status().get("config_revision", 0))
            route = create_route(label)
            if index == 0:
                wildcard = "*." + route["hostname"].split(".", 1)[1]
                _dashboard_exec(
                    args.dashboard_container,
                    acceptance_certificate_command(wildcard),
                )
            loaded, elapsed = wait_loaded(
                route["reservation_id"], baseline, f"activate {label}", 10.0
            )
            cold.append(elapsed)
            record_timing("cold-activation", label, elapsed, status=loaded)
            config_text = json.dumps(
                _active_caddy_config(args.dashboard_container), sort_keys=True
            )
            if route["hostname"] not in config_text:
                raise ProofFailure(f"Caddy omitted cold route {label}")
            if "<form" not in _canary_request(
                args.dashboard_container, route["hostname"]
            ):
                raise ProofFailure(f"cold route {label} did not reach Port 8000")
            release_route(route)
            _stopped, stopped_in = wait_stopped(f"stop after {label}")
            record_timing("final-removal", label, stopped_in)

        changing: list[dict[str, str]] = []
        change_durations: list[float] = []
        for index in range(5):
            label = f"{args.app_prefix}-change-{index:02d}"
            baseline = int(status().get("config_revision", 0))
            started = time.monotonic()
            route = create_route(label)
            wait_loaded(route["reservation_id"], baseline, f"add {label}", 10.0)
            duration = time.monotonic() - started
            change_durations.append(duration)
            record_timing("route-change", f"add-{label}", duration)
            changing.append(route)

        for action, state in (("pause", "paused"), ("resume", "active")):
            for route in changing:
                baseline = int(status().get("config_revision", 0))
                started = time.monotonic()
                api(
                    "PUT",
                    f"/api/network/service-reservations/{route['reservation_id']}/state",
                    {"state": state},
                )
                wait_loaded(
                    route["reservation_id"], baseline, f"{action} route", 10.0
                )
                duration = time.monotonic() - started
                change_durations.append(duration)
                record_timing("route-change", f"{action}-{route['hostname']}", duration)

        for index, route in enumerate(changing):
            baseline = int(status().get("config_revision", 0))
            started = time.monotonic()
            release_route(route)
            if index == len(changing) - 1:
                wait_stopped("remove final change route")
            else:
                _wait(
                    "remove route",
                    lambda: (
                        current
                        if (current := status()).get("state") == "healthy"
                        and route["reservation_id"]
                        not in current.get("advertised_routes", [])
                        and current.get("config_revision", 0) > baseline
                        else None
                    ),
                    timeout=10.0,
                )
            duration = time.monotonic() - started
            change_durations.append(duration)
            record_timing("route-change", f"remove-{route['hostname']}", duration)

        restart_route = create_route(f"{args.app_prefix}-restart")
        baseline = int(status().get("config_revision", 0))
        wait_loaded(restart_route["reservation_id"], baseline, "restart route", 10.0)
        before_restart = status()
        _require(
            _run(["docker", "restart", args.dashboard_container], timeout=90),
            "restart Dashboard",
        )
        reconstructed, dashboard_recovery = _wait(
            "Dashboard route reconstruction",
            lambda: (
                current
                if (current := status()).get("state") == "healthy"
                and restart_route["reservation_id"]
                in current.get("advertised_routes", [])
                else None
            ),
            timeout=60.0,
        )
        record_timing(
            "dashboard-recovery",
            restart_route["hostname"],
            dashboard_recovery,
            before=before_restart,
            after=reconstructed,
        )

        gateway_ids = _gateway_container_ids()
        if len(gateway_ids) != 1:
            raise ProofFailure(f"expected one gateway before kill: {gateway_ids!r}")
        before_kill = status()
        _require(_run(["docker", "kill", gateway_ids[0]]), "kill Caddy")
        recovered, caddy_recovery = _wait(
            "Caddy process recovery and reload",
            lambda: (
                current
                if (current := status()).get("state") == "healthy"
                and restart_route["reservation_id"]
                in current.get("advertised_routes", [])
                and current.get("config_revision", 0)
                > before_kill.get("config_revision", 0)
                else None
            ),
            timeout=30.0,
        )
        record_timing(
            "caddy-recovery",
            restart_route["hostname"],
            caddy_recovery,
            before=before_kill,
            after=recovered,
        )
        if "<form" not in _canary_request(
            args.dashboard_container, restart_route["hostname"]
        ):
            raise ProofFailure("recovered Caddy did not reach Port 8000")

        release_route(restart_route)
        final_state, final_stop = wait_stopped("final dormant state")
        record_timing("final-removal", restart_route["hostname"], final_stop)

        cold_max = max(cold)
        changes_p95 = _p95(change_durations)
        if cold_max > 10.0:
            raise ProofFailure(f"cold activation exceeded 10s: {cold_max:.3f}s")
        if changes_p95 > 2.0:
            raise ProofFailure(f"route-change p95 exceeded 2s: {changes_p95:.3f}s")
        if final_stop > 60.0:
            raise ProofFailure(f"final stop exceeded 60s: {final_stop:.3f}s")

        ended_epoch = int(time.time()) + 1
        events_result = _run(
            [
                "docker",
                "events",
                "--since",
                str(started_epoch),
                "--until",
                str(ended_epoch),
                "--filter",
                "label=com.docker.compose.service=service-gateway",
                "--format",
                "{{json .}}",
            ],
            timeout=15,
        )
        docker_events = [
            json.loads(line)
            for line in events_result.stdout.splitlines()
            if line.strip()
        ] if events_result.returncode == 0 else []
        evidence = {
            "ok": True,
            "organization": args.org,
            "session": args.session,
            "cold_cycles": len(cold),
            "route_changes": len(change_durations),
            "cold_activation_max_seconds": round(cold_max, 6),
            "route_change_p95_seconds": round(changes_p95, 6),
            "final_stop_seconds": round(final_stop, 6),
            "final_state": final_state,
            "timings": timings,
            "docker_events": docker_events,
            "dashboard_transcript": transcript,
        }
        (output_dir / "evidence.json").write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return evidence
    finally:
        (output_dir / "dashboard-transcript.json").write_text(
            json.dumps(transcript, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (output_dir / "gateway-status.json").write_text(
            json.dumps(status_observations, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for route in list(created.values()):
            reservation_id = route["reservation_id"]
            try:
                api(
                    "DELETE",
                    f"/api/network/service-targets/{reservation_id}",
                    expected=(204, 404),
                )
            except Exception:
                pass
            try:
                api(
                    "PUT",
                    f"/api/network/service-reservations/{reservation_id}/state",
                    {"state": "released"},
                    expected=(200, 409),
                )
            except Exception:
                pass
        try:
            if cert_backup not in {"present", "absent"}:
                raise RuntimeError("certificate backup state was never established")
            _dashboard_exec(
                args.dashboard_container,
                [
                    "sh",
                    "-c",
                    "set -eu; d=/run/autonomy-keycache/service-gateway; "
                    "if test '" + cert_backup + "' = present; then "
                    "mv $d/tls.crt.acceptance-backup $d/tls.crt; "
                    "mv $d/tls.key.acceptance-backup $d/tls.key; "
                    "else rm -f $d/tls.crt $d/tls.key; fi",
                ],
            )
        except Exception:
            pass
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
    parser.add_argument("--app-prefix", required=True)
    parser.add_argument("--dashboard-container", default="autonomy-dashboard-1")
    auth = parser.add_mutually_exclusive_group(required=True)
    auth.add_argument("--cookie-jar")
    auth.add_argument("--bearer-env")
    parser.add_argument(
        "--output-dir",
        default="/opt/autonomy/data/service-gateway-acceptance/lifecycle",
    )
    parser.add_argument("--verify-dashboard-tls", action="store_true")
    args = parser.parse_args()
    evidence = run(args)
    print(
        json.dumps(
            {
                "ok": evidence["ok"],
                "cold_activation_max_seconds": evidence[
                    "cold_activation_max_seconds"
                ],
                "route_change_p95_seconds": evidence["route_change_p95_seconds"],
                "evidence": str(Path(args.output_dir) / "evidence.json"),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
