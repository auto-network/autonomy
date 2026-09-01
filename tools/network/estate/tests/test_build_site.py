"""build-site.sh — the one-command site provisioner — includes the two
steps a fresh box needs that pure provisioning does not: the 53 firewall
and the public-smoke skip (a bare box has no public edge to smoke)."""
from __future__ import annotations
from pathlib import Path

ESTATE = Path(__file__).resolve().parents[1]
BUILD = ESTATE / "build-site.sh"
REG_DEPLOY = ESTATE.parents[0] / "registry" / "deploy" / "deploy.sh"


def test_opens_dns_firewall_before_dns_deploy():
    s = BUILD.read_text()
    # Match the actual invocations (leading ./), not the header comment.
    fw = s.index("./dns/ensure-dns-firewall.sh")
    dns = s.index("./dns/deploy.sh")
    assert fw < dns  # 53 open before the DNS deploy proves an answer


def test_skips_public_smoke_on_the_fresh_box():
    assert "SKIP_PUBLIC_SMOKE=1" in BUILD.read_text()


def test_registry_deploy_honors_the_skip_guard():
    s = REG_DEPLOY.read_text()
    assert 'if [ -n "${SKIP_PUBLIC_SMOKE:-}" ]; then' in s
    # The guard wraps the smoke INVOCATION (last mention), not the loopback
    # /healthz proof; the first "smoke.py" is in the header comment.
    assert s.index("SKIP_PUBLIC_SMOKE") < s.rindex("smoke.py")


def test_gates_are_printed_not_hidden():
    s = BUILD.read_text()
    assert "--public-edge" in s          # public TLS edge is opt-in
    assert "add-delegation" in s         # delegation named as operator gate
