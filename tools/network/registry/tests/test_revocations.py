"""§4.5: revocation records — authority, chain rejection, I7 retention."""

from __future__ import annotations

from tools.network.idkit import KeyPair, Subject, issue_cert, issue_revocation
from tools.network.registry.relay import Tunnel

from .conftest import DAY, NOW, ORG, publish_link, signed


def post_revocation(client, record, revoked_cert, org=ORG):
    return client.post(
        "/v1/revocations",
        json={
            "org": org,
            "record": record.to_json().decode("ascii"),
            "revoked_cert": revoked_cert.to_json().decode("ascii"),
        },
    )


class TestRevocationAuthority:
    def test_posted_revocation_closes_matching_standing_tunnel(
        self, app, client, clock, root, bound_org,
    ):
        class Socket:
            def __init__(self):
                self.closed = None

            async def close(self, code):
                self.closed = code

        serve_key = KeyPair.generate()
        serve_cert = issue_cert(
            root,
            serve_key.public_hex,
            scope=("tunnel:serve",),
            org=ORG,
            subject=Subject("persona", "ab" * 32),
            not_before=clock.now - 10,
            not_after=clock.now + DAY,
        )
        socket = Socket()
        tunnel = Tunnel(
            socket,
            ORG,
            persona_pub="ab" * 32,
            signer_pub=serve_key.public_hex,
        )
        app.state.tunnel_hub.register(tunnel)
        record = issue_revocation(
            root,
            serve_key.public_hex,
            org=ORG,
            revoked_at=clock.now,
            expires_at=serve_cert.not_after,
            revoked_cert=serve_cert,
        )

        assert post_revocation(client, record, serve_cert).status_code == 201
        assert app.state.tunnel_hub.get(ORG) is None
        assert socket.closed == 4403

    def test_root_signed_revocation_kills_chain(self, client, clock, root, bound_org,
                                                session_key, session_cert):
        assert publish_link(client, clock, session_key, cert=session_cert).status_code == 201
        record = issue_revocation(
            root, session_key.public_hex, org=ORG,
            revoked_at=clock.now, expires_at=clock.now + DAY,
            revoked_cert=session_cert,
        )
        assert post_revocation(client, record, session_cert).status_code == 201
        # The revoked key's chain is now rejected on every mutation (I4+§4.5).
        assert publish_link(client, clock, session_key, cert=session_cert).status_code == 403

    def test_revocation_rejects_descendant_chains(self, client, clock, root, bound_org,
                                                  session_key, session_cert,
                                                  agent_key, agent_cert):
        """Revoking the session key kills the AGENT's chain too — the revoked
        id appears as an ancestor hop."""
        record = issue_revocation(
            root, session_key.public_hex, org=ORG,
            revoked_at=clock.now, expires_at=clock.now + DAY,
            revoked_cert=session_cert,
        )
        assert post_revocation(client, record, session_cert).status_code == 201
        assert publish_link(client, clock, agent_key, cert=agent_cert).status_code == 403

    def test_ancestor_signed_revocation(self, client, clock, bound_org,
                                        session_key, session_cert,
                                        agent_key, agent_cert):
        """A parent may revoke its own descendants (issuer_cert proves both
        authority and descent)."""
        record = issue_revocation(
            session_key, agent_key.public_hex, org=ORG,
            revoked_at=clock.now, expires_at=clock.now + DAY,
            issuer_cert=session_cert, revoked_cert=agent_cert,
        )
        assert post_revocation(client, record, agent_cert).status_code == 201
        assert publish_link(client, clock, agent_key, cert=agent_cert).status_code == 403
        # The session key itself is untouched.
        assert publish_link(client, clock, session_key, cert=session_cert).status_code == 201

    def test_non_ancestor_cannot_revoke(self, client, clock, bound_org,
                                        session_key, session_cert,
                                        agent_key, agent_cert):
        """The agent (a LEAF) trying to revoke its parent session key: the
        registry refuses — a delegated key revokes descendants only."""
        record = issue_revocation(
            agent_key, session_key.public_hex, org=ORG,
            revoked_at=clock.now, expires_at=clock.now + DAY,
            issuer_cert=agent_cert,
        )
        assert post_revocation(client, record, session_cert).status_code == 403

    def test_stranger_signed_record_403(self, client, clock, bound_org,
                                        session_key, session_cert):
        stranger = KeyPair.generate()
        record = issue_revocation(
            stranger, session_key.public_hex, org=ORG,
            revoked_at=clock.now, expires_at=clock.now + DAY,
        )
        assert post_revocation(client, record, session_cert).status_code == 403

    def test_retention_beyond_key_expiry_403(self, client, clock, root, bound_org,
                                             session_key, session_cert):
        """I7 at the door: a record whose expires_at outlives the revoked
        key's natural not_after is refused outright."""
        record = issue_revocation(
            root, session_key.public_hex, org=ORG,
            revoked_at=clock.now, expires_at=NOW + 400 * DAY,  # cert dies at NOW+300d
        )
        assert post_revocation(client, record, session_cert).status_code == 403


class TestRetentionI7:
    def _short_lived_delegate(self, session_key, session_cert):
        """A key that naturally expires at T = NOW + 2d."""
        key = KeyPair.generate()
        cert = issue_cert(
            session_key, key.public_hex, scope=("link:publish",), org=ORG,
            subject=Subject("agent", "sess-99"),
            not_before=NOW - 10, not_after=NOW + 2 * DAY,
            parent_cert=session_cert,
        )
        return key, cert

    def test_record_purged_after_expiry_horizon(self, app, client, clock, root, bound_org,
                                                session_key, session_cert):
        """I7: a revocation record for a key expiring at T is gone from the
        store once the purge sweep passes T — the denylist never outlives
        the keys it denies."""
        key, cert = self._short_lived_delegate(session_key, session_cert)
        expiry_t = NOW + 2 * DAY
        record = issue_revocation(
            root, key.public_hex, org=ORG,
            revoked_at=clock.now, expires_at=expiry_t, revoked_cert=cert,
        )
        assert post_revocation(client, record, cert).status_code == 201

        store = app.state.store
        assert store.get_revocation(ORG, key.public_hex) is not None
        assert publish_link(client, clock, key, cert=cert).status_code == 403

        # Sweep strictly past T.
        clock.advance(2 * DAY + 1)
        purged = store.purge_expired_revocations(now=clock.now)
        assert purged == 1
        assert store.get_revocation(ORG, key.public_hex) is None
        # The key is gone with it: its cert expired naturally at T, so the
        # chain still fails — just with ExpiredError instead of RevokedError.
        assert publish_link(client, clock, key, cert=cert).status_code == 403

    def test_purge_runs_lazily_on_mutations(self, app, client, clock, root, bound_org,
                                            session_key, session_cert):
        """The sweep is not operator homework: any authorized mutation after
        the horizon triggers it."""
        key, cert = self._short_lived_delegate(session_key, session_cert)
        record = issue_revocation(
            root, key.public_hex, org=ORG,
            revoked_at=clock.now, expires_at=NOW + 2 * DAY, revoked_cert=cert,
        )
        assert post_revocation(client, record, cert).status_code == 201

        clock.advance(2 * DAY + 1)
        signed(client, "POST", f"/v1/orgs/{ORG}/renew", root, {}, clock, expect=200)
        assert app.state.store.get_revocation(ORG, key.public_hex) is None

    def test_purge_keeps_live_records(self, app, client, clock, root, bound_org,
                                      session_key, session_cert):
        record = issue_revocation(
            root, session_key.public_hex, org=ORG,
            revoked_at=clock.now, expires_at=clock.now + 10 * DAY,
            revoked_cert=session_cert,
        )
        assert post_revocation(client, record, session_cert).status_code == 201
        store = app.state.store
        assert store.purge_expired_revocations(now=clock.now + DAY) == 0
        assert store.get_revocation(ORG, session_key.public_hex) is not None

