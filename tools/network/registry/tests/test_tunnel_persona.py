"""Persona-scoped tunnel admission and connection-memory routing facts."""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.registry.relay import _verify_tunnel_hello
from tools.network.relaykit.hello import HelloError, build_tunnel_hello

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


def _verify(app, key, cert):
    hello = build_tunnel_hello(key, cert, org=ORG, ts=NOW)
    return _verify_tunnel_hello(hello, ORG, app.state.store, NOW)


def test_two_devices_for_one_persona_retain_same_routing_identity(
    app, bound_org, root,
):
    first = KeyPair.generate()
    second = KeyPair.generate()
    assert first.public_hex != second.public_hex

    persona_a, signer_a = _verify(app, first, _cert(root, first))
    persona_b, signer_b = _verify(app, second, _cert(root, second))

    assert persona_a == persona_b == "ab" * 32
    assert signer_a == first.public_hex
    assert signer_b == second.public_hex


def test_different_personas_remain_distinguishable(app, bound_org, root):
    first = KeyPair.generate()
    second = KeyPair.generate()
    persona_a, _ = _verify(app, first, _cert(root, first))
    persona_b, _ = _verify(
        app,
        second,
        _cert(root, second, subject=Subject("persona", "cd" * 32)),
    )
    assert persona_a != persona_b


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
