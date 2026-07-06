"""Server-side verification-key resolution (DN5 D5-5).

The key used to verify a signature is resolved from the authenticated operator
identity alone — the GPG public key from the broker keyring or the SSH public
key from the broker ``allowed_signers``, keyed to ``operator_id``. A key (or
allowed-signers entry) supplied by an agent or in the signing-client request
body is never consulted, and a request carrying one is rejected as a
confused-deputy attempt. If no key is registered for ``(operator_id,
signing_kind)``, resolution fails closed — a typed not-registered error, never a
fallback or empty-pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

SIGNING_KINDS = ("gpg", "ssh")


class VerificationKeyNotRegistered(Exception):
    """Fail-closed: no verification key is registered for this operator/kind."""

    def __init__(self, operator_id: str, signing_kind: str) -> None:
        super().__init__(
            f"no {signing_kind} verification key registered for operator {operator_id!r}"
        )
        self.operator_id = operator_id
        self.signing_kind = signing_kind


class ConfusedDeputyKeyError(Exception):
    """A request tried to supply its own key material — refused, never trusted."""


@dataclass(frozen=True)
class VerificationKey:
    operator_id: str
    signing_kind: str
    material: bytes


class BrokerKeyStore(Protocol):
    """Server-side, operator-keyed key material. GPG public keys live in the
    broker keyring; SSH public keys live in the broker ``allowed_signers``.
    Implementations read only trusted server state."""

    def lookup(self, operator_id: str, signing_kind: str) -> bytes | None:
        ...


def resolve_verification_key(
    *,
    operator_id: str,
    signing_kind: str,
    keystore: BrokerKeyStore,
    request_key_material: bytes | None = None,
) -> VerificationKey:
    """Resolve the verification key for ``(operator_id, signing_kind)``.

    ``request_key_material`` is present only so an agent/request-supplied key can
    be *detected and rejected* — it is never used as the resolved key. Resolution
    consults only the server-side ``keystore`` keyed by the resolved
    ``operator_id``; it never reads an agent worktree or any request field.
    """
    if request_key_material is not None:
        raise ConfusedDeputyKeyError(
            "verification key must come from the broker keystore keyed to the "
            "resolved operator identity, never from the request"
        )
    if signing_kind not in SIGNING_KINDS:
        raise ValueError(f"signing_kind must be one of {SIGNING_KINDS}, got {signing_kind!r}")
    material = keystore.lookup(operator_id, signing_kind)
    if material is None:
        raise VerificationKeyNotRegistered(operator_id, signing_kind)
    return VerificationKey(operator_id=operator_id, signing_kind=signing_kind, material=material)


class WritableBrokerKeyStore(BrokerKeyStore, Protocol):
    """A key store the provisioning path writes the operator's public half to."""

    def store(self, operator_id: str, signing_kind: str, public_material: bytes) -> None:
        ...


class InMemoryBrokerKeyStore:
    """Concrete broker key store for bootstrap/tests. The production store is
    DAO-backed (later task); this satisfies the same read+write protocol."""

    def __init__(self) -> None:
        self._keys: dict[tuple[str, str], bytes] = {}

    def lookup(self, operator_id: str, signing_kind: str) -> bytes | None:
        return self._keys.get((operator_id, signing_kind))

    def store(self, operator_id: str, signing_kind: str, public_material: bytes) -> None:
        self._keys[(operator_id, signing_kind)] = public_material


def register_verification_key(
    *,
    operator_id: str,
    signing_kind: str,
    public_material: bytes,
    keystore: WritableBrokerKeyStore,
) -> VerificationKey:
    """Register the operator's PUBLIC verification key at provision time.

    This is the DN3->DN5 seam: the signer's provisioning path (D3-22) calls this
    with the public half of the operator's signing key, so that later
    :func:`resolve_verification_key` and signature verification can find it.
    Public material only — a private/secret key must never reach here.
    """
    if signing_kind not in SIGNING_KINDS:
        raise ValueError(f"signing_kind must be one of {SIGNING_KINDS}, got {signing_kind!r}")
    if not public_material:
        raise ValueError("public_material must be non-empty")
    keystore.store(operator_id, signing_kind, public_material)
    return VerificationKey(operator_id=operator_id, signing_kind=signing_kind, material=public_material)
