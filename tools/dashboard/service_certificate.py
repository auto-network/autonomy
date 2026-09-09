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
import datetime as _dt
import hashlib
import logging
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
    apex_for_identity,
    certificate_identity,
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


logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_NOISE_LINE_RE = re.compile(
    r"^\s*(Container .* (Creating|Created|Starting|Started|Stopping|Stopped|Removing|Removed)\s*$"
    r"|Ask for help or search for solutions|See the logfile .* for more details|Saving debug log to)",
)


def _meaningful_output(stdout: bytes, stderr: bytes, limit: int = 3000) -> str:
    """Certbot's actual complaint, not Compose's progress renderer or the epilogue.

    Certbot writes hook failures and ACME errors to STDOUT and only the generic
    'see the logfile' epilogue to STDERR; Compose overwrites its progress lines
    with ANSI/CR on STDERR. Strip both so the surfaced detail names the cause.
    """
    lines: list[str] = []
    for raw in (stdout, stderr):
        text = _ANSI_RE.sub("", raw.decode("utf-8", "replace")).replace("\r", "\n")
        for line in text.splitlines():
            if line.strip() and not _NOISE_LINE_RE.search(line):
                lines.append(line.rstrip())
    return "\n".join(lines)[-limit:]


def _preserve_attempt(persona_label: str, stdout: bytes, stderr: bytes, returncode: int) -> Path | None:
    """Keep the whole story of a failed attempt somewhere durable, BEFORE the
    ephemeral ACME_ROOT is wiped: Certbot's own log directory plus both captured
    streams. Returns the attempt directory, or None when no data root exists."""
    try:
        from tools.data_paths import resolve_data_root
        root = resolve_data_root()
    except Exception:
        root = None
    if root is None:
        return None
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    attempt = Path(root) / "service-certs" / "attempts" / f"{stamp}-{persona_label}"
    try:
        attempt.mkdir(parents=True, exist_ok=True, mode=0o700)
        (attempt / "output.txt").write_bytes(
            b"# returncode " + str(returncode).encode() + b"\n# --- stdout ---\n" + stdout
            + b"\n# --- stderr ---\n" + stderr
        )
        logs = ACME_ROOT / "logs"
        if logs.is_dir():
            shutil.copytree(logs, attempt / "logs", dirs_exist_ok=True)
    except Exception:
        logger.exception("service certificate: could not preserve attempt %s", attempt)
        return None
    return attempt


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


def certificate_key(org: str, identity: str) -> str:
    """Metadata row key. ``identity`` is a persona serving label or an
    organization-owned zone (the certificate identity, see the schema)."""
    return f"{org}:{identity}"


def certificate_vault_key(org: str, identity: str, serial: str) -> str:
    return f"service.tls.{org}.{identity}.{serial}"


def _pair_directory(org: str, identity: str, serial: str) -> Path:
    return PERSONA_CERT_ROOT / org / identity / serial


def _identity_fields(identity: str) -> dict:
    """The metadata fields that name an identity: one of the two, never both."""
    return {"zone": identity} if "." in identity else {"persona_label": identity}


def pair_paths(metadata: dict) -> tuple[Path, Path]:
    directory = _pair_directory(
        metadata["org"], certificate_identity(metadata), metadata["serial"]
    )
    return directory / "tls.crt", directory / "tls.key"


def gateway_pair_paths(metadata: dict) -> tuple[str, str]:
    """Return the same ramfs pair in the Caddy container's mount frame."""
    relative = _pair_directory(
        metadata["org"], certificate_identity(metadata), metadata["serial"]
    ).relative_to(Path("/run/autonomy-keycache/service-gateway"))
    root = Path("/run/autonomy-service-gateway-certs") / relative
    return str(root / "tls.crt"), str(root / "tls.key")


def _bundle_payload(cert_path: Path, key_path: Path, metadata: dict) -> dict:
    value = json.dumps(
        {
            "fullchain_pem": cert_path.read_text(),
            "private_key_pem": key_path.read_text(),
            "org": metadata["org"],
            **_identity_fields(certificate_identity(metadata)),
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
    fields = set(bundle)
    if fields not in (
        {"fullchain_pem", "private_key_pem", "org", "persona_label", "serial"},
        {"fullchain_pem", "private_key_pem", "org", "zone", "serial"},
    ):
        raise ServiceCertificateError("certificate vault bundle has invalid fields")
    return bundle


def _write_bundle(cert_path: Path, key_path: Path, metadata: dict) -> str:
    vault_key = certificate_vault_key(
        metadata["org"], certificate_identity(metadata), metadata["serial"]
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
    for name in ("org", "persona_label", "zone", "serial"):
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
    root = PERSONA_CERT_ROOT / metadata["org"] / certificate_identity(metadata)
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
    identity: str,
    cert_path: Path,
    key_path: Path,
    metadata: dict,
) -> dict:
    """Seal, verify, materialize, then publish the active metadata pointer.
    ``identity`` is the persona serving label or the organization zone."""
    current = certificate_metadata(org, identity)
    value = dict(metadata)
    value.pop("persona_label", None)
    value.pop("zone", None)
    value.update({"org": org, **_identity_fields(identity)})
    vault_key = certificate_vault_key(value["org"], identity, value["serial"])
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
        certificate_key(org, identity),
        value,
        org=None,          # personal: the record is a fleet fact (auto-7jhm3)
        state="raw",
    )
    _retire_old_ramfs(value)
    return value


class _LegacyRow:
    """Minimal stand-in matching the `.key`/`.payload` shape callers expect."""

    __slots__ = ("key", "payload")

    def __init__(self, key: str, payload: dict):
        self.key = key
        self.payload = payload


def _legacy_machine_rows() -> dict:
    """Legacy machine-homed certificate records, read PAST the home redirect.

    `read_set_key`/`read_owned_set` resolve the store through the schema's
    declared home (settings_ops `_open_read`: ``if home in ("personal",
    "machine"): org = home``). Now that this set declares `personal`, passing
    ``org="machine"`` is silently DISCARDED and the read lands on the personal
    store — so the legacy fallback could never see the very rows it exists to
    find, and returned None with no error.

    Confirmed live on 2026-09-09: three intact machine rows, zero personal
    rows, zero promotions, and `certificate_metadata` returning None for all
    three. That is the whole migration defeated by one silent override.

    Passing ``set_id=None`` to the opener asks for the named database and
    nothing else; the redirect keys on the set_id it is not given. Same
    technique as `migrate_session_uploads_to_machine.legacy_rows`, which
    exists for exactly this reason.
    """
    import json as _json

    from tools.graph import settings_ops as _ops

    try:
        db = _ops._open_read("machine", None)
    except Exception:
        return {}
    try:
        rows = db.conn.execute(
            "SELECT key, payload FROM settings"
            "  WHERE set_id = ? AND deprecated = 0"
            "    AND supersedes IS NULL AND excludes IS NULL",
            (SERVICE_CERTIFICATE_SET_ID,),
        ).fetchall()
    except Exception:
        return {}
    finally:
        with contextlib.suppress(Exception):
            db.close()
    out = {}
    for row in rows:
        try:
            payload = _json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            out[row["key"]] = payload
    return out


def certificate_metadata(org: str, identity: str) -> dict | None:
    """The active certificate record for *identity*, fleet-wide.

    Reads the personal (replicated) store, then falls back to the LEGACY
    machine-homed row and promotes it. The fallback is not optional: this
    record moved home in auto-7jhm3, and a machine that could no longer see
    its own existing certificate would conclude none existed and go to ACME
    for a duplicate — turning a sharing fix into exactly the rate-limit burn
    it exists to prevent. Promotion makes the move self-healing and one-way;
    once promoted the row replicates and every other machine finds it.
    """
    key = certificate_key(org, identity)
    row = settings_ops.read_set_key(
        SERVICE_CERTIFICATE_SET_ID, key, org=None, peers=[],
    )
    if row is not None:
        return dict(row["payload"])
    payload = _legacy_machine_rows().get(key)
    if payload is None:
        return None
    try:
        settings_ops.write_by_key(
            SERVICE_CERTIFICATE_SET_ID,
            SERVICE_CERTIFICATE_REVISION,
            key,
            payload,
            org=None,
            state="raw",
        )
        logger.warning(
            "promoted the certificate record for %s/%s from this machine's "
            "store to the fleet's — other machines can now find it instead of "
            "issuing a duplicate", org, identity)
    except Exception:
        # The legacy row still answers this call, so serving is unaffected;
        # only the sharing is deferred to the next attempt.
        logger.warning(
            "could not promote the certificate record for %s/%s to the fleet "
            "store; this machine still serves, but another machine asking for "
            "the same identity will still issue its own", org, identity,
            exc_info=True)
    return payload


def materialize_active_pairs() -> list[dict]:
    """Restore every active pair from the warm audited vault into ramfs."""
    result = []
    # Fleet store first, then LEGACY machine rows for identities the fleet
    # store does not yet carry. Union rather than either alone: dropping the
    # legacy half would stop materializing certificates this machine already
    # holds, and dropping the fleet half would defeat the sharing.
    rows = list(settings_ops.read_owned_set(
        SERVICE_CERTIFICATE_SET_ID,
        org=None,
        target_revision=SERVICE_CERTIFICATE_REVISION,
    ).members)
    seen = {row.key for row in rows}
    # Same redirect trap as the fallback above: read_owned_set(org="machine")
    # is silently redirected to the personal store now that this set declares
    # `personal`, so it would return the rows we ALREADY have and none of the
    # legacy ones. Read the machine database directly instead.
    legacy = [
        _LegacyRow(key, payload)
        for key, payload in _legacy_machine_rows().items()
        if key not in seen
    ]
    rows.extend(legacy)
    for row in rows:
        metadata = dict(row.payload)
        bundle = _read_bundle(metadata["vault_key"])
        _materialize_bundle(metadata, bundle)
        _retire_old_ramfs(metadata)
        result.append(metadata)
    return result


def active_gateway_pair(org: str, identity: object) -> tuple[str, str] | None:
    """Return one verified materialized pair in the gateway mount frame.
    ``identity`` is a persona serving label or an organization zone."""
    if not isinstance(identity, str) or not identity:
        return None
    metadata = certificate_metadata(org, identity)
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
            or legacy.get("apex") != apex_for_identity(identity)
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


def _dns01_preflight(client, apex: str, *, wait=None) -> None:
    """Prove, against our OWN authoritative name servers, that a challenge for
    this apex will land where ACME looks — before any ACME order exists.

    Presents a canary value, checks the relay published it under exactly
    ``_acme-challenge.<apex>`` (the relay derives the name from the persona's
    bound serving label, never from us), waits until both authoritative
    servers answer it, then cleans up. Raises with both labels named on a
    mismatch, so a wrong apex costs nothing at Let's Encrypt.
    """
    if wait is None:
        from tools.dashboard.acme_dns01 import wait_authoritative_txt as wait
    expected = f"_acme-challenge.{apex}"
    order = f"preflight-{uuid.uuid4().hex}"
    canary = "preflight-" + uuid.uuid4().hex
    zone_kwargs = _zone_kwargs_for_apex(apex)
    result = client.present(order, canary, ttl=30, lifetime=120, **zone_kwargs)
    try:
        published = str(result.get("name", ""))
        if published != expected:
            relay_label = published.removeprefix("_acme-challenge.").removesuffix(".serve.auto.network")
            raise ServiceCertificateError(
                "DNS-01 preflight: the relay publishes challenges for this identity under "
                f"{published!r} but the certificate is for {expected!r}; the bound serving "
                f"label is {relay_label!r}, not {apex.removesuffix('.serve.auto.network')!r}. "
                "No ACME order was placed."
            )
        try:
            wait(expected, canary)
        except Exception as exc:
            raise ServiceCertificateError(
                f"DNS-01 preflight: canary TXT for {expected!r} did not reach every "
                f"authoritative nameserver ({type(exc).__name__}: {exc}). No ACME order was placed."
            ) from exc
    finally:
        with contextlib.suppress(Exception):
            client.cleanup(order, canary, **zone_kwargs)


def _zone_kwargs_for_apex(apex: str) -> dict:
    """An organization zone is its own apex; a persona apex is under the base
    zone and the relay derives its challenge name from the bound label."""
    return {} if apex.endswith(".serve.auto.network") else {"zone": apex}


async def obtain(
    org: str, identity: str, *, staging: bool = False
) -> tuple[dict, bytes, bytes]:
    """Obtain and verify a candidate without activating it. ``identity`` is
    a persona serving label or an organization-owned zone."""
    try:
        apex = apex_for_identity(identity)
    except Exception as exc:
        raise ServiceCertificateError(f"invalid certificate identity: {exc}") from None
    persona_label = identity
    client = load_dns01_client(org)
    order = f"service-{uuid.uuid4().hex}"
    await asyncio.to_thread(_dns01_preflight, client, apex)
    cert_name = order if staging else certificate_name(org, identity)
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
        async with Dns01HookServer(
            client, order, socket_path, **_zone_kwargs_for_apex(apex)
        ):
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
            detail = _meaningful_output(stdout, stderr) or "(no output)"
            preserved = _preserve_attempt(persona_label, stdout, stderr, proc.returncode)
            logger.error(
                "service certificate: certbot failed for apex %s (exit %s); attempt kept at %s\n%s",
                apex, proc.returncode, preserved, detail,
            )
            where = f" [attempt kept at {preserved}]" if preserved else ""
            raise ServiceCertificateError(
                f"Certbot failed ({proc.returncode}): {detail}{where}"
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


async def issue(org: str, identity: str, *, staging: bool = False) -> dict:
    if not staging and not settings_ops.personal_delegate_audited_is_warm():
        # A production order is useful only if its account state and verified
        # pair can be read back and committed. Refuse before contacting ACME
        # when this process cannot complete that transaction.
        # State what was actually checked. This tests whether THIS PROCESS
        # holds the warm audited delegate key in memory -- a per-process
        # registration that a fresh process legitimately lacks until it is
        # handed one. It is NOT a reading of the operator's vault state, and
        # saying "the vault is locked" sent a live diagnosis down the wrong
        # path for half an hour (2026-09-08): the vault was warm the whole
        # time and a restarted process simply had not been given the key.
        raise ServiceCertificateError(
            "this process holds no warm audited delegate key, so it cannot "
            "read back and commit the issued pair. This is a property of "
            "THIS PROCESS, not of the vault: a process restarted since the "
            "last unlock has not been handed one. It does not mean the "
            "operator's vault is locked."
        )
    metadata, cert_bytes, key_bytes = await obtain(
        org, identity, staging=staging
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
        return activate_pair(org, identity, cert_path, key_path, metadata)
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
