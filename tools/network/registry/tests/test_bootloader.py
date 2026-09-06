"""§5.3 bootloader: served bytes carry no identifiers and honest states.

The cryptographic path (handshake, render) is exercised end-to-end in the
headless browser suite (``tools/network/relaykit/tests/test_bootloader_browser.py``);
these tests pin the server-side static-shell and HTTP-liveness contract.
"""

from __future__ import annotations

from pathlib import Path

from .conftest import HOUR, ORG, TARGET, mint_link, register, revoke_link


def _publish(client, clock, root, recovery):
    register(client, clock, root, policy="recovery-key", recovery_pub=recovery.public_hex)
    return mint_link(client, clock, root)["token"]


class TestBootloaderBytes:
    def test_shell_carries_no_identifiers(self, client, clock, root, recovery):
        token = _publish(client, clock, root, recovery)
        body = client.get(f"/l/{token}").text
        # Nothing org-, target-, key-, or even token-identifying may appear
        # in the served page (§5.3: the URL is a pure network pointer).
        for needle in (ORG, TARGET, root.public_hex, token, "present"):
            assert needle not in body

    def test_shell_bytes_identical_across_tokens(self, client, clock, root, recovery):
        """Live, revoked, expired, and invented tokens all serve the SAME
        bytes — only the status code (mirroring envelope liveness) differs."""
        live = _publish(client, clock, root, recovery)

        revoked = mint_link(client, clock, root)["token"]
        revoke_link(client, clock, root, revoked)

        expiring = mint_link(client, clock, root, meta={"ttl": HOUR})["token"]

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

    def test_status_mirrors_envelope_liveness(self, client, clock, root, recovery):
        live = _publish(client, clock, root, recovery)
        assert client.get(f"/l/{live}").status_code == 200
        for dead in ("0" * 32, "not-a-token"):
            assert client.get(f"/l/{dead}").status_code == 404

    def test_security_headers(self, client, clock, root, recovery):
        token = _publish(client, clock, root, recovery)
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

    def test_header_brand_is_replaced_only_after_authenticated_content(self, client):
        shell = client.get("/l/not-a-token").text
        assert '<span id="brand" class="brand">auto.network</span>' in shell
        script = client.get("/l-assets/autonet.js").text
        assert "renderBrand(artifact.branding, body)" in script

    def test_shell_distinguishes_publicly_observable_failure_stages(self, client):
        shell = client.get("/l/not-a-token").text
        assert "Autonomy Disconnected" in shell
        assert "currently offline" in shell
        assert "invalid, expired, or was revoked" in shell
        assert "Secure connection failed" in shell

    def test_asset_served(self, client):
        response = client.get("/l-assets/autonet.js")
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]
        assert response.headers["cache-control"] == "no-store"
        assert b"performHandshake" in response.content

    def test_shared_relaykit_core_is_served_byte_for_byte(self, client):
        source = (
            Path(__file__).resolve().parents[3]
            / "dashboard" / "static" / "js" / "lib" / "relaykit-core.js"
        )
        response = client.get("/l-assets/relaykit-core.js")
        assert response.status_code == 200
        assert response.content == source.read_bytes()
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_asset_uses_sandboxed_srcdoc_not_blob_frame_navigation(self, client):
        script = client.get("/l-assets/autonet.js").text
        # The property under test is that the viewer document is handed to
        # the frame via srcdoc and NEVER by navigating it to a blob: URL.
        # There is now exactly ONE assignment, and it hands over the bytes
        # unmodified: no viewer's HTML is rewritten on the way in.
        assert script.count("frame.srcdoc") == 1
        assert "frame.srcdoc = viewerHtml" in script
        assert "frame.src = URL.createObjectURL" not in script

    def test_shell_busts_legacy_asset_cache_without_fallback(self, client):
        shell = client.get("/l/not-a-token").text
        assert 'type="module" src="/l-assets/autonet.js?v=4"' in shell
        assert 'id="error-view"' not in shell
        assert "This page needs to be refreshed" not in shell

    def test_no_org_enumeration_via_status(self, client, clock, root, recovery):
        """A revoked token and an unknown token are indistinguishable from
        the /l/ endpoint — same status, same bytes."""
        token = _publish(client, clock, root, recovery)
        revoke_link(client, clock, root, token)
        revoked = client.get(f"/l/{token}")
        unknown = client.get(f"/l/{'a' * 32}")
        assert revoked.status_code == unknown.status_code == 404
        assert revoked.content == unknown.content
