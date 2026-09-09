"""Persona-scoped tunnel admission and connection-memory routing facts."""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.registry.relay import _verify_tunnel_hello
from tools.network.relaykit.hello import (
    HelloError,
    SERVING_MACHINE_HELLO_DOMAIN,
    build_tunnel_hello_v2,
)

from .conftest import DAY, NOW, ORG


def _cert(
    root,
    key,
    *,
    subject=Subject("persona", "ab" * 32),
    scope=("tunnel:serve",),
    parent_cert=None,
):
    return issue_cert(
        root,
        key.public_hex,
        scope=scope,
        org=ORG,
        subject=subject,
        not_before=NOW - 100,
        not_after=NOW + 30 * DAY,
        parent_cert=parent_cert,
    )


def _verify(app, key, cert, machine_key=None):
    # v2: these tests are about PERSONA routing identity, not the hello
    # version. v1 is deleted, so they present a machine identity like every
    # real tunnel does; a fresh machine key per call keeps two devices of one
    # persona distinct, which is what several of them assert.
    hello = build_tunnel_hello_v2(
        key, cert, machine_key=machine_key or KeyPair.generate(),
        org=ORG, ts=NOW, machine_hello_domain=SERVING_MACHINE_HELLO_DOMAIN,
    )
    return _verify_tunnel_hello(hello, ORG, app.state.store, NOW)


def test_two_devices_for_one_persona_retain_same_routing_identity(
    app, bound_org, root,
):
    first = KeyPair.generate()
    second = KeyPair.generate()
    assert first.public_hex != second.public_hex

    verified_a = _verify(app, first, _cert(root, first))
    verified_b = _verify(app, second, _cert(root, second))

    assert verified_a.persona_pub == verified_b.persona_pub == "ab" * 32
    assert verified_a.signer_pub == first.public_hex
    assert verified_b.signer_pub == second.public_hex


def test_different_personas_remain_distinguishable(app, bound_org, root):
    first = KeyPair.generate()
    second = KeyPair.generate()
    verified_a = _verify(app, first, _cert(root, first))
    verified_b = _verify(
        app,
        second,
        _cert(root, second, subject=Subject("persona", "cd" * 32)),
    )
    assert verified_a.persona_pub != verified_b.persona_pub


@pytest.mark.parametrize(
    "subject",
    [
        Subject("operator", "ab" * 32),
        Subject("persona", "AB" * 32),
        Subject("persona", "browser-label"),
    ],
)
def test_noncanonical_persona_subject_is_refused(
    app, bound_org, root, subject,
):
    key = KeyPair.generate()
    with pytest.raises(HelloError, match="organization persona"):
        _verify(app, key, _cert(root, key, subject=subject))


def test_extra_scope_is_refused(app, bound_org, root):
    key = KeyPair.generate()
    with pytest.raises(HelloError, match="exactly"):
        _verify(
            app,
            key,
            _cert(root, key, scope=("link:publish", "tunnel:serve")),
        )


def test_intermediate_cannot_substitute_persona(app, bound_org, root):
    intermediate = KeyPair.generate()
    parent = _cert(
        root,
        intermediate,
        subject=Subject("operator", "root-session"),
        scope=("link:publish", "tunnel:serve"),
    )
    leaf = KeyPair.generate()
    child = issue_cert(
        intermediate,
        leaf.public_hex,
        scope=("tunnel:serve",),
        org=ORG,
        subject=Subject("persona", "ab" * 32),
        not_before=NOW - 50,
        not_after=NOW + 20 * DAY,
        parent_cert=parent,
    )
    with pytest.raises(HelloError, match="directly"):
        _verify(app, leaf, child)
