#!/usr/bin/env python3
"""Prove a real bridge-network session's Port 8000 through the Dashboard API.

The disposable session must have been launched through the target Dashboard's
normal session path, be attached to the Dashboard's discovered Compose network,
and run an HTTP canary on ``0.0.0.0:8000``.  This harness creates no container
and touches no Dashboard database directly: every publication mutation uses the
same authenticated HTTPS routes as the operator UI.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx


RESERVATION_SET_ID = "autonomy.network.namespace-reservation"
TARGET_SET_ID = "autonomy.network.service-target"


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


def _expect(response: httpx.Response, status: int | tuple[int, ...]) -> dict:
    statuses = (status,) if isinstance(status, int) else status
    if response.status_code not in statuses:
        raise ProofFailure(
            f"{response.request.method} {response.request.url.path}: expected "
            f"{statuses}, got {response.status_code}: {response.text[:500]}"
        )
    if not response.content:
        return {}
    value = response.json()
    if not isinstance(value, dict):
        raise ProofFailure("Dashboard returned non-object JSON")
    return value


def _expect_error(response: httpx.Response, status: int, code: str) -> None:
    body = _expect(response, status)
    if body != {"ok": False, "error": code}:
        raise ProofFailure(f"expected exact {code!r} error, got {body!r}")


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    transcript: list[dict[str, Any]] = []
    cookies = (
        _cookies_from_jar(Path(args.cookie_jar))
        if args.cookie_jar
        else httpx.Cookies()
    )
    headers = {"X-Graph-Org": args.org}
    if args.bearer_env:
        bearer = os.environ.get(args.bearer_env)
        if not bearer:
            raise ProofFailure(
                f"bearer environment variable {args.bearer_env!r} is unset"
            )
        headers["Authorization"] = f"Bearer {bearer}"

    with httpx.Client(
        base_url=args.base_url.rstrip("/"),
        headers=headers,
        cookies=cookies,
        verify=args.verify_tls,
        timeout=15.0,
    ) as client:

        def call(method: str, path: str, body: dict | None = None) -> httpx.Response:
            response = client.request(method, path, json=body)
            try:
                response_body: Any = response.json()
            except ValueError:
                response_body = response.text[:1000]
            transcript.append(
                {
                    "method": method,
                    "path": path,
                    "request": body,
                    "status": response.status_code,
                    "response": response_body,
                }
            )
            return response

        reservation = _expect(
            call(
                "POST",
                "/api/network/service-reservations",
                {"app_label": args.app_label},
            ),
            (200, 201),
        )["reservation"]
        if reservation["state"] != "active":
            raise ProofFailure("disposable reservation is not active")
        reservation_id = reservation["reservation_id"]
        target_path = f"/api/network/service-targets/{reservation_id}"
        check_path = target_path + "/check"
        state_path = f"/api/network/service-reservations/{reservation_id}/state"

        bound = _expect(
            call(
                "PUT",
                target_path,
                {"session_id": args.session, "port": 8000},
            ),
            (200, 201),
        )["target"]
        if bound["session_id"] != args.session or bound["port"] != 8000:
            raise ProofFailure("bind projection does not name the requested session:8000")
        same = _expect(
            call(
                "PUT",
                target_path,
                {"session_id": args.session, "port": 8000},
            ),
            200,
        )["target"]
        if same != bound:
            raise ProofFailure("exact rebind was not an idempotent no-op")

        checked = _expect(call("POST", check_path), 200)["target"]
        if checked["session_id"] != args.session or checked["port"] != 8000:
            raise ProofFailure("serving check returned a different target")

        _expect_error(
            call(
                "PUT",
                target_path,
                {"session_id": args.session, "port": args.closed_port},
            ),
            409,
            "target_port_unreachable",
        )
        listed = _expect(call("GET", "/api/network/service-targets"), 200)
        durable = next(
            (row for row in listed["targets"] if row["reservation_id"] == reservation_id),
            None,
        )
        if durable != bound:
            raise ProofFailure("closed-port refusal changed the durable target")

        paused = _expect(call("PUT", state_path, {"state": "paused"}), 200)[
            "reservation"
        ]
        if paused["origin"] != reservation["origin"]:
            raise ProofFailure("pause changed the stable origin")
        _expect_error(call("POST", check_path), 409, "reservation_paused")
        paused_rebind = _expect(
            call(
                "PUT",
                target_path,
                {"session_id": args.session, "port": 8000},
            ),
            200,
        )["target"]
        if paused_rebind != bound:
            raise ProofFailure("paused revalidation changed the target")
        resumed = _expect(call("PUT", state_path, {"state": "active"}), 200)[
            "reservation"
        ]
        if resumed["origin"] != reservation["origin"]:
            raise ProofFailure("resume changed the stable origin")
        _expect(call("POST", check_path), 200)

        _expect(call("DELETE", target_path), 204)
        _expect_error(call("POST", check_path), 404, "target_not_found")
        rebound = _expect(
            call(
                "PUT",
                target_path,
                {"session_id": args.session, "port": 8000},
            ),
            201,
        )["target"]
        if rebound["reservation_id"] != reservation_id:
            raise ProofFailure("unbind/rebind changed the reservation identity")

        raw_targets = _expect(
            call("GET", f"/api/graph/settings/{quote(TARGET_SET_ID, safe='')}"),
            200,
        )
        raw_reservations = _expect(
            call(
                "GET",
                f"/api/graph/settings/{quote(RESERVATION_SET_ID, safe='')}",
            ),
            200,
        )

    raw_target = next(
        member
        for member in raw_targets.get("members", [])
        if member.get("key") == reservation_id
    )
    payload = raw_target.get("payload") or {}
    forbidden = {
        "reservation_id",
        "org",
        "organization",
        "network",
        "ip",
        "docker_ip",
        "url",
        "upstream",
    }
    if forbidden & set(payload):
        raise ProofFailure(f"raw target contains forbidden fields: {forbidden & set(payload)}")
    if payload.get("session_id") != args.session or payload.get("port") != 8000:
        raise ProofFailure("raw target disagrees with the proven live target")
    raw_reservation = next(
        member
        for member in raw_reservations.get("members", [])
        if member.get("key") == reservation_id
    )
    if raw_reservation.get("payload", {}).get("state") != "active":
        raise ProofFailure("raw reservation did not durably resume")

    evidence = {
        "ok": True,
        "organization": args.org,
        "session": args.session,
        "reservation": reservation,
        "bound": bound,
        "checked": checked,
        "rebound": rebound,
        "raw_target": raw_target,
        "raw_reservation": raw_reservation,
        "transcript": transcript,
    }
    (output_dir / "evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--org", required=True)
    parser.add_argument("--session", required=True)
    auth = parser.add_mutually_exclusive_group(required=True)
    auth.add_argument("--cookie-jar")
    auth.add_argument(
        "--bearer-env",
        help=(
            "Name of an environment variable containing the bearer token; "
            "the token is never accepted as an argument or written to evidence"
        ),
    )
    parser.add_argument("--app-label", required=True)
    parser.add_argument("--closed-port", type=int, default=8001)
    parser.add_argument(
        "--output-dir", default="/workspace/output/service-target-port8000"
    )
    parser.add_argument("--verify-tls", action="store_true")
    args = parser.parse_args()
    evidence = run(args)
    print(
        json.dumps(
            {
                "ok": True,
                "origin": evidence["reservation"]["origin"],
                "session": evidence["session"],
                "evidence": str(Path(args.output_dir) / "evidence.json"),
            }
        )
    )


if __name__ == "__main__":
    main()
