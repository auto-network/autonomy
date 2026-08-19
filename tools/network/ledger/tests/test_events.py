"""Event schema (L8), signing, and anti-malleable wire parsing."""

from __future__ import annotations

import json

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger import (
    HLC,
    Event,
    MalformedEventError,
    SchemaError,
    make_event,
    validate_payload,
)

from .conftest import ORG, T0

KP = KeyPair.generate()
PARENT = "11" * 32


def make(payload, parents=(PARENT,), ts=T0):
    return make_event(KP, payload, list(parents), HLC(ts))


def delegate_payload(**over):
    child = KeyPair.generate()
    payload = {
        "type": "delegate",
        "child_pub": child.public_hex,
        "scope": ["link:publish"],
        "can_redelegate": False,
        # Payload validation checks shape only; the binding is verified at
        # fold admission, so any 128-hex signature satisfies the schema.
        "proof": child.sign_hex(b"shape-only"),
    }
    payload.update(over)
    return payload


class TestL8SchemaBoundary:
    """The ledger holds authority only: everything else is schema-rejected."""

    @pytest.mark.parametrize(
        "kind",
        [
            "content.view",
            "content.read",
            "note.read",
            "access.log",
            "presence.ping",
            "message",
            "",
        ],
    )
    def test_non_authority_events_rejected(self, kind):
        with pytest.raises(SchemaError):
            validate_payload({"type": kind, "anything": 1})

    def test_member_rekey_malformed_continuity_is_schema_error(self):
        # A junk continuity is an L8 SchemaError, never an uncaught idkit
        # MalformedError that a sync/ingest path (catching SchemaError to reject
        # bad peer input) would miss and crash on.
        payload = {
            "type": "member.rekey",
            "persona": KeyPair.generate().public_hex,
            "old_pub": KeyPair.generate().public_hex,
            "new_pub": KeyPair.generate().public_hex,
            "continuity": "not-a-valid-signature",
            "approvals": [],
        }
        with pytest.raises(SchemaError):
            validate_payload(payload)

    def test_key_rotate_malformed_recovery_continuity_is_schema_error(self):
        # The optional recovery co-signature gets the same fail-safe as
        # continuity: junk hex is a SchemaError, not an uncaught idkit fault.
        payload = {
            "type": "key.rotate",
            "old_pub": KeyPair.generate().public_hex,
            "new_pub": KeyPair.generate().public_hex,
            "continuity": "a" * 128,
            "recovery_continuity": "not-a-valid-signature",
        }
        with pytest.raises(SchemaError):
            validate_payload(payload)

    def test_genesis_recovery_policy_validation(self):
        rk = KeyPair.generate().public_hex
        base = {"type": "genesis", "org": "o", "root_pub": KeyPair.generate().public_hex}
        validate_payload(base)  # no recovery declared -> policy "none"
        validate_payload({**base, "recovery": {"policy": "none"}})
        validate_payload({**base, "recovery": {"policy": "recovery-key", "recovery_pub": rk}})
        for bad in (
            {"policy": "recovery-key"},                    # missing recovery_pub
            {"policy": "none", "recovery_pub": rk},        # recovery_pub forbidden
            {"policy": "vouch-quorum"},                    # unknown policy, fail closed
            {"policy": "recovery-key", "recovery_pub": rk, "extra": 1},  # unknown field
        ):
            with pytest.raises(SchemaError):
                validate_payload({**base, "recovery": bad})
        with pytest.raises(SchemaError):
            validate_payload({**base, "recovery": "not-an-object"})
        # The recovery factor must be a DISTINCT key from the root -- else a
        # stolen root signs both the possession proof and the co-signature.
        with pytest.raises(SchemaError):
            validate_payload({
                **base,
                "recovery": {"policy": "recovery-key", "recovery_pub": base["root_pub"]},
            })

    def test_payload_type_must_be_string(self):
        with pytest.raises(SchemaError):
            validate_payload({"type": 7})
        with pytest.raises(SchemaError):
            validate_payload(["delegate"])

    def test_unknown_fields_rejected(self):
        with pytest.raises(SchemaError):
            validate_payload(delegate_payload(viewer="alice"))

    def test_missing_fields_rejected(self):
        p = delegate_payload()
        del p["scope"]
        with pytest.raises(SchemaError):
            validate_payload(p)

    def test_vocabulary_is_closed_at_make_event(self):
        with pytest.raises(SchemaError):
            make({"type": "content.view", "target": "note-1"})


class TestPayloadValidation:
    def test_scope_must_be_sorted_unique(self):
        with pytest.raises(SchemaError):
            validate_payload(delegate_payload(scope=["b:x", "a:x"]))
        with pytest.raises(SchemaError):
            validate_payload(delegate_payload(scope=["a:x", "a:x"]))

    def test_scope_wildcard_placement(self):
        validate_payload(delegate_payload(scope=["*"]))
        validate_payload(delegate_payload(scope=["role:grant:*"]))
        with pytest.raises(SchemaError):
            validate_payload(delegate_payload(scope=["role:*:grant"]))
        with pytest.raises(SchemaError):
            validate_payload(delegate_payload(scope=["*:*"]))

    def test_can_redelegate_must_be_bool(self):
        with pytest.raises(SchemaError):
            validate_payload(delegate_payload(can_redelegate=1))

    def test_ttl_positive_int(self):
        validate_payload(delegate_payload(ttl=1000))
        for bad in (0, -5, True, 1.5, "1000"):
            with pytest.raises((SchemaError, MalformedEventError)):
                validate_payload(delegate_payload(ttl=bad))

    def test_revoke_exactly_one_target(self):
        with pytest.raises(SchemaError):
            validate_payload({"type": "revoke"})
        with pytest.raises(SchemaError):
            validate_payload(
                {"type": "revoke", "target_event": "22" * 32, "target_key": KP.public_hex}
            )

    def test_invite_exactly_one_of_pub_token(self):
        base = {"type": "invite", "granted_role": "member", "expiry": T0, "sponsor": KP.public_hex}
        with pytest.raises(SchemaError):
            validate_payload(dict(base))
        with pytest.raises(SchemaError):
            validate_payload(
                dict(base, invite_pub=KP.public_hex, token_hash="22" * 32)
            )
        validate_payload(dict(base, invite_pub=KP.public_hex))
        validate_payload(dict(base, token_hash="22" * 32))

    def test_role_define_claim_requires_enum(self):
        base = {"type": "role.define", "name": "member", "scope_set": [], "version": 1}
        for good in ("self", "sponsor", "admin-ack"):
            validate_payload(dict(base, claim_requires=good))
        with pytest.raises(SchemaError):
            validate_payload(dict(base, claim_requires="anyone"))

    def test_role_name_charset(self):
        base = {"type": "role.define", "scope_set": [], "claim_requires": "self", "version": 1}
        with pytest.raises(SchemaError):
            validate_payload(dict(base, name="Admin Role"))
        with pytest.raises(SchemaError):
            validate_payload(dict(base, name=""))

    def test_approvals_sorted_unique_shape(self):
        base = {
            "type": "member.claim",
            "invite_ref": "22" * 32,
            "persona_pub": KP.public_hex,
            "profile": {},
        }
        sig = "0" * 128
        k1, k2 = sorted([KeyPair.generate().public_hex, KeyPair.generate().public_hex])
        validate_payload(dict(base, approvals=[{"key": k1, "sig": sig}, {"key": k2, "sig": sig}]))
        with pytest.raises(SchemaError):
            validate_payload(
                dict(base, approvals=[{"key": k2, "sig": sig}, {"key": k1, "sig": sig}])
            )
        with pytest.raises(SchemaError):
            validate_payload(
                dict(base, approvals=[{"key": k1, "sig": sig}, {"key": k1, "sig": sig}])
            )
        with pytest.raises(SchemaError):
            validate_payload(dict(base, approvals=[{"key": k1}]))

    def test_profile_size_capped(self):
        base = {
            "type": "member.claim",
            "invite_ref": "22" * 32,
            "persona_pub": KP.public_hex,
            "approvals": [],
        }
        validate_payload(dict(base, profile={"name": "x"}))
        with pytest.raises(SchemaError):
            validate_payload(dict(base, profile={"blob": "x" * 3000}))

    def test_timestamps_reject_bool_and_negative(self):
        base = {
            "type": "invite",
            "granted_role": "member",
            "sponsor": KP.public_hex,
            "invite_pub": KP.public_hex,
        }
        for bad in (True, -1, 2**63, 1.0, "0"):
            with pytest.raises(SchemaError):
                validate_payload(dict(base, expiry=bad))


class TestEventStructure:
    def test_genesis_must_have_no_parents(self):
        payload = {"type": "genesis", "org": ORG, "root_pub": KP.public_hex}
        event = make_event(KP, payload, [], HLC(T0))
        data = event.to_dict()
        data["parents"] = [PARENT]
        with pytest.raises(MalformedEventError):
            Event.from_dict(data)

    def test_non_genesis_requires_parents(self):
        with pytest.raises(MalformedEventError):
            Event.from_dict(make(delegate_payload(), parents=(PARENT,)).to_dict() | {"parents": []})

    def test_parents_sorted_unique(self):
        event = make(delegate_payload(), parents=("aa" * 32, "bb" * 32))
        data = event.to_dict()
        data["parents"] = ["bb" * 32, "aa" * 32]
        with pytest.raises(MalformedEventError):
            Event.from_dict(data)
        data["parents"] = ["aa" * 32, "aa" * 32]
        with pytest.raises(MalformedEventError):
            Event.from_dict(data)

    def test_make_event_sorts_parents(self):
        event = make_event(KP, delegate_payload(), ["bb" * 32, "aa" * 32], HLC(T0))
        assert event.parents == ("aa" * 32, "bb" * 32)

    def test_unknown_and_missing_envelope_fields(self):
        data = make(delegate_payload()).to_dict()
        with pytest.raises(MalformedEventError):
            Event.from_dict(dict(data, extra=1))
        short = dict(data)
        del short["hlc"]
        with pytest.raises(MalformedEventError):
            Event.from_dict(short)

    def test_bad_version(self):
        data = make(delegate_payload()).to_dict()
        data["v"] = 2
        with pytest.raises(MalformedEventError):
            Event.from_dict(data)

    def test_hlc_shape(self):
        data = make(delegate_payload()).to_dict()
        for bad in ([1], [1, 2, 3], [1.0, 0], [True, 0], "now", [-1, 0]):
            with pytest.raises(MalformedEventError):
                Event.from_dict(dict(data, hlc=bad))


class TestWireForm:
    def test_roundtrip_bit_identical(self):
        event = make(delegate_payload())
        raw = event.to_json()
        parsed = Event.from_json(raw)
        assert parsed.to_json() == raw
        assert parsed.event_id == event.event_id
        parsed.verify_sig()

    def test_non_canonical_forms_rejected(self):
        event = make(delegate_payload())
        raw = event.to_json().decode()
        data = json.loads(raw)

        with_spaces = json.dumps(data, indent=1)
        reordered = json.dumps(
            {k: data[k] for k in reversed(sorted(data))}, separators=(",", ":")
        )
        assert json.loads(with_spaces) == data
        assert json.loads(reordered) == data
        for variant in (with_spaces, reordered):
            with pytest.raises(MalformedEventError):
                Event.from_json(variant)

    def test_unicode_escape_variant_rejected(self):
        event = make(
            {
                "type": "revoke",
                "target_event": "22" * 32,
                "reason": "bye",
            }
        )
        raw = event.to_json().decode()
        variant = raw.replace('"bye"', '"by\\u0065"')
        assert json.loads(variant) == json.loads(raw)
        with pytest.raises(MalformedEventError):
            Event.from_json(variant)

    def test_duplicate_keys_rejected(self):
        event = make(delegate_payload())
        raw = event.to_json().decode()
        variant = raw.replace('"v":1', '"v":1,"v":1', 1)
        with pytest.raises(MalformedEventError):
            Event.from_json(variant)

    def test_oversized_event_rejected(self):
        with pytest.raises(MalformedEventError):
            Event.from_json(b" " * 20_000)

    def test_tampered_payload_fails_signature(self):
        event = make(delegate_payload())
        data = event.to_dict()
        data["payload"] = dict(data["payload"], can_redelegate=True)
        forged = Event.from_dict(data)
        with pytest.raises(Exception):
            forged.verify_sig()

    def test_id_commits_to_signature(self):
        payload = delegate_payload()
        e1 = make(payload)
        other = KeyPair.generate()
        e2 = make_event(other, payload, [PARENT], HLC(T0))
        assert e1.event_id != e2.event_id


class TestApproverThreshold:
    """role.define approver_threshold: a D16 tagged value, admin-ack only."""

    def _role(self, **over):
        payload = {
            "type": "role.define",
            "name": "member",
            "scope_set": ["link:publish"],
            "claim_requires": "admin-ack",
            "version": 1,
            "approver_threshold": {"kind": "static", "count": 2},
        }
        payload.update(over)
        return payload

    def test_static_bounds_accepted(self):
        for count in (1, 16):
            validate_payload(
                self._role(approver_threshold={"kind": "static", "count": count})
            )

    def test_omitting_the_field_validates(self):
        payload = self._role()
        del payload["approver_threshold"]
        validate_payload(payload)

    @pytest.mark.parametrize("count", [0, -1, True, 17, "2"])
    def test_bad_counts_rejected(self, count):
        with pytest.raises(SchemaError):
            validate_payload(
                self._role(approver_threshold={"kind": "static", "count": count})
            )

    @pytest.mark.parametrize(
        "value",
        [
            3,  # bare integer: the tag is the extension point, never an int
            {"kind": "percent", "count": 2},  # unknown kind fails closed
            {"kind": "static"},  # missing count
            {"count": 2},  # missing kind
            {"kind": "static", "count": 2, "extra": 1},
            None,
            "static:2",
        ],
    )
    def test_untagged_or_unknown_forms_rejected(self, value):
        with pytest.raises(SchemaError):
            validate_payload(self._role(approver_threshold=value))

    @pytest.mark.parametrize("requires", ["self", "sponsor"])
    def test_requires_admin_ack(self, requires):
        with pytest.raises(SchemaError):
            validate_payload(self._role(claim_requires=requires))

    def test_duplicate_approver_key_rejected(self):
        with pytest.raises(SchemaError):
            validate_payload(
                {
                    "type": "member.claim",
                    "invite_ref": "11" * 32,
                    "persona_pub": KP.public_hex,
                    "profile": {},
                    "approvals": [
                        {"key": KP.public_hex, "sig": "ab" * 64},
                        {"key": KP.public_hex, "sig": "ab" * 64},
                    ],
                }
            )
