"""Bounded TURN-REST credential issuance for authenticated org tunnels.

The long-lived shared secret stays on the auto.network host.  A Dashboard
receives only an opaque, short-lived username/password pair after its local
application authorization has already succeeded.  TURN credentials convey no
application authority and carry no link, persona, or organization identifier.
"""

from __future__ import annotations

import base64
from collections import deque
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import time

from tools.network.relaykit.ice_signaling import (
    IceConfiguration,
    STUN_URL,
    TURN_URLS,
)


TURN_CREDENTIAL_TTL_SECONDS = 15 * 60
TURN_ISSUANCE_WINDOW_SECONDS = 60
# These are abuse tripwires, not normal product quotas.  One authenticated org
# can mint ten fresh coupons per second before refusal; the whole first node can
# mint forty per second.  Coturn's allocation and bandwidth caps remain the
# actual scarce-resource enforcement layer.
TURN_ISSUANCE_PER_ORG = 600
TURN_ISSUANCE_GLOBAL = 2400
TURN_ISSUANCE_TRACKED_ORGS = 1024
_SECRET_RE = re.compile(r"^[0-9a-f]{64}\Z")


class TurnCredentialError(RuntimeError):
    """Credential issuance is unavailable or over its abuse tripwire."""


def load_turn_secrets(path: str | Path) -> tuple[str, ...]:
    """Read the one-or-two-secret rotation file without accepting ambiguity."""
    try:
        raw = Path(path).read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise TurnCredentialError("TURN credential secret is unavailable") from exc
    values = tuple(line for line in raw.splitlines() if line)
    if len(values) not in (1, 2) or any(_SECRET_RE.fullmatch(item) is None for item in values):
        raise TurnCredentialError("TURN credential secret has an invalid shape")
    if len(set(values)) != len(values):
        raise TurnCredentialError("TURN credential rotation secrets must differ")
    return values


class TurnCredentialIssuer:
    """Mint opaque TURN-REST coupons under bounded authenticated demand.

    During rotation coturn accepts both configured secrets and the issuer uses
    the final line, making append-new the explicit switch.  The service is
    restarted after the systemd credential changes; old credentials continue
    to validate against coturn's first line until their TTL drains.
    """

    def __init__(self, secrets_: tuple[str, ...], *, clock=time.time, token_hex=secrets.token_hex):
        if len(secrets_) not in (1, 2) or any(
            _SECRET_RE.fullmatch(item) is None for item in secrets_
        ):
            raise ValueError("TURN issuer requires one or two valid secrets")
        self._secret = secrets_[-1].encode("ascii")
        self._clock = clock
        self._token_hex = token_hex
        self._global: deque[int] = deque()
        self._by_org: dict[str, deque[int]] = {}

    @classmethod
    def from_file(cls, path: str | Path, **kwargs) -> "TurnCredentialIssuer":
        return cls(load_turn_secrets(path), **kwargs)

    @staticmethod
    def _purge(queue: deque[int], cutoff: int) -> None:
        while queue and queue[0] <= cutoff:
            queue.popleft()

    def _charge(self, org: str, now: int) -> None:
        cutoff = now - TURN_ISSUANCE_WINDOW_SECONDS
        self._purge(self._global, cutoff)
        if len(self._global) >= TURN_ISSUANCE_GLOBAL:
            raise TurnCredentialError("TURN credential issuance is temporarily unavailable")

        queue = self._by_org.get(org)
        if queue is None:
            # Reclaim idle org keys before applying the hard memory bound.
            for key, candidate in tuple(self._by_org.items()):
                self._purge(candidate, cutoff)
                if not candidate:
                    del self._by_org[key]
            if len(self._by_org) >= TURN_ISSUANCE_TRACKED_ORGS:
                raise TurnCredentialError("TURN credential issuance is temporarily unavailable")
            queue = deque()
            self._by_org[org] = queue
        else:
            self._purge(queue, cutoff)
        if len(queue) >= TURN_ISSUANCE_PER_ORG:
            raise TurnCredentialError("TURN credential issuance is temporarily unavailable")
        self._global.append(now)
        queue.append(now)

    def issue(self, org: str) -> IceConfiguration:
        if not isinstance(org, str) or not org or len(org) > 128:
            raise TurnCredentialError("TURN credential request is malformed")
        now = int(self._clock())
        self._charge(org, now)
        expires_at = now + TURN_CREDENTIAL_TTL_SECONDS
        # No identity or bearer enters the TURN username.  The random suffix
        # prevents two issuance events at the same second from sharing quota.
        username = f"{expires_at}:{self._token_hex(16)}"
        digest = hmac.new(self._secret, username.encode("ascii"), hashlib.sha1).digest()
        credential = base64.b64encode(digest).decode("ascii")
        return IceConfiguration(
            ice_servers=(
                {"urls": [STUN_URL]},
                {
                    "urls": list(TURN_URLS),
                    "username": username,
                    "credential": credential,
                    "credentialType": "password",
                },
            ),
            expires_at=expires_at,
        )

