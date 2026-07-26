"""member.claim kem_credential: schema, fold verification, projection."""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger.errors import SchemaError
from tools.network.ledger.fold import R_CLAIM_BAD_CREDENTIAL
from tools.network.storagekit.credentials import build as build_credential

from .conftest import Sim, T0

HLC0 = (T0, 0)
KEM_SEED = bytes(range(32))


def _org_with_invite():
    sim = Sim()
    sim.role_define(sim.root, "member", scope_set=["link:publish"])
    persona, invite_key = KeyPair.generate(), KeyPair.generate()
    iid = sim.invite(sim.root, "member", invite_key=invite_key)
    return sim, persona, invite_key, iid


def _credential_dict(sim, persona, genesis_id=None, seed=KEM_SEED):
    credential, _ = build_credential(
        persona, genesis_id or sim.genesis_id, seed, [sim.genesis_id], HLC0
    )
    return credential.to_dict()


def _claim(sim, invite_key, iid, persona, credential=None, profile=None):
    payload = {
        "type": "member.claim",
        "invite_ref": iid,
        "persona_pub": persona.public_hex,
        "profile": profile if profile is not None else {},
        "approvals": [],
    }
    if credential is not None:
        payload["kem_credential"] = credential
    return sim.emit(invite_key, payload)


def test_wellformed_credential_admits_and_projects():
    sim, persona, invite_key, iid = _org_with_invite()
    credential = _credential_dict(sim, persona)
    cid = _claim(sim, invite_key, iid, persona, credential)
    state = sim.fold()
    assert state.valid[cid] is True
    member = state.members[persona.public_hex]
    assert member.kem_credential == credential


def test_absent_field_admits_exactly_as_today():
    sim, persona, invite_key, iid = _org_with_invite()
    cid = _claim(sim, invite_key, iid, persona)
    state = sim.fold()
    assert state.valid[cid] is True
    assert state.members[persona.public_hex].kem_credential is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: {**c, "signature": c["signature"][:-1] + ("0" if c["signature"][-1] != "0" else "1")},
        lambda c: {**c, "kem_key_id": ("0" if c["kem_key_id"][0] != "0" else "1") + c["kem_key_id"][1:]},
    ],
)
def test_fold_rejects_tampered_credential(mutate):
    sim, persona, invite_key, iid = _org_with_invite()
    credential = mutate(_credential_dict(sim, persona))
    cid = _claim(sim, invite_key, iid, persona, credential)
    state = sim.fold()
    assert state.valid[cid] is False
    assert state.reasons[cid] == R_CLAIM_BAD_CREDENTIAL
    assert persona.public_hex not in state.members


def test_fold_rejects_foreign_signer_and_foreign_genesis():
    sim, persona, invite_key, iid = _org_with_invite()
    other = KeyPair.generate()
    # Signed by a different keypair over the same binding shape — but the
    # binding names `persona`, so forge the whole record under `other`
    # and relabel: kem_key_id then mismatches; instead sign persona's
    # binding with other's key via build on other and swapping persona is
    # structurally blocked (persona==persona_pub). The realistic forgery:
    # other's own credential presented on persona's claim.
    foreign_signer = _credential_dict(sim, other)
    cid = _claim(sim, invite_key, iid, persona, {**foreign_signer, "persona": persona.public_hex})
    state = sim.fold()
    assert state.valid[cid] is False
    assert state.reasons[cid] == R_CLAIM_BAD_CREDENTIAL  # kem_key_id mismatch

    sim2, persona2, invite_key2, iid2 = _org_with_invite()
    cross_org = _credential_dict(sim2, persona2, genesis_id="9c" * 32)
    cid2 = _claim(sim2, invite_key2, iid2, persona2, cross_org)
    state2 = sim2.fold()
    assert state2.valid[cid2] is False
    assert state2.reasons[cid2] == R_CLAIM_BAD_CREDENTIAL


def test_structural_rejections_at_append():
    sim, persona, invite_key, iid = _org_with_invite()
    good = _credential_dict(sim, persona)

    missing = dict(good)
    del missing["kem_key_id"]
    extra = {**good, "extra": 1}
    bad_hex = {**good, "kem_public_key": "XY" * 32}
    bad_hlc = {**good, "created_hlc": "not-a-list"}
    bad_suite = {**good, "suite_id": True}
    other_persona = _credential_dict(sim, KeyPair.generate())
    oversize = {**good, "authority_heads": sorted({("%064x" % i) for i in range(16)})}

    for bad in (missing, extra, bad_hex, bad_hlc, bad_suite, other_persona, oversize):
        with pytest.raises(SchemaError):
            _claim(sim, invite_key, iid, persona, bad)


def test_credential_nested_in_profile_is_pii_not_credential():
    sim, persona, invite_key, iid = _org_with_invite()
    credential = _credential_dict(sim, persona)
    cid = _claim(sim, invite_key, iid, persona, profile={"kem_credential": credential})
    state = sim.fold()
    assert state.valid[cid] is True  # passes as profile PII (<= 2048 bytes)
    assert state.members[persona.public_hex].kem_credential is None


def test_cross_package_fidelity():
    # The storagekit record passes the ledger checker unchanged and its
    # signature verifies under the events.py domain constant.
    from tools.network.ledger.events import (
        KEM_CREDENTIAL_DOMAIN,
        _require_kem_credential,
    )
    from tools.network.storagekit.credentials import CREDENTIAL_DOMAIN

    assert KEM_CREDENTIAL_DOMAIN == CREDENTIAL_DOMAIN
    sim, persona, invite_key, iid = _org_with_invite()
    credential = _credential_dict(sim, persona)
    assert _require_kem_credential(credential, persona.public_hex) is None
    cid = _claim(sim, invite_key, iid, persona, credential)
    assert sim.fold().valid[cid] is True


def test_rekey_retires_the_credential():
    sim, persona, invite_key, iid = _org_with_invite()
    credential = _credential_dict(sim, persona)
    _claim(sim, invite_key, iid, persona, credential)
    new_key = KeyPair.generate()
    sim.rekey(persona, persona, persona, new_key)
    member = sim.fold().members[persona.public_hex]
    assert member.current_key == new_key.public_hex
    assert member.kem_credential is None
