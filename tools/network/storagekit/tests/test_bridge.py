"""Parent-secret bridge: round-trip, fail-closed paths, ancestor recovery."""

from __future__ import annotations

import base64
import dataclasses
import os

import pytest

from tools.network.idkit import KeyPair, canonical_json
from tools.network.storagekit import (
    CommitmentError,
    MalformedRecordError,
    RecordSignatureError,
    SuiteError,
)
from tools.network.storagekit import state
from tools.network.storagekit.bridge import (
    BridgeError,
    ParentBridge,
    create,
    open as bridge_open,
    recover_ancestors,
    verify_signature,
)

GENESIS = "1a" * 32
DOMAIN_ID = "2b" * 32
HEADS = ["5e" * 32]
DIGEST = "8b" * 32


class Dag:
    """G <- P1 <- L, P2 <- L: descriptors, secrets, and bridges."""

    def __init__(self):
        self.issuer = KeyPair.generate()
        self.dG, self.sG = self._mint([])
        self.dP2, self.sP2 = self._mint([])
        self.dP1, self.sP1 = self._mint([self.dG.state_id])
        self.dL, self.sL = self._mint(sorted([self.dP1.state_id, self.dP2.state_id]))
        self.descriptors = {
            d.state_id: d for d in (self.dG, self.dP1, self.dP2, self.dL)
        }
        self.b_L_P1 = self._bridge(self.dL, self.sL, self.dP1, self.sP1)
        self.b_L_P2 = self._bridge(self.dL, self.sL, self.dP2, self.sP2)
        self.b_P1_G = self._bridge(self.dP1, self.sP1, self.dG, self.sG)
        self.bridges = [self.b_L_P1, self.b_L_P2, self.b_P1_G]

    def _mint(self, parents):
        return state.generate(
            self.issuer, GENESIS, DOMAIN_ID, parents, HEADS, [], DIGEST
        )

    def _bridge(self, child_desc, child_secret, parent_desc, parent_secret):
        return create(
            self.issuer,
            genesis_id=GENESIS,
            domain_id=DOMAIN_ID,
            child_state_id=child_desc.state_id,
            parent_state_id=parent_desc.state_id,
            child_state_secret=child_secret,
            parent_state_secret=parent_secret,
            authority_heads=HEADS,
        )


@pytest.fixture(scope="module")
def dag() -> Dag:
    return Dag()


def _flip_b64(value: str, offset: int = 0) -> str:
    raw = bytearray(base64.b64decode(value))
    raw[offset] ^= 0x01
    return base64.b64encode(bytes(raw)).decode("ascii")


# -- open ------------------------------------------------------------------------


def test_roundtrip_recovers_parent_secret(dag):
    assert bridge_open(dag.b_L_P1, dag.sL) == dag.sP1
    assert bridge_open(dag.b_L_P2, dag.sL) == dag.sP2
    assert bridge_open(dag.b_P1_G, dag.sP1) == dag.sG


def test_wrong_child_secret_fails(dag):
    with pytest.raises(BridgeError):
        bridge_open(dag.b_L_P1, os.urandom(32))
    # Invariant 4, direct form: the parent's own secret is not the edge key.
    with pytest.raises(BridgeError):
        bridge_open(dag.b_L_P1, dag.sP1)


def test_ciphertext_and_nonce_tamper_fail(dag):
    for field, offset in (
        ("encrypted_parent_secret", 0),
        ("encrypted_parent_secret", 47),
        ("nonce", 0),
    ):
        tampered = dataclasses.replace(
            dag.b_L_P1, **{field: _flip_b64(getattr(dag.b_L_P1, field), offset)}
        )
        with pytest.raises(BridgeError):
            bridge_open(tampered, dag.sL)


def test_context_field_alteration_fails(dag):
    other = "9c" * 32
    for field in ("genesis_id", "domain_id", "child_state_id", "parent_state_id"):
        replayed = dataclasses.replace(dag.b_L_P1, **{field: other})
        with pytest.raises(BridgeError):
            bridge_open(replayed, dag.sL)


def test_unrecognized_suite_fails_closed(dag):
    downgraded = dataclasses.replace(dag.b_L_P1, suite_id="aes-128-gcm")
    with pytest.raises(SuiteError):
        bridge_open(downgraded, dag.sL)


def test_open_rejects_malformed_inputs(dag):
    with pytest.raises(MalformedRecordError):
        bridge_open(dag.b_L_P1, dag.sL[:31])
    with pytest.raises(MalformedRecordError):
        bridge_open(dataclasses.replace(dag.b_L_P1, nonce="!!!"), dag.sL)


# -- signature ---------------------------------------------------------------------


def test_signature_verifies_and_rejects_mutations(dag):
    assert verify_signature(dag.b_L_P1) is None
    mutations = {
        "genesis_id": "9c" * 32,
        "domain_id": "9c" * 32,
        "child_state_id": "9c" * 32,
        "parent_state_id": "9c" * 32,
        "nonce": _flip_b64(dag.b_L_P1.nonce),
        "encrypted_parent_secret": _flip_b64(dag.b_L_P1.encrypted_parent_secret),
        "authority_heads": (),
        "issuer_persona": KeyPair.from_private_hex("aa" * 32).public_hex,
    }
    for field, value in mutations.items():
        with pytest.raises(RecordSignatureError):
            verify_signature(dataclasses.replace(dag.b_L_P1, **{field: value}))
    forged = dataclasses.replace(
        dag.b_L_P1,
        signature=KeyPair.generate().sign_hex(dag.b_L_P1.signing_input()),
    )
    with pytest.raises(RecordSignatureError):
        verify_signature(forged)


# -- wire --------------------------------------------------------------------------


def test_wire_roundtrip_and_non_canonical_rejection(dag):
    wire = dag.b_L_P1.to_json()
    restored = ParentBridge.from_json(wire)
    assert restored == dag.b_L_P1
    assert restored.bridge_id == dag.b_L_P1.bridge_id

    text = wire.decode("ascii")
    with pytest.raises(MalformedRecordError):
        ParentBridge.from_json(text.replace(":", ": ", 1).encode("ascii"))
    with pytest.raises(MalformedRecordError):
        ParentBridge.from_json(canonical_json({**dag.b_L_P1.signed_dict(), "signature": dag.b_L_P1.signature, "extra": 1}))
    duplicated = canonical_json(
        {
            **dag.b_L_P1.signed_dict(),
            "authority_heads": HEADS + HEADS,
            "signature": dag.b_L_P1.signature,
        }
    )
    with pytest.raises(MalformedRecordError):
        ParentBridge.from_json(duplicated)


# -- recovery ----------------------------------------------------------------------


def test_recover_all_reachable_ancestors(dag):
    recovered = recover_ancestors(dag.dL.state_id, dag.sL, dag.bridges, dag.descriptors)
    assert recovered == {
        dag.dL.state_id: dag.sL,
        dag.dP1.state_id: dag.sP1,
        dag.dP2.state_id: dag.sP2,
        dag.dG.state_id: dag.sG,
    }


def test_recovery_is_one_directional(dag):
    recovered = recover_ancestors(
        dag.dP1.state_id, dag.sP1, dag.bridges, dag.descriptors
    )
    assert recovered == {dag.dP1.state_id: dag.sP1, dag.dG.state_id: dag.sG}
    assert dag.dL.state_id not in recovered
    assert dag.dP2.state_id not in recovered


def test_poisoned_bridge_raises_commitment_error_naming_parent(dag):
    poisoned = create(
        dag.issuer,
        genesis_id=GENESIS,
        domain_id=DOMAIN_ID,
        child_state_id=dag.dP1.state_id,
        parent_state_id=dag.dG.state_id,
        child_state_secret=dag.sP1,
        parent_state_secret=os.urandom(32),  # decrypts fine, wrong secret
        authority_heads=HEADS,
    )
    bridges = [dag.b_L_P1, dag.b_L_P2, poisoned]
    with pytest.raises(CommitmentError) as excinfo:
        recover_ancestors(dag.dL.state_id, dag.sL, bridges, dag.descriptors)
    assert dag.dG.state_id in str(excinfo.value)


def test_forged_bridge_in_set_fails_loud(dag):
    forged = dataclasses.replace(
        dag.b_P1_G,
        signature=KeyPair.generate().sign_hex(dag.b_P1_G.signing_input()),
    )
    with pytest.raises(RecordSignatureError):
        recover_ancestors(
            dag.dL.state_id, dag.sL, [dag.b_L_P1, forged], dag.descriptors
        )


def test_missing_descriptor_fails_loud(dag):
    descriptors = dict(dag.descriptors)
    del descriptors[dag.dP2.state_id]
    with pytest.raises(MalformedRecordError):
        recover_ancestors(dag.dL.state_id, dag.sL, dag.bridges, descriptors)
