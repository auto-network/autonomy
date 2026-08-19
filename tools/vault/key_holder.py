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
from tools.network.storagekit.store import ContentStore
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

    def clear(self) -> None:
        self._secrets = {}

    @property
    def secrets(self) -> Dict[str, bytes]:
        return dict(self._secrets)

    def __bool__(self) -> bool:
        return bool(self._secrets)


def build_key_holder(
    cache: VaultKeyCache,
    keycontrol_path: "str | Path",
    content_path: "str | Path",
) -> Callable[..., settings_ops.VaultKeyControl]:
    """Return the holder callable ``settings_ops`` invokes on an audited read.

    The callable takes ``set_id``/``org`` (which it does not need — the cache is
    already scoped to the unlocked org) and returns a
    :class:`settings_ops.VaultKeyControl` built from the current cache and the
    persisted key-control store. The store is re-opened per read for the reason
    the read path documents: ``states`` and ``accepted_bridges`` are snapshots of
    already-verified records, so re-opening costs nothing and picks up records
    written since the last read.
    """
    keycontrol_path = Path(keycontrol_path)
    content_path = Path(content_path)

    def holder(*, set_id: str, org: "str | None") -> settings_ops.VaultKeyControl:
        with KeyControlStore(keycontrol_path) as key_control:
            holdings = Holdings(
                secrets=cache.secrets,
                descriptors=key_control.states,
                bridges=list(key_control.accepted_bridges()),
            )
        return settings_ops.VaultKeyControl(
            holdings=holdings,
            content_store=ContentStore(content_path),
        )

    return holder


def register_key_holder(
    cache: VaultKeyCache,
    keycontrol_path: "str | Path",
    content_path: "str | Path",
) -> Callable[..., settings_ops.VaultKeyControl]:
    """Build the holder and install it as the process's vault key holder.

    Call once per process, after the key-control and content store paths are
    known. The cache may be empty at registration and filled at unlock — the
    holder reads it live, so an audited read before the first unlock fails
    closed with an empty holdings rather than with a missing-holder error.
    """
    holder = build_key_holder(cache, keycontrol_path, content_path)
    settings_ops.set_vault_key_holder(holder)
    return holder
