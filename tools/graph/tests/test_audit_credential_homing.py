"""The credential-homing audit finds what the structural guarantee misses.

auto-ehyoh. "Secret-bearing sets are pinned max=raw, so secrets never leak"
protects the sets somebody LABELLED secret. It says nothing about a set that
merely CONTAINS one. `autonomy.workspace` had no home, no band, and the
operator's GitHub tokens in an organization's shared store.

These test the audit's LOGIC on synthetic rows rather than asserting anything
about live data — a test that passes only because this machine happens to be
clean would go green on the very estate the audit exists to inspect.
"""
from __future__ import annotations

from tools.graph.audit_credential_homing import (
    Finding,
    audit,
    credential_shaped_fields,
)

TOKENISH = "ghp_" + "a" * 36
OPAQUE = "b" * 40


# ── what counts as credential-shaped ─────────────────────────


def test_a_credential_nested_under_env_is_found():
    """The live finding was `env.GH_TOKEN` — one level down, not top level."""
    found = list(credential_shaped_fields({"env": {"GH_TOKEN": OPAQUE}}))

    assert found == ["env.GH_TOKEN"]


def test_a_credential_inside_a_list_is_found():
    found = list(credential_shaped_fields(
        {"repos": [{"name": "x"}, {"auth_token": TOKENISH}]}))

    assert found == ["repos[1].auth_token"]


def test_the_field_path_is_yielded_and_never_the_value():
    """An audit that must be trusted with secrets to find them is not one
    anybody will run."""
    payload = {"env": {"GH_TOKEN": OPAQUE}}

    for found in credential_shaped_fields(payload):
        assert OPAQUE not in found


def test_a_content_hash_is_not_a_credential():
    """Shape alone matches every SHA and UUID in the estate and drowns the
    signal; the name has to agree."""
    assert list(credential_shaped_fields({"head_sha": "c" * 40})) == []
    assert list(credential_shaped_fields({"design_id": "d" * 36})) == []
    assert list(credential_shaped_fields({"key_id": "e" * 40})) == []


def test_a_short_value_in_a_secret_named_field_is_not_flagged():
    """`token: "none"` is a placeholder, not material."""
    assert list(credential_shaped_fields({"api_token": "none"})) == []


def test_a_container_path_is_not_a_credential():
    """Both non-credential entries on the live rows were container paths."""
    assert list(credential_shaped_fields(
        {"env": {"ANCHORE_SSH_PRIV_KEY_PATH": "/etc/autonomy/artifacts/id_ed25519"}}
    )) == []


# ── protected vs not ─────────────────────────────────────────


def test_a_set_with_neither_a_personal_home_nor_a_band_is_unprotected():
    """THE ONE THAT MATTERS. This is exactly `autonomy.workspace`'s shape and
    exactly what every previous audit missed: it classified declarations and
    row counts, never values."""
    finding = Finding(
        set_id="autonomy.workspace", store="anchore", key="scale-harness",
        field_path="env.GH_TOKEN", home="organization", band=None,
    )

    assert finding.protected is False


def test_personal_homed_is_protected():
    """Personal keeps it off other people's machines — a sync boundary."""
    assert Finding("s", "personal", "k", "f", home="personal", band=None).protected


def test_band_pinned_is_protected():
    """A band keeps it off the federated read surface — a read boundary."""
    assert Finding("s", "anchore", "k", "f", home="organization",
                   band=("raw", "raw")).protected


def test_an_undeclared_set_holding_a_credential_is_reported():
    """Undeclared asserts nothing, so it cannot be treated as safe."""
    findings = audit([("some.set", "anchore", "k", {"env": {"GH_TOKEN": OPAQUE}})])

    assert len(findings) == 1
    assert findings[0].field_path == "env.GH_TOKEN"
    assert findings[0].protected is False


def test_a_row_with_no_credential_produces_nothing():
    findings = audit([("autonomy.workspace", "anchore", "docs",
                       {"name": "Docs", "image": "img"})])

    assert findings == []
