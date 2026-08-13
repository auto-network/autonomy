"""Serving-persona credentials never enter durable reachability hints."""

from __future__ import annotations

import time

import pytest

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.relaykit.node import NodeServer


ORG = "11111111-1111-4111-8111-111111111111"


def _cert(root, key, subject):
    now = int(time.time())
    return issue_cert(
        root,
        key.public_hex,
        scope=("tunnel:serve",),
        org=ORG,
        subject=subject,
        not_before=now - 10,
        not_after=now + 3600,
    )


def test_persona_cert_is_refused_as_node_viewer_credential():
    root = KeyPair.generate()
    key = KeyPair.generate()
    cert = _cert(root, key, Subject("persona", "ab" * 32))

    with pytest.raises(ValueError, match="NodeServer viewer credential"):
        NodeServer(
            ORG,
            root.public_hex,
            key,
            cert,
            registry_url="https://registry.auto.network",
            announce_addrs=("ws://192.0.2.10:9410",),
        )


def test_persona_cert_is_refused_even_without_hint_announcer():
    root = KeyPair.generate()
    key = KeyPair.generate()
    cert = _cert(root, key, Subject("persona", "ab" * 32))

    with pytest.raises(ValueError, match="NodeServer viewer credential"):
        NodeServer(ORG, root.public_hex, key, cert, floor_url=None)


def test_floor_serving_key_must_be_distinct_from_reachability_key():
    root = KeyPair.generate()
    node_key = KeyPair.generate()
    node_cert = _cert(root, node_key, Subject("agent", "node"))
    floor_cert = _cert(root, node_key, Subject("persona", "ab" * 32))
    viewer_cert = _cert(root, node_key, Subject("operator", node_key.public_hex))

    with pytest.raises(ValueError, match="cannot join persona to addresses"):
        NodeServer(
            ORG, root.public_hex, node_key, node_cert,
            floor_key=node_key, floor_cert=floor_cert,
            floor_channel_cert=viewer_cert,
            floor_url="wss://relay.auto.network",
            registry_url="https://registry.auto.network",
        )


def test_distinct_floor_serving_key_preserves_reachability_boundary():
    root = KeyPair.generate()
    node_key = KeyPair.generate()
    floor_key = KeyPair.generate()
    node_cert = _cert(root, node_key, Subject("agent", "node"))
    floor_cert = _cert(root, floor_key, Subject("persona", "ab" * 32))
    viewer_cert = _cert(root, floor_key, Subject("operator", floor_key.public_hex))

    NodeServer(
        ORG, root.public_hex, node_key, node_cert,
        floor_key=floor_key, floor_cert=floor_cert,
        floor_channel_cert=viewer_cert,
        floor_url="wss://relay.auto.network",
        registry_url="https://registry.auto.network",
    )
