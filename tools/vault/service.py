"""Store-backed orchestration of the policy-class lifecycle.

One layer both the headless CLI and the acceptance tests drive. Writes seal to
the class's public key and therefore need no factor; reads and factor enrollment
open the class and remain factor-gated. Write authorization belongs to the
Settings/application layer, while this layer enforces confidentiality.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from tools.network.idkit.enrollment import verified_provisioning_key
from tools.network.idkit.armor import canonicalize_armor

from .errors import VaultError
from .factors import (
    PASSKEY,
    PASSWORD,
    PublishedFactor,
    create_password_factor,
    open_password_seed,
)
from .policy_class import (
    create_class,
    create_root_reachable_class,
    enable_public_sealing,
    extend_class,
    open_cek,
    revoke_factor,
    seal_cek,
)
from .store import VaultSecretRecord, VaultStore
from .root_anchor import RootAnchorRecord

_CEK_LEN = 32


# ── enrollment (INTENT acts — gated on the human at the ceremony) ──────────


def enroll_password_factor(store: VaultStore, factor_id: str, password: str) -> PublishedFactor:
    """Mint a password factor and persist its armor + published pub."""
    factor = create_password_factor(password, factor_id=factor_id)
    store.put_password_factor(factor_id, factor.published.public_key, factor.armor)
    return factor.published


def enroll_password_factor_material(
    store: VaultStore, factor_id: str, public_key: str, armor: str
) -> PublishedFactor:
    """Persist browser-produced password material without receiving a password.

    The browser derives the factor public key and creates the PBKDF2/AES armor;
    the server only validates and canonicalizes the armor envelope.
    """
    canonical = canonicalize_armor(armor)
    if not isinstance(public_key, str) or len(public_key) != 64:
        raise VaultError("factor public_key must be 64 hex characters")
    try:
        int(public_key, 16)
    except ValueError as exc:
        raise VaultError("factor public_key must be lowercase hexadecimal") from exc
    store.put_password_factor(factor_id, public_key, canonical)
    return PublishedFactor(factor_id, PASSWORD, public_key)


def enroll_passkey_factor(
    store: VaultStore,
    factor_id: str,
    *,
    statement,
    root_pub: str,
    row_key: str | None = None,
) -> PublishedFactor:
    """Persist a passkey factor, taking its address FROM THE ROOT'S SIGNATURE.

    This deliberately does not accept a :class:`PublishedFactor`. A public key
    handed in by a caller is a public key nobody attested, and every downstream
    seal would be addressed to it — the settings store is agent-writable by
    design (crib B1), so "the caller supplied it" is not evidence of anything.

    Taking the statement instead makes the unsafe call unrepresentable rather
    than merely discouraged: there is no argument shape that lets an unsigned
    key reach ``put_passkey_factor``. The PRF seed is still never stored, and
    still never reaches this process.
    """
    public_key = verified_provisioning_key(
        statement, root_pub=root_pub, row_key=row_key
    )
    store.put_passkey_factor(factor_id, public_key)
    return PublishedFactor(factor_id, PASSKEY, public_key)


# ── openers ────────────────────────────────────────────────────────────────


def password_seed(store: VaultStore, factor_id: str, password: str) -> bytes:
    """Open a stored password factor's armor → its 32-byte seed."""
    return open_password_seed(store.get_password_armor(factor_id), password)


# ── class lifecycle ────────────────────────────────────────────────────────


def create_policy_class(
    store: VaultStore, policy: str, factor_ids: list[str], *, created_at: str
) -> str:
    """Create a class over already-enrolled factors (uses their pubs only)."""
    factors = [store.get_published_factor(fid) for fid in factor_ids]
    record = create_class(policy, factors, created_at=created_at)
    store.put_class(record)
    return record.class_id


def create_root_policy_class(
    store: VaultStore,
    anchor_id: str,
    *,
    display_name: str,
    created_at: str,
) -> str:
    """Create the default class inherited from the personal root's policy."""
    anchor = store.get_root_anchor(anchor_id)
    record = create_root_reachable_class(
        anchor.published_recipient(),
        display_name=display_name,
        created_at=created_at,
    )
    store.put_class(record)
    return record.class_id


def ensure_root_policy_class(
    store: VaultStore,
    anchor_id: str,
    *,
    display_name: str,
    created_at: str,
) -> str:
    """Atomically create or reuse the one class inherited from an anchor."""
    anchor = store.get_root_anchor(anchor_id)
    candidate = create_root_reachable_class(
        anchor.published_recipient(),
        display_name=display_name,
        created_at=created_at,
    )
    return store.put_root_class_once(candidate, anchor_id).class_id


def enroll_root_anchor(store: VaultStore, value: dict) -> RootAnchorRecord:
    """Validate and insert a browser-created, root-signed anchor envelope."""
    record = RootAnchorRecord.from_dict(value)
    store.put_root_anchor(record)
    return record


def enroll_into_class(
    store: VaultStore,
    class_id: str,
    opener_seeds: dict[str, bytes],
    new_factor_id: str,
    *,
    new_password: str | None = None,
    new_passkey_statement=None,
    root_pub: str | None = None,
    row_key: str | None = None,
) -> None:
    """Extend a class with a new factor — requires opening the class first.

    Exactly one of *new_password* / *new_passkey_statement* names the factor
    being added. A passkey is named by its ROOT-SIGNED ENROLLMENT STATEMENT and
    never by a bare public key, for the reason given on
    :func:`enroll_passkey_factor`: this call seals every existing generation to
    that address, so an unattested one is the whole attack.
    """
    record = store.get_class(class_id)
    if new_password is not None:
        factor = create_password_factor(new_password, factor_id=new_factor_id)
        store.put_password_factor(new_factor_id, factor.published.public_key, factor.armor)
        published = factor.published
    elif new_passkey_statement is not None:
        if not root_pub:
            raise VaultError(
                "enrolling a passkey needs root_pub to verify its statement against"
            )
        published = enroll_passkey_factor(
            store, new_factor_id,
            statement=new_passkey_statement, root_pub=root_pub, row_key=row_key,
        )
    else:
        raise VaultError("enroll_into_class needs a new password or passkey factor")
    store.put_class(extend_class(record, opener_seeds, published))


def revoke_and_rekey(store: VaultStore, class_id: str, factor_id: str, *, created_at: str) -> None:
    """Revoke a factor: append a new generation sealed to survivors, persist it.

    Ceremony-free (the crib's REMEDY SHAPE). Existing generations — and every
    sealed_cek under them — are left untouched; new writes use the new
    generation. Re-sealing a setting to follow the rotation is a subsequent
    write (:func:`reseal_setting`), not part of revocation.
    """
    record = store.get_class(class_id)
    store.put_class(revoke_factor(record, factor_id, created_at=created_at))


# ── secured settings ───────────────────────────────────────────────────────


def seal_setting(
    store: VaultStore,
    setting_name: str,
    class_id: str,
    genesis_id: str,
    required_policy: str,
    *,
    cek: bytes | None = None,
    created_at: str | None = None,
) -> bytes:
    """Seal a setting's data key (CEK) to its class's public key.

    Generates a fresh CEK if none is given. Returns the CEK so a caller can
    round-trip it in tests. No factor or private class material is consumed.
    A legacy class is migrated by appending a public-sealing generation using
    only its current factors' published keys.
    """
    record = store.get_class(class_id)
    if record.current().sealing_public_key is None:
        record = enable_public_sealing(
            record,
            created_at=created_at or datetime.now(timezone.utc).isoformat(),
        )
        store.put_class(record)
    cek = cek or secrets.token_bytes(_CEK_LEN)
    sealed = seal_cek(
        record,
        cek,
        genesis_id=genesis_id,
        setting_name=setting_name,
        required_policy=required_policy,
    )
    store.put_secret(
        VaultSecretRecord(setting_name, genesis_id, class_id, required_policy, sealed)
    )
    return cek


def open_setting(
    store: VaultStore, setting_name: str, opener_seeds: dict[str, bytes]
) -> bytes:
    """Recover a setting's data key by opening its class. Requires an opener."""
    secret = store.get_secret(setting_name)
    record = store.get_class(secret.policy_class_id)
    return open_cek(
        record,
        opener_seeds,
        secret.sealed_cek,
        genesis_id=secret.genesis_id,
        setting_name=secret.setting_name,
        required_policy=secret.required_policy,
    )


def reseal_setting(
    store: VaultStore, setting_name: str, opener_seeds: dict[str, bytes]
) -> None:
    """The "next write" after a rotation: re-seal a setting's SAME data key
    to its class's current public key, so the setting follows the rotation.

    Opens the setting with *opener_seeds* to recover the CEK, then re-seals it
    to the current generation. This is what "applied lazily at the next write"
    means — a real write, not a bulk re-wrap.
    """
    secret = store.get_secret(setting_name)
    cek = open_setting(store, setting_name, opener_seeds)
    seal_setting(
        store,
        setting_name,
        secret.policy_class_id,
        secret.genesis_id,
        secret.required_policy,
        cek=cek,
    )
