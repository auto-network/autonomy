"""Production vault key holder — the seam ``settings_ops`` reads through.

``settings_ops`` seals a ``@vaulted`` set on write and, on an ``audited`` read,
asks a registered *key holder* for the material that opens the object. In tests
that holder is injected; in production nothing built one, so every audited read
answered "no vault key holder is registered in this process" and no vault row
could ever be read (crib ``1e005d5c-c11`` §1). This module is that missing
production caller (``auto-a1pub``).

The holder owns no key material of its own. It reads two things at call time:

* the **generation keys** the process is holding in memory — delivered at
  unlock and never persisted (crib §12: the dashboard may hold domain content
  keys, never identity keys); this module keeps them in :class:`VaultKeyCache`,
  a plain in-memory dict that dies with the process, so a restart forces a fresh
  unlock rather than resurrecting keys from disk.
* the **descriptors and bridges** from the persisted local ``KeyControlStore``,
  re-opened per read so a record written since the last read is seen and every
  content address and bridge signature is re-verified on the way out.

Neither can sign. The generation keys decrypt objects in the org's storage
domain and nothing else; the store holds only signed public records.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict

from tools.network.storagekit.keycontrol import KeyControlStore
from tools.vault.db_content_store import DbContentStore, vault_db_path_for
from tools.vault.storage_object import Holdings
from tools.graph import settings_ops


class VaultKeyCache:
    """In-memory generation keys for one process, keyed by state id.

    Populated at unlock, read by the holder, cleared on restart by simply
    ceasing to exist — there is no disk backing and no persistence path. Holding
    a generation key lets the process derive any object's wrap key in that
    generation and its ancestors; it is a content key, not an identity key.
    """

    def __init__(self) -> None:
        self._secrets: Dict[str, bytes] = {}

    def load(self, secrets: Dict[str, bytes]) -> None:
        """Install the generation keys obtained at unlock. Replaces any prior
        set — an unlock is authoritative, not additive."""
        self._secrets = dict(secrets)

    def add(self, state_id: str, secret: bytes) -> None:
        """Add one generation key the process itself just minted.

        A vaulted write that advances the generation (the first write, or the
        first after an access removal) mints a new generation key inside the
        seal. That key must land here, because the read holder opens objects
        from this same cache — without it, a value this process just wrote reads
        back as unopenable. This is additive, unlike :meth:`load`: a mint extends
        the held set, it does not replace it.
        """
        self._secrets[state_id] = secret

    def clear(self) -> None:
        self._secrets = {}

    @property
    def secrets(self) -> Dict[str, bytes]:
        return dict(self._secrets)

    def __bool__(self) -> bool:
        return bool(self._secrets)


def _scoped_db(set_id: str, org: "str | None"):
    """The database a vault set's records live in, by DECLARED HOME.

    The same routing key the sealer and the ledger provider use. Home, not the
    org argument: a personal-homed set's records belong in personal.db whatever
    organization the caller is acting as.
    """
    from tools.graph import schemas

    home = schemas.declared_home(set_id)
    return vault_db_path_for(None if home == "personal" else org)


def build_key_holder(cache) -> Callable[..., settings_ops.VaultKeyControl]:
    """Return the holder callable ``settings_ops`` invokes on an audited read.

    No store paths. The content and key-control records are read from the SAME
    database file the settings row was written to, resolved per call. A single
    path put every scope in one sidecar — a store the design does not have, and
    a leak between organizations.
    """

    def holder(*, set_id: str, org: "str | None") -> settings_ops.VaultKeyControl:
        db = _scoped_db(set_id, org)
        with KeyControlStore(db) as key_control:
            holdings = Holdings(
                secrets=cache.secrets,
                descriptors=key_control.states,
                bridges=list(key_control.accepted_bridges()),
            )
        return settings_ops.VaultKeyControl(
            holdings=holdings, content_store=DbContentStore(db)
        )

    return holder


def register_key_holder(cache) -> Callable[..., settings_ops.VaultKeyControl]:
    """Build the holder and install it as the process's vault key holder.

    Call once per process. The cache may be empty at registration and filled at
    unlock — the holder reads it live, so a read before the first unlock fails
    closed with empty holdings rather than a missing-holder error.
    """
    holder = build_key_holder(cache)
    settings_ops.set_vault_key_holder(holder)
    return holder
