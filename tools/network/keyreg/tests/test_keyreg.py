"""Tests for the key registry: the real data validates, the required
inventory is present, and validation errors are precise enough to act on."""

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyreg  # noqa: E402

# Every key the crib sheet's section 9 register names, plus the keys sections
# 2, 18, 23, and 24 define. This list is the coverage contract: a key removed
# from the registry without being removed here fails the suite.
REQUIRED_KEYS = [
    "personal_root_seed",
    "org_root_signing_key",
    "recovery_code",
    "recovery_signing_key",
    "member_recovery_key",
    "recovery_slot_recipient",
    "factor_seed",
    "factor_recipient",
    "master_kek",
    "per_credential_wrapping_key",
    "browser_session_key",
    "persona_signing_key",
    "persona_kem_private",
    "generation_secret",
    "object_wrap_key",
    "object_cek",
    "agent_delegate_signing_key",
    "per_machine_key",
    "serving_delegate_key",
    "root_anchor_seed",
    "class_key",
    "sealed_index",
    "k_index_k_meta",
    "pepper",
]


@pytest.fixture(scope="module")
def registry():
    return keyreg.load()


def test_registry_validates(registry):
    assert keyreg.validate(registry) == []


def test_required_keys_present(registry):
    missing = sorted(set(REQUIRED_KEYS) - set(registry["keys"]))
    assert not missing, f"registry.yaml is missing required keys: {missing}"


def test_every_key_has_complete_descriptor(registry):
    for key_id, entry in registry["keys"].items():
        for field in keyreg.DESCRIPTOR_FIELDS:
            assert entry["descriptor"].get(field, "").strip(), (
                f"{key_id}: descriptor field '{field}' is empty"
            )
        assert entry["custody"]["forced_by"].strip(), (
            f"{key_id}: custody.forced_by is empty"
        )


def test_built_keys_carry_code_anchors(registry):
    for key_id, entry in registry["keys"].items():
        if entry.get("status", "built") == "built":
            assert entry["code"], f"{key_id}: built key with no code anchor"


def test_code_anchor_files_exist(registry):
    repo_root = Path(__file__).resolve().parents[4]
    for key_id, entry in registry["keys"].items():
        for anchor in entry["code"]:
            path = anchor.split(":", 1)[0]
            assert (repo_root / path).is_file(), (
                f"{key_id}: code anchor file does not exist: {path}"
            )


def test_derivation_parents_resolve(registry):
    for child, parent, _fn in keyreg.derivation_edges(registry):
        assert parent in registry["keys"], (
            f"{child}: derivation parent '{parent}' is not a registered key"
        )


def test_reachable_walks_transitively(registry):
    from_root = keyreg.reachable(registry, "personal_root_seed")
    assert "persona_kem_private" in from_root
    assert "per_machine_key" in from_root
    from_code = keyreg.reachable(registry, "recovery_code")
    assert "member_recovery_key" in from_code
    assert "personal_root_seed" not in from_code


def test_unknown_key_id_error_lists_valid_ids(registry):
    with pytest.raises(KeyError) as exc:
        keyreg.key(registry, "no_such_key")
    assert "no_such_key" in str(exc.value)
    assert "personal_root_seed" in str(exc.value)


def test_missing_descriptor_field_error_names_entry_and_field(registry):
    broken = copy.deepcopy(registry)
    del broken["keys"]["per_machine_key"]["descriptor"]["snapshot"]
    errors = keyreg.validate(broken)
    assert any(
        "keys.per_machine_key.descriptor" in err and "snapshot" in err
        for err in errors
    ), errors


def test_bad_custody_class_error_names_entry_and_value(registry):
    broken = copy.deepcopy(registry)
    broken["keys"]["generation_secret"]["custody"]["class"] = "warm"
    errors = keyreg.validate(broken)
    assert any(
        "keys.generation_secret.custody.class" in err and "warm" in err
        for err in errors
    ), errors


def test_dangling_derivation_parent_is_an_error(registry):
    broken = copy.deepcopy(registry)
    broken["keys"]["member_recovery_key"]["derivation"]["parent"] = "ghost_key"
    errors = keyreg.validate(broken)
    assert any(
        "keys.member_recovery_key.derivation.parent" in err and "ghost_key" in err
        for err in errors
    ), errors


def test_built_key_without_code_is_an_error(registry):
    broken = copy.deepcopy(registry)
    broken["keys"]["class_key"]["code"] = []
    errors = keyreg.validate(broken)
    assert any(
        "keys.class_key.code" in err for err in errors
    ), errors


def test_schema_json_parses():
    import json

    schema = json.loads((Path(keyreg.SCHEMA_PATH)).read_text())
    assert schema["$id"] == "autonomy:keyreg:v1"
    assert set(schema["properties"]) == {"version", "keys", "mutations", "purposes"}


def test_jsonschema_agrees_when_available(registry):
    jsonschema = pytest.importorskip("jsonschema")
    import json

    schema = json.loads(Path(keyreg.SCHEMA_PATH).read_text())
    jsonschema.Draft202012Validator(schema).validate(registry)
