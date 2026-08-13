"""Drift control for the agent-first install primer (auto-2dt9b).

The primer at deploy/install/INSTALL.md instructs agents with concrete
commands and environment names. Those facts have owners elsewhere in the
repo — docker-compose.yml, DEPLOY.md, deploy/verify-image.sh,
tools/portability. This suite fails when the primer and its owners drift,
so the served instructions can never silently diverge from what the
checkout actually does.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INSTALL_DIR = REPO / "deploy" / "install"
PRIMER = (INSTALL_DIR / "INSTALL.md").read_text(encoding="utf-8")
COMPOSE = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
DEPLOY_MD = (REPO / "DEPLOY.md").read_text(encoding="utf-8")


def test_compose_one_liner_matches_the_compose_header():
    """The primer's Path A is verbatim the sovereign one-liner the compose
    file itself documents."""
    for line in (
        "git clone <source you chose> autonomy && cd autonomy",
        "AUTONOMY_FIRST_ORG=myorg docker compose up -d",
        "https://localhost:8080",
    ):
        assert line in PRIMER
        assert line in COMPOSE


def test_env_names_exist_where_the_primer_says_they_do():
    for env in (
        "AUTONOMY_FIRST_ORG",
        "AUTONOMY_IMAGE",
        "AUTONOMY_BASE_IMAGE",
        "AUTONOMY_TAILWIND_URL",
        "DASHBOARD_TLS",
        "DASHBOARD_PORT",
    ):
        assert env in PRIMER, env
        assert env in COMPOSE, env
    # The invite path is docker-run based, exactly as DEPLOY.md documents
    # (compose does not pass AUTONOMY_INVITE through — auto-1orut tracks it).
    for env in ("AUTONOMY_INVITE", "AUTONOMY_PERSONAL_PASSWORD_FILE"):
        assert env in PRIMER, env
        assert env in DEPLOY_MD, env


def test_referenced_scripts_and_tools_exist():
    assert "./deploy/verify-image.sh" in PRIMER
    assert (REPO / "deploy" / "verify-image.sh").is_file()
    assert "tools.portability snapshot" in PRIMER
    portability = (REPO / "tools" / "portability.py").read_text(encoding="utf-8")
    for sub in ("snapshot", "restore", "migrate-on-mount"):
        assert f'"{sub}"' in portability or f"'{sub}'" in portability


def test_mutual_exclusivity_claim_matches_deploy_md():
    assert "mutually exclusive" in PRIMER
    assert "mutually exclusive" in DEPLOY_MD


def test_every_install_link_in_the_primer_resolves():
    """Every /install/<doc> the primer links must exist in deploy/install/ —
    a served primer must never point an agent at a 404."""
    linked = set(re.findall(r"\]\(/install/([\w/.-]+\.md)\)", PRIMER))
    assert linked, "the primer links its sub-documents"
    for doc in sorted(linked):
        assert (INSTALL_DIR / doc).is_file(), f"primer links missing doc: {doc}"


def test_loopback_join_endpoint_matches_deploy_md():
    assert "POST /api/identity/join" in PRIMER
    assert "/api/identity/join" in DEPLOY_MD
