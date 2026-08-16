"""A field can declare that its value is a key in another set.

That declaration is the whole input to the check. Nothing here knows what a
workspace, a repository or a credential is: the checker walks field metadata,
composes the key the declaration describes, and asks whether it exists. So a
capability cannot implement this check wrongly, and cannot forget to implement
it at all.

It reports rather than refuses. Writing a row before provisioning what it names
is a legitimate order to work in — the useful thing is to say which key is
still missing, by name, at the moment someone is in a position to act on it.
"""
from __future__ import annotations

import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    keyed_per_entity,
    validate_payload,
)
import tools.graph.schemas.secure_setting  # noqa: F401 — registers the target set


SECRETS = "autonomy.secure.setting"


@pytest.fixture
def acme(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    GraphDB.create_org_db("acme").close()
    yield
    GraphDB.close_all_pooled()


def _workspace(*hosts: str) -> dict:
    return {
        "name": "w",
        "image": "i",
        "repos": [
            {"host": host, "repo": f"o/r{i}", "mount": f"/w/r{i}"}
            for i, host in enumerate(hosts)
        ],
    }


def _provision(org: str, target_key: str) -> None:
    settings_ops.add_setting(
        SECRETS, 1, f"{org}:{target_key}",
        {
            "ciphertext_hex": "ab" * 40,
            "key_id": "c" * 64,
            "purpose": f"autonomy.secure-setting.v1|{org}|{target_key}|" + "d" * 64,
            "origin": "operator",
            "provisioned_at": 1.0,
        },
        org="personal",
    )


# ── the declared shape ───────────────────────────────────────


def test_a_list_entry_is_checked_against_its_declared_shape(acme):
    """The entry's shape is metadata now, not a validate() body."""
    base = {"name": "w", "image": "i"}
    ok = {"host": "github.com", "repo": "o/r", "mount": "/m"}

    validate_payload("autonomy.workspace", 1, dict(base, repos=[ok]))

    with pytest.raises(SchemaValidationError, match="undeclared field"):
        validate_payload("autonomy.workspace", 1,
                         dict(base, repos=[dict(ok, bogus=1)]))
    with pytest.raises(SchemaValidationError, match="must be an absolute path"):
        validate_payload("autonomy.workspace", 1,
                         dict(base, repos=[dict(ok, base_source="relative")]))


def test_a_repository_is_named_one_way_or_the_other(acme):
    """On a git host, or local. Never both, never neither."""
    base = {"name": "w", "image": "i"}

    validate_payload("autonomy.workspace", 1, dict(base, repos=[
        {"host": "github.com", "repo": "o/r", "mount": "/m"}]))
    validate_payload("autonomy.workspace", 1, dict(base, repos=[
        {"local_path": "/home/j/mirror", "mount": "/m"}]))

    with pytest.raises(SchemaValidationError, match="one or the other"):
        validate_payload("autonomy.workspace", 1, dict(base, repos=[
            {"host": "h", "repo": "o/r", "local_path": "/p", "mount": "/m"}]))
    with pytest.raises(SchemaValidationError, match="needs both"):
        validate_payload("autonomy.workspace", 1, dict(base, repos=[
            {"host": "h", "mount": "/m"}]))
    with pytest.raises(SchemaValidationError, match="names no repository"):
        validate_payload("autonomy.workspace", 1, dict(base, repos=[
            {"mount": "/m"}]))


# ── the reference check ──────────────────────────────────────


def test_every_unprovisioned_reference_is_named(acme):
    payload = _workspace("github.com", "github-autonomy")
    settings_ops.add_setting("autonomy.workspace", 1, "w", payload, org="acme")

    missing = settings_ops.unresolved_references(
        "autonomy.workspace", 1, payload, org="acme")

    assert [key for _, key in missing] == ["acme:github.com", "acme:github-autonomy"]
    assert {target for target, _ in missing} == {SECRETS}


def test_provisioning_one_leaves_only_the_other(acme):
    payload = _workspace("github.com", "github-autonomy")
    _provision("acme", "github.com")

    missing = settings_ops.unresolved_references(
        "autonomy.workspace", 1, payload, org="acme")

    assert [key for _, key in missing] == ["acme:github-autonomy"]


def test_two_repositories_on_one_host_need_one_credential(acme):
    """The address is the host, so they collapse to a single requirement."""
    payload = _workspace("github.com", "github.com")

    missing = settings_ops.unresolved_references(
        "autonomy.workspace", 1, payload, org="acme")

    assert [key for _, key in missing] == ["acme:github.com", "acme:github.com"]
    assert len({key for _, key in missing}) == 1


def test_one_organizations_credential_does_not_satisfy_another(acme):
    """The org is in the key because the store holds several organizations."""
    payload = _workspace("github.com")
    _provision("acme", "github.com")

    assert settings_ops.unresolved_references(
        "autonomy.workspace", 1, payload, org="acme") == []
    assert [key for _, key in settings_ops.unresolved_references(
        "autonomy.workspace", 1, payload, org="other")] == ["other:github.com"]


def test_a_local_repository_needs_no_credential(acme):
    """Answered by the data — it has no host — not by an omitted field."""
    payload = {"name": "w", "image": "i",
               "repos": [{"local_path": "/home/j/mirror", "mount": "/m"}]}

    assert settings_ops.unresolved_references(
        "autonomy.workspace", 1, payload, org="acme") == []


def test_an_unprovisioned_reference_does_not_block_the_write(acme):
    """Reported, not refused — the row may legitimately come first."""
    payload = _workspace("github.com")

    sid = settings_ops.add_setting("autonomy.workspace", 1, "w", payload, org="acme")

    assert sid
    assert settings_ops.unresolved_references(
        "autonomy.workspace", 1, payload, org="acme")


# ── genericity ───────────────────────────────────────────────


def test_the_check_knows_nothing_about_workspaces(acme):
    """Same machinery on a schema invented here, with its own field name."""
    @keyed_per_entity(key_strategy="probe_id")
    class Unrelated(SettingSchema):
        set_id = "probe.reference.unrelated"
        schema_revision = 1
        unlocks: str = field(
            required=True, description="a key in the operator's secrets",
            references=SECRETS, reference_scope="org",
        )

    payload = {"unlocks": "some.other.secret"}

    assert [key for _, key in settings_ops.unresolved_references(
        "probe.reference.unrelated", 1, payload, org="acme")] == [
        "acme:some.other.secret"]

    _provision("acme", "some.other.secret")
    assert settings_ops.unresolved_references(
        "probe.reference.unrelated", 1, payload, org="acme") == []
