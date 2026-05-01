"""Tests for the typed payload schemas declared in
:mod:`agents.capabilities.github.schemas`.

The schemas exist as the codegen source for ``capability-github.d.ts``
and as the import-time drift gate that future consumers
(``service.py`` / ``probe.py``) will hook into via
``link_dataclass_to_schema``. This commit covers the substrate:

* schema metadata sanity (field-set pinning, enum lists, element
  shapes for nested types);
* round-trip validation: a hand-built payload dict matching the
  documented ``.to_dict()`` shape validates without raising;
* link-helper behavior (drift gate raises on field-set mismatch,
  rejects non-dataclasses);
* live ``capability-github.d.ts`` byte-equality with what
  ``render_dts`` produces from the live ``CODEGEN_SCHEMAS`` tuple.

Consumer adoption (service.py / probe.py linking + worktrees.js JSDoc
imports) lands in the follow-up commit; tests for the runtime link
land alongside it.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from agents.capabilities.github import probe, service
from agents.capabilities.github.schemas import (
    CheckEntryV1,
    CODEGEN_SCHEMAS,
    ProbeResultV1,
    ReviewPayloadV1,
    WorktreeGithubExecResultV1,
    link_dataclass_to_schema,
)
from tools.graph import typegen_cmd
from tools.graph.typegen_cmd import SchemaSource, render_dts


# ── Schema metadata sanity ──────────────────────────────────


def test_codegen_schemas_tuple_contains_every_v1_class():
    """``CODEGEN_SCHEMAS`` is the canonical list typegen consumes;
    deletions or additions must update the tuple. Pin it explicitly.
    """
    assert CODEGEN_SCHEMAS == (
        CheckEntryV1,
        ReviewPayloadV1,
        WorktreeGithubExecResultV1,
        ProbeResultV1,
    )


def test_check_entry_field_set():
    assert set(CheckEntryV1._field_metadata) == {
        "id", "icon", "label", "status", "detail",
    }
    assert CheckEntryV1._field_metadata["status"]["enum"] == [
        "pass", "fail", "running", "pending",
    ]


def test_review_payload_field_set():
    expected = {
        "number", "url", "title", "body", "head_sha", "base_sha",
        "base_branch", "state", "is_draft", "aggregate_state",
        "running", "checks", "commit_shas",
    }
    assert set(ReviewPayloadV1._field_metadata) == expected
    assert ReviewPayloadV1._field_metadata["state"]["enum"] == [
        "open", "closed", "merged", "unknown",
    ]
    assert ReviewPayloadV1._field_metadata["aggregate_state"]["enum"] == [
        "green", "yellow",
    ]


def test_review_payload_checks_element_carries_full_check_shape():
    """The ``checks`` array element must mirror ``CheckEntryV1`` so the
    generated ``capability-github.d.ts`` renders an inline object
    literal that JS consumers can typecheck against the standalone
    ``CheckEntryV1`` interface.
    """
    element = ReviewPayloadV1._field_metadata["checks"]["element"]
    assert set(element.keys()) == {"id", "icon", "label", "status", "detail"}
    assert element["status"]["enum"] == ["pass", "fail", "running", "pending"]


def test_exec_result_field_set():
    assert set(WorktreeGithubExecResultV1._field_metadata) == {
        "operation", "session_name", "repo_name", "ok", "stdout", "stderr",
        "exit_code", "timed_out", "container_name", "branch", "repo_slug",
        "command", "failure", "error_message",
    }


def test_probe_result_field_set_and_state_enum():
    assert set(ProbeResultV1._field_metadata) == {
        "contract", "contract_version", "implementation",
        "implementation_version", "delivery_mode", "state", "reason",
        "missing_tools", "missing_env", "missing_secret_files", "details",
    }
    assert ProbeResultV1._field_metadata["state"]["enum"] == [
        "ready", "unavailable", "degraded",
    ]
    assert ProbeResultV1._field_metadata["delivery_mode"]["enum"] == [
        "image_baked", "mounted_tools", "host_proxy", "hybrid",
    ]


# ── Round-trip validation ───────────────────────────────────


def test_check_entry_payload_validates():
    CheckEntryV1.validate({
        "id": "ci/build",
        "icon": "BU",
        "label": "build",
        "status": "pass",
        "detail": None,
    })


def test_review_payload_validates():
    ReviewPayloadV1.validate({
        "number": 303,
        "url": "https://example.com/pr/303",
        "title": "Add capability layer",
        "body": "Body…",
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "base_branch": "master",
        "state": "open",
        "is_draft": False,
        "aggregate_state": "green",
        "running": False,
        "checks": [
            {
                "id": "ci/build",
                "icon": "BU",
                "label": "build",
                "status": "pass",
                "detail": None,
            },
        ],
        "commit_shas": ["a" * 40],
    })


def test_exec_result_payload_validates():
    WorktreeGithubExecResultV1.validate({
        "operation": "source_control_review_read_v1",
        "session_name": "auto-test",
        "repo_name": "autonomy",
        "ok": True,
        "stdout": "[]",
        "stderr": "",
        "exit_code": 0,
        "timed_out": False,
        "container_name": "autonomy-agent_auto-test",
        "branch": "session/auto-test",
        "repo_slug": "auto-network/autonomy",
        "command": ["docker", "exec", "..."],
        "failure": None,
        "error_message": None,
    })


def test_probe_result_payload_validates():
    ProbeResultV1.validate({
        "contract": "source_control",
        "contract_version": 1,
        "implementation": "autonomy/github",
        "implementation_version": 1,
        "delivery_mode": "image_baked",
        "state": "ready",
        "reason": None,
        "missing_tools": [],
        "missing_env": [],
        "missing_secret_files": [],
        "details": {},
    })


# ── link_dataclass_to_schema ────────────────────────────────


def test_link_helper_sets_payload_schema_when_fields_match():
    @dataclass
    class _Match:
        id: str = ""
        label: str = ""

    class _MatchSchema:
        _field_metadata = {
            "id": {"type": "string"},
            "label": {"type": "string"},
        }

    link_dataclass_to_schema(_Match, _MatchSchema)
    assert _Match.payload_schema is _MatchSchema


def test_link_helper_rejects_field_drift_only_on_schema():
    @dataclass
    class _Drifted:
        a: str = ""

    class _DriftedSchema:
        _field_metadata = {
            "a": {"type": "string"},
            "b": {"type": "string"},
        }

    with pytest.raises(RuntimeError, match="only on schema"):
        link_dataclass_to_schema(_Drifted, _DriftedSchema)


def test_link_helper_rejects_field_drift_only_on_dataclass():
    @dataclass
    class _Drifted:
        a: str = ""
        b: str = ""

    class _DriftedSchema:
        _field_metadata = {"a": {"type": "string"}}

    with pytest.raises(RuntimeError, match="only on dataclass"):
        link_dataclass_to_schema(_Drifted, _DriftedSchema)


def test_link_helper_rejects_non_dataclass():
    class _NotADataclass:
        a: str = ""

    class _Schema:
        _field_metadata = {"a": {"type": "string"}}

    with pytest.raises(RuntimeError, match="not a dataclass"):
        link_dataclass_to_schema(_NotADataclass, _Schema)


# ── Runtime dataclass linkage (consumer adoption) ───────────


def test_service_dataclasses_are_linked_to_their_schemas():
    """``service.py`` calls ``link_dataclass_to_schema`` at module
    import time for every runtime dataclass it owns. If anyone deletes
    those calls (or the dataclass / schema field sets drift), the
    import itself raises — and ``payload_schema`` would be missing
    here. Pin the link so the regression surfaces immediately.
    """
    assert service.WorktreeGithubExecResult.payload_schema is WorktreeGithubExecResultV1
    assert service.CheckEntry.payload_schema is CheckEntryV1
    assert service.ReviewPayload.payload_schema is ReviewPayloadV1


def test_probe_result_is_linked_to_its_schema():
    """Same contract as the service link, scoped to ``probe.py``."""
    assert probe.ProbeResult.payload_schema is ProbeResultV1


def test_real_dataclass_round_trips_through_its_payload_schema():
    """The runtime dataclass's own ``.to_dict()`` output must validate
    against the linked schema. Catches drift between the dataclass's
    serialization and the schema's field metadata even when
    field-name parity is intact (e.g. value-shape skew on enums).
    """
    entry = service.CheckEntry(
        id="ci/build", icon="BU", label="build", status="pass",
    )
    entry.payload_schema.validate(entry.to_dict())

    result = probe.ProbeResult(
        contract="source_control",
        contract_version=1,
        implementation="autonomy/github",
        implementation_version=1,
        delivery_mode="image_baked",
        state="ready",
    )
    result.payload_schema.validate(result.to_dict())


# ── Codegen drift gate ──────────────────────────────────────


CAPABILITY_DTS = (
    typegen_cmd.DEFAULT_OUTPUT_ROOT / "capability-github.d.ts"
)


def _capability_regen_command() -> str:
    refs = ",".join(
        f"agents.capabilities.github.schemas:{cls.__name__}"
        for cls in CODEGEN_SCHEMAS
    )
    return f"graph set typegen --schemas {refs} --name capability-github"


def test_capability_github_dts_matches_live_schemas():
    """The committed ``capability-github.d.ts`` must equal what
    ``render_dts`` produces from the live ``CODEGEN_SCHEMAS`` tuple.
    Drift fails the test suite immediately, mirroring the live drift
    gate the typegen module already runs against plugin manifests.
    """
    sources = [
        SchemaSource(
            ref=f"agents.capabilities.github.schemas:{cls.__name__}",
            cls_name=cls.__name__,
            payload=cls.export_json_schema(),
        )
        for cls in CODEGEN_SCHEMAS
    ]
    expected = render_dts(
        "capability-github",
        sources,
        regen_command=_capability_regen_command(),
    )
    assert CAPABILITY_DTS.exists(), (
        f"missing {CAPABILITY_DTS} — run `{_capability_regen_command()}`"
    )
    assert CAPABILITY_DTS.read_text() == expected, (
        f"capability-github.d.ts is out of date — run "
        f"`{_capability_regen_command()}` to refresh"
    )
