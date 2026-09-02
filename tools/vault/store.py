"""Persistence for policy classes, their factors, and the settings that name
them — the "store" half of the bead's MODIFIES (a new policy-class schema and
store; the vault secret schema's reference to its class).

SQLite, one writer, mirroring ``storagekit.keycontrol.KeyControlStore``'s
idiom. Four tables:

* ``policy_classes`` — the class record (``class_id`` → canonical JSON). A class
  is mutable in place: extending adds a wrap and revoking re-mints under the
  SAME ``class_id``, so this upserts. No class_key is ever stored here.
* ``vault_factors`` — a password factor's armor (its persisted, password-gated
  material). A passkey factor stores no seed; the PRF output is produced live.
* ``root_anchors`` — root-signed envelopes for stable personal vault anchors.
* ``vault_secrets`` — a setting's ``sealed_cek`` plus its **reference to its
  class**: ``policy_class_id`` and the ``required_policy`` the setting demands.
  The data key opens ONLY through the named class.

Nothing here holds plaintext or a class_key. The store is open to agents by
design (crib §0); confidentiality is the seal, verified at use, never the table.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from tools.network.fleet_sync_connection import FleetSyncConnection
from tools.network.idkit.canonical import canonical_json

from .recipients import PERSONAL_ROOT_RECIPIENT
from .errors import ConcurrencyError, PolicyClassError, VaultError
from .factors import PublishedFactor
from .policy_class import PolicyClassRecord
from .root_anchor import RootAnchorRecord


def _assert_append_only_successor(old: PolicyClassRecord, new: PolicyClassRecord) -> None:
    """Refuse *new* unless it only GROWS *old*: same policy, every stored
    generation kept in order with a superset of its wraps, plus zero or more
    appended generations. A stale-read write that would drop a generation or a
    wrap fails here instead of clobbering a concurrent revoke/enroll."""
    if new.policy != old.policy:
        raise ConcurrencyError(
            f"class {old.class_id!r} policy changed {old.policy!r}→{new.policy!r}"
        )
    if new.governance != old.governance:
        raise ConcurrencyError(
            f"class {old.class_id!r} governance changed; stale or corrupt write"
        )
    if len(new.generations) < len(old.generations):
        raise ConcurrencyError(
            f"class {old.class_id!r} write drops generations "
            f"({len(old.generations)}→{len(new.generations)}); stale read"
        )
    for i, old_gen in enumerate(old.generations):
        new_gen = new.generations[i]
        if new_gen.gen_id != old_gen.gen_id:
            raise ConcurrencyError(
                f"class {old.class_id!r} generation {i} id changed; stale read"
            )
        if new_gen.sealing_public_key != old_gen.sealing_public_key:
            raise ConcurrencyError(
                f"class {old.class_id!r} generation {old_gen.gen_id!r} "
                "sealing public key changed; stale or corrupt write"
            )
        old_wraps = {json.dumps(w.to_dict(), sort_keys=True) for w in old_gen.wraps}
        new_wraps = {json.dumps(w.to_dict(), sort_keys=True) for w in new_gen.wraps}
        if not old_wraps <= new_wraps:
            raise ConcurrencyError(
                f"class {old.class_id!r} generation {old_gen.gen_id!r} loses a "
                f"wrap; stale read racing a concurrent write"
            )

_SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_classes (
    class_id TEXT PRIMARY KEY,
    wire     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vault_factors (
    factor_id   TEXT PRIMARY KEY,
    factor_type TEXT NOT NULL,
    public_key  TEXT NOT NULL,
    armor       TEXT
);
CREATE TABLE IF NOT EXISTS root_anchors (
    anchor_id TEXT PRIMARY KEY,
    wire      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vault_secrets (
    setting_name TEXT PRIMARY KEY,
    wire         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delegate_recipients (
    recipient_id TEXT PRIMARY KEY,
    public_hex   TEXT NOT NULL
);
"""

#: The single audited-tier delegate recipient in a personal scope. Its 64-hex
#: X25519 public half is published so a personal audited write seals COLD; the
#: matching private half is re-derived at unlock and never stored.
DELEGATE_AUDITED_RECIPIENT_ID = "audited"


@dataclass(frozen=True)
class VaultSecretRecord:
    """A setting's sealed data key and its reference to its policy class.

    The setting NAMES its class (``policy_class_id``) and declares the policy it
    requires (``required_policy``); its ``sealed_cek`` opens only through that
    class at that policy. ``genesis_id`` and ``setting_name`` are bound into the
    seal's AAD, so a sealed_cek lifted to another setting or genesis will not
    verify.
    """

    setting_name: str
    genesis_id: str
    policy_class_id: str
    required_policy: str
    sealed_cek: dict

    def to_dict(self) -> dict:
        return {
            "setting_name": self.setting_name,
            "genesis_id": self.genesis_id,
            "policy_class_id": self.policy_class_id,
            "required_policy": self.required_policy,
            "sealed_cek": self.sealed_cek,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "VaultSecretRecord":
        try:
            return cls(
                setting_name=d["setting_name"],
                genesis_id=d["genesis_id"],
                policy_class_id=d["policy_class_id"],
                required_policy=d["required_policy"],
                sealed_cek=d["sealed_cek"],
            )
        except (KeyError, TypeError) as exc:
            raise VaultError(f"malformed vault secret record: {exc}") from exc


class VaultStore:
    """A durable store of policy classes, factor material, and vault secrets."""

    def __init__(self, path=":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, factory=FleetSyncConnection)
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode = WAL")
        with self.db:
            self.db.executescript(_SCHEMA)
        from tools.network.fleet_sync.catalog import (
            attach_active_production_catalog,
        )
        self._fleet_catalog = attach_active_production_catalog(self.db)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "VaultStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- policy classes ------------------------------------------------------

    def put_class(self, record: PolicyClassRecord) -> None:
        """Persist a class as an APPEND-ONLY successor of the stored one.

        A class only ever grows: extending adds wraps to existing generations,
        revoking appends a generation. A write built from a stale read — one
        that would drop a generation or a wrap — is refused with
        :class:`ConcurrencyError` rather than silently clobbering a concurrent
        revoke or enroll (attack findings BROKEN-2 / BROKEN-3). The check runs
        under an IMMEDIATE write lock so the read-modify-write is atomic against
        other writers; the caller retries on conflict.
        """
        # Widen-only invariant (operator ruling 2026-08-27): every persisted
        # class carries the root in every generation — a policy class widens
        # access beyond root, it never narrows below it. A record whose
        # generations lack a personal-root wrap cannot become real.
        for gen in record.generations:
            if not any(w.factor_type == PERSONAL_ROOT_RECIPIENT for w in gen.wraps):
                raise PolicyClassError(
                    f"class {record.class_id!r} generation {gen.gen_id!r} has no "
                    f"personal-root recipient; classes widen access beyond root, "
                    f"never narrow below it"
                )
        wire = canonical_json(record.to_dict()).decode("ascii")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT wire FROM policy_classes WHERE class_id = ?", (record.class_id,)
            ).fetchone()
            if row is not None:
                _assert_append_only_successor(
                    PolicyClassRecord.from_dict(json.loads(row[0])), record
                )
            self.db.execute(
                "INSERT OR REPLACE INTO policy_classes(class_id, wire) VALUES (?, ?)",
                (record.class_id, wire),
            )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def get_class(self, class_id: str) -> PolicyClassRecord:
        row = self.db.execute(
            "SELECT wire FROM policy_classes WHERE class_id = ?", (class_id,)
        ).fetchone()
        if row is None:
            raise PolicyClassError(f"no policy class {class_id!r}")
        return PolicyClassRecord.from_dict(json.loads(row[0]))

    def put_root_class_once(
        self, record: PolicyClassRecord, anchor_id: str,
    ) -> PolicyClassRecord:
        """Atomically reuse or insert the sole root class for ``anchor_id``.

        Two browser tabs can finish the same login bootstrap concurrently.
        The check and insert therefore share one IMMEDIATE transaction; an
        application-level check before ``put_class`` would still permit two
        independently keyed classes to land between those operations.
        """
        # Widen-only invariant (operator ruling 2026-08-27): every persisted
        # class carries the root in every generation — a policy class widens
        # access beyond root, it never narrows below it. A record whose
        # generations lack a personal-root wrap cannot become real.
        for gen in record.generations:
            if not any(w.factor_type == PERSONAL_ROOT_RECIPIENT for w in gen.wraps):
                raise PolicyClassError(
                    f"class {record.class_id!r} generation {gen.gen_id!r} has no "
                    f"personal-root recipient; classes widen access beyond root, "
                    f"never narrow below it"
                )
        wire = canonical_json(record.to_dict()).decode("ascii")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            rows = self.db.execute("SELECT wire FROM policy_classes").fetchall()
            for (stored_wire,) in rows:
                stored = PolicyClassRecord.from_dict(json.loads(stored_wire))
                if (
                    stored.governance
                    and stored.governance.get("form") == "root-reachable"
                    and stored.governance.get("anchor_id") == anchor_id
                ):
                    self.db.commit()
                    return stored
            self.db.execute(
                "INSERT INTO policy_classes(class_id, wire) VALUES (?, ?)",
                (record.class_id, wire),
            )
            self.db.commit()
            return record
        except BaseException:
            self.db.rollback()
            raise

    # -- factor material -----------------------------------------------------

    def put_password_factor(self, factor_id: str, public_key: str, armor: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO vault_factors(factor_id, factor_type, public_key, armor)"
                " VALUES (?, 'password', ?, ?)",
                (factor_id, public_key, armor),
            )

    def put_passkey_factor(self, factor_id: str, public_key: str) -> None:
        """Persist a passkey factor's PUBLIC identity only — no seed is stored;
        the PRF output is produced live at open time (crib §18)."""
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO vault_factors(factor_id, factor_type, public_key, armor)"
                " VALUES (?, 'passkey', ?, NULL)",
                (factor_id, public_key),
            )

    def get_password_armor(self, factor_id: str) -> str:
        row = self.db.execute(
            "SELECT armor FROM vault_factors WHERE factor_id = ? AND factor_type = 'password'",
            (factor_id,),
        ).fetchone()
        if row is None or row[0] is None:
            raise VaultError(f"no password factor {factor_id!r}")
        return row[0]

    def get_published_factor(self, factor_id: str) -> "PublishedFactor":
        row = self.db.execute(
            "SELECT factor_type, public_key FROM vault_factors WHERE factor_id = ?",
            (factor_id,),
        ).fetchone()
        if row is None:
            raise VaultError(f"no factor {factor_id!r}")
        return PublishedFactor(factor_id, row[0], row[1])

    def factors(self) -> list[PublishedFactor]:
        rows = self.db.execute(
            "SELECT factor_id, factor_type, public_key FROM vault_factors ORDER BY factor_id"
        ).fetchall()
        return [PublishedFactor(*row) for row in rows]

    # -- personal-root anchors ---------------------------------------------

    def put_root_anchor(self, record: RootAnchorRecord) -> None:
        """Enroll one immutable, root-attested anchor.

        Replacing an anchor in place would silently substitute the key behind
        every class that names it.  Root rotation re-wraps the same anchor seed
        through a separate continuity operation; ordinary enrollment is
        insert-only.
        """
        wire = canonical_json(record.to_dict()).decode("ascii")
        with self.db:
            row = self.db.execute(
                "SELECT wire FROM root_anchors WHERE anchor_id = ?",
                (record.anchor_id,),
            ).fetchone()
            if row is not None:
                if row[0] == wire:
                    return
                raise VaultError(
                    f"root anchor {record.anchor_id!r} already exists and cannot be replaced"
                )
            self.db.execute(
                "INSERT INTO root_anchors(anchor_id, wire) VALUES (?, ?)",
                (record.anchor_id, wire),
            )

    def get_root_anchor(self, anchor_id: str) -> RootAnchorRecord:
        row = self.db.execute(
            "SELECT wire FROM root_anchors WHERE anchor_id = ?", (anchor_id,),
        ).fetchone()
        if row is None:
            raise VaultError(f"no root anchor {anchor_id!r}")
        return RootAnchorRecord.from_dict(json.loads(row[0]))

    def root_anchor_ids(self) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT anchor_id FROM root_anchors ORDER BY anchor_id"
        ).fetchall()]

    def class_ids(self) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT class_id FROM policy_classes ORDER BY class_id"
        ).fetchall()]

    # -- vault secrets -------------------------------------------------------

    def put_secret(self, record: VaultSecretRecord) -> None:
        wire = canonical_json(record.to_dict()).decode("ascii")
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO vault_secrets(setting_name, wire) VALUES (?, ?)",
                (record.setting_name, wire),
            )

    def get_secret(self, setting_name: str) -> VaultSecretRecord:
        row = self.db.execute(
            "SELECT wire FROM vault_secrets WHERE setting_name = ?", (setting_name,)
        ).fetchone()
        if row is None:
            raise VaultError(f"no vault secret {setting_name!r}")
        return VaultSecretRecord.from_dict(json.loads(row[0]))

    def secret_names(self) -> list[str]:
        return [
            r[0]
            for r in self.db.execute(
                "SELECT setting_name FROM vault_secrets ORDER BY setting_name"
            ).fetchall()
        ]

    # -- delegate recipients -------------------------------------------------

    def put_delegate_audited_recipient(self, public_hex: str) -> None:
        """Publish the audited delegate's X25519 public half (insert-only).

        This public half is what a personal audited write seals to COLD. It is
        deterministic from the operator's root seed, so re-publishing the same
        value is a no-op, but a DIFFERENT value would silently orphan every
        secret sealed to the old one and is refused.
        """
        with self.db:
            row = self.db.execute(
                "SELECT public_hex FROM delegate_recipients WHERE recipient_id = ?",
                (DELEGATE_AUDITED_RECIPIENT_ID,),
            ).fetchone()
            if row is not None:
                if row[0] == public_hex:
                    return
                raise VaultError(
                    "the audited delegate recipient is already published and "
                    "cannot be replaced with a different key"
                )
            self.db.execute(
                "INSERT INTO delegate_recipients(recipient_id, public_hex) VALUES (?, ?)",
                (DELEGATE_AUDITED_RECIPIENT_ID, public_hex),
            )

    def get_delegate_audited_recipient(self) -> str:
        """The published audited delegate X25519 public half (64 hex), or raise."""
        row = self.db.execute(
            "SELECT public_hex FROM delegate_recipients WHERE recipient_id = ?",
            (DELEGATE_AUDITED_RECIPIENT_ID,),
        ).fetchone()
        if row is None:
            raise VaultError(
                "no audited delegate recipient is published; the operator must "
                "unlock the vault once to provision it"
            )
        return row[0]
