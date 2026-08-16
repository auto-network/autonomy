"""Policy classes — one key-encryption key per access policy (crib §18).

The problem this avoids (bead auto-39d26): if every secret's data key were
wrapped directly to every factor, enrolling a passkey would re-wrap every
secret, and any secret missed would end up protected by a divergent factor set.
So a setting's data key does not name factors at all — it names a **policy
class**. The class holds a symmetric ``class_key`` and carries the per-factor
wraps; a setting's content-encryption key (CEK) is sealed *under the class_key*.
Enrolling a factor adds one wrap to the class and touches no setting. Individual
secrets hold no factor wraps, so they cannot diverge.

**Key generations.** Revoking a factor does not mutate the existing key — that
would take a secret back from a holder who already opened it, which is
impossible (bead NOTES, crib §3: "revocation ≡ excluded from FUTURE seals; never
loses synced data"). Instead revocation appends a NEW *generation*: a fresh
``class_key`` sealed to the SURVIVING factors only. Old generations are retained
untouched, so survivors keep reading existing secrets and the revoked factor
cannot read anything sealed under the new generation. A ``sealed_cek`` records
which generation sealed it; new writes use the current generation. Generations
grow with revocations, never with the number of secrets — enrolling a factor is
still O(generations), independent of secret count. This is the same structural
move as the storage-state DAG: re-wrap the state, never the objects.

This construction serves BOTH domains (bead): in PERSONAL the class is the whole
hierarchy under the master KEK; in an ORGANIZATION the generation wraps the
object and the class wraps the content key beneath it. Nothing here is scoped to
organizations.

Named primitives are ADOPTED, not reimplemented (bead):

* per-factor wraps  — ``idkit.sealing.seal`` / ``.open`` (RFC 9180 HPKE base
  mode, X25519 / HKDF-SHA-256 / ChaCha20-Poly1305), purpose-labelled.
* the class→CEK seal — the AEAD suite resolved by ``storagekit.suites`` from the
  record's ``suite_id`` (AES-256-GCM-SIV, nonce-misuse-resistant).

Phase one ships the ``password`` policy. ``prf`` and ``both`` are constructible
here (the XOR split is pure symmetric crypto) so the cross-model attack can
exercise a downgrade headlessly, but their *real* factor seed is a WebAuthn PRF
output whose library is out of this epic — see :mod:`tools.vault.factors`.

THE NARROWING (crib §18): ``class_key`` is symmetric, so sealing a CEK requires
HOLDING it, which requires opening a per-factor wrap — a human at that instant.
No function here caches a class_key; callers must not either.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, replace

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV

from tools.network.idkit.canonical import canonical_json
from tools.network.idkit.sealing import SealingError, open as seal_open, seal
from tools.network.storagekit import suites

from .errors import (
    ClassOpenError,
    FactorError,
    FactorIndependenceError,
    PolicyClassError,
    PolicyMismatchError,
)
from .factors import (
    PASSKEY,
    PASSWORD,
    PublishedFactor,
    factor_private_from_seed,
)

#: Purpose label the per-factor wrap of a class_key (or share) is sealed under.
#: Class id, generation id, policy and role are appended so a wrap cannot be
#: lifted into another class, another generation, re-read under another policy,
#: or swapped between the two shares of a ``both`` split — every such move
#: changes the HPKE info string and fails authentication (attack surface:
#: cross-class key confusion, both→password downgrade).
CLASS_WRAP_PURPOSE = "autonomy/vault-policy-class/v1"

#: AAD domain separator for the class→CEK AEAD seal.
_CEK_AAD_DOMAIN = "autonomy/vault-policy-class/cek/v1"

PASSWORD_POLICY = "password"
PRF_POLICY = "prf"
BOTH_POLICY = "both"
POLICIES = (PASSWORD_POLICY, PRF_POLICY, BOTH_POLICY)

#: which factor type each single-wrap policy admits
_SINGLE_TYPE = {PASSWORD_POLICY: PASSWORD, PRF_POLICY: PASSKEY}

ROLE_SINGLE = "single"
ROLE_A = "a"  # password share of a `both` split
ROLE_B = "b"  # passkey  share of a `both` split
_BOTH_ROLE_TYPE = {PASSWORD: ROLE_A, PASSKEY: ROLE_B}

_CLASS_KEY_LEN = 32


# ── records ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Wrap:
    """One factor's wrap of a generation's class_key (or of one share)."""

    factor_id: str
    factor_type: str
    role: str
    public_key: str
    wrapped: str  # hex of the idkit sealing wire record

    def to_dict(self) -> dict:
        return {
            "factor_id": self.factor_id,
            "factor_type": self.factor_type,
            "role": self.role,
            "public_key": self.public_key,
            "wrapped": self.wrapped,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Wrap":
        try:
            return cls(d["factor_id"], d["factor_type"], d["role"], d["public_key"], d["wrapped"])
        except (KeyError, TypeError) as exc:
            raise PolicyClassError(f"malformed wrap: {exc}") from exc


@dataclass(frozen=True)
class Generation:
    """One key generation: a class_key sealed to the factors admitted at the
    time it was minted. The newest generation is the one new writes use."""

    gen_id: str
    wraps: tuple[Wrap, ...]

    def to_dict(self) -> dict:
        return {"gen_id": self.gen_id, "wraps": [w.to_dict() for w in self.wraps]}

    @classmethod
    def from_dict(cls, d: dict) -> "Generation":
        try:
            return cls(d["gen_id"], tuple(Wrap.from_dict(w) for w in d["wraps"]))
        except (KeyError, TypeError) as exc:
            raise PolicyClassError(f"malformed generation: {exc}") from exc

    def factor_ids(self) -> tuple[str, ...]:
        return tuple(w.factor_id for w in self.wraps)


@dataclass(frozen=True)
class PolicyClassRecord:
    """Everything persisted for a class: id, policy, key generations,
    created_at. Immutable; every operation returns a NEW record and no
    class_key is ever stored in it."""

    class_id: str
    policy: str
    generations: tuple[Generation, ...]
    created_at: str

    def to_dict(self) -> dict:
        return {
            "class_id": self.class_id,
            "policy": self.policy,
            "generations": [g.to_dict() for g in self.generations],
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PolicyClassRecord":
        try:
            record = cls(
                class_id=d["class_id"],
                policy=d["policy"],
                generations=tuple(Generation.from_dict(g) for g in d["generations"]),
                created_at=d["created_at"],
            )
        except (KeyError, TypeError) as exc:
            raise PolicyClassError(f"malformed policy-class record: {exc}") from exc
        if not record.generations:
            # A class always has at least one generation; a zero-generation row
            # is a corrupted store, surfaced in the package's own taxonomy
            # rather than as a downstream IndexError (attack finding MINOR-6e).
            raise PolicyClassError("policy-class record has no generations")
        return record

    def current(self) -> Generation:
        if not self.generations:
            raise PolicyClassError("policy-class record has no generations")
        return self.generations[-1]

    def generation(self, gen_id: str) -> Generation:
        for g in self.generations:
            if g.gen_id == gen_id:
                return g
        raise PolicyClassError(f"no generation {gen_id!r} in class {self.class_id!r}")

    def factor_ids(self) -> tuple[str, ...]:
        """Factor ids admitted to the CURRENT generation (the live factor set)."""
        return self.current().factor_ids()


# ── helpers ──────────────────────────────────────────────────────────────


def _require_policy(policy: str) -> None:
    if policy not in POLICIES:
        raise PolicyClassError(f"unknown policy {policy!r}; known: {POLICIES}")


def _wrap_purpose(class_id: str, gen_id: str, policy: str, role: str) -> str:
    return f"{CLASS_WRAP_PURPOSE}|{class_id}|{gen_id}|{policy}|{role}"


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def _seal_wrap(key, factor, class_id, gen_id, policy, role) -> Wrap:
    wire = seal(key, factor.public_key, _wrap_purpose(class_id, gen_id, policy, role))
    return Wrap(factor.factor_id, factor.factor_type, role, factor.public_key, wire.hex())


def _open_wrap(wrap, seed, class_id, gen_id, policy) -> bytes:
    priv = factor_private_from_seed(seed)
    return seal_open(
        bytes.fromhex(wrap.wrapped), priv, _wrap_purpose(class_id, gen_id, policy, wrap.role)
    )


def _reject_shared_public_keys(factors) -> None:
    """No two factors in one generation may share a public key.

    A shared public key means two factors are backed by the same key material.
    For a ``both`` class that collapses the 2-of-2 to 1-of-1 — one secret
    registered as both the password and the passkey factor opens the class
    alone (attack finding BROKEN-1). For single-wrap policies it is a
    degenerate no-op enrollment. Rejecting it makes the demonstrated collapse
    unrepresentable; genuine independence of two *distinct* secrets is the
    enrolling ceremony's responsibility and cannot be decided here.
    """
    seen: dict[str, str] = {}
    for f in factors:
        prior = seen.get(f.public_key)
        if prior is not None:
            raise FactorIndependenceError(
                f"factors {prior!r} and {f.factor_id!r} share a public key; two "
                f"factors backed by the same key material collapse the policy"
            )
        seen[f.public_key] = f.factor_id


def _mint_generation(
    policy: str, factors, class_id: str, gen_id: str, class_key: bytes
) -> Generation:
    """Seal *class_key* (or its shares) to *factors* for a new generation."""
    _reject_shared_public_keys(factors)
    if policy in (PASSWORD_POLICY, PRF_POLICY):
        want = _SINGLE_TYPE[policy]
        wraps = []
        for f in factors:
            if f.factor_type != want:
                raise PolicyClassError(
                    f"{policy} class admits only {want} factors, got {f.factor_type!r}"
                )
            wraps.append(_seal_wrap(class_key, f, class_id, gen_id, policy, ROLE_SINGLE))
    else:  # both
        pw = [f for f in factors if f.factor_type == PASSWORD]
        pk = [f for f in factors if f.factor_type == PASSKEY]
        if not pw or not pk:
            raise PolicyClassError(
                "a both class needs at least one password and one passkey factor"
            )
        share_a = secrets.token_bytes(_CLASS_KEY_LEN)
        share_b = _xor(class_key, share_a)
        wraps = [_seal_wrap(share_a, f, class_id, gen_id, policy, ROLE_A) for f in pw]
        wraps += [_seal_wrap(share_b, f, class_id, gen_id, policy, ROLE_B) for f in pk]
    return Generation(gen_id, tuple(wraps))


def _open_generation(record: PolicyClassRecord, gen: Generation, seeds) -> tuple[bytes, dict]:
    """Recover *gen*'s class_key. Returns ``(class_key, shares)`` where shares
    is ``{}`` for single-wrap policies and ``{"a","b"}`` for ``both``."""
    policy = record.policy
    if policy in (PASSWORD_POLICY, PRF_POLICY):
        for wrap in gen.wraps:
            seed = seeds.get(wrap.factor_id)
            if seed is None:
                continue
            try:
                key = _open_wrap(wrap, seed, record.class_id, gen.gen_id, policy)
            except (SealingError, FactorError):
                continue
            if len(key) == _CLASS_KEY_LEN:
                return key, {}
        raise ClassOpenError("no supplied factor opens this generation")
    share_a = _recover_role(record, gen, seeds, ROLE_A)
    share_b = _recover_role(record, gen, seeds, ROLE_B)
    if share_a is None or share_b is None:
        raise ClassOpenError(
            "a both class needs both a password and a passkey factor to open"
        )
    return _xor(share_a, share_b), {ROLE_A: share_a, ROLE_B: share_b}


def _recover_role(record, gen, seeds, role):
    for wrap in gen.wraps:
        if wrap.role != role:
            continue
        seed = seeds.get(wrap.factor_id)
        if seed is None:
            continue
        try:
            share = _open_wrap(wrap, seed, record.class_id, gen.gen_id, record.policy)
        except (SealingError, FactorError):
            continue
        if len(share) == _CLASS_KEY_LEN:
            return share
    return None


# ── create ───────────────────────────────────────────────────────────────


def create_class(
    policy: str,
    factors: list[PublishedFactor],
    *,
    class_id: str | None = None,
    created_at: str,
) -> PolicyClassRecord:
    """Mint a class: a first generation sealed to *factors*' public keys.

    Requires NO existing factor and opens nothing — it consumes only the
    factors' published public keys (crib §18). Returns the record; the
    class_key is not persisted or returned. Seal a setting under it with
    :func:`seal_cek`, which re-derives the key from a factor.
    """
    _require_policy(policy)
    if not factors:
        raise PolicyClassError("a class needs at least one factor")
    class_id = class_id or secrets.token_hex(16)
    gen = _mint_generation(policy, factors, class_id, secrets.token_hex(12), secrets.token_bytes(_CLASS_KEY_LEN))
    return PolicyClassRecord(class_id, policy, (gen,), created_at)


# ── open ─────────────────────────────────────────────────────────────────


def open_class(record: PolicyClassRecord, seeds: dict[str, bytes]) -> bytes:
    """Recover the CURRENT generation's class_key after the policy is satisfied
    by *seeds* (``factor_id`` → 32-byte seed). This is the key new writes use.
    Raises :class:`ClassOpenError` if the policy is not satisfied."""
    key, _ = _open_generation(record, record.current(), seeds)
    return key


# ── extend (enroll an additional factor) ───────────────────────────────────


def extend_class(
    record: PolicyClassRecord,
    seeds: dict[str, bytes],
    new_factor: PublishedFactor,
) -> PolicyClassRecord:
    """Enroll *new_factor* — one added wrap PER GENERATION, keys unchanged.

    Extending REQUIRES opening the class (crib §18: "extending one requires
    opening it"); *seeds* must open EVERY generation or :class:`ClassOpenError`
    is raised — a design that extended a generation it could not open would be a
    backdoor, and granting the new factor read access to existing secrets means
    sealing it into the generations those secrets live under. No class_key
    changes, so no setting's stored ciphertext is touched. Returns a NEW record.
    """
    if new_factor.factor_id in _all_factor_ids(record):
        raise PolicyClassError(f"factor {new_factor.factor_id!r} is already enrolled")
    if new_factor.public_key in _all_public_keys(record):
        # Enrolling a factor whose key material already backs another factor
        # collapses the policy exactly as minting one would (BROKEN-1).
        raise FactorIndependenceError(
            f"factor {new_factor.factor_id!r} shares a public key with an "
            f"already-enrolled factor"
        )
    _validate_new_factor_type(record.policy, new_factor)

    new_gens = []
    for gen in record.generations:
        class_key, shares = _open_generation(record, gen, seeds)  # raises if unopenable
        if record.policy in (PASSWORD_POLICY, PRF_POLICY):
            wrap = _seal_wrap(class_key, new_factor, record.class_id, gen.gen_id, record.policy, ROLE_SINGLE)
        else:
            role = _BOTH_ROLE_TYPE[new_factor.factor_type]
            wrap = _seal_wrap(shares[role], new_factor, record.class_id, gen.gen_id, record.policy, role)
        new_gens.append(replace(gen, wraps=gen.wraps + (wrap,)))
    return replace(record, generations=tuple(new_gens))


def _validate_new_factor_type(policy: str, new_factor: PublishedFactor) -> None:
    if policy in (PASSWORD_POLICY, PRF_POLICY):
        want = _SINGLE_TYPE[policy]
        if new_factor.factor_type != want:
            raise PolicyClassError(f"{policy} class admits only {want} factors")
    elif new_factor.factor_type not in _BOTH_ROLE_TYPE:
        raise PolicyClassError("both class admits password or passkey factors only")


def _all_factor_ids(record: PolicyClassRecord) -> set:
    return {w.factor_id for g in record.generations for w in g.wraps}


def _all_public_keys(record: PolicyClassRecord) -> set:
    return {w.public_key for g in record.generations for w in g.wraps}


# ── revoke (append a generation; NO bulk re-wrap of data keys, ever) ────────


def revoke_factor(
    record: PolicyClassRecord, factor_id: str, *, created_at: str
) -> PolicyClassRecord:
    """Revoke *factor_id*: APPEND a new generation sealed to the SURVIVORS.

    Existing generations are left exactly as they are, so survivors keep
    reading everything sealed before now and the revoked factor is simply
    excluded from the new generation's writes (crib §3). This takes NO settings
    argument and CANNOT re-wrap existing data keys: bulk re-wrap is a
    correctness defect (bead NOTES) — a revoked device that already holds an old
    class_key opens anything it can still obtain ciphertext for, so the only
    honest operations are to cycle the key forward (here) and to destroy
    plaintext rigorously (the caller's job).

    Ceremony-free by design (the crib's REMEDY SHAPE / auto-resolve): a factor
    is necessarily open at any secured write, so the acting client appends the
    generation itself with no prompt. ``created_at`` re-stamps the class.
    """
    current = record.current()
    survivors = [
        PublishedFactor(w.factor_id, w.factor_type, w.public_key)
        for w in current.wraps
        if w.factor_id != factor_id
    ]
    if len(survivors) == len(current.wraps):
        raise PolicyClassError(f"factor {factor_id!r} is not in the current generation")
    if not survivors:
        raise PolicyClassError("cannot revoke the last factor of a class")

    new_gen = _mint_generation(
        record.policy, survivors, record.class_id, secrets.token_hex(12),
        secrets.token_bytes(_CLASS_KEY_LEN),
    )
    return replace(
        record, generations=record.generations + (new_gen,), created_at=created_at
    )


# ── seal / open a setting's CEK under the class ────────────────────────────


def seal_cek(
    record: PolicyClassRecord,
    seeds: dict[str, bytes],
    cek: bytes,
    *,
    genesis_id: str,
    setting_name: str,
    required_policy: str,
) -> dict:
    """Seal a setting's *cek* under the class's CURRENT generation.

    Opens the current generation with *seeds* first — sealing a secured setting
    requires HOLDING the class_key, which requires a factor (crib §18: no
    unattended process can write a secured setting). ``associated_data`` binds
    ``genesis_id``, ``class_id``, the ``gen_id``, ``setting_name`` and the
    class's ``policy``, so material sealed for one class, generation, setting or
    policy does not verify for another. *required_policy* is the policy the
    naming setting demands; it must equal the class's policy, else
    :class:`PolicyMismatchError` — this stops a setting from being sealed under a
    class with a weaker factor set than it requires.
    """
    _check_policy_match(record, required_policy)
    if not isinstance(cek, (bytes, bytearray)) or not cek:
        raise PolicyClassError("cek must be non-empty bytes")
    gen = record.current()
    class_key = open_class(record, seeds)  # requires a factor
    suite_id = suites.WRAP_SUITE
    suites.require_suite(suite_id, suites.WRAP_SUITES)
    nonce = secrets.token_bytes(12)
    aad = _cek_aad(record, gen.gen_id, genesis_id, setting_name, suite_id)
    ct = AESGCMSIV(class_key).encrypt(nonce, bytes(cek), aad)
    return {"suite_id": suite_id, "gen_id": gen.gen_id, "nonce": nonce.hex(), "ciphertext": ct.hex()}


def open_cek(
    record: PolicyClassRecord,
    seeds: dict[str, bytes],
    sealed_cek: dict,
    *,
    genesis_id: str,
    setting_name: str,
    required_policy: str,
) -> bytes:
    """Recover a setting's CEK sealed by :func:`seal_cek`.

    Opens the GENERATION named in *sealed_cek* (so a survivor still reads a
    secret sealed before a revocation) with *seeds*, then AEAD-decrypts. Fails
    closed on any mismatch of key, class, generation, setting, policy or suite.
    """
    _check_policy_match(record, required_policy)
    try:
        suite_id = sealed_cek["suite_id"]
        gen_id = sealed_cek["gen_id"]
        nonce = bytes.fromhex(sealed_cek["nonce"])
        ct = bytes.fromhex(sealed_cek["ciphertext"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyClassError(f"malformed sealed_cek: {exc}") from exc
    suites.require_suite(suite_id, suites.WRAP_SUITES)
    gen = record.generation(gen_id)
    class_key, _ = _open_generation(record, gen, seeds)  # raises if not satisfied
    aad = _cek_aad(record, gen_id, genesis_id, setting_name, suite_id)
    try:
        return AESGCMSIV(class_key).decrypt(nonce, ct, aad)
    except (InvalidTag, ValueError) as exc:
        raise PolicyClassError(
            "sealed_cek does not open with that class key in this context"
        ) from exc


def _check_policy_match(record: PolicyClassRecord, required_policy: str) -> None:
    _require_policy(required_policy)
    if record.policy != required_policy:
        raise PolicyMismatchError(
            f"setting requires policy {required_policy!r} but its class is "
            f"{record.policy!r}; refusing to seal/open across a policy boundary"
        )


def _cek_aad(record, gen_id, genesis_id, setting_name, suite_id) -> bytes:
    return _CEK_AAD_DOMAIN.encode("ascii") + canonical_json(
        {
            "genesis_id": genesis_id,
            "class_id": record.class_id,
            "gen_id": gen_id,
            "setting_name": setting_name,
            "policy": record.policy,
            "suite_id": suite_id,
        }
    )
