"""``graph workspace doctor`` renders the health report; it checks nothing.

What it owes a reader, per thing: what it is, where the requirement comes
from, how to obtain it -- and the same bytes for the same report, so two runs
can be diffed.
"""
from __future__ import annotations

from tools.graph.workspace_doctor_cmd import blocks, render


def _report():
    return {
        "org": "acme",
        "asked_in": "the platform host",
        "things": [
            {
                "kind": "machine_mount_elsewhere", "severity": "advisory",
                "subject": "widgets-ng:data", "name": "Fixture data",
                "what": "machine-local mount pinned to Home", "needed_by": ["NG"],
                "set_id": "autonomy.workspace.mount", "key": "widgets-ng:data",
                "looked_in": "the mount row's pinned machine",
                "remediation_id": "",
            },
            {
                "kind": "missing_capability_install", "severity": "blocking",
                "subject": "browser", "name": "browser",
                "description": "A shared, visible browser for a session.",
                "thing": "capability:browser",
                "what": "no organization installation",
                "reasons": [
                    {"kind": "missing_capability_install", "subject": "browser",
                     "what": "enabled contract 'browser' has no organization "
                             "installation", "severity": "blocking"},
                    {"kind": "missing_capability_contract_version",
                     "subject": "browser@1", "what": "enabled contract requires "
                     "browser@1, but no such contract row resolves",
                     "severity": "blocking"},
                ],
                "needed_by": ["NG", "UI"], "field": "contract",
                "set_id": "autonomy.org.capability.install", "key": "browser",
                "looked_in": "the organization's capability installations",
                "remediation_id": "capability.install-chain.v1",
            },
            {
                "kind": "missing_vault_credential", "severity": "blocking",
                "subject": "acme:docker-config", "name": "Docker login",
                "description": "Registry pull credentials",
                "help": "Ask the registry admin for a robot account",
                "what": "vault entry 'acme:docker-config' is not in the vault",
                "needed_by": ["NG"], "field": "vault_links[0]",
                "set_id": "autonomy.workspace", "key": "widgets-ng",
                "looked_in": "the operator's audited vault",
                "remediation_id": "workspace.env.credential.v1",
            },
        ],
        "workspaces": [
            {"id": "widgets-ui", "name": "UI", "ready": False, "unresolved": 1},
            {"id": "widgets-ng", "name": "NG", "ready": False, "unresolved": 2},
            {"id": "docs", "name": "docs", "ready": True, "unresolved": 0},
        ],
    }


def _flat(text):
    """Wrapping is layout; assert on the words."""
    return " ".join(text.split())


def test_each_thing_says_what_it_is_where_it_comes_from_and_how_to_get_it():
    text = _flat(render(_report()))

    assert "✗ browser — No organization installation" in text
    assert "A shared, visible browser for a session." in text
    # Both broken edges of the one capability are listed under it.
    assert "has no organization installation" in text
    assert "no such contract row resolves" in text
    assert "contract on autonomy.org.capability.install key 'browser'" in text
    # No authored help: the registered remediation says what to do.
    assert "Repair capability installation chain" in text
    # Authored help wins over the remediation's generic words.
    assert "Ask the registry admin for a robot account" in text
    assert "Provision workspace credential" not in text


def test_blocking_comes_before_advisory_and_workspaces_are_sorted():
    text = render(_report())

    assert text.index("Blocks launch") < text.index("Advisory")
    assert text.index("✗ browser") < text.index("✗ Docker login")
    listing = text[text.index("\nWorkspaces\n"):]
    assert listing.index("docs") < listing.index("widgets-ng") < listing.index("widgets-ui")
    assert "widgets-ng  NG — not ready (2 things)" in text
    assert "✓ docs — ready" in text


def test_same_report_same_bytes_whatever_the_input_order():
    report = _report()
    shuffled = {**report, "things": list(reversed(report["things"])),
                "workspaces": list(reversed(report["workspaces"]))}

    assert render(report) == render(shuffled)


def test_exit_status_follows_workspace_readiness():
    report = _report()
    assert blocks(report)
    ready = {**report, "things": [report["things"][0]],
             "workspaces": [{"id": "docs", "ready": True, "unresolved": 0}]}
    assert not blocks(ready)
    assert "Advisory" in render(ready)
