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
    """existing_row_id decrypts nothing, so readiness answers while cold."""
    from tools.dashboard import vault_seal_central

    probed = []
    monkeypatch.setattr(vault_seal_central, "existing_row_id",
                        lambda set_id, key: probed.append((set_id, key)) or None)
    findings = WorkspaceV1.readiness_findings(
        key="widgets-ng", payload={"name": "w", "image": "i", "vault_links": [LINK]},
        org="anchore", read=None)
    assert probed == [("autonomy.vault.audited", "anchore:docker-config")]
    assert [f.subject for f in findings] == ["anchore:docker-config"]


def test_the_remediation_is_registered_for_the_kind_with_no_params():
    from tools.graph.remediation import validate_registered_ref

    (finding,) = vault_link_findings([LINK], "anchore", exists=lambda key: False)
    assert validate_registered_ref({"id": finding.remediation_id,
                                    "params": finding.remediation_params}) == ()
