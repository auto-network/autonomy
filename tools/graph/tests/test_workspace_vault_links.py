"""autonomy.workspace vault_links (auto-2eqpb): the declaration's shape, and
readiness reporting an entry the audited vault lacks as
missing_vault_credential, answered without decrypting anything."""

from __future__ import annotations

import pytest

from tools.graph.schemas.registry import SchemaValidationError
from tools.graph.schemas.workspace import (
    WorkspaceV1, WorkspaceV2, vault_link_findings,
)

LINK = {"vault": "docker-config", "path": "/etc/autonomy/artifacts/docker-config.json",
        "name": "Docker config", "description": "Anchore registry auth",
        "help": "Ask the operator to seal it"}


@pytest.mark.parametrize("schema", [WorkspaceV1, WorkspaceV2])
def test_a_vault_link_validates_on_both_revisions(schema):
    schema.validate({"name": "w", "image": "i", "vault_links": [LINK]})


@pytest.mark.parametrize("bad", [
    {**LINK, "vault": "anchore:docker-config"},      # an org prefix reaches another org
    {**LINK, "vault": ""},
    {**LINK, "path": "relative/path"},
    {**LINK, "path": "/etc/../root/x"},
    {**LINK, "path": "/etc/autonomy//x"},
    {**LINK, "required": "yes"},
    {**LINK, "name": "x" * 61},
    {**LINK, "extra": True},
    {"path": "/a"},
])
def test_a_malformed_vault_link_is_refused(bad):
    with pytest.raises(SchemaValidationError):
        WorkspaceV1.validate({"name": "w", "image": "i", "vault_links": [bad]})


def test_two_links_may_not_share_an_entry_or_a_path():
    with pytest.raises(SchemaValidationError):
        WorkspaceV1.validate({"name": "w", "image": "i", "vault_links": [
            LINK, {**LINK, "path": "/other"}]})
    with pytest.raises(SchemaValidationError):
        WorkspaceV1.validate({"name": "w", "image": "i", "vault_links": [
            LINK, {**LINK, "vault": "other"}]})


def test_an_absent_entry_is_missing_vault_credential_naming_it():
    optional = {**LINK, "vault": "enterprise-license",
                "path": "/etc/autonomy/artifacts/license.yaml", "required": False}
    present = {"anchore:enterprise-license"}
    (finding,) = vault_link_findings([LINK, optional], "anchore",
                                     exists=lambda key: key in present)
    assert finding.kind == "missing_vault_credential"
    assert finding.severity == "blocking"
    assert finding.subject == "anchore:docker-config"
    assert finding.field == "vault_links[0]"
    assert "Docker config — Anchore registry auth" in finding.detail
    assert "Ask the operator to seal it" in finding.detail
    assert finding.remediation_id == "workspace.env.credential.v1"
    assert finding.remediation_params == {}


def test_an_optional_absent_entry_is_advisory():
    optional = {**LINK, "required": False}
    (finding,) = vault_link_findings([optional], "anchore", exists=lambda key: False)
    assert finding.severity == "advisory"


def test_the_readiness_hook_probes_existence_without_opening(monkeypatch):
    """The live-base-row probe decrypts nothing, so readiness answers while
    the vault is cold; the schema layer does not reach into the dashboard."""
    from tools.graph import settings_ops

    probed = []
    monkeypatch.setattr(settings_ops, "_existing_base_id",
                        lambda set_id, rev, key, org: probed.append((set_id, key, org)) or None)
    findings = WorkspaceV1.readiness_findings(
        key="widgets-ng", payload={"name": "w", "image": "i", "vault_links": [LINK]},
        org="anchore", read=None)
    assert probed == [("autonomy.vault.audited", "anchore:docker-config", None)]
    assert [f.subject for f in findings] == ["anchore:docker-config"]


@pytest.mark.parametrize("org, key", [
    ("anchore", "anchore:docker-config"),
    ("personal", "docker-config"),       # the operator's own entries are bare
    ("machine", "docker-config"),
])
def test_the_key_matches_how_the_write_side_names_the_entry(org, key):
    from tools.graph.schemas.workspace import vault_link_key

    assert vault_link_key(org, "docker-config") == key
    (finding,) = vault_link_findings([LINK], org, exists=lambda k: False)
    assert finding.subject == key


def test_the_remediation_is_registered_for_the_kind_with_no_params():
    from tools.graph.remediation import validate_registered_ref

    (finding,) = vault_link_findings([LINK], "anchore", exists=lambda key: False)
    assert validate_registered_ref({"id": finding.remediation_id,
                                    "params": finding.remediation_params}) == ()


def test_findings_carry_the_links_display_text_as_fields():
    """The doctor and the settings cards read name/description/help
    directly, never parse detail."""
    (finding,) = vault_link_findings([LINK], "anchore", exists=lambda key: False)
    assert (finding.name, finding.description, finding.help) == (
        "Docker config", "Anchore registry auth", "Ask the operator to seal it")


def test_an_unreadable_store_is_unreadable_vault_not_missing():
    """Failing to read the store is not "no such entry": telling the
    operator to seal what may already be sealed would be wrong."""
    def broken(key):
        raise OSError("database is locked")

    (finding,) = vault_link_findings([LINK], "anchore", exists=broken)
    assert finding.kind == "unreadable_vault"
    assert finding.subject == "anchore:docker-config"
    assert "database is locked" in finding.detail
    # No "seal it" offered: the entry may already be sealed.
    assert (finding.remediation_id, finding.remediation_params) == ("", {})
    from tools.graph.remediation import get_remediation

    assert "unreadable_vault" not in get_remediation(
        "workspace.env.credential.v1").supported_finding_kinds
