"""Policy classes — one key-encryption key per access policy (crib §18).

The problem this avoids (bead auto-39d26): if every secret's data key were
wrapped directly to every factor, enrolling a passkey would re-wrap every
secret, and any secret missed would end up protected by a divergent factor set.
So a setting's data key does not name factors at all — it names a **policy
class**. The class holds a factor-wrapped secret from which an asymmetric
sealing keypair is derived. Its PUBLIC sealing key wraps a setting's
content-encryption key (CEK); opening the private half still requires the
factor-wrapped class secret.
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
* the class→CEK seal — ``idkit.sealing.seal`` again, addressed to the class's
  public sealing key. This is what permits unattended writes without granting
  unattended reads.

Phase one ships the ``password`` policy. ``prf`` and ``both`` are constructible
here (the XOR split is pure symmetric crypto) so the cross-model attack can
exercise a downgrade headlessly, but their *real* factor seed is a WebAuthn PRF
output whose library is out of this epic — see :mod:`tools.vault.factors`.

.. warning:: ``prf`` and ``both`` are PRE-REVIEW. The bead defers them because
   they "carry their own cryptographic review" (``auto-7ej7d``), and that review
   has not happened. They are built here so the attack suite can exercise a
   downgrade, NOT so a caller can use them. Do not wire either policy into a
   production read or write path until that review lands.

Writing is deliberately NOT factor-gated. Anyone authorized by the Settings
layer to write can seal to the class's public key; only opening derives the
private key and therefore requires the policy's factor gesture. The vault owns
confidentiality, not application-level write authorization.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, replace

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV

from tools.network.idkit.canonical import canonical_json
from tools.network.idkit.sealing import (
    SealingError,
    derive_encapsulation_keypair,
    open as seal_open,
    seal,
)
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
from .recipients import (
    PERSONAL_ROOT_RECIPIENT,
    PublishedRecipient,
    recipient_private_from_seed,
)

#: Purpose label the per-factor wrap of a class_key (or share) is sealed under.
#: Class id, generation id, policy and role are appended so a wrap cannot be
#: lifted into another class, another generation, re-read under another policy,
#: or swapped between the two shares of a ``both`` split — every such move
#: changes the HPKE info string and fails authentication (attack surface:
#: cross-class key confusion, both→password downgrade).
CLASS_WRAP_PURPOSE = "autonomy/vault-policy-class/v1"

#: AAD domain separator for legacy class→CEK AES-GCM-SIV records.
_CEK_AAD_DOMAIN = "autonomy/vault-policy-class/cek/v1"

#: Deterministic derivation domain for a generation's asymmetric sealing key.
CLASS_SEAL_KEY_PURPOSE = "autonomy/vault-policy-class/sealing-key/v1"
#: HPKE info domain for a setting CEK sealed to that public key.
CEK_PUBLIC_SEAL_PURPOSE = "autonomy/vault-policy-class/cek-hpke/v1"
CEK_PUBLIC_SEAL_FORMAT = "hpke-x25519-v1"

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
ROOT_REACHABLE_FORM = "root-reachable"
ROOT_GOVERNANCE_VERSION = 1


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
        except (AttributeError, KeyError, TypeError) as exc:
            raise PolicyClassError(f"malformed wrap: {exc}") from exc


@dataclass(frozen=True)
class Generation:
    """One key generation: a class_key sealed to the factors admitted at the
    time it was minted. The newest generation is the one new writes use."""

    gen_id: str
    wraps: tuple[Wrap, ...]
    sealing_public_key: str | None = None

    def to_dict(self) -> dict:
        result = {"gen_id": self.gen_id, "wraps": [w.to_dict() for w in self.wraps]}
        if self.sealing_public_key is not None:
            result["sealing_public_key"] = self.sealing_public_key
        return result

    @classmethod
    def from_dict(cls, d: dict) -> "Generation":
        try:
            sealing_public_key = d.get("sealing_public_key")
            if sealing_public_key is not None and (
                not isinstance(sealing_public_key, str)
                or len(sealing_public_key) != 64
                or any(c not in "0123456789abcdef" for c in sealing_public_key)
            ):
                raise PolicyClassError(
                    "generation sealing_public_key must be 64 lowercase hex characters"
                )
            return cls(
                d["gen_id"],
                tuple(Wrap.from_dict(w) for w in d["wraps"]),
                sealing_public_key,
            )
        except PolicyClassError:
            raise
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
    governance: dict | None = None

    def to_dict(self) -> dict:
        value = {
            "class_id": self.class_id,
            "policy": self.policy,
            "generations": [g.to_dict() for g in self.generations],
            "created_at": self.created_at,
        }
        if self.governance is not None:
            value["governance"] = self.governance
        return value

    @classmethod
    def from_dict(cls, d: dict) -> "PolicyClassRecord":
        try:
            record = cls(
                class_id=d["class_id"],
                policy=d["policy"],
                generations=tuple(Generation.from_dict(g) for g in d["generations"]),
                created_at=d["created_at"],
                governance=d.get("governance"),
            )
        except (KeyError, TypeError) as exc:
            raise PolicyClassError(f"malformed policy-class record: {exc}") from exc
        if not record.generations:
            # A class always has at least one generation; a zero-generation row
            # is a corrupted store, surfaced in the package's own taxonomy
            # rather than as a downstream IndexError (attack finding MINOR-6e).
            raise PolicyClassError("policy-class record has no generations")
        if record.governance is not None:
            _validate_governance(record.governance)
            if record.policy != governance_policy(record.governance):
                raise PolicyClassError(
                    "policy-class policy does not match its governance commitment"
                )
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


def _validate_governance(governance: object) -> dict:
    if not isinstance(governance, dict) or set(governance) != {
        "v", "form", "anchor_id", "display_name",
    }:
        raise PolicyClassError("root governance has unknown or missing fields")
    if governance.get("v") != ROOT_GOVERNANCE_VERSION:
        raise PolicyClassError("unsupported root governance version")
    if governance.get("form") != ROOT_REACHABLE_FORM:
        raise PolicyClassError("unsupported policy-class governance form")
    for field in ("anchor_id", "display_name"):
        value = governance.get(field)
        if not isinstance(value, str) or not value or len(value) > 256:
            raise PolicyClassError(
                f"root governance {field} must be a short non-empty string"
            )
    return governance


def governance_policy(governance: dict) -> str:
    """The canonical cryptographic commitment carried in secured locators."""
    _validate_governance(governance)
    return "expr-sha256:" + hashlib.sha256(canonical_json(governance)).hexdigest()


def root_governance(anchor_id: str, display_name: str) -> dict:
    governance = {
        "v": ROOT_GOVERNANCE_VERSION,
        "form": ROOT_REACHABLE_FORM,
        "anchor_id": anchor_id,
        "display_name": display_name,
    }
    return _validate_governance(governance)


def is_root_reachable(record: PolicyClassRecord) -> bool:
    return bool(
        record.governance
        and record.governance.get("form") == ROOT_REACHABLE_FORM
    )


def _wrap_purpose(class_id: str, gen_id: str, policy: str, role: str) -> str:
    return f"{CLASS_WRAP_PURPOSE}|{class_id}|{gen_id}|{policy}|{role}"


def _class_seal_key_purpose(class_id: str, gen_id: str) -> str:
    digest = hashlib.sha256(canonical_json({
        "class_id": class_id,
        "gen_id": gen_id,
    })).hexdigest()
    return f"{CLASS_SEAL_KEY_PURPOSE}|{digest}"


def _cek_public_seal_purpose(
    record: "PolicyClassRecord", gen_id: str, genesis_id: str, setting_name: str
) -> str:
    digest = hashlib.sha256(canonical_json({
        "format": CEK_PUBLIC_SEAL_FORMAT,
        "genesis_id": genesis_id,
        "class_id": record.class_id,
        "gen_id": gen_id,
        "setting_name": setting_name,
        "policy": record.policy,
    })).hexdigest()
    return f"{CEK_PUBLIC_SEAL_PURPOSE}|{digest}"


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def _seal_wrap(key, factor, class_id, gen_id, policy, role) -> Wrap:
    wire = seal(key, factor.public_key, _wrap_purpose(class_id, gen_id, policy, role))
    return Wrap(factor.factor_id, factor.factor_type, role, factor.public_key, wire.hex())


def _open_wrap(wrap, seed, class_id, gen_id, policy) -> bytes:
    if wrap.factor_type in (PASSWORD, PASSKEY):
        priv = factor_private_from_seed(seed)
    else:
        priv = recipient_private_from_seed(seed, wrap.factor_type)
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
    """Seal *class_key* (or its shares) to *factors* for a new generation.

    A ``PERSONAL_ROOT_RECIPIENT`` among *factors* is a RECOVERY recipient
    (operator ruling 2026-08-27: root authority is always sufficient — a
    class scopes its day-to-day factors, it never locks out the root). It is
    sealed the full class key under single-key policies and BOTH shares under
    ``both`` (extension needs the shares), so a root opening can both read
    and enroll a replacement device with no member factor present.
    """
    _reject_shared_public_keys(factors)
    recovery = [f for f in factors if getattr(f, "factor_type", None) == PERSONAL_ROOT_RECIPIENT
                or getattr(f, "recipient_kind", None) == PERSONAL_ROOT_RECIPIENT]
    members = [f for f in factors if f not in recovery]
    recovery = [
        f if hasattr(f, "factor_type")
        else PublishedFactor(f.factor_id, f.recipient_kind, f.public_key)
        for f in recovery
    ]
    if policy in (PASSWORD_POLICY, PRF_POLICY):
        want = _SINGLE_TYPE[policy]
        wraps = []
        for f in members:
            if f.factor_type != want:
                raise PolicyClassError(
                    f"{policy} class admits only {want} factors, got {f.factor_type!r}"
                )
            wraps.append(_seal_wrap(class_key, f, class_id, gen_id, policy, ROLE_SINGLE))
        for r in recovery:
            wraps.append(_seal_wrap(class_key, r, class_id, gen_id, policy, ROLE_SINGLE))
    else:  # both
        pw = [f for f in members if f.factor_type == PASSWORD]
        pk = [f for f in members if f.factor_type == PASSKEY]
        if not pw or not pk:
            raise PolicyClassError(
                "a both class needs at least one password and one passkey factor"
            )
        share_a = secrets.token_bytes(_CLASS_KEY_LEN)
        share_b = _xor(class_key, share_a)
        wraps = [_seal_wrap(share_a, f, class_id, gen_id, policy, ROLE_A) for f in pw]
        wraps += [_seal_wrap(share_b, f, class_id, gen_id, policy, ROLE_B) for f in pk]
        for r in recovery:
            wraps.append(_seal_wrap(share_a, r, class_id, gen_id, policy, ROLE_A))
            wraps.append(_seal_wrap(share_b, r, class_id, gen_id, policy, ROLE_B))
    _, sealing_public_key = derive_encapsulation_keypair(
        class_key, _class_seal_key_purpose(class_id, gen_id)
    )
    return Generation(gen_id, tuple(wraps), sealing_public_key)


def _open_generation(record: PolicyClassRecord, gen: Generation, seeds) -> tuple[bytes, dict]:
    """Recover *gen*'s class_key. Returns ``(class_key, shares)`` where shares
    is ``{}`` for single-wrap policies and ``{"a","b"}`` for ``both``."""
    policy = record.policy
    if is_root_reachable(record):
        if (
            len(gen.wraps) != 1
            or gen.wraps[0].factor_type != PERSONAL_ROOT_RECIPIENT
        ):
            raise ClassOpenError("root-reachable class has no canonical anchor wrap")
        wrap = gen.wraps[0]
        seed = seeds.get(wrap.factor_id)
        if seed is None:
            raise ClassOpenError("the personal-root vault anchor was not opened")
        try:
            key = _open_wrap(wrap, seed, record.class_id, gen.gen_id, policy)
        except (SealingError, FactorError) as exc:
            raise ClassOpenError("the personal-root vault anchor did not open") from exc
        if len(key) != _CLASS_KEY_LEN:
            raise ClassOpenError("the personal-root vault anchor opened malformed material")
        return key, {}
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
    recovery: "PublishedRecipient | None" = None,
) -> PolicyClassRecord:
    """Mint a class: a first generation sealed to *factors*' public keys.

    Requires NO existing factor and opens nothing — it consumes only the
    factors' published public keys (crib §18). Returns the record; the
    class secret is not persisted or returned. Its derived public sealing key
    is persisted so :func:`seal_cek` can write without a factor.

    *recovery* (a personal-root anchor recipient) additionally seals every
    generation to the root: the default for member classes, so root authority
    always reads and always enrolls a replacement device (a class WITHOUT it
    is a deliberate enclave the root cannot recover).
    """
    _require_policy(policy)
    if not factors:
        raise PolicyClassError("a class needs at least one factor")
    class_id = class_id or secrets.token_hex(16)
    minted = list(factors) + ([recovery] if recovery is not None else [])
    gen = _mint_generation(policy, minted, class_id, secrets.token_hex(12), secrets.token_bytes(_CLASS_KEY_LEN))
    return PolicyClassRecord(class_id, policy, (gen,), created_at)


def create_root_reachable_class(
    anchor: PublishedRecipient,
    *,
    display_name: str,
    class_id: str | None = None,
    created_at: str,
) -> PolicyClassRecord:
    """Mint the default personal class behind one stable root anchor.

    The class record names no password or passkey.  The root armor owns that
    policy; this class has one public recipient reached after the root opens.
    """
    if anchor.recipient_kind != PERSONAL_ROOT_RECIPIENT:
        raise PolicyClassError("a root-reachable class requires a root anchor")
    governance = root_governance(anchor.factor_id, display_name)
    policy = governance_policy(governance)
    class_id = class_id or secrets.token_hex(16)
    gen_id = secrets.token_hex(12)
    class_key = secrets.token_bytes(_CLASS_KEY_LEN)
    wrap = _seal_wrap(
        class_key, anchor, class_id, gen_id, policy, ROLE_SINGLE,
    )
    _, sealing_public_key = derive_encapsulation_keypair(
        class_key, _class_seal_key_purpose(class_id, gen_id),
    )
    return PolicyClassRecord(
        class_id,
        policy,
        (Generation(gen_id, (wrap,), sealing_public_key),),
        created_at,
        governance,
    )


def enable_public_sealing(
    record: PolicyClassRecord, *, created_at: str
) -> PolicyClassRecord:
    """Make a legacy class public-sealable without opening it.

    Records created before public sealing have no ``sealing_public_key``.  A
    public key cannot be recovered from their factor wraps alone, so migration
    appends a fresh generation sealed to the current factors' PUBLIC keys. Old
    generations remain byte-identical and readable; new writes use the new
    generation. This operation consumes no opener material.
    """
    if record.current().sealing_public_key is not None:
        return record
    factors = [
        PublishedFactor(w.factor_id, w.factor_type, w.public_key)
        for w in record.current().wraps
    ]
    new_gen = _mint_generation(
        record.policy,
        factors,
        record.class_id,
        secrets.token_hex(12),
        secrets.token_bytes(_CLASS_KEY_LEN),
    )
    return replace(
        record, generations=record.generations + (new_gen,), created_at=created_at
    )


# ── open ─────────────────────────────────────────────────────────────────


def open_class(record: PolicyClassRecord, seeds: dict[str, bytes]) -> bytes:
    """Recover the CURRENT generation's class_key after the policy is satisfied
    by *seeds* (``factor_id`` → 32-byte seed). It derives the private sealing
    key used by reads; writes use only the published half. Raises
    :class:`ClassOpenError` if the policy is not satisfied."""
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
    if is_root_reachable(record):
        raise PolicyClassError(
            "a root-reachable class inherits factors from the personal root"
        )
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

    Ceremony-free by design: minting and wrapping the fresh class secret uses
    only surviving factors' published keys. ``created_at`` re-stamps the class.
    """
    if is_root_reachable(record):
        raise PolicyClassError(
            "root factors are changed on the personal armor, not on this class"
        )
    current = record.current()
    seen: set[tuple[str, str]] = set()
    survivors = []
    for w in current.wraps:
        if w.factor_id == factor_id:
            continue
        if (w.factor_id, w.public_key) in seen:
            continue   # a 'both' recovery anchor holds two role wraps — one factor
        seen.add((w.factor_id, w.public_key))
        survivors.append(
            PublishedRecipient(w.factor_id, w.factor_type, w.public_key)
            if w.factor_type == PERSONAL_ROOT_RECIPIENT
            else PublishedFactor(w.factor_id, w.factor_type, w.public_key)
        )
    if len(seen) == len({(w.factor_id, w.public_key) for w in current.wraps}):
        raise PolicyClassError(f"factor {factor_id!r} is not in the current generation")
    if not [s for s in survivors if s.factor_type != PERSONAL_ROOT_RECIPIENT]:
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
    cek: bytes,
    *,
    genesis_id: str,
    setting_name: str,
    required_policy: str,
) -> dict:
    """Seal a setting's *cek* under the class's CURRENT generation.

    Uses only the current generation's PUBLIC sealing key. The HPKE purpose
    binds ``genesis_id``, ``class_id``, ``gen_id``, ``setting_name`` and the
    class policy, so material sealed for one context does not verify in
    another. *required_policy* is the policy the
    naming setting demands; it must equal the class's policy, else
    :class:`PolicyMismatchError` — this stops a setting from being sealed under a
    class with a weaker factor set than it requires.
    """
    _check_policy_match(record, required_policy)
    if not isinstance(cek, (bytes, bytearray)) or not cek:
        raise PolicyClassError("cek must be non-empty bytes")
    gen = record.current()
    if gen.sealing_public_key is None:
        raise PolicyClassError(
            "current generation has no public sealing key; append a public-sealing generation"
        )
    wire = seal(
        bytes(cek),
        gen.sealing_public_key,
        _cek_public_seal_purpose(record, gen.gen_id, genesis_id, setting_name),
    )
    return {
        "format": CEK_PUBLIC_SEAL_FORMAT,
        "gen_id": gen.gen_id,
        "ciphertext": wire.hex(),
    }


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
    secret sealed before a revocation) with *seeds*, derives its private
    sealing key, and opens the HPKE record. Legacy AES-GCM-SIV CEK records
    remain readable. Fails closed on any mismatch of key, class, generation,
    setting, policy, format, or suite.
    """
    _check_policy_match(record, required_policy)
    if not isinstance(sealed_cek, dict):
        raise PolicyClassError("sealed_cek must be an object")
    seal_format = sealed_cek.get("format")
    if seal_format == CEK_PUBLIC_SEAL_FORMAT:
        try:
            gen_id = sealed_cek["gen_id"]
            wire = bytes.fromhex(sealed_cek["ciphertext"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PolicyClassError(f"malformed sealed_cek: {exc}") from exc
        gen = record.generation(gen_id)
        if gen.sealing_public_key is None:
            raise PolicyClassError("public-sealed CEK names a legacy generation")
        class_key, _ = _open_generation(record, gen, seeds)
        private_key, derived_public = derive_encapsulation_keypair(
            class_key, _class_seal_key_purpose(record.class_id, gen_id)
        )
        if not hmac.compare_digest(derived_public, gen.sealing_public_key):
            raise PolicyClassError("generation sealing public key does not match its factor-wrapped key")
        try:
            return seal_open(
                wire,
                private_key,
                _cek_public_seal_purpose(record, gen_id, genesis_id, setting_name),
            )
        except SealingError as exc:
            raise PolicyClassError(
                "sealed_cek does not open with that class key in this context"
            ) from exc
    if seal_format is not None:
        raise PolicyClassError(f"unsupported sealed_cek format {seal_format!r}")

    # Compatibility: records written before public sealing used AES-GCM-SIV
    # directly under the factor-wrapped symmetric class key.
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
    if record.governance is None:
        _require_policy(required_policy)
    elif required_policy != governance_policy(record.governance):
        raise PolicyMismatchError(
            "setting policy commitment does not match its class governance"
        )
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
