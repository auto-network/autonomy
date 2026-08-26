"""RFC Web Push publication over a DNS-pinned HTTPS connection.

``pywebpush`` and ``py_vapid`` own RFC 8291 encryption and RFC 8292
signing.  This module deliberately owns the browser-supplied destination:
validation, bounded DNS/CNAME resolution, exact-address socket connection,
TLS hostname verification, redirect refusal, and bounded response classes.

An accepted push-service response means only that the service accepted the
encrypted message.  It is not evidence that a browser or device displayed it.
"""

from __future__ import annotations

import base64
import email.utils
import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence
from urllib.parse import SplitResult, urlsplit, urlunsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


MAX_ENDPOINT_CHARS = 4096
MAX_PLAINTEXT_BYTES = 2048
MAX_RESPONSE_BYTES = 16 * 1024
MAX_CNAME_HOPS = 8
MAX_DNS_ADDRESSES = 16
MAX_TTL_SECONDS = 86_400
VAPID_LIFETIME_SECONDS = 12 * 60 * 60
URGENCIES = frozenset({"very-low", "low", "normal", "high"})
_TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_AUTHORIZATION = re.compile(
    r"^vapid t=([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+),"
    r"k=([A-Za-z0-9_-]+)$"
)
_IPV6_EMBEDDED_V4_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)


class WebPushInputError(ValueError):
    """A malformed payload, subscription, key, or Web Push header."""


class WebPushEgressPolicyError(PermissionError):
    """A destination was refused before any network publication."""


class WebPushResolutionError(OSError):
    """A transient or empty public DNS resolution."""


@dataclass(frozen=True, slots=True)
class Endpoint:
    url: str
    host: str
    request_target: str
    audience: str


@dataclass(frozen=True, slots=True)
class PinnedEndpoint:
    endpoint: Endpoint
    address: str
    validated_addresses: tuple[str, ...]
    canonical_host: str


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status: int
    headers: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class SendResult:
    status: int
    outcome: str
    retryable: bool
    retire_subscription: bool
    retry_after: float | None


class Resolver(Protocol):
    def cname(self, host: str) -> str | None: ...

    def addresses(self, host: str) -> Sequence[str]: ...


class PostTransport(Protocol):
    def post(
        self,
        destination: PinnedEndpoint,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> TransportResponse: ...


class PayloadEncoder(Protocol):
    def encode(
        self,
        *,
        endpoint: str,
        p256dh: str,
        auth_secret: str,
        plaintext: bytes,
    ) -> bytes: ...


class PyWebPushPayloadEncoder:
    """Small maintained-library boundary for RFC 8291 encryption only."""

    def encode(
        self,
        *,
        endpoint: str,
        p256dh: str,
        auth_secret: str,
        plaintext: bytes,
    ) -> bytes:
        try:
            from pywebpush import WebPusher

            encoded = WebPusher({
                "endpoint": endpoint,
                "keys": {"p256dh": p256dh, "auth": auth_secret},
            }).encode(plaintext, content_encoding="aes128gcm")
            body = encoded["body"]
        except ImportError as exc:
            raise RuntimeError(
                "Web Push runtime is unavailable; install deploy/requirements.txt"
            ) from exc
        except Exception as exc:
            raise WebPushInputError("subscription encryption keys are invalid") from exc
        if not isinstance(body, bytes) or not body:
            raise RuntimeError("Web Push encoder returned an invalid body")
        return body


def _ascii_host(value: str, *, label: str = "endpoint host") -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise WebPushEgressPolicyError(f"{label} is invalid")
    candidate = value.rstrip(".")
    if not candidate or any(ord(character) < 33 for character in candidate):
        raise WebPushEgressPolicyError(f"{label} is invalid")
    try:
        host = candidate.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError) as exc:
        raise WebPushEgressPolicyError(f"{label} is invalid") from exc
    if len(host) > 253 or any(
        not part or len(part) > 63 for part in host.split(".")
    ):
        raise WebPushEgressPolicyError(f"{label} is invalid")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise WebPushEgressPolicyError("IP-literal push endpoints are forbidden")


def validate_endpoint(value: object) -> Endpoint:
    """Return the canonical HTTPS destination without resolving it."""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_ENDPOINT_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise WebPushEgressPolicyError("push endpoint is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise WebPushEgressPolicyError("push endpoint is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise WebPushEgressPolicyError(
            "push endpoint must be absolute HTTPS on port 443 without userinfo or fragment"
        )
    host = _ascii_host(parsed.hostname)
    path = parsed.path or "/"
    request_target = path + (("?" + parsed.query) if parsed.query else "")
    try:
        target_bytes = request_target.encode("ascii", "strict")
    except UnicodeEncodeError as exc:
        raise WebPushEgressPolicyError(
            "push endpoint request target must be ASCII URL syntax"
        ) from exc
    if len(target_bytes) > MAX_ENDPOINT_CHARS or "\\" in request_target:
        raise WebPushEgressPolicyError("push endpoint request target is invalid")
    canonical = urlunsplit(SplitResult("https", host, path, parsed.query, ""))
    return Endpoint(
        url=canonical,
        host=host,
        request_target=request_target,
        audience=f"https://{host}",
    )


def _public_address(value: object) -> str:
    if not isinstance(value, str) or not value or "%" in value:
        raise WebPushEgressPolicyError("DNS returned an invalid address")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise WebPushEgressPolicyError("DNS returned an invalid address") from exc
    if isinstance(address, ipaddress.IPv6Address) and (
        address.ipv4_mapped is not None
        or address.sixtofour is not None
        or address.teredo is not None
        or any(address in network for network in _IPV6_EMBEDDED_V4_NETWORKS)
    ):
        raise WebPushEgressPolicyError("IPv4-embedded IPv6 destinations are forbidden")
    if (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise WebPushEgressPolicyError("push endpoint DNS must contain only global addresses")
    return address.compressed


class SystemResolver:
    """Bounded dnspython resolver; imported lazily with the sender runtime."""

    def __init__(self, *, lifetime: float = 2.0):
        self.lifetime = lifetime

    def _resolve(self, host: str, record_type: str):
        try:
            import dns.exception
            import dns.resolver
        except ImportError as exc:
            raise RuntimeError(
                "Web Push runtime is unavailable; install deploy/requirements.txt"
            ) from exc
        try:
            return dns.resolver.resolve(
                host,
                record_type,
                lifetime=self.lifetime,
                search=False,
                raise_on_no_answer=False,
            )
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return None
        except dns.exception.DNSException as exc:
            raise WebPushResolutionError("push endpoint DNS resolution failed") from exc

    def cname(self, host: str) -> str | None:
        answer = self._resolve(host, "CNAME")
        if answer is None or answer.rrset is None:
            return None
        targets = [str(record.target).rstrip(".") for record in answer]
        if len(targets) != 1:
            raise WebPushEgressPolicyError("CNAME answer must contain one target")
        return targets[0]

    def addresses(self, host: str) -> Sequence[str]:
        result: list[str] = []
        for record_type in ("A", "AAAA"):
            answer = self._resolve(host, record_type)
            if answer is not None and answer.rrset is not None:
                result.extend(str(record.address) for record in answer)
        return result


def resolve_endpoint(
    endpoint: Endpoint,
    resolver: Resolver,
    *,
    max_cname_hops: int = MAX_CNAME_HOPS,
) -> PinnedEndpoint:
    if isinstance(max_cname_hops, bool) or not 0 <= max_cname_hops <= MAX_CNAME_HOPS:
        raise ValueError("invalid CNAME limit")
    current = endpoint.host
    seen: set[str] = set()
    for _hop in range(max_cname_hops + 1):
        if current in seen:
            raise WebPushEgressPolicyError("CNAME loop refused")
        seen.add(current)
        target = resolver.cname(current)
        if target is None:
            break
        if len(seen) > max_cname_hops:
            raise WebPushEgressPolicyError("CNAME chain is too deep")
        current = _ascii_host(target, label="CNAME target")
    else:  # pragma: no cover - loop exits through the explicit checks above
        raise WebPushEgressPolicyError("CNAME chain is too deep")
    raw_addresses = resolver.addresses(current)
    if not raw_addresses:
        raise WebPushResolutionError("push endpoint DNS returned no addresses")
    if len(raw_addresses) > MAX_DNS_ADDRESSES:
        raise WebPushEgressPolicyError("push endpoint DNS returned too many addresses")
    addresses: list[str] = []
    for raw in raw_addresses:
        address = _public_address(raw)
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise WebPushResolutionError("push endpoint DNS returned no addresses")
    return PinnedEndpoint(endpoint, addresses[0], tuple(addresses), current)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        address: str,
        *,
        timeout: float,
        context: ssl.SSLContext,
    ):
        super().__init__(host, port=443, timeout=timeout, context=context)
        self._pinned_address = address

    def connect(self) -> None:
        # No proxy tunnel and no second hostname lookup: the socket receives
        # the exact address accepted by resolve_endpoint.  self.host remains
        # the original endpoint hostname for both SNI and certificate checks.
        raw = socket.create_connection(
            (self._pinned_address, 443),
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class PinnedHTTPSPostTransport:
    def __init__(self, *, context: ssl.SSLContext | None = None):
        self._context = context or ssl.create_default_context()
        if (
            not self._context.check_hostname
            or self._context.verify_mode != ssl.CERT_REQUIRED
        ):
            raise ValueError("Web Push TLS must verify the original hostname")

    def post(
        self,
        destination: PinnedEndpoint,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> TransportResponse:
        connection = _PinnedHTTPSConnection(
            destination.endpoint.host,
            destination.address,
            timeout=timeout,
            context=self._context,
        )
        try:
            connection.request(
                "POST",
                destination.endpoint.request_target,
                body=body,
                headers=dict(headers),
            )
            response = connection.getresponse()
            # Drain only a bounded diagnostic body.  No response content is
            # accepted as semantic truth or persisted by this transport.
            response.read(MAX_RESPONSE_BYTES + 1)
            return TransportResponse(
                status=int(response.status),
                headers={key.lower(): value for key, value in response.getheaders()},
            )
        finally:
            connection.close()


def _reject_json_constant(_value: str):
    raise ValueError("non-finite JSON number")


def _payload_bytes(value: object) -> bytes:
    if isinstance(value, str):
        try:
            result = value.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise WebPushInputError("payload must be valid UTF-8 JSON") from exc
    elif isinstance(value, bytes):
        result = value
    else:
        raise WebPushInputError("payload must be UTF-8 JSON")
    if not result or len(result) > MAX_PLAINTEXT_BYTES:
        raise WebPushInputError("payload must be 1..2048 bytes")
    try:
        decoded = json.loads(
            result,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise WebPushInputError("payload must be valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise WebPushInputError("payload must be a JSON object")
    return result


def _subscription_key(value: object, *, name: str, length: int) -> bytes:
    if not isinstance(value, str) or not value or not _BASE64URL.fullmatch(value):
        raise WebPushInputError(f"subscription {name} is invalid")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise WebPushInputError(f"subscription {name} is invalid") from exc
    if len(decoded) != length:
        raise WebPushInputError(f"subscription {name} is invalid")
    return decoded


def _subject(value: object) -> str:
    try:
        encoded_length = len(value.encode("utf-8")) if isinstance(value, str) else 0
    except UnicodeEncodeError as exc:
        raise WebPushInputError("VAPID subject is invalid") from exc
    if (
        not isinstance(value, str)
        or not 1 <= encoded_length <= 256
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise WebPushInputError("VAPID subject is invalid")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise WebPushInputError("VAPID subject is invalid") from exc
    if parsed.scheme == "mailto" and parsed.path and not parsed.query and not parsed.fragment:
        return value
    if (
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    ):
        return value
    raise WebPushInputError("VAPID subject must be a mailto: or HTTPS URI")


def _integer(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise WebPushInputError(f"{name} is invalid")
    return value


def _vapid_headers(vapid_key, *, audience: str, subject: str, now: float) -> dict[str, str]:
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        raise WebPushInputError("clock is invalid")
    try:
        numeric_now = float(now)
    except OverflowError as exc:
        raise WebPushInputError("clock is invalid") from exc
    if not math.isfinite(numeric_now) or not 0 <= numeric_now <= 253_402_257_599:
        raise WebPushInputError("clock is invalid")
    expiry = int(numeric_now) + VAPID_LIFETIME_SECONDS
    try:
        public = vapid_key.public_key.public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint,
        )
        signed = vapid_key.sign({
            "aud": audience,
            "sub": subject,
            "exp": expiry,
        })
    except Exception as exc:
        raise WebPushInputError("VAPID key or claims are invalid") from exc
    authorization = signed.get("Authorization") if isinstance(signed, Mapping) else None
    if not isinstance(authorization, str):
        raise WebPushInputError("VAPID signer returned invalid headers")
    match = _AUTHORIZATION.fullmatch(authorization)
    expected = base64.urlsafe_b64encode(public).rstrip(b"=").decode("ascii")
    if match is None or match.group(2) != expected:
        raise WebPushInputError("VAPID signer used an unexpected public key")
    try:
        token = match.group(1)
        header_text, claims_text, signature_text = token.split(".")
        header = json.loads(base64.urlsafe_b64decode(
            header_text + "=" * (-len(header_text) % 4),
        ))
        claims = json.loads(base64.urlsafe_b64decode(
            claims_text + "=" * (-len(claims_text) % 4),
        ))
        signature = base64.urlsafe_b64decode(
            signature_text + "=" * (-len(signature_text) % 4),
        )
        if (
            header != {"typ": "JWT", "alg": "ES256"}
            or claims != {"aud": audience, "sub": subject, "exp": expiry}
            or len(signature) != 64
        ):
            raise ValueError("unexpected VAPID JWT")
        public_key = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), public,
        )
        public_key.verify(
            encode_dss_signature(
                int.from_bytes(signature[:32], "big"),
                int.from_bytes(signature[32:], "big"),
            ),
            f"{header_text}.{claims_text}".encode("ascii"),
            ec.ECDSA(hashes.SHA256()),
        )
    except Exception as exc:
        raise WebPushInputError("VAPID signer returned an invalid token") from exc
    return {"Authorization": authorization}


def _retry_after(headers: Mapping[str, str], *, now: float) -> float | None:
    raw = headers.get("retry-after")
    if not isinstance(raw, str) or not raw or len(raw) > 128:
        return None
    if raw.isascii() and raw.isdigit():
        try:
            return min(float(int(raw)), 3600.0)
        except OverflowError:
            return None
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        seconds = parsed.timestamp() - now
    except (TypeError, ValueError, OverflowError):
        return None
    return min(max(0.0, seconds), 3600.0)


def classify_response(
    status: object,
    headers: Mapping[str, str] | None = None,
    *,
    now: float | None = None,
) -> SendResult:
    code = _integer(status, name="HTTP status", minimum=100, maximum=599)
    normalized = {
        str(key).lower(): str(value) for key, value in (headers or {}).items()
    }
    instant = time.time() if now is None else now
    if 200 <= code < 300:
        return SendResult(code, "accepted", False, False, None)
    if code in {404, 410}:
        return SendResult(code, "retired", False, True, None)
    if code in {408, 425, 429} or 500 <= code < 600:
        return SendResult(code, "retryable", True, False, _retry_after(normalized, now=instant))
    if 300 <= code < 400:
        return SendResult(code, "redirect_refused", False, False, None)
    return SendResult(code, "permanent_failure", False, False, None)


def send_encrypted_web_push(
    *,
    endpoint: object,
    p256dh: object,
    auth_secret: object,
    payload: object,
    vapid_key,
    vapid_subject: object,
    ttl: object,
    urgency: object = "normal",
    topic: object | None = None,
    resolver: Resolver | None = None,
    transport: PostTransport | None = None,
    encoder: PayloadEncoder | None = None,
    timeout: float = 10.0,
    now: float | None = None,
) -> SendResult:
    """Encrypt and publish once; callers own leases, retries, and retirement."""

    destination = validate_endpoint(endpoint)
    receiver_key = _subscription_key(p256dh, name="p256dh", length=65)
    _subscription_key(auth_secret, name="auth", length=16)
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), receiver_key)
    except ValueError as exc:
        raise WebPushInputError("subscription p256dh is invalid") from exc
    assert isinstance(p256dh, str) and isinstance(auth_secret, str)
    plaintext = _payload_bytes(payload)
    ttl_value = _integer(ttl, name="TTL", minimum=0, maximum=MAX_TTL_SECONDS)
    if not isinstance(urgency, str) or urgency not in URGENCIES:
        raise WebPushInputError("Urgency is invalid")
    if topic is not None and (not isinstance(topic, str) or not _TOPIC.fullmatch(topic)):
        raise WebPushInputError("Topic is invalid")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise WebPushInputError("timeout is invalid")
    try:
        timeout_value = float(timeout)
    except OverflowError as exc:
        raise WebPushInputError("timeout is invalid") from exc
    if not math.isfinite(timeout_value) or not 0 < timeout_value <= 60:
        raise WebPushInputError("timeout is invalid")
    instant = time.time() if now is None else now
    headers = _vapid_headers(
        vapid_key,
        audience=destination.audience,
        subject=_subject(vapid_subject),
        now=instant,
    )
    try:
        body = (encoder or PyWebPushPayloadEncoder()).encode(
            endpoint=destination.url,
            p256dh=p256dh,
            auth_secret=auth_secret,
            plaintext=plaintext,
        )
    except (WebPushInputError, RuntimeError):
        raise
    except Exception as exc:
        raise WebPushInputError("subscription encryption keys are invalid") from exc
    if not isinstance(body, bytes) or not body:
        raise RuntimeError("Web Push encoder returned an invalid body")
    headers.update({
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(body)),
        "TTL": str(ttl_value),
        "Urgency": urgency,
    })
    if topic is not None:
        headers["Topic"] = topic
    pinned = resolve_endpoint(destination, resolver or SystemResolver())
    response = (transport or PinnedHTTPSPostTransport()).post(
        pinned,
        headers=headers,
        body=body,
        timeout=timeout_value,
    )
    return classify_response(response.status, response.headers, now=float(instant))
