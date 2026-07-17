"""§4.1–4.3: registration, renewal, and the I3 rebind invariant."""

from __future__ import annotations

from tools.network.idkit import KeyPair, Subject, issue_cert

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
        # Names are not authority: a different key cannot take the UUID...
        other = KeyPair.generate()
        assert register(client, clock, other).status_code == 409
        # ...and re-claiming with the SAME key is still a collision.
        assert register(client, clock, root).status_code == 409

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
