"""E1 session linking — spec §4.7 (assertion redemption), §4.8 (QR
challenge), §6.8 (endpoint hints), and the I12 no-attribution invariant.

Every test drives the HTTP surface the way a browser will: an assertion
is minted client-side with ``build_assertion`` (the dashboard picker's job)
and redeemed at ``POST /v1/link``. The clock is injected, so TTL and
single-use horizons are deterministic.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.registry.app import SESSION_COOKIE
from tools.network.registry.assertion import Assertion, IDENTIFY_SCOPE, build_assertion

from tools.network.registry.store import LinkGrant

from .conftest import DAY, NOW, ORG, TARGET, publish_link, signed

ORIGIN = "https://dash.example"


def _n(ch: str) -> str:
    """A distinct valid 128-bit (32 lowercase hex) nonce/challenge."""
    return ch * 32


def operator_assertion(session_key, session_cert, *, nonce, challenge=None,
                       scope=IDENTIFY_SCOPE, not_before=NOW - 5, not_after=NOW + 60):
    return build_assertion(
        session_key, org=ORG, nonce=nonce, dashboard_origin=ORIGIN,
        not_before=not_before, not_after=not_after, cert=session_cert,
        scope=scope, challenge=challenge,
    )


# -- §4.7 happy path -----------------------------------------------------------

class TestRedeemHappyPath:
    def test_operator_assertion_identifies_session(
        self, client, clock, bound_org, session_key, session_cert
    ):
        wire = operator_assertion(session_key, session_cert, nonce=_n("a"))
        r = client.post("/v1/link", json=wire)
        assert r.status_code == 200, r.json()
        body = r.json()
        assert body["linked"] is True
        assert body["org"] == ORG
        assert body["subject"] == {"kind": "operator", "id": "op-1"}
        # A first-party session cookie was set and resolves to identity.
        sid = client.cookies.get(SESSION_COOKIE)
        assert sid
        session = client.app.state.store.get_session(sid)
        assert session.identified
        assert session.subject_kind == "operator" and session.subject_id == "op-1"
        assert session.dashboard_origin == ORIGIN

    def test_same_browser_redemption_rotates_session_id(
        self, client, clock, bound_org, session_key, session_cert
    ):
        """A pre-seeded cookie must not fixate the identified session:
        same-browser redemption always hands back a fresh id."""
        seeded = TestClient(client.app)
        seeded.cookies.set(SESSION_COOKIE, "a" * 32)  # attacker-chosen id
        wire = operator_assertion(session_key, session_cert, nonce=_n("2"))
        r = seeded.post("/v1/link", json=wire)
        assert r.status_code == 200
        # The server minted a fresh id (read from Set-Cookie, not the jar).
        sid = r.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
        assert sid and sid != "a" * 32
        # The attacker-chosen id was never made identified.
        assert client.app.state.store.get_session("a" * 32) is None
        assert client.app.state.store.get_session(sid).identified

    def test_persona_assertion_carries_persona_identity(
        self, client, clock, bound_org, persona_key, persona_cert
    ):
        """Personas are first-class at the redemption endpoint (the mutation
        gate rejects them with 501 — here they are the whole point)."""
        wire = build_assertion(
            persona_key, org=ORG, nonce=_n("b"), dashboard_origin=ORIGIN,
            not_before=NOW - 5, not_after=NOW + 60, cert=persona_cert,
        )
        r = client.post("/v1/link", json=wire)
        assert r.status_code == 200, r.json()
        assert r.json()["subject"] == {"kind": "persona", "id": "persona-P"}


# -- §4.7 negative space (the attacker's menu) ---------------------------------

class TestRedeemRejections:
    def test_replay_is_401(self, client, clock, bound_org, session_key, session_cert):
        wire = operator_assertion(session_key, session_cert, nonce=_n("c"))
        assert client.post("/v1/link", json=wire).status_code == 200
        # Same nonce, byte-identical assertion, second time: single-use.
        assert client.post("/v1/link", json=wire).status_code == 401

    def test_expired_is_401(self, client, clock, bound_org, session_key, session_cert):
        wire = operator_assertion(
            session_key, session_cert, nonce=_n("d"),
            not_before=NOW - 61, not_after=NOW - 1,  # window closed before now
        )
        assert client.post("/v1/link", json=wire).status_code == 401

    def test_not_yet_valid_is_401(self, client, clock, bound_org, session_key, session_cert):
        wire = operator_assertion(
            session_key, session_cert, nonce=_n("e"),
            not_before=NOW + 30, not_after=NOW + 90,
        )
        assert client.post("/v1/link", json=wire).status_code == 401

    def test_oversized_ttl_window_is_400(self, client, clock, bound_org, session_key, session_cert):
        wire = operator_assertion(
            session_key, session_cert, nonce=_n("f"),
            not_before=NOW - 5, not_after=NOW + 3600,  # way past seconds-scale
        )
        assert client.post("/v1/link", json=wire).status_code == 400

    def test_scope_other_than_identify_is_403(
        self, client, clock, bound_org, session_key, session_cert
    ):
        wire = operator_assertion(
            session_key, session_cert, nonce=_n("1"), scope="link:publish"
        )
        assert client.post("/v1/link", json=wire).status_code == 403

    def test_chain_to_unbound_root_is_403(self, client, clock, bound_org):
        """A perfectly-formed, freshly-signed, identify-scoped assertion whose
        cert chains to a root that is NOT the org's bound root: 403."""
        fake_root, leaf = KeyPair.generate(), KeyPair.generate()
        fake_cert = issue_cert(
            fake_root, leaf.public_hex, scope=("viewer:identify",), org=ORG,
            subject=Subject("operator", "mallory"),
            not_before=NOW - 10, not_after=NOW + DAY,
        )
        wire = build_assertion(
            leaf, org=ORG, nonce=_n("2"), dashboard_origin=ORIGIN,
            not_before=NOW - 5, not_after=NOW + 60, cert=fake_cert,
        )
        assert client.post("/v1/link", json=wire).status_code == 403

    def test_unknown_org_is_403(self, client, clock, session_key, session_cert):
        """No binding at all → nothing to chain to → 403 (not a 404 oracle)."""
        wire = operator_assertion(session_key, session_cert, nonce=_n("3"))
        assert client.post("/v1/link", json=wire).status_code == 403

    def test_tampered_signature_is_401(self, client, clock, bound_org, session_key, session_cert):
        wire = operator_assertion(session_key, session_cert, nonce=_n("4"))
        wire["sig"] = "0" * len(wire["sig"])  # valid shape, wrong signature
        assert client.post("/v1/link", json=wire).status_code == 401

    def test_cert_not_delegating_to_signer_is_403(
        self, client, clock, bound_org, session_key, session_cert, agent_key
    ):
        """A validly-signed assertion whose cert delegates to a DIFFERENT
        key than its signer: the signature verifies, but the cert-to-signer
        binding does not → 403. (Built by hand: build_assertion refuses to
        mint this mismatch.)"""
        forged = Assertion(
            v=1, signer=agent_key.public_hex, org=ORG, nonce=_n("6"),
            dashboard_origin=ORIGIN, scope=IDENTIFY_SCOPE,
            not_before=NOW - 5, not_after=NOW + 60,
            cert=session_cert.to_json().decode("ascii"), sig="0" * 128,
        )
        wire = forged.signed_dict()
        wire["cert"] = forged.cert
        wire["sig"] = agent_key.sign_hex(forged.signing_input())
        assert client.post("/v1/link", json=wire).status_code == 403


# -- §4.8 QR cross-device ------------------------------------------------------

class TestQrChallenge:
    def _mint(self, app):
        anon = TestClient(app)
        r = anon.post("/v1/link/challenge")
        assert r.status_code == 200, r.json()
        return anon, r.json()["challenge"]

    def test_pwa_redemption_upgrades_bound_session_not_submitter(
        self, app, clock, bound_org, persona_key, persona_cert
    ):
        anon, challenge = self._mint(app)
        anon_sid = anon.cookies.get(SESSION_COOKIE)
        assert anon_sid
        store = app.state.store
        assert store.get_session(anon_sid).identified is False

        # A DIFFERENT browser (the PWA) submits the signed assertion.
        pwa = TestClient(app)
        wire = build_assertion(
            persona_key, org=ORG, nonce=_n("7"), dashboard_origin=ORIGIN,
            not_before=NOW - 5, not_after=NOW + 60, cert=persona_cert,
            challenge=challenge,
        )
        r = pwa.post("/v1/link", json=wire)
        assert r.status_code == 200, r.json()
        assert r.json() == {"linked": True}  # no org/subject leaked to submitter

        # Exactly the challenge-bound anonymous session got the identity...
        upgraded = store.get_session(anon_sid)
        assert upgraded.identified
        assert upgraded.subject_kind == "persona" and upgraded.subject_id == "persona-P"
        # ...and the submitter's browser was handed NO session at all.
        assert pwa.cookies.get(SESSION_COOKIE) is None

        # The waiting browser observes the upgrade via the poll contract.
        assert anon.get(f"/v1/link/challenge/{challenge}").json() == {"linked": True}

    def test_expired_challenge_is_404(
        self, app, clock, bound_org, session_key, session_cert
    ):
        anon, challenge = self._mint(app)
        clock.advance(61)  # past the ~60s challenge window
        pwa = TestClient(app)
        wire = build_assertion(
            session_key, org=ORG, nonce=_n("8"), dashboard_origin=ORIGIN,
            not_before=NOW + 60, not_after=NOW + 120,  # assertion itself still fresh
            cert=session_cert, challenge=challenge,
        )
        assert pwa.post("/v1/link", json=wire).status_code == 404

    def test_reused_challenge_is_404(
        self, app, clock, bound_org, session_key, session_cert
    ):
        anon, challenge = self._mint(app)
        pwa = TestClient(app)
        first = build_assertion(
            session_key, org=ORG, nonce=_n("9"), dashboard_origin=ORIGIN,
            not_before=NOW - 5, not_after=NOW + 60, cert=session_cert, challenge=challenge,
        )
        assert pwa.post("/v1/link", json=first).status_code == 200
        # Fresh assertion (new nonce), same already-redeemed challenge.
        second = build_assertion(
            session_key, org=ORG, nonce=_n("0"), dashboard_origin=ORIGIN,
            not_before=NOW - 5, not_after=NOW + 60, cert=session_cert, challenge=challenge,
        )
        assert pwa.post("/v1/link", json=second).status_code == 404

    def test_unknown_challenge_status_is_404(self, client, clock, bound_org):
        assert client.get(f"/v1/link/challenge/{_n('c')}").status_code == 404

    def test_sse_wait_streams_linked_after_redemption(
        self, app, clock, bound_org, session_key, session_cert
    ):
        anon, challenge = self._mint(app)
        pwa = TestClient(app)
        wire = build_assertion(
            session_key, org=ORG, nonce=_n("d"), dashboard_origin=ORIGIN,
            not_before=NOW - 5, not_after=NOW + 60, cert=session_cert, challenge=challenge,
        )
        assert pwa.post("/v1/link", json=wire).status_code == 200
        stream = anon.get(f"/v1/link/challenge/{challenge}/wait")
        assert stream.status_code == 200
        assert "event: linked" in stream.text

    def test_sse_wait_unknown_challenge_is_404(self, client, clock, bound_org):
        assert client.get(f"/v1/link/challenge/{_n('e')}/wait").status_code == 404


# -- §6.8 endpoint hints (identified sessions only) ----------------------------

class TestEndpointHints:
    def _register_with_hints(self, client, clock, root, hints):
        payload = {
            "org_uuid": ORG, "root_pub": root.public_hex,
            "recovery_policy": "none", "endpoint_hints": hints,
        }
        signed(client, "POST", "/v1/orgs", root, payload, clock, expect=201)

    def test_hints_served_to_identified_session(
        self, client, clock, root, session_key, session_cert
    ):
        hints = [{"url": "https://dash.example", "sig": "ff" * 64}]
        self._register_with_hints(client, clock, root, hints)
        # Become identified, then ask for hints on the same cookie jar.
        wire = operator_assertion(session_key, session_cert, nonce=_n("f"))
        assert client.post("/v1/link", json=wire).status_code == 200
        r = client.get("/v1/link/hints")
        assert r.status_code == 200
        assert r.json() == {"endpoint_hints": hints}

    def test_anonymous_gets_no_hints(self, client, clock, root):
        self._register_with_hints(client, clock, root, [{"url": "https://x", "sig": "0" * 128}])
        # No session cookie at all.
        assert client.get("/v1/link/hints").status_code == 403

    def test_anonymous_qr_session_gets_no_hints(self, app, clock, bound_org):
        """An anonymous QR-minting session is present but NOT identified —
        still no hints."""
        anon = TestClient(app)
        anon.post("/v1/link/challenge")
        assert anon.get("/v1/link/hints").status_code == 403


# -- I12 — bearer views carry no identity --------------------------------------

class TestI12NoAttribution:
    def test_noauth_envelope_from_identified_session_records_nothing(
        self, client, clock, bound_org, session_key, session_cert
    ):
        # A plain (no require_auth) grant.
        token = publish_link(client, clock, session_key, cert=session_cert).json()["token"]
        # The same browser becomes identified.
        wire = operator_assertion(session_key, session_cert, nonce=_n("1"))
        assert client.post("/v1/link", json=wire).status_code == 200
        assert client.cookies.get(SESSION_COOKIE)

        # Fetch the envelope WITH the identified cookie present.
        env = client.get(f"/v1/links/{token}/envelope")
        assert env.status_code == 200
        body = env.json()
        # No identity fields leak into the envelope...
        assert "subject" not in body and "persona" not in body and "identity" not in body
        # ...and NO attribution row was written for this bearer view (I12).
        assert client.app.state.store.view_attributions(token) == []
        assert client.app.state.store.view_attributions() == []

    def test_authgrant_envelope_does_attribute_identified_view(
        self, client, clock, bound_org, session_key, session_cert
    ):
        """The positive counterpart: for a require_auth grant, an identified
        session's view IS attributed — so the bearer-grant emptiness above
        is a real gate, not dead code. (require_auth grants are fenced at
        501 on the publish API, so seed one directly in the store.)"""
        store = client.app.state.store
        store.create_link(LinkGrant(
            token="authtok", org_uuid=ORG, target_uuid=TARGET, target_type="present",
            meta={"require_auth": True}, created_at=NOW, expires_at=None, revoked_at=None,
            signer_pub=session_key.public_hex, subject_kind="operator", subject_id="op-1",
        ))
        # An anonymous fetch attributes nothing (no identified session).
        anon = TestClient(client.app)
        assert anon.get("/v1/links/authtok/envelope").status_code == 200
        assert store.view_attributions("authtok") == []

        # The identified session's fetch DOES get attributed.
        wire = operator_assertion(session_key, session_cert, nonce=_n("3"))
        assert client.post("/v1/link", json=wire).status_code == 200
        assert client.get("/v1/links/authtok/envelope").status_code == 200
        rows = store.view_attributions("authtok")
        assert len(rows) == 1
        assert rows[0]["subject_kind"] == "operator" and rows[0]["subject_id"] == "op-1"
