"""Issue, persist, and activate node-local persona wildcard TLS certificates.

The org root never enters this process.  DNS changes are signed by the
existing serving child through its exact-scope ``serve:dns-01`` certificate;
Certbot sees only one order-bound Unix socket. Verified production pairs are
sealed in the audited vault and materialized into protected ramfs for Caddy.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import time
import uuid

from tools.dashboard.acme_dns01 import Dns01Authority, Dns01Client
from tools.dashboard.acme_dns01_hook import Dns01HookServer
from tools.dashboard.link_serving_supervisor import serve_cert_state
from tools.graph import settings_ops
from tools.graph.schemas.service_certificate import (
    SERVICE_CERTIFICATE_REVISION,
    SERVICE_CERTIFICATE_SET_ID,
    ServiceCertificateV1,
)
from tools.graph.schemas.vault_credential import (
    VAULT_AUDITED_SET_ID,
    VAULT_CREDENTIAL_REVISION,
)
from tools.network.idkit import DelegationCert, KeyPair


ACME_ROOT = Path("/run/autonomy-keycache/service-acme")
GATEWAY_CERT = Path("/run/autonomy-keycache/service-gateway/tls.crt")
GATEWAY_KEY = Path("/run/autonomy-keycache/service-gateway/tls.key")
STATUS_PATH = Path("/run/autonomy-keycache/service-gateway/tls-status.json")
PERSONA_CERT_ROOT = Path("/run/autonomy-keycache/service-gateway/personas")
ACME_VAULT_KEY = "service.acme.production"
ACME_BUNDLE_VERSION = 1
CERTIFICATE_CHECK_INTERVAL_SECONDS = 6 * 3600
RENEWAL_WINDOW_SECONDS = 30 * 24 * 3600
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


def _compose_environment() -> dict[str, str]:
    """Environment for Compose interpolation in a fresh ``docker exec``.

    ``AUTONOMY_HOST_ROOT`` is derived by the dashboard entrypoint and therefore
    exists in the long-lived server process, but Docker does not retroactively
    add that export to later ``docker exec`` processes. Re-derive the same host
    path from the compose-declared data root and refuse an ambiguous relative
    bind source.
    """
    env = dict(os.environ)
    host_root = env.get("AUTONOMY_HOST_ROOT")
    if not host_root:
        host_data_root = env.get("AUTONOMY_HOST_DATA_ROOT")
        if host_data_root:
            host_root = str(Path(host_data_root) / "code")
    if not host_root or not Path(host_root).is_absolute():
        raise ServiceCertificateError("AUTONOMY_HOST_ROOT is unavailable")
    env["AUTONOMY_HOST_ROOT"] = host_root
    return env


def certificate_name(org: str, domain_identity: str) -> str:
    """A stable Certbot lineage name; v2 passes its domain reservation id."""
    digest = hashlib.sha256(f"{org}\0{domain_identity}".encode()).hexdigest()[:24]
    return f"service-{digest}"


def _certbot_command(
    apex: str, cert_name: str, *, staging: bool, renew: bool = False
) -> list[str]:
    command = [
        *_compose_base(), "run", "--rm", "--no-deps",
        "-e", "AUTONOMY_ACME_SOCKET=/run/autonomy-acme/dns01.sock",
        "service-certbot", "renew" if renew else "certonly", "--manual",
        "--preferred-challenges", "dns",
        "--manual-auth-hook", "/usr/local/bin/autonomy-dns01-hook present",
        "--manual-cleanup-hook", "/usr/local/bin/autonomy-dns01-hook cleanup",
        "--non-interactive",
        "--config-dir", "/run/autonomy-acme/config",
        "--work-dir", "/run/autonomy-acme/work",
        "--logs-dir", "/run/autonomy-acme/logs",
        "--cert-name", cert_name,
    ]
    if renew:
        command.append("--no-random-sleep-on-renew")
    else:
        command.extend([
            "--agree-tos", "--register-unsafely-without-email",
            "--key-type", "ecdsa", "--elliptic-curve", "secp256r1",
            "-d", apex, "-d", f"*.{apex}",
        ])
    if staging:
        command.append("--staging")
    return command


def _acme_bundle_payload() -> dict:
    """Serialize the bounded production Certbot config as one credential."""
    _prune_acme_lineages()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        if (ACME_ROOT / "config").is_dir():
            archive.add(ACME_ROOT / "config", arcname="config", recursive=True)
    value = json.dumps({
        "version": ACME_BUNDLE_VERSION,
        "archive": base64.b64encode(buffer.getvalue()).decode("ascii"),
    }, separators=(",", ":"), sort_keys=True)
    return {"value": value}


def _write_acme_bundle() -> None:
    settings_ops.write_by_key(
        VAULT_AUDITED_SET_ID,
        VAULT_CREDENTIAL_REVISION,
        ACME_VAULT_KEY,
        _acme_bundle_payload(),
        org=None,
        state="raw",
    )


def _restore_acme_bundle() -> bool:
    row = settings_ops.read_set_key(
        VAULT_AUDITED_SET_ID, ACME_VAULT_KEY, org=None, peers=[]
    )
    if row is None:
        return False
    try:
        payload = json.loads(row["payload"]["value"])
        if payload != {"version": ACME_BUNDLE_VERSION, "archive": payload["archive"]}:
            raise ValueError("unexpected fields")
        raw = base64.b64decode(payload["archive"], validate=True)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            archive.extractall(ACME_ROOT, filter="data")
    except Exception as exc:
        raise ServiceCertificateError("ACME account bundle is invalid") from exc
    return True


def _prune_acme_lineages() -> None:
    """Keep current + previous Certbot archive generations per lineage."""
    archive_root = ACME_ROOT / "config" / "archive"
    if not archive_root.is_dir():
        return
    pattern = re.compile(r"^(cert|chain|fullchain|privkey)(\d+)\.pem$")
    for lineage in archive_root.iterdir():
        if not lineage.is_dir():
            continue
        generations: dict[int, list[Path]] = {}
        for path in lineage.iterdir():
            match = pattern.fullmatch(path.name)
            if match:
                generations.setdefault(int(match.group(2)), []).append(path)
        keep = set(sorted(generations)[-2:])
        for generation, paths in generations.items():
            if generation not in keep:
                for path in paths:
                    path.unlink()


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


def certificate_key(org: str, persona_label: str) -> str:
    return f"{org}:{persona_label}"


def certificate_vault_key(org: str, persona_label: str, serial: str) -> str:
    return f"service.tls.{org}.{persona_label}.{serial}"


def _pair_directory(org: str, persona_label: str, serial: str) -> Path:
    return PERSONA_CERT_ROOT / org / persona_label / serial


def pair_paths(metadata: dict) -> tuple[Path, Path]:
    directory = _pair_directory(
        metadata["org"], metadata["persona_label"], metadata["serial"]
    )
    return directory / "tls.crt", directory / "tls.key"


def gateway_pair_paths(metadata: dict) -> tuple[str, str]:
    """Return the same ramfs pair in the Caddy container's mount frame."""
    relative = _pair_directory(
        metadata["org"], metadata["persona_label"], metadata["serial"]
    ).relative_to(Path("/run/autonomy-keycache/service-gateway"))
    root = Path("/run/autonomy-service-gateway-certs") / relative
    return str(root / "tls.crt"), str(root / "tls.key")


def _bundle_payload(cert_path: Path, key_path: Path, metadata: dict) -> dict:
    value = json.dumps(
        {
            "fullchain_pem": cert_path.read_text(),
            "private_key_pem": key_path.read_text(),
            "org": metadata["org"],
            "persona_label": metadata["persona_label"],
            "serial": metadata["serial"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return {"value": value}


def _read_bundle(vault_key: str) -> dict:
    row = settings_ops.read_set_key(
        VAULT_AUDITED_SET_ID, vault_key, org=None, peers=[]
    )
    if row is None or row.get("vault_error") is not None:
        raise ServiceCertificateError("certificate vault bundle is unavailable")
    payload = row.get("payload")
    try:
        bundle = json.loads(payload["value"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ServiceCertificateError("certificate vault bundle is invalid") from exc
    if set(bundle) != {
        "fullchain_pem", "private_key_pem", "org", "persona_label", "serial"
    }:
        raise ServiceCertificateError("certificate vault bundle has invalid fields")
    return bundle


def _write_bundle(cert_path: Path, key_path: Path, metadata: dict) -> str:
    vault_key = certificate_vault_key(
        metadata["org"], metadata["persona_label"], metadata["serial"]
    )
    settings_ops.write_by_key(
        VAULT_AUDITED_SET_ID,
        VAULT_CREDENTIAL_REVISION,
        vault_key,
        _bundle_payload(cert_path, key_path, metadata),
        org=None,
        state="raw",
    )
    return vault_key


def _materialize_bundle(metadata: dict, bundle: dict) -> tuple[Path, Path]:
    for name in ("org", "persona_label", "serial"):
        if bundle.get(name) != metadata.get(name):
            raise ServiceCertificateError("certificate bundle identity mismatch")
    cert_path, key_path = pair_paths(metadata)
    cert_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    candidate_cert = cert_path.parent / ".candidate.crt"
    candidate_key = cert_path.parent / ".candidate.key"
    try:
        candidate_cert.write_text(bundle["fullchain_pem"])
        candidate_key.write_text(bundle["private_key_pem"])
        os.chmod(candidate_cert, 0o600)
        os.chmod(candidate_key, 0o600)
        verified = _verify_pair(candidate_cert, candidate_key, metadata["apex"])
        if verified["serial"] != metadata["serial"]:
            raise ServiceCertificateError("certificate serial does not match metadata")
        _atomic_copy(candidate_cert, cert_path, 0o644)
        _atomic_copy(candidate_key, key_path, 0o600)
    finally:
        for candidate in (candidate_cert, candidate_key):
            with contextlib.suppress(FileNotFoundError):
                candidate.unlink()
    return cert_path, key_path


def _retire_old_ramfs(metadata: dict) -> None:
    root = PERSONA_CERT_ROOT / metadata["org"] / metadata["persona_label"]
    keep = {metadata["serial"]}
    previous = metadata.get("previous_serial")
    if isinstance(previous, str):
        keep.add(previous)
    if not root.is_dir():
        return
    for child in root.iterdir():
        if child.is_dir() and child.name not in keep:
            # Bound secret lifetime explicitly. This is ramfs (no durable
            # media), but overwrite each allocated file before unlink so an
            # older private-key generation is not left readable in the live
            # mount until memory reclamation happens to run.
            for path in child.rglob("*"):
                if path.is_symlink():
                    path.unlink()
                    continue
                if not path.is_file():
                    continue
                with path.open("r+b", buffering=0) as handle:
                    remaining = path.stat().st_size
                    while remaining:
                        chunk = min(remaining, 65536)
                        handle.write(b"\0" * chunk)
                        remaining -= chunk
                    os.fsync(handle.fileno())
            shutil.rmtree(child)


def activate_pair(
    org: str,
    persona_label: str,
    cert_path: Path,
    key_path: Path,
    metadata: dict,
) -> dict:
    """Seal, verify, materialize, then publish the active metadata pointer."""
    current = certificate_metadata(org, persona_label)
    value = dict(metadata)
    value.update({"org": org, "persona_label": persona_label})
    vault_key = certificate_vault_key(
        value["org"], value["persona_label"], value["serial"]
    )
    expected_bundle = json.loads(_bundle_payload(cert_path, key_path, value)["value"])
    existing = settings_ops.read_set_key(
        VAULT_AUDITED_SET_ID, vault_key, org=None, peers=[]
    )
    if existing is None:
        value["vault_key"] = _write_bundle(cert_path, key_path, value)
        bundle = _read_bundle(value["vault_key"])
    else:
        bundle = _read_bundle(vault_key)
        if bundle != expected_bundle:
            raise ServiceCertificateError(
                "existing certificate vault bundle conflicts with the verified pair"
            )
        value["vault_key"] = vault_key
    if current is not None and current.get("serial") != value["serial"]:
        value["previous_serial"] = current["serial"]
    ServiceCertificateV1.validate(value)
    _materialize_bundle(value, bundle)
    settings_ops.write_by_key(
        SERVICE_CERTIFICATE_SET_ID,
        SERVICE_CERTIFICATE_REVISION,
        certificate_key(org, persona_label),
        value,
        org="machine",
        state="raw",
    )
    _retire_old_ramfs(value)
    return value


def certificate_metadata(org: str, persona_label: str) -> dict | None:
    row = settings_ops.read_set_key(
        SERVICE_CERTIFICATE_SET_ID,
        certificate_key(org, persona_label),
        org="machine",
        peers=[],
    )
    return dict(row["payload"]) if row is not None else None


def materialize_active_pairs() -> list[dict]:
    """Restore every active pair from the warm audited vault into ramfs."""
    result = []
    rows = settings_ops.read_owned_set(
        SERVICE_CERTIFICATE_SET_ID,
        org="machine",
        target_revision=SERVICE_CERTIFICATE_REVISION,
    ).members
    for row in rows:
        metadata = dict(row.payload)
        bundle = _read_bundle(metadata["vault_key"])
        _materialize_bundle(metadata, bundle)
        _retire_old_ramfs(metadata)
        result.append(metadata)
    return result


def active_gateway_pair(org: str, persona_label: object) -> tuple[str, str] | None:
    """Return one verified materialized pair in the gateway mount frame."""
    if not isinstance(persona_label, str):
        return None
    metadata = certificate_metadata(org, persona_label)
    if metadata is None:
        # One-time migration bridge for the certificate issued during the
        # emergency launch. It is exact-identity and expiry checked, and the
        # manager imports it into the audited vault as soon as that vault is
        # warm. Without this bridge the deployment that introduces persistence
        # would take the already-live site offline before import can run.
        try:
            legacy = json.loads(STATUS_PATH.read_text())
        except Exception:
            return None
        if (
            legacy.get("org") != org
            or legacy.get("apex") != f"{persona_label}.serve.auto.network"
            or int(legacy.get("not_after") or 0) <= int(time.time())
            or not all(
                path.is_file() and path.stat().st_size > 0
                for path in (GATEWAY_CERT, GATEWAY_KEY)
            )
        ):
            return None
        return (
            "/run/autonomy-service-gateway-certs/tls.crt",
            "/run/autonomy-service-gateway-certs/tls.key",
        )
    if int(metadata.get("not_after") or 0) <= int(time.time()):
        return None
    cert_path, key_path = pair_paths(metadata)
    if not all(path.is_file() and path.stat().st_size > 0 for path in (cert_path, key_path)):
        return None
    return gateway_pair_paths(metadata)


async def obtain(
    org: str, persona_label: str, *, staging: bool = False
) -> tuple[dict, bytes, bytes]:
    """Obtain and verify a candidate without activating it."""
    if not _LABEL_RE.fullmatch(persona_label):
        raise ServiceCertificateError("invalid persona label")
    client = load_dns01_client(org)
    order = f"service-{uuid.uuid4().hex}"
    apex = f"{persona_label}.serve.auto.network"
    cert_name = order if staging else certificate_name(org, persona_label)
    socket_path = ACME_ROOT / "dns01.sock"
    ACME_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        # A killed prior job may not have reached its finally block. Never
        # merge ambiguous working state into the authoritative vaulted copy,
        # and never let a staging run observe production account material.
        for child in ("config", "work", "logs"):
            shutil.rmtree(ACME_ROOT / child, ignore_errors=True)
        if not staging:
            _restore_acme_bundle()
        renew = (
            not staging
            and (ACME_ROOT / "config" / "renewal" / f"{cert_name}.conf").is_file()
        )
        async with Dns01HookServer(client, order, socket_path):
            proc = await asyncio.create_subprocess_exec(
                *_certbot_command(
                    apex, cert_name, staging=staging, renew=renew
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_compose_environment(),
            )
            stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", "replace")[-2000:]
            raise ServiceCertificateError(
                f"Certbot failed ({proc.returncode}): {detail}"
            )
        lineage = ACME_ROOT / "config" / "live" / cert_name
        cert_path = lineage / "fullchain.pem"
        key_path = lineage / "privkey.pem"
        metadata = _verify_pair(cert_path, key_path, apex)
        metadata.update({"org": org, "staging": staging, "activated_at": int(time.time())})
        if not staging:
            _write_acme_bundle()
        return metadata, cert_path.read_bytes(), key_path.read_bytes()
    finally:
        # Certbot's account, work, logs, and generated key are deliberately
        # ephemeral.  The verified pair has already moved to the gateway
        # ramfs; leave no second copy behind.
        for child in ("config", "work", "logs"):
            shutil.rmtree(ACME_ROOT / child, ignore_errors=True)


async def issue(org: str, persona_label: str, *, staging: bool = False) -> dict:
    if not staging and not settings_ops.personal_delegate_audited_is_warm():
        # A production order is useful only if its account state and verified
        # pair can be read back and committed. Refuse before contacting ACME
        # when this process cannot complete that transaction.
        raise ServiceCertificateError(
            "certificate vault is locked; unlock before issuing a certificate"
        )
    metadata, cert_bytes, key_bytes = await obtain(
        org, persona_label, staging=staging
    )
    candidate = ACME_ROOT / f"activate-{uuid.uuid4().hex}"
    cert_path = candidate / "fullchain.pem"
    key_path = candidate / "privkey.pem"
    candidate.mkdir(parents=True, mode=0o700)
    try:
        cert_path.write_bytes(cert_bytes)
        key_path.write_bytes(key_bytes)
        os.chmod(cert_path, 0o600)
        os.chmod(key_path, 0o600)
        return activate_pair(org, persona_label, cert_path, key_path, metadata)
    finally:
        shutil.rmtree(candidate, ignore_errors=True)


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
    if args.staging:
        metadata, cert_bytes, key_bytes = asyncio.run(
            obtain(args.org, args.persona_label, staging=True)
        )
        # A staging run proves the complete DNS/CA path but can never replace
        # a browser-trusted production pair.
        del cert_bytes, key_bytes
        print(json.dumps(metadata))
        return
    print(json.dumps(asyncio.run(issue(args.org, args.persona_label))))


if __name__ == "__main__":
    main()
