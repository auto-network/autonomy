"""Store-backed orchestration of the policy-class lifecycle.

One layer both the headless CLI and the acceptance tests drive, so the flows —
which always combine *opening a factor* with a class operation — are defined
once. The security property that falls out: sealing a secured setting REQUIRES
an opener (a factor secret), because holding the ``class_key`` requires opening
a wrap. There is deliberately no code path that seals a setting without a
factor present — "NO UNATTENDED PROCESS CAN WRITE A SECURED SETTING" (crib §18).
"""

from __future__ import annotations

import secrets

from .errors import VaultError
from .factors import PASSWORD, PublishedFactor, create_password_factor, open_password_seed
from .policy_class import (
    create_class,
    extend_class,
    open_cek,
    revoke_factor,
    seal_cek,
)
from .store import VaultSecretRecord, VaultStore

_CEK_LEN = 32


# ── enrollment (INTENT acts — gated on the human at the ceremony) ──────────


def enroll_password_factor(store: VaultStore, factor_id: str, password: str) -> PublishedFactor:
    """Mint a password factor and persist its armor + published pub."""
    factor = create_password_factor(password, factor_id=factor_id)
    store.put_password_factor(factor_id, factor.published.public_key, factor.armor)
    return factor.published


def enroll_passkey_factor(store: VaultStore, factor_id: str, published: PublishedFactor) -> None:
    """Persist a passkey factor's published identity (PRF seed never stored)."""
    store.put_passkey_factor(factor_id, published.public_key)


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


def enroll_into_class(
    store: VaultStore,
    class_id: str,
    opener_seeds: dict[str, bytes],
    new_factor_id: str,
    *,
    new_password: str | None = None,
    new_passkey: PublishedFactor | None = None,
) -> None:
    """Extend a class with a new factor — requires opening the class first.

    Exactly one of *new_password* / *new_passkey* names the factor being added.
    """
    record = store.get_class(class_id)
    if new_password is not None:
        factor = create_password_factor(new_password, factor_id=new_factor_id)
        store.put_password_factor(new_factor_id, factor.published.public_key, factor.armor)
        published = factor.published
    elif new_passkey is not None:
        store.put_passkey_factor(new_factor_id, new_passkey.public_key)
        published = new_passkey
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
    opener_seeds: dict[str, bytes],
    *,
    cek: bytes | None = None,
) -> bytes:
    """Seal a setting's data key (CEK) under its class. Requires an opener.

    Generates a fresh CEK if none is given. Returns the CEK so a caller can
    round-trip it in tests. The class_key is opened, used, and dropped.
    """
    record = store.get_class(class_id)
    cek = cek or secrets.token_bytes(_CEK_LEN)
    sealed = seal_cek(
        record,
        opener_seeds,  # sealing requires a factor — opened inside seal_cek
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
    under its class's current class_key, so the setting follows the rotation.

    Opens the setting with *opener_seeds* to recover the CEK, then re-seals it
    under the current class key (also opened). This is what "applied lazily at
    the next write" means — a real write, not a bulk re-wrap.
    """
    secret = store.get_secret(setting_name)
    cek = open_setting(store, setting_name, opener_seeds)
    seal_setting(
        store,
        setting_name,
        secret.policy_class_id,
        secret.genesis_id,
        secret.required_policy,
        opener_seeds,
        cek=cek,
    )
