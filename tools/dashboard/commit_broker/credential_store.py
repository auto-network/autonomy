"""File-backed credential store behind the provider seam (DN5 D5-10).

The minimal production/dev backing for :class:`CredentialProvider`. Each
provider's secret lives in its own host file created with mode ``0600``, in a
directory that must sit OUTSIDE every agent-mounted path — so no agent container
can read the bytes off disk. Every credential read appends an audit record
(operator / time / provider / scope). Callers above the broker receive only a
redaction-wrapped :class:`Credential`; the raw file path is never handed out.

Isolation is enforced fail-closed on *every* access, not just at construction:
the store directory is re-resolved (following any symlinks) and re-checked
against the agent mounts before each read/write, so an ancestor swapped to a
symlink into a mount after startup is caught rather than silently followed. The
final file is opened ``O_NOFOLLOW`` so it can never be a symlink either.
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
    """The store path resolves inside an agent-mounted directory — refused,
    because a store an agent can read is not a broker secret at all. Raised at
    construction AND on every access (a symlinked ancestor swapped in after
    startup resolves into a mount and is caught here, not silently followed)."""


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

    Satisfies the :class:`CredentialProvider` protocol. Isolation from every
    ``agent_mount_roots`` entry is the physical half of "no agent process ever
    touches a credential", and it is re-verified on each access — a construction
    -time-only check would be a TOCTOU (swap the store's ancestor to a symlink
    into a mount afterward and the next write lands the secret in the mount).
    """

    def __init__(
        self,
        *,
        store_dir: Path | str,
        agent_mount_roots: Iterable[Path | str],
        audit_sink: Callable[[CredentialAuditRecord], None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        # Keep the configured path UNRESOLVED so each access re-resolves it and
        # re-checks isolation (catches an ancestor symlinked in after startup).
        self._configured_dir = Path(store_dir)
        self._roots = [Path(r).resolve() for r in agent_mount_roots]
        self._audit_sink = audit_sink
        self._clock = clock
        resolved = self._resolved_dir(create=True)
        self._assert_isolated(resolved)

    def _resolved_dir(self, *, create: bool = False) -> Path:
        if create:
            self._configured_dir.mkdir(parents=True, exist_ok=True)
        return self._configured_dir.resolve()

    def _assert_isolated(self, resolved_dir: Path) -> None:
        for root in self._roots:
            if _is_within(resolved_dir, root):
                raise CredentialStoreLocationError(
                    f"credential store {resolved_dir} is inside agent mount {root}"
                )

    def _path_for(self, resolved_dir: Path, provider: str) -> Path:
        # provider is a fixed vocabulary ("github", ...), not a request field;
        # guard anyway so it can never escape the store directory.
        if "/" in provider or "\\" in provider or provider in ("", ".", ".."):
            raise ValueError(f"invalid provider name {provider!r}")
        return resolved_dir / f"{provider}.cred"

    def put_secret(self, provider: str, secret: str) -> None:
        """Write ``secret`` for ``provider`` as a fresh 0600 file.

        Re-checks isolation against the freshly-resolved store dir first, then
        opens the file ``O_NOFOLLOW`` with mode 0600 from creation so the secret
        is never briefly world-readable and the target can never be a symlink.
        """
        if not secret:
            raise ValueError("secret must be non-empty")
        resolved = self._resolved_dir(create=True)
        self._assert_isolated(resolved)
        path = self._path_for(resolved, provider)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
        fd = os.open(str(path), flags, _STORE_MODE)
        with os.fdopen(fd, "w") as handle:
            # Tighten a pre-existing loose file via the OPEN fd (fchmod), never
            # chmod-by-path: chmod would follow a symlink and could mutate a
            # target elsewhere, and would run even when O_NOFOLLOW already
            # refused a planted symlink. fchmod acts only on the file we
            # actually opened here.
            os.fchmod(fd, _STORE_MODE)
            handle.write(secret)

    def get_real_credential(
        self, provider: str, authorized_scope: AuthorizedScope
    ) -> Credential:
        """Read the provider secret, append one audit record, return a wrapped
        :class:`Credential`. Isolation is re-verified on this access too; the
        file path is never exposed to the caller."""
        resolved = self._resolved_dir()
        self._assert_isolated(resolved)
        path = self._path_for(resolved, provider)
        if not path.exists():
            raise KeyError(f"no credential stored for provider {provider!r}")
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            with os.fdopen(fd, "r") as handle:
                secret = handle.read()
        finally:
            pass
        self._audit_sink(
            CredentialAuditRecord(
                operator_id=authorized_scope.operator_id,
                at=self._clock(),
                provider=provider,
                repos=tuple(sorted(authorized_scope.repos)),
            )
        )
        return Credential(secret=secret, provider=provider, scope=authorized_scope)
