"""Root authority is always sufficient: member classes carry a recovery wrap.

Operator ruling 2026-08-27: whatever it takes to get root, that unlocks
anything — a policy class scoped to passkeys (or one specific passkey) limits
which FACTORS open it day-to-day, it never locks out the root. This is what
makes device enrollment workable (the enrollment ceremony opens root and can
therefore extend any class with no other device present) and what makes a
lost phone an errand instead of data loss (the lost device's PRF was the only
member seed; the root anchor still opens the class to enroll the replacement).

Mechanism: every member class seals its generations to the root anchor as a
recovery recipient alongside the members — the full class key for single-key
policies, BOTH shares for 'both' (extension needs the shares). Revocation
generations carry the anchor forward.
"""

from __future__ import annotations

import pytest

from tools.vault.errors import ClassOpenError, PolicyClassError
from tools.vault.factors import PublishedFactor, random_seed, public_from_seed
from tools.vault.policy_class import (
    create_class,
    extend_class,
    open_class,
    revoke_factor,
)
from tools.vault.recipients import (
    PERSONAL_ROOT_RECIPIENT,
    PublishedRecipient,
    recipient_public_from_seed,
)

NOW = "2026-08-27T18:00:00Z"


def factor(fid: str, kind: str = "passkey"):
    seed = random_seed()
    return seed, PublishedFactor(fid, kind, public_from_seed(seed))


def anchor(aid: str = "anchor.root"):
    seed = random_seed()
    public = recipient_public_from_seed(seed, PERSONAL_ROOT_RECIPIENT)
    return seed, PublishedRecipient(aid, PERSONAL_ROOT_RECIPIENT, public)


def test_prf_class_opens_via_member_or_root_anchor():
    pk_seed, pk = factor("pk.iphone")
    anchor_seed, root = anchor()
    record = create_class("prf", [pk], created_at=NOW, recovery=root)
    key_member = open_class(record, {"pk.iphone": pk_seed})
    key_root = open_class(record, {"anchor.root": anchor_seed})
    assert key_member == key_root


def test_lost_phone_recovery_root_extends_with_no_member_present():
    # the ONLY member's device is gone; its seed no longer exists anywhere
    _, pk_lost = factor("pk.iphone.lost")
    anchor_seed, root = anchor()
    record = create_class("prf", [pk_lost], created_at=NOW, recovery=root)

    new_seed, pk_new = factor("pk.iphone.replacement")
    extended = extend_class(record, {"anchor.root": anchor_seed}, pk_new)
    assert open_class(extended, {"pk.iphone.replacement": new_seed}) == \
        open_class(extended, {"anchor.root": anchor_seed})


def test_both_class_root_recovery_opens_and_extends():
    pw_seed, pw = factor("pw.main", "password")
    pk_seed, pk = factor("pk.mac")
    anchor_seed, root = anchor()
    record = create_class("both", [pw, pk], created_at=NOW, recovery=root)
    # both-policy day path needs both members…
    key = open_class(record, {"pw.main": pw_seed, "pk.mac": pk_seed})
    # …and the root anchor alone recovers the same key
    assert open_class(record, {"anchor.root": anchor_seed}) == key
    # extension via root alone works for a passkey (needs the b-share)
    new_seed, pk_new = factor("pk.iphone")
    extended = extend_class(record, {"anchor.root": anchor_seed}, pk_new)
    assert open_class(extended, {"pw.main": pw_seed, "pk.iphone": new_seed}) == key


def test_revocation_generations_carry_the_recovery_anchor_forward():
    pk_seed_a, pk_a = factor("pk.a")
    _, pk_b = factor("pk.b")
    anchor_seed, root = anchor()
    record = create_class("prf", [pk_a, pk_b], created_at=NOW, recovery=root)
    revoked = revoke_factor(record, "pk.b", created_at=NOW)
    # the NEW generation (and the old) both open via the anchor
    assert open_class(revoked, {"anchor.root": anchor_seed})
    # …and the anchor never counts as the last member: revoking the final
    # member factor is still refused even though the anchor wrap remains
    with pytest.raises(PolicyClassError):
        revoke_factor(revoked, "pk.a", created_at=NOW)


def test_without_recovery_root_stays_locked_out():
    # the opt-out shape (no recovery recipient) keeps today's enclave behavior
    pk_seed, pk = factor("pk.only")
    anchor_seed, root = anchor()
    record = create_class("prf", [pk], created_at=NOW)
    with pytest.raises(ClassOpenError):
        open_class(record, {"anchor.root": anchor_seed})
