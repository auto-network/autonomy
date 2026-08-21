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


def test_repository_hosts_are_addresses_not_secure_setting_references(acme):
    payload = _workspace("github.com", "github-autonomy")
    settings_ops.add_setting("autonomy.workspace", 1, "w", payload, org="acme")

    assert settings_ops.unresolved_references(
        "autonomy.workspace", 1, payload, org="acme") == []


def test_a_local_repository_needs_no_credential(acme):
    """Answered by the data — it has no host — not by an omitted field."""
    payload = {"name": "w", "image": "i",
               "repos": [{"local_path": "/home/j/mirror", "mount": "/m"}]}

    assert settings_ops.unresolved_references(
        "autonomy.workspace", 1, payload, org="acme") == []


def test_an_unprovisioned_reference_does_not_block_the_write(acme):
    """Generic references are reported, not refused; rows may come first."""
    @keyed_per_entity(key_strategy="probe_id")
    class NeedsSecret(SettingSchema):
        set_id = "probe.reference.needs-secret"
        schema_revision = 1
        secret: str = field(
            required=True,
            description="a key in the operator's secrets",
            references=SECRETS,
            reference_scope="org",
        )
    payload = {"secret": "github.com"}

    sid = settings_ops.add_setting(
        "probe.reference.needs-secret", 1, "w", payload, org="acme",
    )

    assert sid
    assert settings_ops.unresolved_references(
        "probe.reference.needs-secret", 1, payload, org="acme")


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
