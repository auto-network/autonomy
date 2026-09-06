"""§4.4/§4.6: link grants, envelope resolution, I2 tokens.

Grants are minted and revoked on the org's authenticated serving tunnel
(D19 / auto-qol1v) — the tunnel-side op surface itself (auth, org:join
argument law, rung-2 fence, cross-org revoke) is pinned in
``test_tunnel_control.py``. These tests cover the grant properties the
public read path then exposes: token independence, envelope contents and
liveness, absolute org:join expiry, and anti-enumeration. The signed-HTTP
envelope gate that the surviving mutation routes share is pinned in
``test_orgs.py`` (renew) and the delegation-chain law itself in the idkit
unit suite.
"""

from __future__ import annotations

import sqlite3

from tools.network.registry.store import LinkGrant, RegistryStore

from .conftest import (
    HOUR,
    NOW,
    ORG,
    TARGET,
    ctrl,
    mint_link,
    open_tunnel,
    register,
    revoke_link,
)


# -- I2: tokens ----------------------------------------------------------------

class TestTokensI2:
    def test_same_target_two_issuances_differ(self, client, clock, bound_org, root):
        """I2: the token is not derived from the target — same org, same
        target, two DIFFERENT tokens."""
        first = mint_link(client, clock, root)["token"]
        second = mint_link(client, clock, root)["token"]
        assert first != second


# -- membership invitation transport -------------------------------------------

class TestOrganizationJoin:
    INVITE_REF = "bc" * 32
    EXPIRY = 1_900_000_000_000  # far-future absolute unix-ms

    def test_join_link_resolves_public_context_without_bearer_secret(
        self, client, clock, bound_org, root,
    ):
        reply = mint_link(
            client, clock, root, target=ORG, target_type="org:join",
            invite_ref=self.INVITE_REF, expires_at=self.EXPIRY,
        )
        grant_token = reply["token"]

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

    def test_join_absolute_expiry_is_stored_exactly_and_enforced(
        self, client, clock, bound_org, root,
    ):
        invite_expiry = (NOW + HOUR) * 1000 + 500
        reply = mint_link(
            client, clock, root, target=ORG, target_type="org:join",
            invite_ref=self.INVITE_REF, expires_at=invite_expiry,
        )
        assert reply["expires_at"] == invite_expiry
        token = reply["token"]
        stored = client.app.state.store.get_link(token)
        assert stored.expires_at is None
        assert stored.expires_at_ms == invite_expiry

        clock.advance(HOUR)
        assert client.get(f"/v1/links/{token}/envelope").status_code == 200
        clock.advance(1)
        assert client.get(f"/v1/links/{token}/envelope").status_code == 404

    def test_expires_at_refused_on_non_org_join(self, client, clock, bound_org, root):
        """Absolute expiry is the invitation's lifetime — a plain share
        grant must use meta.ttl, never expires_at."""
        with open_tunnel(client, clock, root) as ws:
            reply = ctrl(ws, "a" * 32, "create-link", {
                "target_uuid": TARGET, "target_type": "present",
                "expires_at": self.EXPIRY,
            })
        assert reply["ok"] is False
        assert "only valid for target_type org:join" in reply["error"]


def test_absolute_expiry_column_migrates_existing_registry(tmp_path):
    path = tmp_path / "registry.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE links (
            token TEXT PRIMARY KEY,
            org_uuid TEXT NOT NULL,
            target_uuid TEXT NOT NULL,
            target_type TEXT NOT NULL,
            invite_ref TEXT,
            meta TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER,
            revoked_at INTEGER,
            signer_pub TEXT NOT NULL,
            subject_kind TEXT NOT NULL,
            subject_id TEXT NOT NULL
        )
        """
    )
    connection.commit()
    connection.close()

    store = RegistryStore(str(path))
    columns = {
        row["name"]
        for row in store._conn.execute("PRAGMA table_info(links)")
    }
    assert "expires_at_ms" in columns
    store.close()


def test_d19_rebuild_preserves_rows_and_makes_persona_cols_nullable(tmp_path):
    """The D19 store change makes signer_pub / subject_id nullable so
    org-tunnel grants (no persona) can be stored. A pre-D19 table has them
    NOT NULL, so the store rebuilds it — this must preserve existing rows
    byte-for-byte and admit a NULL-persona grant afterwards."""
    path = tmp_path / "registry.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE links (
            token TEXT PRIMARY KEY, org_uuid TEXT NOT NULL,
            target_uuid TEXT NOT NULL, target_type TEXT NOT NULL,
            invite_ref TEXT, meta TEXT NOT NULL, created_at INTEGER NOT NULL,
            expires_at INTEGER, revoked_at INTEGER,
            signer_pub TEXT NOT NULL, subject_kind TEXT NOT NULL,
            subject_id TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO links (token, org_uuid, target_uuid, target_type, meta,"
        " created_at, signer_pub, subject_kind, subject_id)"
        " VALUES ('tok1', 'org-1', 'tgt-1', 'present', '{}', 100,"
        " 'signer-1', 'operator', 'persona-1')"
    )
    connection.commit()
    connection.close()

    store = RegistryStore(str(path))
    try:
        # The pre-D19 row survived the table rebuild unchanged.
        old = store.get_link("tok1")
        assert old is not None
        assert (old.org_uuid, old.target_type, old.signer_pub,
                old.subject_kind, old.subject_id) == (
            "org-1", "present", "signer-1", "operator", "persona-1")
        # And a persona-less org-tunnel grant is now storable.
        store.create_link(LinkGrant(
            token="tok2", org_uuid="org-1", target_uuid="tgt-2",
            target_type="present", meta={}, created_at=200, expires_at=None,
            revoked_at=None, signer_pub=None, subject_kind="org-tunnel",
            subject_id=None))
        tunnel = store.get_link("tok2")
        assert tunnel.signer_pub is None and tunnel.subject_id is None
        assert tunnel.subject_kind == "org-tunnel"
    finally:
        store.close()


# -- §4.6 envelope + anti-enumeration ----------------------------------------------

class TestEnvelope:
    def test_envelope_contents(self, client, clock, root, bound_org):
        token = mint_link(client, clock, root,
                          meta={"label": "OSS briefing"})["token"]
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

    def test_grant_ttl_enforced(self, client, clock, root, bound_org):
        token = mint_link(client, clock, root, meta={"ttl": HOUR})["token"]
        assert client.get(f"/v1/links/{token}/envelope").status_code == 200
        clock.advance(HOUR + 1)
        assert client.get(f"/v1/links/{token}/envelope").status_code == 404

    def test_dead_binding_kills_envelope(self, client, clock, root, recovery):
        register(client, clock, root, policy="recovery-key",
                 recovery_pub=recovery.public_hex, ttl=HOUR)
        token = mint_link(client, clock, root)["token"]
        clock.advance(HOUR + 1)
        assert client.get(f"/v1/links/{token}/envelope").status_code == 404

    def test_anti_enumeration_uniform_404(self, client, clock, root, bound_org):
        """Unknown, revoked, and expired tokens are byte-identical 404s —
        a prober cannot distinguish which failure they hit (§5.3)."""
        unknown = client.get(f"/v1/links/{'0' * 32}/envelope")

        revoked_token = mint_link(client, clock, root)["token"]
        revoke_link(client, clock, root, revoked_token)
        revoked = client.get(f"/v1/links/{revoked_token}/envelope")

        expiring_token = mint_link(client, clock, root, meta={"ttl": HOUR})["token"]
        clock.advance(HOUR + 1)
        expired = client.get(f"/v1/links/{expiring_token}/envelope")

        assert unknown.status_code == revoked.status_code == expired.status_code == 404
        assert unknown.json() == revoked.json() == expired.json()
