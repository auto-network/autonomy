"""§4.4/§4.6: link grants, envelope resolution, I2 tokens, I4 chain gate."""

from __future__ import annotations

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.registry.signing import sign_request

from .conftest import DAY, HOUR, NOW, ORG, TARGET, publish_link, signed


# -- I2: tokens ----------------------------------------------------------------

class TestTokensI2:
    def test_token_is_32_hex(self, client, clock, bound_org, session_key, session_cert):
        response = publish_link(client, clock, session_key, cert=session_cert)
        assert response.status_code == 201
        token = response.json()["token"]
        assert len(token) == 32 and int(token, 16) >= 0
        assert response.json()["url"].endswith(f"/l/{token}")

    def test_same_target_two_issuances_differ(self, client, clock, bound_org,
                                              session_key, session_cert):
        """I2: the token is not derived from the target — same org, same
        target, same signer, two DIFFERENT tokens."""
        first = publish_link(client, clock, session_key, cert=session_cert).json()["token"]
        second = publish_link(client, clock, session_key, cert=session_cert).json()["token"]
        assert first != second


# -- membership invitation transport -------------------------------------------

class TestOrganizationJoin:
    INVITE_REF = "bc" * 32

    def test_join_link_resolves_public_context_without_bearer_secret(
        self,
        client,
        clock,
        bound_org,
        root,
        session_key,
        session_cert,
    ):
        join_key = KeyPair.generate()
        join_cert = issue_cert(
            session_key,
            join_key.public_hex,
            scope=("link:publish",),
            org=ORG,
            subject=Subject("agent", "invite-linker"),
            target_types=("org:join",),
            not_before=NOW - 10,
            not_after=NOW + DAY,
            parent_cert=session_cert,
        )
        payload = {
            "org": ORG,
            "target_uuid": ORG,
            "target_type": "org:join",
            "invite_ref": self.INVITE_REF,
        }
        response = signed(
            client,
            "POST",
            "/v1/links",
            join_key,
            payload,
            clock,
            cert=join_cert,
        )
        assert response.status_code == 201, response.json()
        grant_token = response.json()["token"]

        stored = client.app.state.store.get_link(grant_token)
        assert stored is not None
        assert stored.invite_ref == self.INVITE_REF
        assert stored.org_uuid == ORG

        envelope = client.get(f"/v1/links/{grant_token}/envelope")
        assert envelope.status_code == 200
        assert envelope.json() == {
            "org": ORG,
            "target_uuid": ORG,
            "target_type": "org:join",
            "invite_ref": self.INVITE_REF,
            "meta": {},
            "root_pub": root.public_hex,
            "endpoints": [],
        }
        assert "token" not in envelope.json()

    def test_join_requires_invite_ref_and_other_targets_refuse_it(
        self,
        client,
        clock,
        bound_org,
        session_key,
        session_cert,
    ):
        missing = {
            "org": ORG,
            "target_uuid": ORG,
            "target_type": "org:join",
        }
        assert signed(
            client,
            "POST",
            "/v1/links",
            session_key,
            missing,
            clock,
            cert=session_cert,
        ).status_code == 400

        stray = {
            "org": ORG,
            "target_uuid": TARGET,
            "target_type": "present",
            "invite_ref": self.INVITE_REF,
        }
        assert signed(
            client,
            "POST",
            "/v1/links",
            session_key,
            stray,
            clock,
            cert=session_cert,
        ).status_code == 400


# -- I4: every mutation verifies a chain ----------------------------------------

class TestChainGateI4:
    def test_non_chained_key_403(self, client, clock, bound_org):
        """A key with NO relationship to the org — even wrapping itself in a
        self-signed 'chain' to its own fake root — gets 403."""
        fake_root, fake_leaf = KeyPair.generate(), KeyPair.generate()
        fake_cert = issue_cert(
            fake_root, fake_leaf.public_hex, scope=("link:publish",), org=ORG,
            subject=Subject("operator", "mallory"),
            not_before=NOW - 10, not_after=NOW + DAY,
        )
        assert publish_link(client, clock, fake_leaf, cert=fake_cert).status_code == 403
        assert publish_link(client, clock, fake_leaf).status_code == 403

    def test_scope_mismatched_chain_403(self, client, clock, root, bound_org,
                                        session_key, session_cert):
        """An agent cert WITHOUT link:publish cannot publish, even though its
        chain to root is perfectly valid."""
        narrow = KeyPair.generate()
        narrow_cert = issue_cert(
            session_key, narrow.public_hex, scope=("viewer:identify",), org=ORG,
            subject=Subject("agent", "sess-42"),
            not_before=NOW - 10, not_after=NOW + DAY,
            parent_cert=session_cert,
        )
        assert publish_link(client, clock, narrow, cert=narrow_cert).status_code == 403

    def test_agent_chain_with_scope_publishes(self, client, clock, bound_org,
                                              agent_key, agent_cert):
        assert publish_link(client, clock, agent_key, cert=agent_cert).status_code == 201

    def test_target_type_restriction_enforced(self, client, clock, bound_org,
                                              session_key, session_cert):
        """A cert restricted to target_types=[present] cannot publish a note."""
        scoped = KeyPair.generate()
        scoped_cert = issue_cert(
            session_key, scoped.public_hex, scope=("link:publish",), org=ORG,
            subject=Subject("agent", "sess-43"), target_types=("present",),
            not_before=NOW - 10, not_after=NOW + DAY,
            parent_cert=session_cert,
        )
        assert publish_link(client, clock, scoped, cert=scoped_cert,
                            target_type="present").status_code == 201
        assert publish_link(client, clock, scoped, cert=scoped_cert,
                            target_type="note").status_code == 403

    def test_expired_cert_403(self, client, clock, bound_org, session_key, session_cert):
        short = KeyPair.generate()
        short_cert = issue_cert(
            session_key, short.public_hex, scope=("link:publish",), org=ORG,
            subject=Subject("agent", "sess-short"),
            not_before=NOW - 10, not_after=NOW + DAY,
            parent_cert=session_cert,
        )
        assert publish_link(client, clock, short, cert=short_cert).status_code == 201
        clock.advance(2 * DAY)  # past the cert, well inside the 30d binding TTL
        assert publish_link(client, clock, short, cert=short_cert).status_code == 403

    def test_envelope_not_replayable_across_endpoints(self, client, clock, bound_org,
                                                      session_key, session_cert):
        """The signature binds method+path: a valid /v1/links envelope
        replayed against another path fails."""
        payload = {"org": ORG, "target_uuid": TARGET, "target_type": "present"}
        envelope = sign_request(session_key, "POST", "/v1/links", payload,
                                ts=clock.now, cert=session_cert)
        assert client.post("/v1/links", json=envelope).status_code == 201
        replayed = client.post(f"/v1/orgs/{ORG}/renew", json=envelope)
        assert replayed.status_code in (400, 403)  # payload shape + signature both fail

    def test_stale_envelope_403(self, client, clock, bound_org, session_key, session_cert):
        payload = {"org": ORG, "target_uuid": TARGET, "target_type": "present"}
        envelope = sign_request(session_key, "POST", "/v1/links", payload,
                                ts=clock.now - HOUR, cert=session_cert)
        assert client.post("/v1/links", json=envelope).status_code == 403


# -- rung-2 fence ----------------------------------------------------------------

class TestRung2:
    def test_require_auth_meta_501(self, client, clock, bound_org, session_key, session_cert):
        response = publish_link(client, clock, session_key, cert=session_cert,
                                meta={"require_auth": True})
        assert response.status_code == 501
        assert "rung-2" in response.json()["detail"]

    def test_persona_subject_501(self, client, clock, bound_org, session_key, session_cert):
        persona = KeyPair.generate()
        persona_cert = issue_cert(
            session_key, persona.public_hex, scope=("link:publish",), org=ORG,
            subject=Subject("persona", "viewer-7"),
            not_before=NOW - 10, not_after=NOW + DAY,
            parent_cert=session_cert,
        )
        response = publish_link(client, clock, persona, cert=persona_cert)
        assert response.status_code == 501
        assert "rung-2" in response.json()["detail"]


# -- DELETE /v1/links/{token} ------------------------------------------------------

class TestRevokeLink:
    def test_revoke_requires_link_revoke_scope(self, client, clock, bound_org,
                                               session_key, session_cert,
                                               agent_key, agent_cert):
        token = publish_link(client, clock, session_key, cert=session_cert).json()["token"]
        # Agent holds link:publish only — revoke is out of scope.
        assert signed(client, "DELETE", f"/v1/links/{token}", agent_key, {}, clock,
                      cert=agent_cert).status_code == 403
        # The session key holds link:revoke.
        assert signed(client, "DELETE", f"/v1/links/{token}", session_key, {}, clock,
                      cert=session_cert).status_code == 200

    def test_revoked_link_envelope_404(self, client, clock, bound_org,
                                       session_key, session_cert):
        token = publish_link(client, clock, session_key, cert=session_cert).json()["token"]
        assert client.get(f"/v1/links/{token}/envelope").status_code == 200
        signed(client, "DELETE", f"/v1/links/{token}", session_key, {}, clock,
               cert=session_cert, expect=200)
        assert client.get(f"/v1/links/{token}/envelope").status_code == 404


# -- §4.6 envelope + anti-enumeration ----------------------------------------------

class TestEnvelope:
    def test_envelope_contents(self, client, clock, root, bound_org,
                               session_key, session_cert):
        token = publish_link(client, clock, session_key, cert=session_cert,
                             meta={"label": "OSS briefing"}).json()["token"]
        body = client.get(f"/v1/links/{token}/envelope").json()
        assert body == {
            "org": ORG,
            "target_uuid": TARGET,
            "target_type": "present",
            "invite_ref": None,
            "meta": {"label": "OSS briefing"},
            "root_pub": root.public_hex,
            "endpoints": [],  # §5.4 direct-connect seam: empty in v1
        }

    def test_grant_ttl_enforced(self, client, clock, bound_org, session_key, session_cert):
        token = publish_link(client, clock, session_key, cert=session_cert,
                             meta={"ttl": HOUR}).json()["token"]
        assert client.get(f"/v1/links/{token}/envelope").status_code == 200
        clock.advance(HOUR + 1)
        assert client.get(f"/v1/links/{token}/envelope").status_code == 404

    def test_dead_binding_kills_envelope(self, client, clock, root, recovery,
                                         session_key, session_cert):
        from .conftest import register
        register(client, clock, root, policy="recovery-key",
                 recovery_pub=recovery.public_hex, ttl=HOUR)
        token = publish_link(client, clock, session_key, cert=session_cert).json()["token"]
        clock.advance(HOUR + 1)
        assert client.get(f"/v1/links/{token}/envelope").status_code == 404

    def test_anti_enumeration_uniform_404(self, client, clock, bound_org,
                                          session_key, session_cert):
        """Unknown, revoked, and expired tokens are byte-identical 404s —
        a prober cannot distinguish which failure they hit (§5.3)."""
        unknown = client.get(f"/v1/links/{'0' * 32}/envelope")

        revoked_token = publish_link(client, clock, session_key,
                                     cert=session_cert).json()["token"]
        signed(client, "DELETE", f"/v1/links/{revoked_token}", session_key, {}, clock,
               cert=session_cert, expect=200)
        revoked = client.get(f"/v1/links/{revoked_token}/envelope")

        expiring_token = publish_link(client, clock, session_key, cert=session_cert,
                                      meta={"ttl": HOUR}).json()["token"]
        clock.advance(HOUR + 1)
        expired = client.get(f"/v1/links/{expiring_token}/envelope")

        assert unknown.status_code == revoked.status_code == expired.status_code == 404
        assert unknown.json() == revoked.json() == expired.json()
