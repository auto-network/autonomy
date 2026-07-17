"""Tests for the auto.network ledger Settings schemas (F2, bead auto-0kkpq).

Acceptance (spec graph://eb245082-b76 §2, L8):

* Both set_ids register, export JSON schema, and build examples.
* Real fold-derived payloads (built by ``tools/network/ledger``) validate
  against the schemas unchanged — the cross-pin that keeps the contract
  and the library from drifting.
* L8 at the Settings layer: payloads smuggling event-content fields are
  rejected with an explicit L8 message.
* Vocabulary constants are pinned against the ledger library exactly.
"""

from __future__ import annotations

import copy

import pytest

from tools.graph.schemas import network_ledger as nl
from tools.graph.schemas.registry import (
    SchemaValidationError,
    get_schema,
    validate_payload,
)
from tools.graph.set_cmd import _build_example
from tools.network.idkit import KeyPair
from tools.network.ledger import (
    EVENT_TYPES,
    PROJECTION_NAMES,
    LedgerStore,
    build_projections,
    fold,
    ledger_state_payload,
)
from tools.network.ledger.events import CLAIM_REQUIRES
from tools.network.ledger.tests.conftest import Sim

STATE_KEY = (nl.NETWORK_LEDGER_STATE_SET_ID, nl.NETWORK_LEDGER_STATE_REVISION)
PROJ_KEY = (nl.NETWORK_LEDGER_PROJECTION_SET_ID, nl.NETWORK_LEDGER_PROJECTION_REVISION)


@pytest.fixture(scope="module")
def org_state():
    """A real org folded by the real library: members, roles, revocation."""
    sim = Sim()
    sim.role_define(sim.root, "member", ["link:publish"])
    sponsor = KeyPair.generate()
    sim.delegate(sim.root, sponsor, ["invite:member", "link:publish"], redelegate=True)
    ik, persona = KeyPair.generate(), KeyPair.generate()
    invite = sim.invite(sponsor, "member", invite_key=ik)
    sim.claim(invite, ik, persona)
    doomed = KeyPair.generate()
    g = sim.delegate(sponsor, doomed, ["link:publish"])
    sim.revoke_event(sponsor, g)
    store = LedgerStore(":memory:")
    store.append_bundle(sim.ledger.events())
    return store, fold(sim.ledger)


class TestRegistration:
    @pytest.mark.parametrize("key", [STATE_KEY, PROJ_KEY])
    def test_registered_and_exports_schema(self, key):
        schema = get_schema(*key)
        exported = schema.export_json_schema()
        assert exported["set_id"] == key[0]
        assert exported["schema_revision"] == key[1]
        assert exported["access_pattern"] == "keyed_per_entity"
        assert exported["properties"]

    @pytest.mark.parametrize("key", [STATE_KEY, PROJ_KEY])
    def test_example_builds(self, key):
        example = _build_example(get_schema(*key).export_json_schema())
        assert isinstance(example, dict) and example


class TestVocabularyPins:
    """The duplicated constants must match the ledger library exactly."""

    def test_event_types_pinned(self):
        assert nl.LEDGER_EVENT_TYPES == tuple(sorted(EVENT_TYPES))

    def test_projection_names_pinned(self):
        assert nl.LEDGER_PROJECTION_NAMES == tuple(sorted(PROJECTION_NAMES))

    def test_claim_requires_pinned(self):
        assert nl.LEDGER_CLAIM_REQUIRES == tuple(sorted(CLAIM_REQUIRES))


class TestLedgerStateContract:
    def payload(self, org_state):
        store, state = org_state
        return ledger_state_payload(
            state,
            org_uuid="11111111-1111-4111-8111-111111111111",
            event_count=len(store),
            last_witnessed_head=state.heads[0],
            sync_cursor={"peer-1": state.heads[0]},
        )

    def test_real_payload_validates(self, org_state):
        validate_payload(*STATE_KEY, self.payload(org_state))

    def test_minimal_payload_validates(self, org_state):
        payload = self.payload(org_state)
        del payload["last_witnessed_head"]
        del payload["sync_cursor"]
        validate_payload(*STATE_KEY, payload)

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda p: p.update(heads=[]),
            lambda p: p.update(heads=list(reversed(p["heads"] + ["ff" * 32]))),
            lambda p: p.update(fingerprint="xyz"),
            lambda p: p.update(genesis_id="00"),
            lambda p: p.update(root_pub="not-hex" * 8),
            lambda p: p.update(event_count=0),
            lambda p: p.update(event_count=True),
            lambda p: p.update(sync_cursor={"peer": "short"}),
            lambda p: p.update(last_witnessed_head="zz" * 32),
            lambda p: p.update(surprise=1),
        ],
    )
    def test_bad_payloads_rejected(self, org_state, mutate):
        payload = self.payload(org_state)
        mutate(payload)
        with pytest.raises(SchemaValidationError):
            validate_payload(*STATE_KEY, payload)

    def test_l8_event_content_rejected(self, org_state):
        payload = self.payload(org_state)
        payload["events"] = [{"type": "delegate"}]
        with pytest.raises(SchemaValidationError, match="L8"):
            validate_payload(*STATE_KEY, payload)


class TestProjectionContract:
    def test_all_real_projections_validate(self, org_state):
        _, state = org_state
        for name, projection in build_projections(state).items():
            validate_payload(*PROJ_KEY, projection)
            assert projection["projection"] == name

    def test_projection_bodies_are_shape_checked(self, org_state):
        _, state = org_state
        projections = build_projections(state)

        roster = copy.deepcopy(projections["roster"])
        roster["body"][0]["extra"] = 1
        with pytest.raises(SchemaValidationError):
            validate_payload(*PROJ_KEY, roster)

        roles = copy.deepcopy(projections["roles"])
        roles["body"]["member"]["claim_requires"] = "anyone"
        with pytest.raises(SchemaValidationError):
            validate_payload(*PROJ_KEY, roles)

        live = copy.deepcopy(projections["live-keys"])
        live["body"]["not-a-key"] = ["x"]
        with pytest.raises(SchemaValidationError):
            validate_payload(*PROJ_KEY, live)

    def test_unknown_projection_kind_rejected(self, org_state):
        _, state = org_state
        projection = copy.deepcopy(build_projections(state)["roster"])
        projection["projection"] = "view-log"
        with pytest.raises(SchemaValidationError):
            validate_payload(*PROJ_KEY, projection)

    def test_l8_event_content_rejected(self, org_state):
        _, state = org_state
        projection = copy.deepcopy(build_projections(state)["roster"])
        projection["wire"] = "deadbeef"
        with pytest.raises(SchemaValidationError, match="L8"):
            validate_payload(*PROJ_KEY, projection)

    def test_cross_head_staleness_stamp_present(self, org_state):
        store, state = org_state
        for projection in build_projections(state).values():
            assert projection["heads"] == list(store.heads())
            assert projection["fingerprint"] == state.fingerprint()
