from __future__ import annotations

import pytest

from tools.dashboard.commit_broker.keys import (
    ConfusedDeputyKeyError,
    InMemoryBrokerKeyStore,
    VerificationKey,
    VerificationKeyNotRegistered,
    register_verification_key,
    resolve_verification_key,
)


def test_resolve_returns_material_keyed_to_operator():
    ks = InMemoryBrokerKeyStore()
    ks.store("op-1", "gpg", b"PUBKEY-1")
    assert resolve_verification_key(operator_id="op-1", signing_kind="gpg", keystore=ks) == VerificationKey(
        "op-1", "gpg", b"PUBKEY-1"
    )


def test_resolve_rejects_request_supplied_key_as_confused_deputy():
    ks = InMemoryBrokerKeyStore()
    ks.store("op-1", "gpg", b"REAL")
    with pytest.raises(ConfusedDeputyKeyError):
        resolve_verification_key(
            operator_id="op-1", signing_kind="gpg", keystore=ks, request_key_material=b"ATTACKER"
        )


def test_confused_deputy_check_fires_before_the_not_registered_path():
    # even with an empty store, a request-supplied key is refused, never returned
    ks = InMemoryBrokerKeyStore()
    with pytest.raises(ConfusedDeputyKeyError):
        resolve_verification_key(
            operator_id="op-x", signing_kind="gpg", keystore=ks, request_key_material=b"ATTACKER"
        )


def test_resolve_fails_closed_when_no_key_registered():
    with pytest.raises(VerificationKeyNotRegistered):
        resolve_verification_key(operator_id="op-x", signing_kind="ssh", keystore=InMemoryBrokerKeyStore())


def test_register_then_resolve_round_trips():
    ks = InMemoryBrokerKeyStore()
    register_verification_key(operator_id="op-2", signing_kind="ssh", public_material=b"ssh-ed25519 AAAA", keystore=ks)
    assert resolve_verification_key(operator_id="op-2", signing_kind="ssh", keystore=ks).material == b"ssh-ed25519 AAAA"


def test_register_rejects_unknown_kind_and_empty_material():
    ks = InMemoryBrokerKeyStore()
    with pytest.raises(ValueError):
        register_verification_key(operator_id="op", signing_kind="rsa", public_material=b"x", keystore=ks)
    with pytest.raises(ValueError):
        register_verification_key(operator_id="op", signing_kind="gpg", public_material=b"", keystore=ks)


def test_resolve_rejects_unknown_signing_kind():
    with pytest.raises(ValueError):
        resolve_verification_key(operator_id="op", signing_kind="rsa", keystore=InMemoryBrokerKeyStore())
