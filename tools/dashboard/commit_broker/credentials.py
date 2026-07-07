"""Brokered credential seam (DN5 D5-9).

The trusted broker resolves a real provider credential (a GitHub token, etc.)
ONLY inside its own process and ONLY for a server-resolved scope. No LLM/agent
process ever receives a credential: the value is wrapped so it cannot be
stringified into a log, response, or event, and the raw secret is reachable
only through one explicit in-broker accessor.

Two types carry the security properties:

* :class:`AuthorizedScope` — the set of repositories a broker action may touch,
  constructed only by the broker's scope resolver from the authenticated
  operator identity. The interface takes this type, not a plain string, so a
  scope can never be an agent-supplied request field slipped through as text.
* :class:`Credential` — the secret itself, redaction-wrapped. ``str``/``repr``/
  ``format``/pickle all yield a redaction marker instead of the secret; the raw
  value comes back only from :meth:`Credential.reveal`, which a caller must name
  on purpose at the point it hands the secret to a subprocess via env/stdin.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

_REDACTED = "***redacted-credential***"


@dataclass(frozen=True)
class AuthorizedScope:
    """A server-resolved set of repositories a broker action is allowed to touch.

    Built only by the broker's scope resolver (``auto-aw3f7``) from the
    authenticated operator identity — never from an agent-supplied request
    field. Because the credential interface is typed to this class rather than
    to ``str``, a raw request string cannot masquerade as a scope: the type
    itself forces scope to be resolved server-side.
    """

    operator_id: str
    repos: frozenset[str]

    @classmethod
    def for_repos(cls, operator_id: str, repos: Iterable[str]) -> "AuthorizedScope":
        return cls(operator_id=operator_id, repos=frozenset(repos))

    def permits(self, repo: str) -> bool:
        """True iff ``repo`` is inside this server-resolved scope."""
        return repo in self.repos


class CredentialScopeError(Exception):
    """A broker action asked for a repo outside its server-resolved scope."""

    def __init__(self, repo: str, scope: AuthorizedScope) -> None:
        super().__init__(
            f"repo {repo!r} is outside the authorized scope for operator "
            f"{scope.operator_id!r}"
        )
        self.repo = repo
        self.scope = scope


class Credential:
    """A real provider secret, wrapped so it cannot leak by accident.

    ``str(cred)``, ``repr(cred)``, ``f"{cred}"`` and pickling all yield a
    redaction marker, never the secret — so a credential that lands in a log
    line, an event payload, or a JSON response redacts itself. The raw secret is
    returned only by :meth:`reveal`, the single explicit accessor; nothing
    serializes it and no public attribute holds it under a plain name.
    """

    __slots__ = ("_secret", "provider", "scope")

    def __init__(self, *, secret: str, provider: str, scope: AuthorizedScope) -> None:
        if not secret:
            raise ValueError("credential secret must be non-empty")
        self._secret = secret
        self.provider = provider
        self.scope = scope

    def reveal(self) -> str:
        """The one explicit accessor for the raw secret.

        Call this only inside the broker, at the moment the secret is handed to
        a subprocess via env or stdin. Never pass its result to a logger, an
        event, or a response body.
        """
        return self._secret

    def __str__(self) -> str:
        return _REDACTED

    def __repr__(self) -> str:
        return f"<Credential provider={self.provider!r} {_REDACTED}>"

    def __format__(self, spec: str) -> str:
        return _REDACTED

    def __reduce__(self):  # blocks pickle/copy from smuggling the secret out
        raise TypeError("Credential is not serializable (would leak the secret)")


class CredentialProvider(Protocol):
    """The seam the publish executor calls to obtain a real credential.

    Implementations resolve the secret from trusted server-side storage keyed to
    the provider and the server-resolved ``authorized_scope``. Neither argument
    is ever sourced from a request body; the returned :class:`Credential` is
    always redaction-wrapped.
    """

    def get_real_credential(
        self, provider: str, authorized_scope: AuthorizedScope
    ) -> Credential:
        ...


class InMemoryCredentialProvider:
    """Concrete provider for bootstrap/tests. The production store is the
    file-backed, 0600, audited store (D5-10); this satisfies the same protocol
    so the publish executor can be exercised without a real secret on disk."""

    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}

    def set_secret(self, provider: str, secret: str) -> None:
        self._secrets[provider] = secret

    def get_real_credential(
        self, provider: str, authorized_scope: AuthorizedScope
    ) -> Credential:
        secret = self._secrets.get(provider)
        if secret is None:
            raise KeyError(f"no credential registered for provider {provider!r}")
        return Credential(secret=secret, provider=provider, scope=authorized_scope)
