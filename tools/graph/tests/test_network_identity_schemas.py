"""Tests for the auto.network identity schemas (A2, bead auto-mjvj6).

Acceptance (spec graph://a17c8657-939 §4.6, §6.2, §6.6; invariants I1, I9):

* ``graph set schema / example / add`` round-trips for all three new
  set_ids (``autonomy.network.org-key#1``, ``autonomy.network.binding#1``,
  ``autonomy.network.link-grant#1``).
* operation_policy resolution: ``link.publish`` resolves ``prompt`` by
  default and ``delegated`` when overridden per org/workspace.
* No plaintext-key field exists anywhere in any registered schema
  (grep-pinned, I1).

Vocabulary is pinned against A1 (``tools/network/idkit``) so the schema
enums and the crypto library can never drift apart silently.
"""

from __future__ import annotations

import copy
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.graph import settings_ops as ops
from tools.graph.network_policy import (
    LINK_DEFAULT_MODE,
    LinkPolicyError,
    resolve_link_operation_mode,
)
from tools.graph.schemas import commit_signing_key  # noqa: F401 — include in the I1 sweep
from tools.graph.schemas import network_identity as ni
from tools.graph.schemas.commit_policy import (
    LINK_OPERATION_CLASSES,
    LINK_OPERATION_MODES,
    OPERATION_POLICY_REVISION,
    OPERATION_POLICY_SET_ID,
)
from tools.graph.schemas.registry import (
    SCHEMAS,
    SchemaValidationError,
    get_schema,
    validate_payload,
)
from tools.graph.set_cmd import _build_example


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db_path


def _flush_schema_meta(graph_db_env):
    """Materialize schema-meta rows into the pinned test DB.

    Schema-meta materialization is decoupled from connection open
    (auto-06ziz) and runs once at dashboard startup via
    ``flush_schema_meta_all_orgs``. Tests that read the ``autonomy.schema``
    meta set must flush the pinned ``GRAPH_DB`` file explicitly.
    """
    from tools.graph.db import GraphDB
    from tools.graph.schemas.registry import flush_schema_meta

    db = GraphDB(str(graph_db_env), mode="rw")
    try:
        flush_schema_meta(db)
    finally:
        db.close()


# ── valid payload factories ──────────────────────────────────


ROOT_PUB = "ab" * 32
TOKEN = "00112233445566778899aabbccddeeff"
TARGET_UUID = "9110a85b-0000-4000-8000-000000000000"
INVITE_REF = "bc" * 32


def _org_key_material():
    """A REAL canonical armor — the schema validator strict-parses it
    (Codex I1 finding: the gate lives at the schema layer, so fixture
    payloads must be genuine canonical armors, not look-alikes)."""
    from tools.network.idkit import KeyPair
    from tools.network.idkit.armor import encrypt_root_key

    root = KeyPair.generate()
    return root, encrypt_root_key(root, "test passphrase", iterations=10_000)


_ORG_KEY_ROOT, _ORG_KEY_ARMOR = _org_key_material()


def org_key_payload() -> dict:
    return {
        "armored_private_key": _ORG_KEY_ARMOR,
        "root_pub": _ORG_KEY_ROOT.public_hex,
    }


def binding_payload() -> dict:
    return {
        "org_uuid": "11111111-1111-4111-8111-111111111111",
        "root_pub": ROOT_PUB,
        "registry_url": "https://auto.network",
        "recovery_policy": {"mode": "recovery-key", "recovery_pub": "cd" * 32},
        "binding_expires_at": "2026-08-16T00:00:00Z",
        "last_renewed_at": "2026-07-17T00:00:00Z",
        "endpoint_hints": [{"url": "https://dashboard.tail1234.ts.net", "sig": "ef" * 64}],
    }


def link_grant_payload() -> dict:
    return {
        "token": TOKEN,
        "url": f"{ni.NETWORK_PUBLIC_LINK_BASE_URL}/l/{TOKEN}",
        "target_uuid": TARGET_UUID,
        "target_type": "present",
        "meta": {"ttl": 3600, "label": "OSS Insights binder", "require_auth": False},
        "subject": {"kind": "agent", "id": "auto-0716-220131"},
        "issued_at": "2026-07-17T00:00:00Z",
    }


def _mint_serve_cert(*, scope=("tunnel:serve",), org="2d4b90cb-0000-4000-8000-000000000000",
                     ttl=30 * 24 * 3600):
    """A REAL root-signed delegate — the validator strict-verifies the chain,
    so fixtures must be genuine certs, not look-alikes. Returns (root, cert)."""
    import time

    from tools.network.idkit import KeyPair, Subject, issue_cert
    root = KeyPair.generate()
    delegate = KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(
        root, delegate.public_hex, scope=scope, org=org,
        subject=Subject("persona", "ab" * 32),
        not_before=now - 300, not_after=now + ttl,
    )
    viewer_cert = issue_cert(
        root, delegate.public_hex, scope=scope, org=org,
        subject=Subject("operator", delegate.public_hex),
        not_before=cert.not_before, not_after=cert.not_after,
    )
    return root, cert, viewer_cert


def serve_cert_payload() -> dict:
    root, cert, viewer_cert = _mint_serve_cert()
    return {
        "cert": cert.to_json().decode("ascii"),
        "viewer_cert": viewer_cert.to_json().decode("ascii"),
        "key_path": "/var/lib/dashboard/network/serve-2d4b90cb.key",
        "root_pub": root.public_hex,
        "not_after": cert.not_after,
    }


ALL_SET_IDS = {
    ni.NETWORK_ORG_KEY_SET_ID: (ni.NETWORK_ORG_KEY_REVISION, org_key_payload),
    ni.NETWORK_BINDING_SET_ID: (ni.NETWORK_BINDING_REVISION, binding_payload),
    ni.NETWORK_LINK_GRANT_SET_ID: (ni.NETWORK_LINK_GRANT_REVISION, link_grant_payload),
    ni.NETWORK_SERVE_CERT_SET_ID: (ni.NETWORK_SERVE_CERT_REVISION, serve_cert_payload),
}


def test_serve_cert_valid_payload_validates():
    validate_payload(
        ni.NETWORK_SERVE_CERT_SET_ID, ni.NETWORK_SERVE_CERT_REVISION,
        serve_cert_payload(),
    )


def test_serve_cert_rejects_scope_without_tunnel_serve():
    root, cert, viewer_cert = _mint_serve_cert(scope=("link:publish",))
    payload = {
        "cert": cert.to_json().decode("ascii"),
        "viewer_cert": viewer_cert.to_json().decode("ascii"), "key_path": "/k",
        "root_pub": root.public_hex, "not_after": cert.not_after,
    }
    with pytest.raises(SchemaValidationError, match="tunnel:serve"):
        validate_payload(
            ni.NETWORK_SERVE_CERT_SET_ID, ni.NETWORK_SERVE_CERT_REVISION, payload)


def test_serve_cert_rejects_not_after_mismatch():
    payload = serve_cert_payload()
    payload["not_after"] += 5   # no longer mirrors the cert
    with pytest.raises(SchemaValidationError, match="not_after"):
        validate_payload(
            ni.NETWORK_SERVE_CERT_SET_ID, ni.NETWORK_SERVE_CERT_REVISION, payload)


def test_serve_cert_rejects_wrong_root():
    from tools.network.idkit import KeyPair
    payload = serve_cert_payload()
    payload["root_pub"] = KeyPair.generate().public_hex  # not the signer
    with pytest.raises(SchemaValidationError, match="chain to root_pub"):
        validate_payload(
            ni.NETWORK_SERVE_CERT_SET_ID, ni.NETWORK_SERVE_CERT_REVISION, payload)


def test_serve_cert_rejects_unparseable_cert():
    payload = serve_cert_payload()
    payload["cert"] = "{not a real cert}"
    with pytest.raises(SchemaValidationError, match="does not parse"):
        validate_payload(
            ni.NETWORK_SERVE_CERT_SET_ID, ni.NETWORK_SERVE_CERT_REVISION, payload)


# ── registration + happy-path validation ─────────────────────


def test_all_three_schemas_are_registered():
    for set_id, (revision, _) in ALL_SET_IDS.items():
        assert get_schema(set_id, revision) is not None, set_id


def test_valid_payloads_validate():
    for set_id, (revision, factory) in ALL_SET_IDS.items():
        validate_payload(set_id, revision, factory())


def test_vocabulary_is_pinned_to_idkit():
    """A1 is the vocabulary source of truth; these constants are local
    copies (so tools.graph never imports `cryptography` at startup) and
    must match idkit exactly."""
    from tools.network.idkit import SUBJECT_KINDS, TOKEN_HEX_LEN
    from tools.network.idkit.keys import PUBLIC_KEY_HEX_LEN

    assert set(ni.SUBJECT_KINDS_NETWORK) == set(SUBJECT_KINDS)
    assert ni.NETWORK_TOKEN_HEX_LEN == TOKEN_HEX_LEN
    assert ni.NETWORK_PUB_HEX_LEN == PUBLIC_KEY_HEX_LEN


# ── org-key rejections ────────────────────────────────────────


def test_org_key_requires_armor():
    with pytest.raises(SchemaValidationError):
        validate_payload(ni.NETWORK_ORG_KEY_SET_ID, 1, {})


def test_org_key_rejects_plaintext_hex_key_material():
    """I1 tripwire: a 64-hex string IS a raw Ed25519 private key."""
    payload = org_key_payload()
    payload["armored_private_key"] = "0f" * 32
    with pytest.raises(SchemaValidationError, match="plaintext|I1"):
        validate_payload(ni.NETWORK_ORG_KEY_SET_ID, 1, payload)


def test_org_key_rejects_malformed_root_pub():
    payload = org_key_payload()
    payload["root_pub"] = "AB" * 32  # uppercase
    with pytest.raises(SchemaValidationError):
        validate_payload(ni.NETWORK_ORG_KEY_SET_ID, 1, payload)


# ── binding rejections ────────────────────────────────────────


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda p: p.update(org_uuid="not-a-uuid"), "UUID"),
        (lambda p: p.update(root_pub="xyz"), "hex"),
        (lambda p: p.update(registry_url="ftp://auto.network"), "http"),
        (lambda p: p.update(registry_url=""), "non-empty"),
        (lambda p: p.update(recovery_policy={"mode": "org-vouch"}), "RESERVED"),
        (lambda p: p.update(recovery_policy={"mode": "blindhash-escrow"}), "RESERVED"),
        (lambda p: p.update(recovery_policy={"mode": "recovery-key"}), "recovery_pub"),
        (lambda p: p.update(recovery_policy={"mode": "none", "recovery_pub": "cd" * 32}),
         "only valid"),
        (lambda p: p.update(recovery_policy={"mode": "shrug"}), "mode"),
        (lambda p: p.update(recovery_policy="none"), "object"),
        (lambda p: p.update(binding_expires_at="tomorrow"), "ISO-8601"),
        (lambda p: p.update(last_renewed_at="2026-07-17 00:00:00"), "ISO-8601"),
        (lambda p: p.update(endpoint_hints=[{"sig": "aa"}]), "url"),
        (lambda p: p.update(endpoint_hints="https://x"), "list"),
        (lambda p: p.pop("binding_expires_at"), "binding_expires_at"),
    ],
)
def test_binding_rejects_malformed_payloads(mutate, match):
    payload = binding_payload()
    mutate(payload)
    with pytest.raises(SchemaValidationError, match=match):
        validate_payload(ni.NETWORK_BINDING_SET_ID, 1, payload)


# ── link-grant rejections ─────────────────────────────────────


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda p: p.update(token="abc123"), "32 lowercase hex"),
        (lambda p: p.pop("url"), "url"),
        (lambda p: p.update(url="http://relay.auto.network/l/" + TOKEN), "url"),
        (lambda p: p.update(url=ni.NETWORK_PUBLIC_LINK_BASE_URL + "/l/" + "f" * 32), "url"),
        (lambda p: p.update(token=TOKEN.upper()), "32 lowercase hex|non-empty"),
        (lambda p: p.update(token=TOKEN + "00"), "32|non-empty"),
        (lambda p: p.update(target_uuid="deck-9110a85b"), "UUID"),
        (lambda p: p.update(target_type="deck"), "target_type"),
        (lambda p: p.update(meta={"ttl": 0}), "positive"),
        (lambda p: p.update(meta={"ttl": "3600"}), "positive integer"),
        (lambda p: p.update(meta={"ttl": True}), "positive integer"),
        (lambda p: p.update(meta={"label": ""}), "label"),
        (lambda p: p.update(meta={"surprise": 1}), "unknown keys"),
        (lambda p: p.update(meta={"require_auth": "yes"}), "boolean"),
        (lambda p: p.update(subject={"kind": "agent"}), "subject"),
        (lambda p: p.update(subject={"kind": "wizard", "id": "x"}), "kind"),
        (lambda p: p.update(subject={"kind": "agent", "id": ""}), "subject.id"),
        (lambda p: p.pop("issued_at"), "issued_at"),
        (lambda p: p.update(issued_at="last tuesday"), "ISO-8601"),
    ],
)
def test_link_grant_rejects_malformed_payloads(mutate, match):
    payload = link_grant_payload()
    mutate(payload)
    with pytest.raises(SchemaValidationError, match=match):
        validate_payload(
            ni.NETWORK_LINK_GRANT_SET_ID,
            ni.NETWORK_LINK_GRANT_REVISION,
            payload,
        )


def test_link_grant_require_auth_true_is_reserved_rung_2():
    payload = link_grant_payload()
    payload["meta"]["require_auth"] = True
    with pytest.raises(SchemaValidationError, match="rung 2"):
        validate_payload(
            ni.NETWORK_LINK_GRANT_SET_ID,
            ni.NETWORK_LINK_GRANT_REVISION,
            payload,
        )


def test_link_grant_persona_subject_is_reserved_rung_2():
    """Persona exists in the enum vocabulary (pinned to idkit) but is
    rejected with a clear message until Track E lands."""
    payload = link_grant_payload()
    payload["subject"] = {"kind": "persona", "id": "p-1"}
    with pytest.raises(SchemaValidationError, match="persona.*RESERVED|RESERVED.*rung 2"):
        validate_payload(
            ni.NETWORK_LINK_GRANT_SET_ID,
            ni.NETWORK_LINK_GRANT_REVISION,
            payload,
        )


def test_link_grant_org_join_requires_exact_invite_reference():
    assert "org:join" in ni.TARGET_TYPES
    payload = link_grant_payload()
    payload["target_type"] = "org:join"
    payload["invite_ref"] = INVITE_REF
    validate_payload(
        ni.NETWORK_LINK_GRANT_SET_ID,
        ni.NETWORK_LINK_GRANT_REVISION,
        payload,
    )

    missing = dict(payload)
    missing.pop("invite_ref")
    with pytest.raises(SchemaValidationError, match="invite_ref"):
        validate_payload(
            ni.NETWORK_LINK_GRANT_SET_ID,
            ni.NETWORK_LINK_GRANT_REVISION,
            missing,
        )

    malformed = dict(payload)
    malformed["invite_ref"] = "BC" * 32
    with pytest.raises(SchemaValidationError, match="invite_ref"):
        validate_payload(
            ni.NETWORK_LINK_GRANT_SET_ID,
            ni.NETWORK_LINK_GRANT_REVISION,
            malformed,
        )


def test_link_grant_non_join_refuses_invite_reference():
    payload = link_grant_payload()
    payload["invite_ref"] = INVITE_REF
    with pytest.raises(SchemaValidationError, match="only valid.*org:join"):
        validate_payload(
            ni.NETWORK_LINK_GRANT_SET_ID,
            ni.NETWORK_LINK_GRANT_REVISION,
            payload,
        )


# ── link-grant participant_id (auto-xwamk) ────────────────────
#
# Same conditional-validity shape as invite_ref/org:join above, deliberately
# a meta key rather than a new typed/versioned field -- see
# graph://ce07a01f-faa "More information" for the full reasoning. Mirrors
# the two invite_ref tests immediately above rather than inventing a new
# test shape.


def test_link_grant_mission_requires_participant_id():
    assert "mission" in ni.TARGET_TYPES
    payload = link_grant_payload()
    payload["target_type"] = "mission"
    payload["meta"] = {"participant_id": "guest:11111111-1111-4111-8111-111111111111"}
    validate_payload(
        ni.NETWORK_LINK_GRANT_SET_ID,
        ni.NETWORK_LINK_GRANT_REVISION,
        payload,
    )

    # Present but empty meta -- participant_id simply absent from it.
    missing = dict(payload)
    missing["meta"] = {}
    with pytest.raises(SchemaValidationError, match="participant_id"):
        validate_payload(
            ni.NETWORK_LINK_GRANT_SET_ID,
            ni.NETWORK_LINK_GRANT_REVISION,
            missing,
        )

    # meta absent entirely -- the case a naive `if meta:` guard would miss.
    no_meta = dict(payload)
    no_meta.pop("meta")
    with pytest.raises(SchemaValidationError, match="participant_id"):
        validate_payload(
            ni.NETWORK_LINK_GRANT_SET_ID,
            ni.NETWORK_LINK_GRANT_REVISION,
            no_meta,
        )

    # Empty-string participant_id is exactly as invalid as absent.
    blank = dict(payload)
    blank["meta"] = {"participant_id": ""}
    with pytest.raises(SchemaValidationError, match="participant_id"):
        validate_payload(
            ni.NETWORK_LINK_GRANT_SET_ID,
            ni.NETWORK_LINK_GRANT_REVISION,
            blank,
        )


def test_link_grant_non_mission_refuses_participant_id():
    payload = link_grant_payload()  # target_type="present" by default
    payload["meta"]["participant_id"] = "guest:11111111-1111-4111-8111-111111111111"
    with pytest.raises(SchemaValidationError, match="only valid.*mission"):
        validate_payload(
            ni.NETWORK_LINK_GRANT_SET_ID,
            ni.NETWORK_LINK_GRANT_REVISION,
            payload,
        )


def test_link_grant_existing_target_types_unaffected_by_participant_id():
    """No schema_revision bump, no new field -- existing design/present/
    note/org:join grants validate exactly as before (present's own base
    fixture already covers this implicitly via link_grant_payload(), this
    pins it explicitly against regression)."""
    payload = link_grant_payload()
    assert payload["target_type"] == "present"
    validate_payload(
        ni.NETWORK_LINK_GRANT_SET_ID,
        ni.NETWORK_LINK_GRANT_REVISION,
        payload,
    )


# ── graph set schema / example / add round-trips ─────────────


def test_set_schema_meta_flush_exposes_all_three(graph_db_env):
    """`graph set schema <id>` reads the autonomy.schema meta-Setting; the
    flush that backs it must carry all three new set_ids with their
    declared shape."""
    _flush_schema_meta(graph_db_env)
    members = ops.read_set("autonomy.schema", org=ops.CALLER_ORG).to_dict()
    for set_id, (revision, _) in ALL_SET_IDS.items():
        key = f"{set_id}#{revision}"
        assert key in members, f"missing schema meta-Setting for {key}"
        payload = members[key].payload
        assert payload["set_id"] == set_id
        assert payload["properties"], f"{key} exports no properties"
        assert payload["required"], f"{key} declares no required fields"
        assert payload["access_pattern"] == "keyed_per_entity"


def test_set_example_stub_covers_required_fields(graph_db_env):
    """`graph set example <id>` builds its stub from the exported schema;
    the stub must name every required field (operators fill in values)."""
    _flush_schema_meta(graph_db_env)
    members = ops.read_set("autonomy.schema", org=ops.CALLER_ORG).to_dict()
    for set_id, (revision, _) in ALL_SET_IDS.items():
        payload = members[f"{set_id}#{revision}"].payload
        stub = _build_example(payload)
        assert set(stub) == set(payload["required"]), set_id


@pytest.mark.parametrize("set_id", sorted(ALL_SET_IDS))
def test_set_add_round_trips_through_storage(graph_db_env, set_id):
    """`graph set add` → validate → store → read back, payload intact."""
    revision, factory = ALL_SET_IDS[set_id]
    payload = factory()
    key = {
        ni.NETWORK_ORG_KEY_SET_ID: "default",
        ni.NETWORK_BINDING_SET_ID: "auto.network",
        ni.NETWORK_LINK_GRANT_SET_ID: payload.get("token", "k"),
        ni.NETWORK_SERVE_CERT_SET_ID: "default",
    }[set_id]
    setting_id = ops.upsert_by_key(set_id, revision, key, payload, org=ops.CALLER_ORG)
    assert setting_id
    members = ops.read_set(set_id, org=ops.CALLER_ORG, target_revision=revision).to_dict()
    assert members[key].payload == payload


def test_link_grant_current_shape_is_returned_from_stored_v1(graph_db_env):
    """Consumers request the current contract and do not handle revisions."""
    payload = link_grant_payload()
    payload.pop("url")
    ops.add_setting(
        ni.NETWORK_LINK_GRANT_SET_ID,
        1,
        TOKEN,
        payload,
        org=ops.CALLER_ORG,
    )

    current = ops.read_set(
        ni.NETWORK_LINK_GRANT_SET_ID,
        org=ops.CALLER_ORG,
        target_revision=ni.NETWORK_LINK_GRANT_REVISION,
    ).members[0]

    assert current.payload["url"] == f"{ni.NETWORK_PUBLIC_LINK_BASE_URL}/l/{TOKEN}"


def test_set_add_rejects_invalid_payload_at_the_boundary(graph_db_env):
    bad = link_grant_payload()
    bad["meta"]["require_auth"] = True
    with pytest.raises(SchemaValidationError, match="rung 2"):
        ops.upsert_by_key(
            ni.NETWORK_LINK_GRANT_SET_ID,
            ni.NETWORK_LINK_GRANT_REVISION,
            bad["token"],
            bad,
            org=ops.CALLER_ORG,
        )


# ── operation_policy: link.* classes ──────────────────────────


def _link_policy(operation: str = "publish", mode: str = "delegated") -> dict:
    return {"contract": "link", "operation": operation, "mode": mode}


def test_link_operation_policy_payloads_validate():
    for op in ("publish", "revoke", "delegate"):
        for mode in LINK_OPERATION_MODES:
            validate_payload(
                OPERATION_POLICY_SET_ID, OPERATION_POLICY_REVISION,
                _link_policy(op, mode),
            )


def test_link_operation_policy_rejects_unknown_operation():
    with pytest.raises(SchemaValidationError, match="unknown link operation"):
        validate_payload(
            OPERATION_POLICY_SET_ID, OPERATION_POLICY_REVISION,
            _link_policy(operation="pubish"),
        )


def test_link_operation_policy_rejects_unknown_mode():
    with pytest.raises(SchemaValidationError, match="mode"):
        validate_payload(
            OPERATION_POLICY_SET_ID, OPERATION_POLICY_REVISION,
            _link_policy(mode="yolo"),
        )


def test_mode_is_rejected_outside_the_link_contract():
    with pytest.raises(SchemaValidationError, match="only defined for"):
        validate_payload(
            OPERATION_POLICY_SET_ID, OPERATION_POLICY_REVISION,
            {"contract": "source_control", "operation": "push", "mode": "delegated"},
        )


# ── resolution: prompt by default, delegated when overridden ──


def _member(payload: dict, member_id: str = "row-1") -> SimpleNamespace:
    return SimpleNamespace(payload=payload, id=member_id)


def test_link_publish_resolves_prompt_by_default():
    resolved = resolve_link_operation_mode(
        "link.publish", workspace_id="ws1", org="autonomy", members={},
    )
    assert resolved.mode == LINK_DEFAULT_MODE == "prompt"
    assert resolved.key == "built-in:prompt"
    assert resolved.source == "built-in"


def test_link_publish_resolves_delegated_when_org_overridden():
    members = {"org:autonomy:link.publish": _member(_link_policy())}
    resolved = resolve_link_operation_mode(
        "link.publish", org="autonomy", members=members,
    )
    assert resolved.mode == "delegated"
    assert resolved.key == "org:autonomy:link.publish"
    assert resolved.source == "row-1"


def test_workspace_row_beats_org_row():
    members = {
        "org:autonomy:link.publish": _member(_link_policy(mode="delegated")),
        "workspace:ws1:link.publish": _member(_link_policy(mode="prompt"), "row-ws"),
    }
    resolved = resolve_link_operation_mode(
        "link.publish", workspace_id="ws1", org="autonomy", members=members,
    )
    assert resolved.mode == "prompt"
    assert resolved.key == "workspace:ws1:link.publish"


def test_operation_classes_resolve_independently():
    members = {"org:autonomy:link.revoke": _member(_link_policy("revoke"))}
    assert resolve_link_operation_mode(
        "link.revoke", org="autonomy", members=members,
    ).mode == "delegated"
    assert resolve_link_operation_mode(
        "link.publish", org="autonomy", members=members,
    ).mode == "prompt"


def test_misconfigured_rows_fail_closed_to_prompt():
    """A row with a wrong contract/operation for its key, or an unknown
    mode, must resolve to prompt — never silently grant delegated, and
    never fall through to a lower-precedence delegated row."""
    wrong_op = {"org:autonomy:link.publish": _member(_link_policy("revoke"))}
    assert resolve_link_operation_mode(
        "link.publish", org="autonomy", members=wrong_op,
    ).mode == "prompt"

    bad_mode = {"org:autonomy:link.publish": _member(
        {"contract": "link", "operation": "publish", "mode": "always"})}
    assert resolve_link_operation_mode(
        "link.publish", org="autonomy", members=bad_mode,
    ).mode == "prompt"

    shadowing = {
        "workspace:ws1:link.publish": _member(_link_policy("revoke"), "broken"),
        "org:autonomy:link.publish": _member(_link_policy(mode="delegated")),
    }
    resolved = resolve_link_operation_mode(
        "link.publish", workspace_id="ws1", org="autonomy", members=shadowing,
    )
    assert resolved.mode == "prompt"
    assert resolved.key == "workspace:ws1:link.publish"


def test_unknown_operation_class_is_refused():
    with pytest.raises(LinkPolicyError):
        resolve_link_operation_mode("link.pubish", members={})
    with pytest.raises(LinkPolicyError):
        resolve_link_operation_mode("source_control.push", members={})
    assert "link.publish" in LINK_OPERATION_CLASSES


def test_resolution_from_real_storage(graph_db_env):
    """End to end: a stored operation_policy row flips link.publish to
    delegated for one workspace; other classes stay prompt."""
    ops.upsert_by_key(
        OPERATION_POLICY_SET_ID, OPERATION_POLICY_REVISION,
        "workspace:ws1:link.publish", _link_policy(), org=ops.CALLER_ORG,
    )
    members = ops.read_set(
        OPERATION_POLICY_SET_ID, org=ops.CALLER_ORG,
        target_revision=OPERATION_POLICY_REVISION,
    ).to_dict()
    assert resolve_link_operation_mode(
        "link.publish", workspace_id="ws1", members=members,
    ).mode == "delegated"
    assert resolve_link_operation_mode(
        "link.delegate", workspace_id="ws1", members=members,
    ).mode == "prompt"


# ── I1 grep-pin: no plaintext-key field anywhere ──────────────


_FORBIDDEN_NAME = re.compile(r"plaintext", re.IGNORECASE)
_KEY_MATERIAL = re.compile(r"(private|secret|priv)_?key", re.IGNORECASE)
_ENCRYPTED_MARKER = re.compile(r"armored|encrypted", re.IGNORECASE)

# Source-level pin: a field assignment named exactly like PRIVATE-key
# material. Scoped to what I1 forbids (identity/signing key plaintext) —
# bearer credentials like claude_setup_tokens' ``raw_key`` (Anthropic's
# field name for a sk-ant-oat01 setup token) are a different storage
# contract and deliberately out of scope here.
_FORBIDDEN_SOURCE = re.compile(
    r"^\s*(plaintext\w*|private_key|secret_key|priv_key|root_key)\s*[:=]",
    re.MULTILINE,
)


def _walk_properties(payload: dict):
    for name in (payload.get("properties") or {}):
        yield name
    for variant in (payload.get("variants") or {}).values():
        yield from _walk_properties(variant)


def test_i1_no_plaintext_key_field_in_any_registered_schema():
    """I1: root/private key plaintext never exists outside the operator's
    browser — so no registered schema, of any set_id, may declare a field
    that stores raw key material. Key-material field names must carry an
    armored/encrypted marker (e.g. armored_private_key)."""
    assert SCHEMAS, "schema registry is empty — imports broken"
    for schema_key_str, cls in sorted(SCHEMAS.items()):
        for name in _walk_properties(cls.export_json_schema()):
            assert not _FORBIDDEN_NAME.search(name), (
                f"{schema_key_str}.{name}: field name admits plaintext key "
                "material (I1)"
            )
            if _KEY_MATERIAL.search(name):
                assert _ENCRYPTED_MARKER.search(name), (
                    f"{schema_key_str}.{name}: key-material field must be "
                    "explicitly armored/encrypted (I1)"
                )


def test_i1_no_plaintext_key_field_in_schema_sources():
    """Grep pin over the schema sources themselves, so even a field that
    dodges registration (or hides in a validator-only dict) trips it."""
    schemas_dir = Path(__file__).resolve().parent.parent / "schemas"
    offenders = []
    for src in sorted(schemas_dir.glob("*.py")):
        for match in _FORBIDDEN_SOURCE.finditer(src.read_text(encoding="utf-8")):
            offenders.append(f"{src.name}: {match.group(0).strip()}")
    assert not offenders, f"raw key-material fields declared in schemas: {offenders}"


def test_org_key_schema_stores_encrypted_armor_only():
    """The one field that touches key material is the armored blob."""
    schema = get_schema(ni.NETWORK_ORG_KEY_SET_ID, 1)
    props = schema.export_json_schema()["properties"]
    assert set(props) == {"armored_private_key", "root_pub"}
    assert "encrypted" in props["armored_private_key"]["description"].lower()


# ── org-key: the I1 gate lives at the SCHEMA layer (Codex re-attack) ──
#
# Hardening one HTTP route was not enough: settings_ops.add_setting and
# POST /api/graph/setting reach this schema directly. The validator now
# strict-parses the armor and requires the canonical byte form, so every
# write path is covered by construction.


def _smuggled_org_key_armor() -> str:
    import base64
    import json as _json

    from tools.network.idkit.armor import ARMOR_BEGIN, ARMOR_END, parse_armor

    data = parse_armor(_ORG_KEY_ARMOR)
    data["private_hex"] = _ORG_KEY_ROOT.private_hex
    body = base64.b64encode(_json.dumps(data).encode()).decode()
    return "\n".join([ARMOR_BEGIN, body, ARMOR_END])


def test_org_key_smuggled_armor_refused_at_schema():
    with pytest.raises(SchemaValidationError, match="I1"):
        validate_payload(ni.NETWORK_ORG_KEY_SET_ID, 1,
                         {"armored_private_key": _smuggled_org_key_armor()})


def test_org_key_smuggle_via_settings_ops_refused(graph_db_env):
    """Codex's exact bypass: writing the setting directly through
    settings_ops. Both write primitives must refuse, and the seed must be
    absent from the DB bytes in raw AND base64-wrapped form."""
    forged = _smuggled_org_key_armor()
    seed = _ORG_KEY_ROOT.private_hex
    with pytest.raises(SchemaValidationError, match="I1"):
        ops.add_setting(ni.NETWORK_ORG_KEY_SET_ID, 1, "default",
                        {"armored_private_key": forged}, org=ops.CALLER_ORG)
    with pytest.raises(SchemaValidationError, match="I1"):
        ops.upsert_by_key(ni.NETWORK_ORG_KEY_SET_ID, 1, "default",
                          {"armored_private_key": forged}, org=ops.CALLER_ORG)
    assert ops.read_set(ni.NETWORK_ORG_KEY_SET_ID, org=ops.CALLER_ORG).members == []
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()
    blob = b"".join(
        p.read_bytes() for p in graph_db_env.parent.glob("graph.db*") if p.is_file()
    )
    assert seed.encode() not in blob
    assert bytes.fromhex(seed) not in blob
    # The forged armor's base64 body (which CONTAINS the seed, encoded)
    # must not have landed either.
    assert forged.splitlines()[1].encode() not in blob


def test_org_key_non_canonical_layout_refused_at_schema():
    """Same fields, different byte layout (body unwrapped to one line):
    the schema requires THE canonical form, byte for byte."""
    lines = _ORG_KEY_ARMOR.strip().splitlines()
    rewrapped = "\n".join([lines[0], "".join(lines[1:-1]), lines[-1]])
    assert rewrapped != _ORG_KEY_ARMOR
    with pytest.raises(SchemaValidationError, match="canonical"):
        validate_payload(ni.NETWORK_ORG_KEY_SET_ID, 1,
                         {"armored_private_key": rewrapped})


def test_org_key_root_pub_must_match_armor():
    payload = org_key_payload()
    payload["root_pub"] = ROOT_PUB  # valid hex, wrong key
    with pytest.raises(SchemaValidationError, match="does not match"):
        validate_payload(ni.NETWORK_ORG_KEY_SET_ID, 1, payload)
