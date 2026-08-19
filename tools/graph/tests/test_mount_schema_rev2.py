"""auto-fteke: autonomy.workspace.mount#2 — a guarded `subpath` ADDED alongside a
deprecated `host_path` fallback (exactly one per row), with traversal guards on
both sides of a subpath bind, a required file|dir kind for subpath rows, and the
name/help tiles folded in from the retired artifact mechanism. The 1->2
upconverter is the IDENTITY (host_path kept, so a rev-1 payload is a valid rev-2
payload), which keeps existing rev-1 rows from dropping on a rev-2 read; legacy
rows keep rev-1 validation (absolute-only, exact spelling)."""

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


def test_identity_upconverter_1_to_2_preserves_host_path_rows():
    # rev 2 KEEPS host_path as a deprecated field, so a rev-1 payload IS a valid
    # rev-2 payload and the 1->2 hop is the IDENTITY (pure by construction) — NOT
    # the impossible host_path->subpath transform the design rejected. Registered
    # so rev-1 rows survive a rev-2 read instead of dropping (the drop that broke
    # 12 rows on the first merge).
    chain = upconvert_chain(SET_ID, 1, 2)
    assert chain is not None and len(chain) == 1
    rev1 = {"host_path": "/abs/x", "container_path": "/opt/x",
            "mode": "ro", "required": True}
    up = chain[0](rev1)
    _rev2().validate(up)                       # upconverted rev-1 validates as rev-2
    assert up["host_path"] == "/abs/x" and "subpath" not in up


def test_legacy_host_path_row_is_a_valid_rev2_payload():
    _rev2().validate({"host_path": "/abs/x", "container_path": "/opt/x",
                      "mode": "ro", "required": True})


@pytest.mark.parametrize("bad,why", [
    ({"container_path": "/opt/x"}, "neither host_path nor subpath"),
    ({"host_path": "/a", "subpath": "b", "container_path": "/opt/x", "kind": "dir"}, "both"),
    ({"subpath": "x", "container_path": "/opt/x"}, "subpath without kind"),
    ({"host_path": "/a", "container_path": "/opt/x", "kind": "dir"}, "host_path with kind"),
    ({"host_path": "relative", "container_path": "/opt/x"}, "host_path not absolute"),
])
def test_exactly_one_source_and_kind_iff_subpath(bad, why):
    _reject(bad)


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
def test_extra_keys_forbidden_org_and_scope():
    # host_path is a VALID (deprecated) field now, so it is not tested here; adding
    # it to a subpath row is rejected by the exactly-one rule, not extra=forbid.
    _reject(_ok(org="sneaky-org"))
    _reject(_ok(scope="personal-org"))


# ── name/help tiles folded in from the artifact mechanism ───────────────────
def test_name_and_help_accepted_name_is_capped():
    _rev2().validate(_ok(name="Anchore Enterprise license",
                         help="Get it from the portal; place under personal/scale-harness/."))
    _reject(_ok(name="x" * 61))  # a title, not a 100-char sentence
