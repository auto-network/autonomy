"""An org connector must present a membership rider, or it cannot serve at all.

A collaborative org's serve cert is persona-signed (network-signon.mjs mints it
under the org persona). The relay anchors a hello at that persona ONLY for v3;
a v2 hello anchors at the org root and is refused with "hop 1: signature does
not verify against its parent key". Every org connector on sjc-2 hit exactly
that on 2026-09-10, the moment they could start at all — the certs verify under
their persona and cannot verify under the root.

membership_plane (auto-3bhy3) has always been able to build the proof; nothing
called it. These cover the wire: who gets a rider, who must not, and what
happens when the proof cannot be built.
"""

from __future__ import annotations

import asyncio

import pytest

from tools.dashboard import link_serving


class _Cert:
    class subject:
        id = "pe" * 32

    org = "org-uuid"


class _Publisher:
    def attach(self, *a, **k):
        pass


def _build(monkeypatch, *, graph_org, machine_key=object(), rider=None,
           raises=None):
    """Construct the production connector against a capturing factory."""
    captured = {}

    def factory(relay, org, key, cert, handler, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        link_serving, "make_ice_grant_handler",
        lambda *a, **k: object(),
    )
    from tools.dashboard import membership_plane
    from tools.dashboard import link_approvals

    monkeypatch.setattr(
        link_approvals, "_load_binding",
        lambda scope: ({"registry_url": "https://registry.test",
                        "org_uuid": "org-uuid", "root_pub": "ab" * 32}, None),
    )

    def _rider_for_org(org, persona, state):
        if raises is not None:
            raise raises
        return {**rider, "persona": persona, "seq_seen": state["seq"]}

    monkeypatch.setattr(membership_plane, "rider_for_org", _rider_for_org)
    monkeypatch.setattr(
        link_serving, "_urlopen_membership", None, raising=False,
    )
    link_serving._make_ice_serving_connector(
        "wss://registry.test",
        "org-uuid",
        key=object(),
        cert=_Cert(),
        channel_cert=None,
        graph_org=graph_org,
        publisher=_Publisher(),
        min_backoff=0.1,
        max_backoff=1.0,
        machine_key=machine_key,
        connector_factory=factory,
    )
    return captured


def test_an_org_connector_is_given_a_rider(monkeypatch):
    captured = _build(monkeypatch, graph_org="anchore")
    assert callable(captured.get("membership_proof_for"))
    assert callable(captured.get("on_reprove"))


def test_the_personal_connector_is_not(monkeypatch):
    """Launched without --graph-org. Its cert is root-signed by the legacy
    mint and its v2 hello is correct; handing it a rider would move its anchor
    to a persona that never signed it."""
    captured = _build(monkeypatch, graph_org=None)
    assert "membership_proof_for" not in captured
    assert "on_reprove" not in captured


def test_a_connector_with_no_machine_key_is_not(monkeypatch):
    """No machine key means no v2/v3 hello at all — the connector refuses to
    dial rather than degrade, so a rider would be meaningless."""
    captured = _build(monkeypatch, graph_org="anchore", machine_key=None)
    assert "membership_proof_for" not in captured


def test_the_rider_carries_the_certs_persona_and_the_registrys_seq(monkeypatch):
    captured = _build(
        monkeypatch, graph_org="anchore", rider={"v": 1, "index": 0, "path": []},
    )
    monkeypatch.setattr(
        link_serving, "_membership_state_reader", None, raising=False,
    )
    import urllib.request

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"seq": 7, "members_root": "cafe"}'

    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None: _Response())
    rider = asyncio.run(captured["membership_proof_for"]())
    assert rider["persona"] == _Cert.subject.id
    assert rider["seq_seen"] == 7


def test_a_proof_that_cannot_be_built_falls_back_instead_of_crashing(
    monkeypatch,
):
    """Including the contested-root refusal. A connector that cannot prove
    membership must keep retrying; raising into the handshake would kill it."""
    captured = _build(
        monkeypatch, graph_org="anchore",
        raises=RuntimeError("membership chain contested"),
    )
    import urllib.request

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"seq": 7, "members_root": "cafe"}'

    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None: _Response())
    assert asyncio.run(captured["membership_proof_for"]()) is None
    assert asyncio.run(captured["on_reprove"](7)) is None
