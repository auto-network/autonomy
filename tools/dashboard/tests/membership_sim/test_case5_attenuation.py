"""Case 5 (auto-lvs3f): attenuation and loss-head assertions.

These are fold predicates — evaluated when an event folds, not over the
wire — so this case is deliberately ledger-only: a tunnel step would add no
coverage. Pinned so the roles design cannot regress them:

* ``role.define`` by a non-root author needs held ``role:define`` (else
  ``role-define-unauthorized``) AND ``attenuates(scope_set, deleg[author])``
  (else ``role-define-overreach``). Role-held scopes live in ``held`` only,
  never ``deleg``, so a persona whose only authority comes from a role can
  define an EMPTY role (attenuation is vacuously true) but not a scoped one.
* ``role.grant`` needs ``role:grant:<role>``; an invite needs
  ``invite:<role>``.
* A redefinition that narrows an existing role is valid (root-signed) but
  becomes a LOSS HEAD; widening or covering redefinitions do not. Root
  widening is root's prerogative (ruling J4, graph://d1b3db8f-879): it
  widens every current holder, with no fold change.
"""

from __future__ import annotations

from tools.network.idkit import KeyPair
from tools.network.ledger.fold import (
    R_INVITE_OVERREACH, R_ROLE_DEFINE_OVERREACH, R_ROLE_DEFINE_UNAUTHORIZED,
    R_ROLE_GRANT_UNAUTHORIZED,
)

from ._harness import Org, RoleSpec, Step, assert_holds, assert_verdict, drive_actions


def test_role_define_grant_and_invite_attenuation():
    org = Org.found(roles={
        "admin": RoleSpec(scope_set=("role:define", "role:grant:member", "invite:member")),
        "member": RoleSpec(scope_set=("invite:member",)),
        "guest": RoleSpec(scope_set=()),
    })
    admin = org.admit("admin", "admin")
    member = org.admit("member", "member")
    guest = org.admit("guest", "guest")

    ids = drive_actions(org, [
        # c1: role-held role:define, no delegable closure — a SCOPED role
        # overreaches, an EMPTY one is vacuously attenuated and valid.
        Step("admin_defines_scoped",
             lambda o: o.define_role("x", RoleSpec(("invite:member",)), author=admin)),
        Step("admin_defines_empty",
             lambda o: o.define_role("empty", RoleSpec(()), author=admin)),
        # c3: no role:define at all — refused before attenuation is consulted.
        Step("member_defines",
             lambda o: o.define_role("y", RoleSpec(("invite:member",)), author=member)),
        # c4: role.grant by a plain member is unauthorized; by a
        # role:grant:member holder it is valid.
        Step("member_grants",
             lambda o: o.grant(guest, "member", author=member)),
        Step("admin_grants",
             lambda o: o.grant(guest, "member", author=admin)),
        # c5: an invite for admin by a persona holding only invite:member.
        Step("member_invites_admin",
             lambda o: o.invite("admin", sponsor=member, invite_key=KeyPair.generate())),
        # c2: root delegates a redelegable closure to the admin persona;
        # a role inside it is valid, one outside it overreaches.
        Step("delegate",
             lambda o: o.delegate(admin, ("role:define", "role:grant:*", "invite:*"),
                                  redelegate=True)),
        Step("admin_defines_within",
             lambda o: o.define_role("z", RoleSpec(("invite:member",)), author=admin)),
        Step("admin_defines_outside",
             lambda o: o.define_role("w", RoleSpec(("link:publish",)), author=admin)),
    ])
    state = org.fold()
    assert_verdict(state, ids["admin_defines_scoped"], admitted=False, reason=R_ROLE_DEFINE_OVERREACH)
    assert_verdict(state, ids["admin_defines_empty"], admitted=True)
    assert_verdict(state, ids["member_defines"], admitted=False, reason=R_ROLE_DEFINE_UNAUTHORIZED)
    assert_verdict(state, ids["member_grants"], admitted=False, reason=R_ROLE_GRANT_UNAUTHORIZED)
    assert_verdict(state, ids["admin_grants"], admitted=True)
    assert "member" in state.roles(guest.public_hex)
    assert_verdict(state, ids["member_invites_admin"], admitted=False, reason=R_INVITE_OVERREACH)
    assert_verdict(state, ids["delegate"], admitted=True)
    assert_verdict(state, ids["admin_defines_within"], admitted=True)
    assert_verdict(state, ids["admin_defines_outside"], admitted=False, reason=R_ROLE_DEFINE_OVERREACH)


def test_narrowing_redefinition_is_a_loss_head_widening_is_not():
    org = Org.found(roles={"member": RoleSpec(scope_set=("invite:member",))})
    member = org.admit("member", "member")

    # v2 COVERS v1 (invite:* ⊇ invite:member): valid, not a loss head, and —
    # J4 — every current holder is widened with no further event.
    v2 = org.define_role("member", RoleSpec(("invite:*",)), version=2)
    state = org.fold()
    assert_verdict(state, v2, admitted=True)
    assert v2 not in state.loss_heads
    assert_holds(state, member, "invite:admin")

    # v3 NARROWS to nothing: valid (root-signed) but a loss head, because a
    # lower-ranked same-name definition is not attenuated by it.
    v3 = org.define_role("member", RoleSpec(()), version=3)
    state = org.fold()
    assert_verdict(state, v3, admitted=True)
    assert v3 in state.loss_heads
    assert_holds(state, member, "invite:member", expected=False)

    # v4 RE-WIDENS to cover EVERY prior version (invite:* ⊇ v1/v2/v3): valid
    # and NOT a loss head — loss-head status compares against all lower-ranked
    # same-name definitions, not just the immediate predecessor, so a plain
    # invite:member here would still lose v2's invite:* and be a loss head.
    v4 = org.define_role("member", RoleSpec(("invite:*",)), version=4)
    state = org.fold()
    assert_verdict(state, v4, admitted=True)
    assert v4 not in state.loss_heads
    assert_holds(state, member, "invite:admin")
