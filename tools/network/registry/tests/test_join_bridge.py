"""The org:join bridge page (auto-y7nap) — server-side contract.

The bridge fills the previously-dead redirect target: the bootloader sends
org:join links to a relative /network/join that no service served, so every
browser-opened invitation 404'd. These tests pin the server half — a fixed
neutral shell, strict headers, client-side-only assembly — and the
bootloader's channel_token pass-through. The client behavior (probe as
progressive enhancement, blurb assembly, local handoff) lives in join.js
and is asserted here at the source-contract level: the trust-critical
properties are structural (what the code CAN'T do), not behavioral.
"""

from __future__ import annotations

from pathlib import Path

BOOTLOADER_DIR = Path(__file__).resolve().parents[1] / "bootloader"
JOIN_JS = (BOOTLOADER_DIR / "join.js").read_text(encoding="utf-8")
AUTONET_JS = (BOOTLOADER_DIR / "autonet.js").read_text(encoding="utf-8")


class TestJoinShell:
    def test_one_byte_sequence_regardless_of_query(self, client):
        bare = client.get("/network/join")
        full = client.get(
            "/network/join?org=11111111-1111-4111-8111-111111111111"
            "&root_pub=" + "a" * 64 + "&invite_ref=" + "e" * 64
        )
        assert bare.status_code == full.status_code == 200
        assert bare.content == full.content

    def test_shell_carries_no_invitation_data(self, client):
        body = client.get(
            "/network/join?org=22222222-2222-4222-8222-222222222222"
        ).text
        assert "22222222" not in body

    def test_headers_and_csp(self, client):
        response = client.get("/network/join")
        csp = response.headers["content-security-policy"]
        assert "default-src 'none'" in csp
        # Only the visitor's own local node is a permitted connect target —
        # the probe can never exfiltrate anywhere else.
        assert "connect-src https://localhost:8080 http://localhost:8080" in csp
        assert "frame-ancestors 'none'" in csp
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_join_js_served(self, client):
        response = client.get("/l-assets/join.js")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/javascript")
        assert response.text == JOIN_JS


class TestClientSourceContract:
    """Structural assertions on the client code's trust properties."""

    def test_bearer_is_fragment_only_and_never_sent(self):
        # The bearer is read from location.hash and appears only in URL
        # CONSTRUCTION (the blurb's invite link, the local-node handoff) —
        # never in a network call. The only fetch in the file is the
        # localhost liveness probe.
        assert "location.hash" in JOIN_JS
        fetches = [
            line for line in JOIN_JS.splitlines() if "fetch(" in line
        ]
        assert len(fetches) == 1
        assert "/api/ping" in fetches[0]
        assert "bearer" not in fetches[0]

    def test_no_ceremony_code(self):
        # The July trust ruling: a relay-served page must not run the join
        # ceremony. No crypto, no claim submission, no envelope handling.
        for forbidden in ("subtle", "crypto.", "member.claim", "submit(",
                          ".sign(", "decrypt", "Ed25519", "keypair"):
            assert forbidden.lower() not in JOIN_JS.lower(), forbidden

    def test_blurb_is_origin_aware(self):
        # Both blurb URLs derive from location.origin so the copied prompt
        # works on the interim registry host and self-upgrades on the apex
        # (same principle as the /install CTA).
        assert 'location.origin + "/install' in JOIN_JS
        assert 'location.origin + "/l/"' in JOIN_JS
        assert "auto.network" not in JOIN_JS

    def test_probe_is_progressive_enhancement(self):
        # A failed/blocked probe must leave both affordances rendered —
        # the code path only ever swaps emphasis on success.
        assert "PROBE_TIMEOUT_MS" in JOIN_JS
        assert "catch" in JOIN_JS


class TestBootloaderPassThrough:
    # The executable proof of the pass-through lives in
    # test_bootloader_join.py::test_join_context_and_fragment_only_delivery,
    # which runs deliverJoinContext under Node and asserts the exact query
    # and fragment shapes (both credentials fragment-only). Source-substring
    # assertions here were review-flagged as satisfiable by unrelated code;
    # only the property a substring CAN carry remains:
    def test_join_page_reads_credentials_from_fragment_only(self):
        # join.js must never look for either credential in the query.
        assert 'query.get("channel_token")' not in JOIN_JS
        assert 'query.get("t")' not in JOIN_JS
        assert 'fragment.get("channel_token")' in JOIN_JS
        assert 'fragment.get("t")' in JOIN_JS
