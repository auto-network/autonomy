"""Rotating a PERSONAL root identity — the elegant wipe (US-8).

You cannot un-copy a file. If someone took your armor and knows the password,
resetting that password does not reach their copy, and no amount of scrubbing
disks will. The answer is not to chase the bytes but to make them worthless:
declare a NEW root, and record that succession so that everything downstream
treats the old key as no longer you. The stolen copy still opens -- and opens
onto an identity that no longer holds anything.

Authority to do that is deliberately split. A rotation needs the OLD root
(you can open your armor) AND the recovery key (you hold the printed code).
Neither alone is enough, which cuts both ways:

  * A thief with your armor and password cannot rotate you out of your own
    identity, because they do not have the printed code.
  * Someone who found only the printed code cannot rotate either, because
    they cannot produce the old root without the armor.

The NEW root signs too, proving it exists and is controlled -- a succession
may never point at a key its author does not hold.

The org-ledger equivalent (``key.rotate``) has an org genesis to bind against.
A personal root has no ledger, so the binding is the ORIGIN identity plus a
sequence number: a co-signature minted for one step of one person's lineage
authorises no other step and nobody else's.
"""

from __future__ import annotations

from .canonical import canonical_json
from .errors import MalformedError, SignatureError
from .keys import KeyPair, verify_signature

#: Distinct domains: the authorising signature and the recovery co-signature
#: can never substitute for each other, nor for any ledger signature.
PERSONAL_ROTATE_DOMAIN = b"autonomy.idkit.personal-root-rotate.v1\n"
PERSONAL_ROTATE_RECOVERY_DOMAIN = b"autonomy.idkit.personal-root-rotate-recovery.v1\n"
PERSONAL_ROTATE_CONTINUITY_DOMAIN = (
    b"autonomy.idkit.personal-root-rotate-continuity.v1\n"
)
ROTATION_VERSION = 1


def _binding(origin_pub: str, seq: int, old_pub: str, new_pub: str) -> bytes:
    return canonical_json(
        {
            "origin_pub": origin_pub,
            "seq": seq,
            "old_pub": old_pub,
            "new_pub": new_pub,
        }
    )


def rotation_input(origin_pub: str, seq: int, old_pub: str, new_pub: str) -> bytes:
    """What the OLD root signs to authorise the succession."""
    return PERSONAL_ROTATE_DOMAIN + _binding(origin_pub, seq, old_pub, new_pub)


def rotation_recovery_input(
    origin_pub: str, seq: int, old_pub: str, new_pub: str
) -> bytes:
    """What the RECOVERY key co-signs. Distinct domain, same binding."""
    return PERSONAL_ROTATE_RECOVERY_DOMAIN + _binding(origin_pub, seq, old_pub, new_pub)


def rotation_continuity_input(
    origin_pub: str, seq: int, old_pub: str, new_pub: str
) -> bytes:
    """What the NEW root signs, proving it is held by whoever wrote this."""
    return PERSONAL_ROTATE_CONTINUITY_DOMAIN + _binding(
        origin_pub, seq, old_pub, new_pub
    )


def make_rotation(
    *,
    origin_pub: str,
    seq: int,
    old_root: KeyPair,
    new_root: KeyPair,
    recovery_root: KeyPair,
    rotated_at: int,
) -> dict:
    """Build one signed succession record. All three signatures are required."""
    if not isinstance(seq, int) or seq < 1:
        raise MalformedError("rotation seq must be an integer >= 1")
    if not isinstance(rotated_at, int) or rotated_at < 0:
        raise MalformedError("rotated_at must be a non-negative timestamp")
    if new_root.public_hex == old_root.public_hex:
        raise MalformedError("a rotation must move to a different key")
    args = (origin_pub, seq, old_root.public_hex, new_root.public_hex)
    return {
        "v": ROTATION_VERSION,
        "origin_pub": origin_pub,
        "seq": seq,
        "old_pub": old_root.public_hex,
        "new_pub": new_root.public_hex,
        "rotated_at": rotated_at,
        "old_sig": old_root.sign_hex(rotation_input(*args)),
        "new_sig": new_root.sign_hex(rotation_continuity_input(*args)),
        "recovery_sig": recovery_root.sign_hex(rotation_recovery_input(*args)),
    }


_FIELDS = {
    "v", "origin_pub", "seq", "old_pub", "new_pub", "rotated_at",
    "old_sig", "new_sig", "recovery_sig",
}


def verify_rotation(record: dict, *, recovery_pub: str) -> None:
    """Check one succession record. Raises; returns None when sound."""
    if not isinstance(record, dict) or set(record) != _FIELDS:
        raise MalformedError(
            f"rotation record must carry exactly {sorted(_FIELDS)}"
        )
    if record["v"] != ROTATION_VERSION:
        raise MalformedError(f"unsupported rotation version: {record['v']!r}")
    if type(record["seq"]) is not int or record["seq"] < 1:
        raise MalformedError("rotation seq must be an integer >= 1")
    if record["old_pub"] == record["new_pub"]:
        raise MalformedError("a rotation must move to a different key")
    args = (
        record["origin_pub"], record["seq"], record["old_pub"], record["new_pub"],
    )
    verify_signature(record["old_pub"], record["old_sig"], rotation_input(*args))
    verify_signature(
        record["new_pub"], record["new_sig"], rotation_continuity_input(*args)
    )
    verify_signature(
        recovery_pub, record["recovery_sig"], rotation_recovery_input(*args)
    )


def resolve_current_root(
    origin_pub: str, records: list, *, recovery_pub: str
) -> str:
    """Walk a lineage and return the identity that is current.

    An empty history means the origin is still current. Otherwise every step
    must verify, start where the previous one ended, and advance the sequence
    by exactly one -- so a step cannot be dropped, reordered, or replayed, and
    a fork cannot quietly become the trunk.
    """
    if not isinstance(records, list):
        raise MalformedError("rotation history must be a list")
    current = origin_pub
    for index, record in enumerate(records):
        verify_rotation(record, recovery_pub=recovery_pub)
        if record["origin_pub"] != origin_pub:
            raise SignatureError(
                "rotation record belongs to a different identity's lineage"
            )
        if record["seq"] != index + 1:
            raise SignatureError(
                f"rotation history is out of order at position {index}: "
                f"expected seq {index + 1}, got {record['seq']}"
            )
        if record["old_pub"] != current:
            raise SignatureError(
                f"rotation at seq {record['seq']} does not follow the "
                "identity it claims to replace"
            )
        current = record["new_pub"]
    return current
