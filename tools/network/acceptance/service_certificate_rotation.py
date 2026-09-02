#!/usr/bin/env python3
"""Prove real persona-certificate staging, rotation, and sibling isolation.

Run this on the Compose host with two already-published HTTPS origins. The
primary certificate is rotated while both origins are continuously requested;
the proof succeeds only after the primary's served serial changes and at least
20 successful probes span that change. No publication state is modified.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import ssl
import subprocess
import time
from urllib.parse import urlparse
import uuid

import httpx
from cryptography import x509


class ProofFailure(RuntimeError):
    pass


def _exec(
    container: str, argv: list[str], *, timeout: float = 300
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", "-u", "autonomy", container, *argv],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _certificate_command(org: str, persona: str, *, staging: bool) -> list[str]:
    argv = [
        "python3", "-m", "tools.dashboard.service_certificate",
        "--org", org, "--persona-label", persona,
    ]
    if staging:
        argv.append("--staging")
    return argv


def _served_serial(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ProofFailure(f"not an HTTPS origin: {url!r}")
    port = parsed.port or 443
    context = ssl.create_default_context()
    import socket

    with socket.create_connection((parsed.hostname, port), timeout=5) as raw:
        with context.wrap_socket(raw, server_hostname=parsed.hostname) as tls:
            cert = x509.load_der_x509_certificate(tls.getpeercert(binary_form=True))
    return format(cert.serial_number, "x")


def _probe(url: str) -> dict:
    started = time.monotonic()
    try:
        response = httpx.get(url, follow_redirects=False, timeout=10)
        ok = 200 <= response.status_code < 400
        return {
            "at": time.time(),
            "elapsed_seconds": time.monotonic() - started,
            "status": response.status_code,
            "ok": ok,
        }
    except Exception as exc:
        return {
            "at": time.time(),
            "elapsed_seconds": time.monotonic() - started,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _dns_cleanup_probe(container: str, org: str) -> dict:
    """Exercise the real signed control and both authoritative servers."""
    order = f"rotation-proof-{uuid.uuid4().hex}"
    value = f"proof-{uuid.uuid4().hex}"
    code = r'''
import json, sys, time
from tools.dashboard.service_certificate import load_dns01_client
from tools.dashboard.acme_dns01 import AUTHORITATIVE_NAMESERVERS, _query_authoritative_txt, wait_authoritative_txt
org, order, value = sys.argv[1:]
client = load_dns01_client(org)
presented = client.present(order, value, ttl=60, lifetime=600)
visible = wait_authoritative_txt(presented["name"], value, timeout=60)
client.cleanup(order, value)
started = time.monotonic()
while time.monotonic() - started <= 60:
    if all(value not in _query_authoritative_txt(server, presented["name"]) for server in AUTHORITATIVE_NAMESERVERS):
        print(json.dumps({"name": presented["name"], "visible_seconds": visible, "removed_seconds": time.monotonic() - started}))
        raise SystemExit(0)
    time.sleep(0.5)
raise RuntimeError("challenge TXT remained authoritative past 60 seconds")
'''
    result = _exec(
        container, ["python3", "-c", code, org, order, value], timeout=90
    )
    if result.returncode != 0:
        raise ProofFailure(f"DNS cleanup proof failed: {result.stderr[-2000:]}")
    return json.loads(result.stdout)


def run(args: argparse.Namespace) -> dict:
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    before = _served_serial(args.primary_url)
    sibling_before = _served_serial(args.sibling_url)
    if sibling_before == before:
        raise ProofFailure("primary and sibling do not serve distinct certificates")
    dns_cleanup = _dns_cleanup_probe(args.dashboard_container, args.org)

    staging = _exec(
        args.dashboard_container,
        _certificate_command(args.org, args.persona, staging=True),
    )
    if staging.returncode != 0:
        raise ProofFailure(f"staging issuance failed: {staging.stderr[-2000:]}")
    staging_metadata = json.loads(staging.stdout)
    if staging_metadata.get("staging") is not True:
        raise ProofFailure("staging issuance did not identify the staging CA")
    if _served_serial(args.primary_url) != before:
        raise ProofFailure("staging issuance changed the publicly served certificate")

    probes: list[dict] = []
    for _ in range(5):
        pair = {
            "primary": _probe(args.primary_url),
            "sibling": _probe(args.sibling_url),
        }
        probes.append(pair)
        if not pair["primary"]["ok"] or not pair["sibling"]["ok"]:
            raise ProofFailure(f"baseline public probe failed: {pair!r}")

    rotation = subprocess.Popen(
        [
            "docker", "exec", "-u", "autonomy", args.dashboard_container,
            *_certificate_command(args.org, args.persona, staging=False),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    changed_at: float | None = None
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        primary = _probe(args.primary_url)
        sibling = _probe(args.sibling_url)
        probes.append({"primary": primary, "sibling": sibling})
        (output / "partial.json").write_text(
            json.dumps({"serial_before": before, "probes": probes}, indent=2) + "\n"
        )
        if not primary["ok"] or not sibling["ok"]:
            rotation.kill()
            rotation.communicate()
            raise ProofFailure(f"public probe failed during rotation: {probes[-1]!r}")
        if changed_at is None and _served_serial(args.primary_url) != before:
            changed_at = time.time()
        if changed_at is not None and len(probes) >= 20 and time.time() > changed_at:
            break
        time.sleep(args.interval)
    stdout, stderr = rotation.communicate(timeout=30)
    if rotation.returncode != 0:
        raise ProofFailure(f"production rotation failed: {stderr[-2000:]}")
    rotated = json.loads(stdout)
    after = _served_serial(args.primary_url)
    sibling_after = _served_serial(args.sibling_url)
    if changed_at is None or after == before or rotated.get("serial") != after:
        raise ProofFailure("verified rotation did not become the publicly served pair")
    if len(probes) < 20:
        raise ProofFailure("fewer than 20 successful probe pairs spanned rotation")
    if sibling_after != sibling_before:
        raise ProofFailure("rotating the primary changed the sibling certificate")

    evidence = {
        "ok": True,
        "primary_url": args.primary_url,
        "sibling_url": args.sibling_url,
        "serial_before": before,
        "serial_after": after,
        "sibling_serial": sibling_after,
        "changed_at": changed_at,
        "staging_serial": staging_metadata.get("serial"),
        "dns_cleanup": dns_cleanup,
        "probe_pairs": probes,
    }
    path = output / "evidence.json"
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    return {"ok": True, "evidence": str(path), "probe_pairs": len(probes)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dashboard-container", default="autonomy-dashboard-1")
    parser.add_argument("--org", required=True)
    parser.add_argument("--persona", required=True)
    parser.add_argument("--primary-url", required=True)
    parser.add_argument("--sibling-url", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--interval", type=float, default=0.25)
    args = parser.parse_args()
    try:
        print(json.dumps(run(args), sort_keys=True))
    except Exception as exc:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
        (Path(args.output_dir) / "failure.json").write_text(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                indent=2,
            )
            + "\n"
        )
        raise


if __name__ == "__main__":
    main()
