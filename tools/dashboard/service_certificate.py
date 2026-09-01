"""Issue and activate the node-local persona wildcard TLS certificate.

The org root never enters this process.  DNS changes are signed by the
existing serving child through its exact-scope ``serve:dns-01`` certificate;
Certbot sees only one order-bound Unix socket.  The resulting TLS key remains
in the node's protected ramfs and is atomically exposed to local Caddy.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

from tools.dashboard.acme_dns01 import Dns01Authority, Dns01Client
from tools.dashboard.acme_dns01_hook import Dns01HookServer
from tools.dashboard.link_serving_supervisor import serve_cert_state
from tools.network.idkit import DelegationCert, KeyPair


ACME_ROOT = Path("/run/autonomy-keycache/service-acme")
GATEWAY_CERT = Path("/run/autonomy-keycache/service-gateway/tls.crt")
GATEWAY_KEY = Path("/run/autonomy-keycache/service-gateway/tls.key")
STATUS_PATH = Path("/run/autonomy-keycache/service-gateway/tls-status.json")
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class ServiceCertificateError(RuntimeError):
    pass


def load_dns01_client(org: str) -> Dns01Client:
    state = serve_cert_state(org)
    if state.get("status") != "ok":
        raise ServiceCertificateError("serving credential is unavailable")
    row = state.get("row") or {}
    wire = row.get("dns01_cert")
    if not isinstance(wire, str):
        raise ServiceCertificateError(
            "serving credential lacks serve:dns-01 authority; unlock to renew it"
        )
    try:
        cert = DelegationCert.from_json(wire)
        key = KeyPair.from_private_hex(Path(state["key_path"]).read_text().strip())
        authority = Dns01Authority(key=key, cert=cert)
    except Exception as exc:
        raise ServiceCertificateError("DNS-01 authority is invalid") from exc
    return Dns01Client(org, authority)


def _compose_base() -> list[str]:
    compose_file = Path(os.environ.get("AUTONOMY_CONTAINER_ROOT", "/app")) / "docker-compose.yml"
    if not compose_file.is_file():
        compose_file = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    return [
        "docker", "compose", "--project-name", "autonomy",
        "--project-directory", str(compose_file.parent),
        "-f", str(compose_file), "--profile", "service-certbot",
    ]


def _certbot_command(apex: str, order: str, *, staging: bool) -> list[str]:
    command = [
        *_compose_base(), "run", "--rm", "--no-deps",
        "-e", "AUTONOMY_ACME_SOCKET=/run/autonomy-acme/dns01.sock",
        "service-certbot", "certonly", "--manual",
        "--preferred-challenges", "dns",
        "--manual-auth-hook", "/usr/local/bin/autonomy-dns01-hook present",
        "--manual-cleanup-hook", "/usr/local/bin/autonomy-dns01-hook cleanup",
        "--agree-tos", "--register-unsafely-without-email", "--non-interactive",
        "--key-type", "ecdsa", "--elliptic-curve", "secp256r1",
        "--config-dir", "/run/autonomy-acme/config",
        "--work-dir", "/run/autonomy-acme/work",
        "--logs-dir", "/run/autonomy-acme/logs",
        "--cert-name", order, "-d", apex, "-d", f"*.{apex}",
    ]
    if staging:
        command.append("--staging")
    return command


def _verify_pair(cert_path: Path, key_path: Path, apex: str) -> dict:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        sans = set(cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.DNSName))
    except Exception as exc:
        raise ServiceCertificateError("ACME result does not parse") from exc
    if sans != {apex, f"*.{apex}"}:
        raise ServiceCertificateError("ACME certificate SANs do not match the persona")
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise ServiceCertificateError("ACME private key is not ECDSA P-256")
    cert_pub = cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    key_pub = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if cert_pub != key_pub:
        raise ServiceCertificateError("ACME certificate does not match its private key")
    now = time.time()
    if cert.not_valid_after_utc.timestamp() <= now:
        raise ServiceCertificateError("ACME certificate is already expired")
    return {
        "apex": apex,
        "sans": sorted(sans),
        "not_before": int(cert.not_valid_before_utc.timestamp()),
        "not_after": int(cert.not_valid_after_utc.timestamp()),
        "serial": format(cert.serial_number, "x"),
    }


def _atomic_copy(source: Path, destination: Path, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with source.open("rb") as src, temporary.open("xb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


async def issue(org: str, persona_label: str, *, staging: bool = False) -> dict:
    if not _LABEL_RE.fullmatch(persona_label):
        raise ServiceCertificateError("invalid persona label")
    client = load_dns01_client(org)
    order = f"service-{uuid.uuid4().hex}"
    apex = f"{persona_label}.serve.auto.network"
    socket_path = ACME_ROOT / "dns01.sock"
    ACME_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        async with Dns01HookServer(client, order, socket_path):
            proc = await asyncio.create_subprocess_exec(
                *_certbot_command(apex, order, staging=staging),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", "replace")[-2000:]
            raise ServiceCertificateError(
                f"Certbot failed ({proc.returncode}): {detail}"
            )
        lineage = ACME_ROOT / "config" / "live" / order
        cert_path = lineage / "fullchain.pem"
        key_path = lineage / "privkey.pem"
        metadata = _verify_pair(cert_path, key_path, apex)
        _atomic_copy(cert_path, GATEWAY_CERT, 0o644)
        _atomic_copy(key_path, GATEWAY_KEY, 0o600)
        metadata.update({"org": org, "staging": staging, "activated_at": int(time.time())})
        status_tmp = STATUS_PATH.with_suffix(".tmp")
        status_tmp.write_text(json.dumps(metadata, sort_keys=True) + "\n")
        os.chmod(status_tmp, 0o600)
        os.replace(status_tmp, STATUS_PATH)
        return metadata
    finally:
        # Certbot's account, work, logs, and generated key are deliberately
        # ephemeral.  The verified pair has already moved to the gateway
        # ramfs; leave no second copy behind.
        for child in ("config", "work", "logs"):
            shutil.rmtree(ACME_ROOT / child, ignore_errors=True)


def status() -> dict:
    try:
        value = json.loads(STATUS_PATH.read_text())
    except Exception:
        return {"status": "missing"}
    if not all(path.is_file() for path in (GATEWAY_CERT, GATEWAY_KEY)):
        return {"status": "missing"}
    if int(value.get("not_after") or 0) <= int(time.time()):
        return {"status": "expired", **value}
    return {"status": "ok", **value}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--org", required=True)
    parser.add_argument("--persona-label", required=True)
    parser.add_argument("--staging", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(issue(args.org, args.persona_label, staging=args.staging))))


if __name__ == "__main__":
    main()
