"""§5.3 bootloader: served bytes carry no identifiers, one error page.

The cryptographic path (handshake, render) is exercised end-to-end in the
headless browser suite (``tools/network/relaykit/tests/test_bootloader_browser.py``);
these tests pin the server-side anti-enumeration contract, which is pure
HTTP and needs no browser.
"""

from __future__ import annotations

from .conftest import HOUR, ORG, TARGET, publish_link, register, signed


def _publish(client, clock, root, recovery, session_key, session_cert):
    register(client, clock, root, policy="recovery-key", recovery_pub=recovery.public_hex)
    return publish_link(client, clock, session_key, cert=session_cert).json()["token"]


class TestBootloaderBytes:
    def test_shell_carries_no_identifiers(self, client, clock, root, recovery,
                                          session_key, session_cert):
        token = _publish(client, clock, root, recovery, session_key, session_cert)
        body = client.get(f"/l/{token}").text
        # Nothing org-, target-, key-, or even token-identifying may appear
        # in the served page (§5.3: the URL is a pure network pointer).
        for needle in (ORG, TARGET, root.public_hex, session_key.public_hex, token, "present"):
            assert needle not in body

    def test_shell_bytes_identical_across_tokens(self, client, clock, root, recovery,
                                                 session_key, session_cert):
        """Live, revoked, expired, and invented tokens all serve the SAME
        bytes — only the status code (mirroring envelope liveness) differs."""
        live = _publish(client, clock, root, recovery, session_key, session_cert)

        revoked = publish_link(client, clock, session_key, cert=session_cert).json()["token"]
        signed(client, "DELETE", f"/v1/links/{revoked}", session_key, {}, clock,
               cert=session_cert, expect=200)

        expiring = publish_link(client, clock, session_key, cert=session_cert,
                                meta={"ttl": HOUR}).json()["token"]

        live_resp = client.get(f"/l/{live}")
        bodies = {
            "live": live_resp.content,
            "revoked": client.get(f"/l/{revoked}").content,
            "unknown": client.get(f"/l/{'0' * 32}").content,
            "malformed": client.get("/l/not-a-token").content,
        }
        clock.advance(HOUR + 1)
        bodies["expired"] = client.get(f"/l/{expiring}").content
        assert len(set(bodies.values())) == 1  # one byte sequence, always

    def test_status_mirrors_envelope_liveness(self, client, clock, root, recovery,
                                              session_key, session_cert):
        live = _publish(client, clock, root, recovery, session_key, session_cert)
        assert client.get(f"/l/{live}").status_code == 200
        for dead in ("0" * 32, "not-a-token"):
            assert client.get(f"/l/{dead}").status_code == 404

    def test_security_headers(self, client, clock, root, recovery,
                              session_key, session_cert):
        token = _publish(client, clock, root, recovery, session_key, session_cert)
        headers = client.get(f"/l/{token}").headers
        csp = headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "connect-src 'self' https: http: wss: ws:" in csp
        assert "img-src blob: data: https: http:" in csp
        assert "frame-ancestors 'none'" in csp
        assert headers["referrer-policy"] == "no-referrer"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["cache-control"] == "no-store"

    def test_frame_is_null_origin_and_allows_links(self, client):
        shell = client.get("/l/not-a-token").text
        assert "allow-scripts allow-popups allow-popups-to-escape-sandbox" in shell
        assert "allow-same-origin" not in shell

    def test_asset_served(self, client):
        response = client.get("/l-assets/autonet.js")
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]
        assert b"performHandshake" in response.content

    def test_no_org_enumeration_via_status(self, client, clock, root, recovery,
                                           session_key, session_cert):
        """A revoked token and an unknown token are indistinguishable from
        the /l/ endpoint — same status, same bytes."""
        token = _publish(client, clock, root, recovery, session_key, session_cert)
        signed(client, "DELETE", f"/v1/links/{token}", session_key, {}, clock,
               cert=session_cert, expect=200)
        revoked = client.get(f"/l/{token}")
        unknown = client.get(f"/l/{'a' * 32}")
        assert revoked.status_code == unknown.status_code == 404
        assert revoked.content == unknown.content
