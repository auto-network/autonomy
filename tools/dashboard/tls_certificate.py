"""Facts about the certificate this dashboard serves (auto-1ei8m part 2).

One reader for every consumer: the Machines page line, the Central attention
item that warns before expiry, and the remote-access seed. A file read of the
served certificate (``AUTONOMY_TLS_CERT`` or ``<data root>/tls.crt``); never
a network probe, never raises.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

#: Warn this many days before the served certificate expires: a monthly
#: renewal that failed twice is caught before the third failure matters.
EXPIRY_WARNING_DAYS = 21


@dataclass(frozen=True)
class CertificateFacts:
    path: str
    names: tuple[str, ...]
    not_before: datetime
    not_after: datetime

    def days_remaining(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (self.not_after - now).total_seconds() / 86400.0

    @property
    def tailnet_name(self) -> str | None:
        return next((name for name in self.names if name.endswith(".ts.net")), None)


def certificate_path(environ=None) -> Path:
    env = os.environ if environ is None else environ
    explicit = env.get("AUTONOMY_TLS_CERT")
    if explicit:
        return Path(explicit)
    return Path(env.get("AUTONOMY_DATA_ROOT", "data")) / "tls.crt"


def read_certificate(path=None, *, environ=None) -> CertificateFacts | None:
    """The served certificate's facts, or None when there is none to read."""
    target = Path(path) if path is not None else certificate_path(environ)
    if not target.is_file():
        return None
    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(target.read_bytes())
        try:
            names = tuple(
                name.rstrip(".").lower() for name in cert.extensions.get_extension_for_class(
                    x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName))
        except x509.ExtensionNotFound:
            names = ()
        not_before = cert.not_valid_before_utc
        not_after = cert.not_valid_after_utc
    except Exception:
        return None
    return CertificateFacts(str(target), names, not_before, not_after)


def expiry_condition(facts: CertificateFacts | None, *, now: datetime | None = None,
                     warning_days: float = EXPIRY_WARNING_DAYS) -> dict | None:
    """What the operator must know about the served certificate, or None
    when it is fine: ``{"state": "expiring" | "expired", "days": float,
    "not_after": iso, "names": [...]}``."""
    if facts is None:
        return None
    days = facts.days_remaining(now)
    if days > warning_days:
        return None
    return {
        "state": "expired" if days <= 0 else "expiring",
        "days": round(days, 1),
        "not_after": facts.not_after.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "names": list(facts.names),
    }
