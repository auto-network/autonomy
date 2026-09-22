"""A refused link-key resolution is generic on the socket and NAMED in the
dashboard log (tools/dashboard/connector_key_resolution.respond)."""
from __future__ import annotations

import logging

from tools.dashboard import connector_key_resolution as ckr
from tools.network.idkit import KeyPair


def _grant(pub: str) -> dict:
    return {"target_type": "note", "channel_pub": pub, "token": "cd" * 16}


def test_respond_logs_the_reason_and_answers_generically(monkeypatch, caplog):
    monkeypatch.setattr(ckr, "_authorized_grant",
                        lambda request: ("cd" * 16, "dynbench", _grant("deadbeef"), "cd" * 16))
    monkeypatch.setattr("tools.dashboard.link_channel_key.channel_key_for",
                        lambda token, org: (_ for _ in ()).throw(
                            RuntimeError("the link's channel key did not open: no key holder")))
    caplog.set_level(logging.WARNING, logger="dashboard.connector_key_resolution")
    reply = ckr.respond({"token": "cd" * 16, "credential_id": "x", "auth": "y"})
    assert reply == {"ok": False, "error": "link key resolution refused"}
    assert ("link key resolution refused for link cdcdcdcd...: RuntimeError: "
            "the link's channel key did not open: no key holder") in caplog.text
    # The request itself (credential, auth) never reaches the log.
    assert "auth" not in caplog.text and "credential_id" not in caplog.text


def test_resolve_names_which_check_failed(monkeypatch):
    pair = KeyPair.generate()
    monkeypatch.setattr("tools.dashboard.link_channel_key.channel_key_for", lambda token, org: pair)
    # The vaulted key does not match the grant's channel_pub.
    monkeypatch.setattr(ckr, "_authorized_grant",
                        lambda request: ("cd" * 16, "dynbench", _grant("not-this-key"), "cd" * 16))
    monkeypatch.setattr("tools.dashboard.link_serving.check_grant",
                        lambda token, org=None, now=None, grant_id=None: _grant("not-this-key"))
    try:
        ckr.resolve({})
    except PermissionError as exc:
        assert "does not match the grant's channel_pub" in str(exc)
    else:
        raise AssertionError("expected a refusal")
    # The key matches but the grant changed between the two reads.
    grant = _grant(pair.public_hex)
    monkeypatch.setattr(ckr, "_authorized_grant", lambda request: ("cd" * 16, "dynbench", grant, "cd" * 16))
    monkeypatch.setattr("tools.dashboard.link_serving.check_grant",
                        lambda token, org=None, now=None, grant_id=None: {**grant, "expires_at": 1})
    try:
        ckr.resolve({})
    except PermissionError as exc:
        assert "changed between the two reads" in str(exc)
    else:
        raise AssertionError("expected a refusal")
    # Everything agrees: the seed is released.
    monkeypatch.setattr("tools.dashboard.link_serving.check_grant",
                        lambda token, org=None, now=None, grant_id=None: grant)
    assert ckr.resolve({}) == {"ok": True, "seed": pair.private_hex}
