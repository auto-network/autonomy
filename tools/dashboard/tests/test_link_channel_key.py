"""Per-link channel key (auto-ryr2z): mint, resolve, drop, fragment coding.

The vault sealing/opening itself is proven by the vault suite; here the
settings seam is faked with an in-memory store so these tests cover the
link_channel_key module's own logic — key generation, seed round-trip, the
fail-closed paths, and the fragment encode/decode — without standing up an
org storage domain.
"""

from __future__ import annotations

import base64

import pytest

from tools.dashboard import link_channel_key as lck
from tools.graph.schemas.network_identity import (
    NETWORK_LINK_CHANNEL_KEY_SET_ID,
    NETWORK_PUB_HEX_LEN,
)
from tools.network.idkit import KeyPair

TOKEN = "a" * 32
ORG = "acme"


class _FakeSettings:
    """Minimal stand-in for the vaulted settings seam: an in-memory
    {(set_id, key): payload} plus the read/remove shapes the module uses."""

    def __init__(self):
        self.rows = {}
        self.raise_on_write = False

    def add_setting(self, set_id, rev, key, payload, *, org=None, **kw):
        if self.raise_on_write:
            raise RuntimeError("vault is cold")
        self.rows[(set_id, key)] = dict(payload)
        return f"id-{key}"

    def read_set_key(self, set_id, key, *, org=None, peers=None):
        payload = self.rows.get((set_id, key))
        return None if payload is None else {"payload": payload}

    def read_owned_set(self, set_id, *, org=None, target_revision=None):
        class _M:
            def __init__(self, key, id_):
                self.key, self.id = key, id_
        members = [_M(k, f"id-{k}") for (s, k) in self.rows if s == set_id]

        class _R:
            pass
        r = _R()
        r.members = members
        return r

    def remove_setting(self, member_id, *, org=None):
        for (s, k) in list(self.rows):
            if f"id-{k}" == member_id:
                del self.rows[(s, k)]


@pytest.fixture
def seam(monkeypatch):
    fake = _FakeSettings()
    monkeypatch.setattr(lck.settings_ops, "add_setting", fake.add_setting)
    monkeypatch.setattr(lck.settings_ops, "read_set_key", fake.read_set_key)
    monkeypatch.setattr(lck.settings_ops, "read_owned_set", fake.read_owned_set)
    monkeypatch.setattr(lck.settings_ops, "remove_setting", fake.remove_setting)
    return fake


def test_mint_vaults_seed_and_returns_matching_pub(seam):
    pub = lck.mint_channel_key(TOKEN, ORG)
    assert len(pub) == NETWORK_PUB_HEX_LEN
    row = seam.rows[(NETWORK_LINK_CHANNEL_KEY_SET_ID, TOKEN)]
    assert KeyPair.from_private_hex(row["seed"]).public_hex == pub


def test_channel_key_for_round_trips(seam):
    pub = lck.mint_channel_key(TOKEN, ORG)
    pair = lck.channel_key_for(TOKEN, ORG)
    assert pair.public_hex == pub


def test_mint_fails_closed_when_vault_cold(seam):
    seam.raise_on_write = True
    with pytest.raises(lck.ChannelKeyUnavailable, match="could not vault"):
        lck.mint_channel_key(TOKEN, ORG)
    assert (NETWORK_LINK_CHANNEL_KEY_SET_ID, TOKEN) not in seam.rows


def test_resolve_absent_link_fails_closed(seam):
    with pytest.raises(lck.ChannelKeyUnavailable, match="legacy or revoked"):
        lck.channel_key_for("f" * 32, ORG)


def test_resolve_reports_vault_error(seam, monkeypatch):
    monkeypatch.setattr(
        lck.settings_ops, "read_set_key",
        lambda *a, **k: {"vault_error": "no_key_holder"})
    with pytest.raises(lck.ChannelKeyUnavailable, match="did not open"):
        lck.channel_key_for(TOKEN, ORG)


def test_drop_removes_the_seed_row(seam):
    lck.mint_channel_key(TOKEN, ORG)
    assert lck.drop_channel_key(TOKEN, ORG) is True
    assert (NETWORK_LINK_CHANNEL_KEY_SET_ID, TOKEN) not in seam.rows
    with pytest.raises(lck.ChannelKeyUnavailable):
        lck.channel_key_for(TOKEN, ORG)


class TestFragment:
    def test_round_trip(self):
        pub = KeyPair.generate().public_hex
        url = lck.fragment_url("https://relay.auto.network/l/" + TOKEN, pub)
        assert "#" in url and "=" not in url.split("#", 1)[1]
        frag = url.split("#", 1)[1]
        assert lck.pub_from_fragment(frag) == pub

    def test_fragment_is_the_base64url_of_the_raw_key(self):
        pub = "11" * 32
        frag = lck.fragment_url("x", pub).split("#", 1)[1]
        assert frag == base64.urlsafe_b64encode(bytes.fromhex(pub)).decode().rstrip("=")

    def test_wrong_length_fragment_refused(self):
        with pytest.raises(ValueError, match="32-byte"):
            lck.pub_from_fragment(base64.urlsafe_b64encode(b"short").decode().rstrip("="))
