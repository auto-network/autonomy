"""auto-fteke: autonomy.workspace.mount#2 — subpath replaces host_path, with
traversal guards on BOTH sides of the bind, a required file|dir kind, and the
name/help tiles folded in from the retired artifact mechanism. Breaking change:
no 1->2 upconverter, so rev-1 rows drop rather than resolve against the wrong
physical storage."""

import pytest

from tools.graph.schemas import mount  # noqa: F401 — registers the schemas
from tools.graph.schemas.registry import (
    get_schema,
    upconvert_chain,
    readiness_gate,
    SchemaValidationError,
    declared_home,
)

SET_ID = "autonomy.workspace.mount"


def _rev2():
    return get_schema(SET_ID, 2)


def _ok(**over):
    payload = {
        "subpath": "personal/scale-harness/license.yaml",
        "container_path": "/etc/autonomy/artifacts/license.yaml",
        "kind": "file",
    }
    payload.update(over)
    return payload


def _reject(payload):
    with pytest.raises(SchemaValidationError):
        _rev2().validate(payload)


# ── registration / breaking-change contract ────────────────────────────────
def test_both_revisions_register_and_homes_agree():
    assert get_schema(SET_ID, 1) is not None
    assert get_schema(SET_ID, 2) is not None
    assert declared_home(SET_ID) == "organization"


def test_rev2_is_readiness_gated_by_required():
    # AC4: @readiness_gated_by("required") carries forward to rev 2 — a generic
    # readiness check reads it to tell a missing optional mount from a broken one.
    assert readiness_gate(SET_ID, 2) == "required"


def test_no_upconverter_1_to_2_so_rev1_rows_drop():
    # A breaking change is expressed by registering NO upconverter for the hop
    # (registry convention): upconvert_chain returns None when a hop is missing,
    # so a caller requesting rev 2 drops a rev-1 row rather than resolving
    # host_path against different physical storage.
    assert upconvert_chain(SET_ID, 1, 2) is None


# ── the source-side (subpath) guard ─────────────────────────────────────────
def test_subpath_accepts_file_and_dir_shaped_relative_paths():
    _rev2().validate(_ok(subpath="vuln-diff-validation", kind="dir"))
    _rev2().validate(_ok(subpath="personal/scale-harness/license.yaml", kind="file"))


@pytest.mark.parametrize("bad", [
    "/absolute/x", "../../root/.ssh", "a/../../b",
    "a//b", "a/b/", "a/./b", "",
])
def test_subpath_rejects_absolute_and_traversal_and_unnormalized(bad):
    _reject(_ok(subpath=bad))


# ── the destination-side (container_path) guard — the point-4 addition ──────
@pytest.mark.parametrize("bad", [
    "relative/x", "/etc/../root/.ssh/authorized_keys", "/etc//x", "/etc/x/", "/etc/./x",
])
def test_container_path_rejects_relative_and_traversal_and_unnormalized(bad):
    _reject(_ok(container_path=bad))


# ── kind: required, file|dir only ───────────────────────────────────────────
def test_kind_is_required_and_undefaulted():
    _reject({"subpath": "license.yaml", "container_path": "/etc/x"})  # no kind


@pytest.mark.parametrize("bad", ["symlink", "device", "", "FILE"])
def test_kind_must_be_file_or_dir(bad):
    _reject(_ok(kind=bad))


# ── no payload field may name an org ────────────────────────────────────────
def test_extra_keys_forbidden_including_host_path_and_org_and_scope():
    _reject(_ok(host_path="/abs/x"))   # rev-1 field is now an unknown key
    _reject(_ok(org="sneaky-org"))
    _reject(_ok(scope="personal-org"))


# ── name/help tiles folded in from the artifact mechanism ───────────────────
def test_name_and_help_accepted_name_is_capped():
    _rev2().validate(_ok(name="Anchore Enterprise license",
                         help="Get it from the portal; place under personal/scale-harness/."))
    _reject(_ok(name="x" * 61))  # a title, not a 100-char sentence
