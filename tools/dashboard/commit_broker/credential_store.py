"""File-backed credential store behind the provider seam (DN5 D5-10).

The minimal production/dev backing for :class:`CredentialProvider`. Each
provider's secret lives in its own host file created with mode ``0600``, in a
directory that must sit OUTSIDE every agent-mounted path — so no agent container
can read the bytes off disk. Every credential read appends an audit record
(operator / time / provider / scope). Callers above the broker receive only a
redaction-wrapped :class:`Credential`; the raw file path is never handed out.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from tools.dashboard.commit_broker.credentials import (
    AuthorizedScope,
    Credential,
)

_STORE_MODE = 0o600


class CredentialStoreLocationError(Exception):
    """The store path is inside an agent-mounted directory — refused at build
    time, because a store an agent can read is not a broker secret at all."""


@dataclass(frozen=True)
class CredentialAuditRecord:
    """One audited credential read: who asked, when, for which provider/scope."""

    operator_id: str
    at: float
    provider: str
    repos: tuple[str, ...]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class FileCredentialStore:
    """Per-provider 0600 files under an agent-isolated directory, audited reads.

    Satisfies the :class:`CredentialProvider` protocol. Construction fails closed
    if ``store_dir`` is inside any of ``agent_mount_roots``; that check is the
    physical half of "no agent process ever touches a credential".
    """

    def __init__(
        self,
        *,
        store_dir: Path | str,
        agent_mount_roots: Iterable[Path | str],
        audit_sink: Callable[[CredentialAuditRecord], None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._dir = Path(store_dir).resolve()
        roots = [Path(r).resolve() for r in agent_mount_roots]
        for root in roots:
            if _is_within(self._dir, root):
                raise CredentialStoreLocationError(
                    f"credential store {self._dir} is inside agent mount {root}"
                )
        self._dir.mkdir(parents=True, exist_ok=True)
        self._audit_sink = audit_sink
        self._clock = clock

    def _path_for(self, provider: str) -> Path:
        # provider is a fixed vocabulary ("github", ...), not a request field;
        # guard anyway so it can never escape the store directory.
        if "/" in provider or "\\" in provider or provider in ("", ".", ".."):
            raise ValueError(f"invalid provider name {provider!r}")
        return self._dir / f"{provider}.cred"

    def put_secret(self, provider: str, secret: str) -> None:
        """Write ``secret`` for ``provider`` as a fresh 0600 file (atomic)."""
        if not secret:
            raise ValueError("secret must be non-empty")
        path = self._path_for(provider)
        # Open with O_CREAT|O_EXCL-free truncation but force 0600 from the start:
        # create via a mode-0600 fd so the secret is never briefly world-readable.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _STORE_MODE)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(secret)
        finally:
            # If the file pre-existed with looser perms, tighten it.
            os.chmod(path, _STORE_MODE)

    def get_real_credential(
        self, provider: str, authorized_scope: AuthorizedScope
    ) -> Credential:
        """Read the provider secret, append one audit record, return a wrapped
        :class:`Credential`. The file path is never exposed to the caller."""
        path = self._path_for(provider)
        if not path.exists():
            raise KeyError(f"no credential stored for provider {provider!r}")
        secret = path.read_text()
        self._audit_sink(
            CredentialAuditRecord(
                operator_id=authorized_scope.operator_id,
                at=self._clock(),
                provider=provider,
                repos=tuple(sorted(authorized_scope.repos)),
            )
        )
        return Credential(secret=secret, provider=provider, scope=authorized_scope)
