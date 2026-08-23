"""§4.1–4.3: registration, renewal, and the I3 rebind invariant."""

from __future__ import annotations

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.registry.signing import sign_recovery_succession

from .conftest import DAY, HOUR, NOW, ORG, ORG_NONE, SESSION_SCOPE, register, signed


# -- §4.1 register ------------------------------------------------------------

class TestRegister:
    def test_register_binds_first_key(self, client, clock, root):
        response = register(client, clock, root)
        assert response.status_code == 201
        body = response.json()
        assert body["org_uuid"] == ORG
        assert body["root_pub"] == root.public_hex
        assert body["expires_at"] == NOW + 30 * DAY  # default binding TTL

    def test_uuid_collision_409(self, client, clock, root):
        register(client, clock, root)
        # Names are not authority: a different key cannot take a live UUID.
        other = KeyPair.generate()
        assert register(client, clock, other).status_code == 409

    def test_same_root_reregistration_is_idempotent(self, client, clock, root):
        # Re-registering a live binding with the SAME root returns the UUID
        # (idempotent), not a 409 — the caller proved control of exactly this
        # key, so this is how a personal identity recovers its own org_uuid.
        first = register(client, clock, root)
        assert first.status_code == 201
        clock.advance(HOUR)
        again = register(client, clock, root)
        assert again.status_code == 201, again.json()
        assert again.json()["org_uuid"] == first.json()["org_uuid"]
        assert again.json()["root_pub"] == root.public_hex
        # ...and the re-registration refreshed liveness (stronger auth than a
        # renew heartbeat), so the binding's expiry advanced with the clock.
        assert again.json()["expires_at"] > first.json()["expires_at"]

    def test_same_uuid_different_root_still_conflicts(self, client, clock, root):
        # The idempotency above is scoped to the OWNING key: a different key
        # aiming at the same live UUID is still refused.
        register(client, clock, root)
        other = KeyPair.generate()
        assert register(client, clock, other).status_code == 409

    def test_expired_binding_is_reclaimable(self, client, clock, root):
        register(client, clock, root, ttl=HOUR)
        clock.advance(HOUR + 1)
        other = KeyPair.generate()
        response = register(client, clock, other)
        assert response.status_code == 201
        assert response.json()["root_pub"] == other.public_hex

    def test_registration_must_be_signed_by_bound_root(self, client, clock, root):
        imposter = KeyPair.generate()
        payload = {"org_uuid": ORG, "root_pub": root.public_hex, "recovery_policy": "none"}
        response = signed(client, "POST", "/v1/orgs", imposter, payload, clock)
        assert response.status_code == 403

    def test_recovery_key_policy_requires_recovery_pub(self, client, clock, root):
        response = register(client, clock, root, policy="recovery-key")
        assert response.status_code == 400

    def test_unknown_policy_rejected(self, client, clock, root):
        response = register(client, clock, root, policy="org-vouch")
        assert response.status_code == 400

    def test_non_uuid_org_rejected(self, client, clock, root):
        response = register(client, clock, root, org_uuid="not-a-uuid")
        assert response.status_code == 400


# -- §4.2 renew ---------------------------------------------------------------

class TestRenew:
    def test_root_direct_renew(self, client, clock, root, bound_org):
        clock.advance(20 * DAY)
        response = signed(client, "POST", f"/v1/orgs/{ORG}/renew", root, {}, clock)
        assert response.status_code == 200
        assert response.json()["expires_at"] == clock.now + 30 * DAY

    def test_chain_signed_renew(self, client, clock, root, bound_org, session_key, session_cert):
        response = signed(client, "POST", f"/v1/orgs/{ORG}/renew", session_key, {}, clock,
                          cert=session_cert)
        assert response.status_code == 200

    def test_unbound_key_cannot_renew(self, client, clock, bound_org):
        stranger = KeyPair.generate()
        response = signed(client, "POST", f"/v1/orgs/{ORG}/renew", stranger, {}, clock)
        assert response.status_code == 403

    def test_renew_expired_binding_410(self, client, clock, root):
        register(client, clock, root, ttl=HOUR)
        clock.advance(HOUR + 1)
        response = signed(client, "POST", f"/v1/orgs/{ORG}/renew", root, {}, clock)
        assert response.status_code == 410

    def test_renew_unknown_org_404(self, client, clock, root):
        response = signed(
            client, "POST",
            "/v1/orgs/99999999-9999-4999-8999-999999999999/renew", root, {}, clock,
        )
        assert response.status_code == 404


# -- §4.3 rebind (I3: no path outside declared policy) --------------------------

class TestRebindI3:
    def test_policy_none_rejects_valid_root_signed(self, client, clock, bound_org_none):
        """I3 hard negative: under policy=none even the org's OWN root key,
        with a perfectly valid signature, cannot rebind."""
        org_root = bound_org_none
        new_root = KeyPair.generate()
        response = signed(
            client, "POST", f"/v1/orgs/{ORG_NONE}/rebind", org_root,
            {"new_root_pub": new_root.public_hex}, clock,
        )
        assert response.status_code == 403

    def test_policy_none_rejects_any_payload(self, client, clock, bound_org_none):
        """Structurally impossible: garbage, empty, and unsigned payloads all
        die on the same closed door — no parse path leads to a rebind."""
        for body in ({}, {"anything": "at-all"}, {"payload": {}, "sig": "00"}):
            response = client.post(f"/v1/orgs/{ORG_NONE}/rebind", json=body)
            assert response.status_code == 403

    def test_recovery_key_signed_rebind_accepted(self, client, clock, root, recovery, bound_org):
        new_root = KeyPair.generate()
        response = signed(
            client, "POST", f"/v1/orgs/{ORG}/rebind", recovery,
            {"new_root_pub": new_root.public_hex}, clock,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["root_pub"] == new_root.public_hex
        assert body["previous_root_pub"] == root.public_hex

        # Authority actually moved: the NEW root renews; the OLD root is out.
        assert signed(client, "POST", f"/v1/orgs/{ORG}/renew", new_root, {}, clock
                      ).status_code == 200
        assert signed(client, "POST", f"/v1/orgs/{ORG}/renew", root, {}, clock
                      ).status_code == 403

    def test_root_cannot_sign_rebind(self, client, clock, root, bound_org):
        """A stolen root must not be able to rotate away the recovery path."""
        new_root = KeyPair.generate()
        response = signed(
            client, "POST", f"/v1/orgs/{ORG}/rebind", root,
            {"new_root_pub": new_root.public_hex}, clock,
        )
        assert response.status_code == 403

    def test_random_key_cannot_sign_rebind(self, client, clock, bound_org):
        stranger = KeyPair.generate()
        response = signed(
            client, "POST", f"/v1/orgs/{ORG}/rebind", stranger,
            {"new_root_pub": KeyPair.generate().public_hex}, clock,
        )
        assert response.status_code == 403

    def test_delegated_cert_cannot_rebind(self, client, clock, root, recovery, bound_org):
        """Even a cert chain SIGNED BY THE RECOVERY KEY does not rebind —
        only the cold key's direct signature does."""
        delegate = KeyPair.generate()
        cert = issue_cert(
            recovery, delegate.public_hex, scope=SESSION_SCOPE, org=ORG,
            subject=Subject("operator", "op-1"),
            not_before=NOW - 100, not_after=NOW + DAY,
        )
        response = signed(
            client, "POST", f"/v1/orgs/{ORG}/rebind", delegate,
            {"new_root_pub": KeyPair.generate().public_hex}, clock, cert=cert,
        )
        assert response.status_code == 403

    def test_rebind_can_rotate_recovery_key(self, client, clock, root, recovery, bound_org):
        new_root, new_recovery = KeyPair.generate(), KeyPair.generate()
        response = signed(
            client, "POST", f"/v1/orgs/{ORG}/rebind", recovery,
            {"new_root_pub": new_root.public_hex,
             "recovery_policy": "recovery-key",
             "recovery_pub": new_recovery.public_hex}, clock,
        )
        assert response.status_code == 200
        # Old recovery key is dead; the new one can rebind again.
        assert signed(client, "POST", f"/v1/orgs/{ORG}/rebind", recovery,
                      {"new_root_pub": KeyPair.generate().public_hex}, clock
                      ).status_code == 403
        assert signed(client, "POST", f"/v1/orgs/{ORG}/rebind", new_recovery,
                      {"new_root_pub": KeyPair.generate().public_hex}, clock
                      ).status_code == 200

    def test_rebind_can_burn_recovery_path(self, client, clock, root, recovery, bound_org):
        """Rebinding down to policy=none closes the door permanently (I3)."""
        new_root = KeyPair.generate()
        response = signed(
            client, "POST", f"/v1/orgs/{ORG}/rebind", recovery,
            {"new_root_pub": new_root.public_hex, "recovery_policy": "none"}, clock,
        )
        assert response.status_code == 200
        assert signed(client, "POST", f"/v1/orgs/{ORG}/rebind", recovery,
                      {"new_root_pub": KeyPair.generate().public_hex}, clock
                      ).status_code == 403


# -- recovery-policy update (sovereign, root-signed) --------------------------

class TestPolicyUpdate:
    """The root freely ADDS recovery to a 'none' org — proof of key control IS
    the authority for that sovereign transition. Changing or REMOVING an
    existing recovery key is NOT sovereign (see TestRecoverySuccessionGate):
    it requires the outgoing recovery key's co-signature. Monotonic
    policy_epoch blocks replay/downgrade throughout."""

    def _policy(self, client, clock, key, epoch, policy,
                recovery_pub=None, org=ORG, expect=None, cert=None,
                succession_sig=None):
        payload = {"recovery_policy": policy, "policy_epoch": epoch}
        if recovery_pub is not None:
            payload["recovery_pub"] = recovery_pub
        if succession_sig is not None:
            payload["recovery_succession_sig"] = succession_sig
        return signed(client, "POST", f"/v1/orgs/{org}/policy", key, payload,
                      clock, cert=cert, expect=expect)

    def test_root_adds_recovery_to_a_none_org_reversing_none(self, client, clock, root, recovery):
        # The whole point: 'none' registered inline is REVERSIBLE by the root.
        register(client, clock, root, policy="none")                     # epoch 0
        r = self._policy(client, clock, root, 1, "recovery-key", recovery.public_hex)
        assert r.status_code == 200, r.json()
        assert r.json()["recovery_policy"] == "recovery-key"
        assert r.json()["policy_epoch"] == 1
        # ...and rebind (recovery-key path) is now available — 'none' was reversed.
        assert signed(client, "POST", f"/v1/orgs/{ORG}/rebind", recovery,
                      {"new_root_pub": KeyPair.generate().public_hex}, clock
                      ).status_code == 200

    def test_root_removes_recovery_with_outgoing_cosignature(self, client, clock, root, recovery):
        # Removal is a subset-authorized transition, not sovereign: it takes
        # the OUTGOING recovery key's co-signature (the root alone must not be
        # able to strip recovery). With it, removal lands.
        register(client, clock, root, policy="recovery-key",
                 recovery_pub=recovery.public_hex)                        # epoch 0
        cosig = sign_recovery_succession(recovery, ORG, "none", None, 1)
        assert self._policy(client, clock, root, 1, "none",
                            succession_sig=cosig).status_code == 200
        # After removal, the recovery-key rebind path is structurally gone.
        assert signed(client, "POST", f"/v1/orgs/{ORG}/rebind", recovery,
                      {"new_root_pub": KeyPair.generate().public_hex}, clock
                      ).status_code == 403

    def test_only_the_bound_root_can_update_policy(self, client, clock, root, recovery):
        register(client, clock, root, policy="none")
        imposter = KeyPair.generate()
        self._policy(client, clock, imposter, 1, "recovery-key",
                     recovery.public_hex, expect=403)

    def test_recovery_key_cannot_update_policy(self, client, clock, root, recovery):
        # policy update is root-signed; the recovery key is for rebind only.
        register(client, clock, root, policy="recovery-key",
                 recovery_pub=recovery.public_hex)
        self._policy(client, clock, recovery, 1, "none", expect=403)

    def test_delegated_cert_cannot_update_policy(self, client, clock, root, recovery):
        register(client, clock, root, policy="none")
        delegate = KeyPair.generate()
        cert = issue_cert(
            root, delegate.public_hex, scope=SESSION_SCOPE, org=ORG,
            subject=Subject("operator", "op-1"),
            not_before=NOW - 100, not_after=NOW + DAY,
        )
        self._policy(client, clock, delegate, 1, "recovery-key",
                     recovery.public_hex, cert=cert, expect=403)

    def test_stale_epoch_is_rejected_no_replay(self, client, clock, root, recovery):
        register(client, clock, root, policy="none")                     # epoch 0
        self._policy(client, clock, root, 1, "recovery-key",
                     recovery.public_hex, expect=200)                     # -> epoch 1
        # Replaying an old epoch (a captured 'set none' downgrade) is refused.
        self._policy(client, clock, root, 1, "none", expect=409)
        self._policy(client, clock, root, 0, "none", expect=409)

    def test_epoch_must_be_exactly_plus_one(self, client, clock, root, recovery):
        register(client, clock, root, policy="none")                     # epoch 0
        self._policy(client, clock, root, 5, "recovery-key",
                     recovery.public_hex, expect=409)                     # gap rejected

    def test_recovery_key_policy_requires_recovery_pub(self, client, clock, root):
        register(client, clock, root, policy="none")
        self._policy(client, clock, root, 1, "recovery-key", expect=400)

    def test_policy_update_on_expired_binding_410(self, client, clock, root, recovery):
        register(client, clock, root, policy="none", ttl=HOUR)
        clock.advance(HOUR + 1)
        self._policy(client, clock, root, 1, "recovery-key",
                     recovery.public_hex, expect=410)

    def test_policy_update_unknown_org_404(self, client, clock, root):
        self._policy(client, clock, root, 1, "none",
                     org="44444444-4444-4444-8444-444444444444", expect=404)


class TestRecoverySuccessionGate:
    """Changing or removing an EXISTING recovery key is subset-authorized, not
    sovereign: a stolen root alone must not be able to swap the recovery factor
    and then sign a rebind, hijacking the org's registry-bound root (the
    relay-visible pin new joiners trust). Such a transition takes the OUTGOING
    recovery key's co-signature, domain-separated and bound to the exact
    epoch/successor. ADD (none -> recovery-key) stays sovereign. Until the
    announced-windowed-vetoable path exists, a co-signature-less succession is
    REFUSED, not deferred.
    """

    def _policy(self, client, clock, key, epoch, policy,
                recovery_pub=None, succession_sig=None, expect=None):
        payload = {"recovery_policy": policy, "policy_epoch": epoch}
        if recovery_pub is not None:
            payload["recovery_pub"] = recovery_pub
        if succession_sig is not None:
            payload["recovery_succession_sig"] = succession_sig
        return signed(client, "POST", f"/v1/orgs/{ORG}/policy", key, payload,
                      clock, expect=expect)

    def _rebind(self, client, clock, key):
        return signed(client, "POST", f"/v1/orgs/{ORG}/rebind", key,
                      {"new_root_pub": KeyPair.generate().public_hex}, clock)

    def test_succession_without_cosignature_refused(self, client, clock, root, recovery, bound_org):
        # Stolen-root scenario: the root tries to install a new recovery key
        # (the accomplice it would then rebind through) with a bare root sig.
        new_recovery = KeyPair.generate()
        self._policy(client, clock, root, 1, "recovery-key",
                     new_recovery.public_hex, expect=403)
        # The swap did NOT take: the ORIGINAL recovery key still owns rebind,
        # the accomplice does not.
        assert self._rebind(client, clock, new_recovery).status_code == 403
        assert self._rebind(client, clock, recovery).status_code == 200

    def test_succession_with_outgoing_cosignature_lands(self, client, clock, root, recovery, bound_org):
        new_recovery = KeyPair.generate()
        cosig = sign_recovery_succession(
            recovery, ORG, "recovery-key", new_recovery.public_hex, 1)
        r = self._policy(client, clock, root, 1, "recovery-key",
                         new_recovery.public_hex, succession_sig=cosig)
        assert r.status_code == 200, r.json()
        assert r.json()["recovery_pub"] == new_recovery.public_hex
        # Authority moved: the NEW key rebinds, the OLD key no longer can.
        assert self._rebind(client, clock, recovery).status_code == 403
        assert self._rebind(client, clock, new_recovery).status_code == 200

    def test_removal_without_cosignature_refused(self, client, clock, root, recovery, bound_org):
        self._policy(client, clock, root, 1, "none", expect=403)
        # Recovery still stands: the rebind path was not silently stripped.
        assert self._rebind(client, clock, recovery).status_code == 200

    def test_cosignature_by_wrong_key_refused(self, client, clock, root, recovery, bound_org):
        # A co-signature by the ROOT (the self-defeat the whole gate exists to
        # block) or any other key is not the outgoing recovery key's.
        new_recovery = KeyPair.generate()
        for wrong in (root, KeyPair.generate()):
            cosig = sign_recovery_succession(
                wrong, ORG, "recovery-key", new_recovery.public_hex, 1)
            self._policy(client, clock, root, 1, "recovery-key",
                         new_recovery.public_hex, succession_sig=cosig, expect=403)

    def test_cosignature_bound_to_wrong_epoch_refused(self, client, clock, root, recovery, bound_org):
        # Epoch-binding: a co-signature the outgoing key made for a DIFFERENT
        # epoch cannot authorize the transition into epoch 1 -- so a co-sig
        # captured/pre-minted for one succession can't be replayed at another.
        new_recovery = KeyPair.generate()
        cosig_for_epoch_2 = sign_recovery_succession(
            recovery, ORG, "recovery-key", new_recovery.public_hex, 2)
        self._policy(client, clock, root, 1, "recovery-key",
                     new_recovery.public_hex, succession_sig=cosig_for_epoch_2,
                     expect=403)

    def test_cosignature_for_different_successor_refused(self, client, clock, root, recovery, bound_org):
        # The co-signature names the successor it authorizes; it cannot be
        # lifted onto a substitution to a DIFFERENT key.
        approved = KeyPair.generate()
        substituted = KeyPair.generate()
        cosig = sign_recovery_succession(
            recovery, ORG, "recovery-key", approved.public_hex, 1)
        self._policy(client, clock, root, 1, "recovery-key",
                     substituted.public_hex, succession_sig=cosig, expect=403)

    def test_add_rejects_stray_succession_sig(self, client, clock, root, recovery):
        # ADD (none -> recovery-key) is sovereign; a succession co-signature
        # authorizes nothing here and is rejected rather than accepted-and-
        # ignored (no unverifiable bytes ride along).
        register(client, clock, root, policy="none")                     # epoch 0
        stray = sign_recovery_succession(recovery, ORG, "recovery-key",
                                         recovery.public_hex, 1)
        self._policy(client, clock, root, 1, "recovery-key",
                     recovery.public_hex, succession_sig=stray, expect=400)

    def test_add_stays_sovereign_no_cosignature(self, client, clock, root, recovery):
        register(client, clock, root, policy="none")                     # epoch 0
        r = self._policy(client, clock, root, 1, "recovery-key", recovery.public_hex)
        assert r.status_code == 200, r.json()


class TestRecoveryFactorDistinctFromRoot:
    """The recovery factor must be a key the root does not control -- enforced
    on the AUTHORITATIVE server on every write path. A stolen root signing both
    its rotation and its "recovery" co-signature is the self-defeat this rejects;
    the browser mirror is only a UX nicety, so these curl the endpoints directly.
    """

    def test_register_rejects_recovery_pub_equal_root(self, client, clock, root):
        response = register(client, clock, root, policy="recovery-key",
                            recovery_pub=root.public_hex)
        assert response.status_code == 400, response.json()
        assert "differ from root_pub" in response.json()["detail"]

    def test_rebind_rejects_recovery_pub_equal_new_root(self, client, clock, recovery, bound_org):
        new_root = KeyPair.generate()
        response = signed(
            client, "POST", f"/v1/orgs/{ORG}/rebind", recovery,
            {"new_root_pub": new_root.public_hex, "recovery_policy": "recovery-key",
             "recovery_pub": new_root.public_hex}, clock,
        )
        assert response.status_code == 400, response.json()

    def test_rebind_onto_recovery_key_kept_as_recovery_rejected(self, client, clock, recovery, bound_org):
        # Rebinding the root ONTO the recovery key while carrying the policy
        # forward would make the new root its own recovery factor -- refused,
        # even though _parse_recovery_policy is not re-run on that path.
        response = signed(
            client, "POST", f"/v1/orgs/{ORG}/rebind", recovery,
            {"new_root_pub": recovery.public_hex}, clock,
        )
        assert response.status_code == 400, response.json()

    def test_policy_rejects_recovery_pub_equal_bound_root(self, client, clock, root, bound_org):
        response = signed(
            client, "POST", f"/v1/orgs/{ORG}/policy", root,
            {"recovery_policy": "recovery-key", "recovery_pub": root.public_hex,
             "policy_epoch": 1}, clock,
        )
        assert response.status_code == 400, response.json()
