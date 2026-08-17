"""The /install content contract (auto-2dt9b, packaging pillar).

GET /install serves the repo-tracked agent-first primer: text/markdown to
agents and curl (the default Accept), a self-contained HTML wrapper to
browsers; GET /install/<doc> serves the fetchable sub-documents. The
canonical bytes are deploy/install/ in the checkout — these tests pin the
negotiation behavior, the path-safety of the sub-document resolver, and
the no-cache/no-sniff headers. Deploying the service is relay-network's
half of the ruled boundary; this file is the contract they deploy.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
INSTALL_DIR = REPO / "deploy" / "install"
DEPLOY_SCRIPT = REPO / "tools" / "network" / "registry" / "deploy" / "deploy.sh"


class TestInstallNegotiation:
    def test_curl_default_accept_gets_markdown(self, client):
        response = client.get("/install", headers={"Accept": "*/*"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/markdown")
        assert response.text == (INSTALL_DIR / "INSTALL.md").read_text(
            encoding="utf-8"
        )

    def test_no_accept_header_gets_markdown(self, client):
        response = client.get("/install", headers={"Accept": ""})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/markdown")

    def test_browser_accept_gets_html_wrapper(self, client):
        response = client.get(
            "/install",
            headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        # The wrapper's one job: hand the URL to a coding agent, then show
        # the primer verbatim (escaped). The CTA names the host that served
        # the page, so the copied prompt resolves at every deployment stage
        # (registry.auto.network before bare-domain DNS lands, auto-9q7a5).
        assert "Tell your coding agent" in response.text
        assert "Please install Autonomy from https://testserver/install" in response.text
        # The primer body itself still names the canonical address.
        assert "auto.network/install" in response.text
        assert "Your contract as the installing agent" in response.text

    def test_cta_falls_back_to_canonical_on_junk_host(self, client):
        response = client.get(
            "/install",
            headers={"Accept": "text/html", "Host": "evil host\"<script>"},
        )
        assert response.status_code == 200
        assert "https://auto.network/install" in response.text
        assert "evil host" not in response.text
        # Self-contained: the CSP forbids every external origin.
        assert "default-src 'none'" in response.headers["content-security-policy"]

    def test_no_store_and_nosniff_on_both_shapes(self, client):
        for accept in ("*/*", "text/html"):
            response = client.get("/install", headers={"Accept": accept})
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"


class TestInstallSubDocuments:
    def test_deploy_copies_canonical_install_tree(self):
        """The production service is an rsync tree, not a full checkout."""
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        assert '"$REPO_ROOT/deploy/install"' in script
        assert '"$TARGET:$APP_DIR/deploy/"' in script

    def test_deploy_copies_the_shared_browser_relaykit_core(self):
        """The registry runtime is an rsync tree, so the shared JS is explicit."""
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        assert 'tools/dashboard/static/js/lib/relaykit-core.js' in script
        assert '$APP_DIR/tools/dashboard/static/js/lib/relaykit-core.js' in script

    def test_every_repo_doc_is_served(self, client):
        docs = sorted(
            p.relative_to(INSTALL_DIR).as_posix()
            for p in INSTALL_DIR.rglob("*.md")
        )
        assert docs, "deploy/install/ must carry the primer content"
        for doc in docs:
            response = client.get(f"/install/{doc}")
            assert response.status_code == 200, doc
            assert response.headers["content-type"].startswith("text/markdown")
            assert response.text == (INSTALL_DIR / doc).read_text(encoding="utf-8")

    def test_unknown_document_is_404(self, client):
        assert client.get("/install/paths/nonexistent.md").status_code == 404

    def test_traversal_and_non_markdown_are_refused(self, client):
        for path in (
            "../DEPLOY.md",
            "..%2F..%2Ftools%2Fdata_paths.py",
            "paths/../../Dockerfile",
            "INSTALL.md/../../entrypoint.sh",
            "verify",  # no suffix
        ):
            response = client.get(f"/install/{path}")
            assert response.status_code in (404, 400), path
            # Never leak file contents on a refused path.
            assert "REPO_ROOT" not in response.text
