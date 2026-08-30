#!/usr/bin/env python3
"""Exercise the Phase 1A reservation lifecycle against a real Dashboard.

The selected organization must be disposable: release is terminal, and this
proof deliberately releases its ``--port-label`` reservation.  The script
does not create an organization, bypass authentication, or write Settings
directly.  It uses the same HTTPS API and operator cookie as the Dashboard UI.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx


SET_ID = "autonomy.network.namespace-reservation"


class ProofFailure(RuntimeError):
    pass


def _cookies_from_jar(path: Path) -> httpx.Cookies:
    jar = http.cookiejar.MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    cookies = httpx.Cookies()
    for cookie in jar:
        cookies.set(
            cookie.name,
            cookie.value,
            domain=cookie.domain,
            path=cookie.path,
        )
    return cookies


def _expect(response: httpx.Response, status: int | tuple[int, ...]) -> dict:
    statuses = (status,) if isinstance(status, int) else status
    if response.status_code not in statuses:
        raise ProofFailure(
            f"{response.request.method} {response.request.url.path}: "
            f"expected {statuses}, got {response.status_code}: {response.text[:500]}"
        )
    if not response.content:
        return {}
    try:
        value = response.json()
    except ValueError as exc:
        raise ProofFailure("Dashboard returned non-JSON evidence") from exc
    if not isinstance(value, dict):
        raise ProofFailure("Dashboard returned a non-object JSON response")
    return value


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    transcript: list[dict[str, Any]] = []
    headers = {"X-Graph-Org": args.org}
    cookies = _cookies_from_jar(Path(args.cookie_jar))

    with httpx.Client(
        base_url=args.base_url.rstrip("/"),
        headers=headers,
        cookies=cookies,
        verify=args.verify_tls,
        timeout=15.0,
    ) as client:

        def call(method: str, path: str, *, json_body: dict | None = None) -> httpx.Response:
            response = client.request(method, path, json=json_body)
            try:
                body: Any = response.json()
            except ValueError:
                body = response.text[:1000]
            transcript.append(
                {
                    "method": method,
                    "path": path,
                    "request": json_body,
                    "status": response.status_code,
                    "response": body,
                }
            )
            return response

        before = _expect(call("GET", "/api/network/service-reservations"), 200)
        docs = _expect(
            call(
                "POST",
                "/api/network/service-reservations",
                json_body={"app_label": args.docs_label},
            ),
            (200, 201),
        )["reservation"]
        port = _expect(
            call(
                "POST",
                "/api/network/service-reservations",
                json_body={"app_label": args.port_label},
            ),
            (200, 201),
        )["reservation"]
        if docs["reservation_id"] == port["reservation_id"]:
            raise ProofFailure("two app labels collapsed to one reservation")
        if docs["origin"] == port["origin"]:
            raise ProofFailure("two app labels collapsed to one origin")
        if docs["state"] != "active" or port["state"] != "active":
            raise ProofFailure("proof requires initially active disposable labels")

        duplicate = _expect(
            call(
                "POST",
                "/api/network/service-reservations",
                json_body={"app_label": args.port_label},
            ),
            200,
        )["reservation"]
        if duplicate != port:
            raise ProofFailure("duplicate create changed the reservation")

        state_path = f"/api/network/service-reservations/{port['reservation_id']}/state"
        paused = _expect(call("PUT", state_path, json_body={"state": "paused"}), 200)[
            "reservation"
        ]
        if paused["state"] != "paused":
            raise ProofFailure("pause did not become observable")
        while_paused = _expect(call("GET", "/api/network/service-reservations"), 200)
        docs_while_paused = next(
            row
            for row in while_paused["reservations"]
            if row["reservation_id"] == docs["reservation_id"]
        )
        if docs_while_paused != docs:
            raise ProofFailure("pausing port label changed its docs sibling")

        resumed = _expect(call("PUT", state_path, json_body={"state": "active"}), 200)[
            "reservation"
        ]
        if resumed["state"] != "active":
            raise ProofFailure("resume did not become observable")
        released = _expect(
            call("PUT", state_path, json_body={"state": "released"}), 200
        )["reservation"]
        if released["state"] != "released" or not released.get("released_at"):
            raise ProofFailure("terminal release is incomplete")
        same_release = _expect(
            call("PUT", state_path, json_body={"state": "released"}), 200
        )["reservation"]
        if same_release != released:
            raise ProofFailure("repeated release was not an idempotent no-op")
        _expect(call("PUT", state_path, json_body={"state": "active"}), 409)
        _expect(
            call(
                "POST",
                "/api/network/service-reservations",
                json_body={"app_label": args.port_label},
            ),
            409,
        )

        final = _expect(call("GET", "/api/network/service-reservations"), 200)
        raw = _expect(
            call("GET", f"/api/graph/settings/{quote(SET_ID, safe='')}"), 200
        )

    raw_rows = [
        member
        for member in raw.get("members", [])
        if member.get("key") in {docs["reservation_id"], port["reservation_id"]}
    ]
    if len(raw_rows) != 2:
        raise ProofFailure("raw Settings read did not return both reservations")
    for member in raw_rows:
        payload = member.get("payload") or {}
        forbidden = {"reservation_id", "org", "organization", "origin"} & set(payload)
        if forbidden:
            raise ProofFailure(f"raw payload repeats authority/identity fields: {forbidden}")

    evidence = {
        "ok": True,
        "organization": args.org,
        "before": before,
        "docs": docs,
        "port_released": released,
        "final": final,
        "raw_settings": raw_rows,
        "transcript": transcript,
    }
    (output_dir / "evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "transcript.json").write_text(
        json.dumps(transcript, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://localhost:8080")
    parser.add_argument("--org", required=True, help="Disposable founded organization slug")
    parser.add_argument("--cookie-jar", required=True, help="Netscape jar from graph session-auth")
    parser.add_argument("--docs-label", default="docs")
    parser.add_argument("--port-label", default="port-8000")
    parser.add_argument(
        "--output-dir",
        default="/workspace/output/service-reservation-api",
    )
    parser.add_argument("--verify-tls", action="store_true")
    args = parser.parse_args()
    evidence = run(args)
    print(json.dumps({"ok": True, "evidence": str(Path(args.output_dir) / "evidence.json"), "origins": [evidence["docs"]["origin"], evidence["port_released"]["origin"]]}))


if __name__ == "__main__":
    main()
