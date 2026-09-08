"""A serving credential belongs to ONE machine, and the store must say so.

The cardinality bug (auto-527te, evidence graph://90ba11c8-3d3): the serve-cert
row was keyed `default` — one row per org for the whole fleet — while the thing
it names, the serving private key, is a mode-0600 file that is local by design
and must never replicate. The org settings store DOES replicate, so the single
row landed on every machine and only the minting machine held the key. Proven on
sjc-2: rows present, `/app/data/network/` empty, and the fleet-sync catalog shows
the row arriving from home's origin.

Two consequences these tests pin:

* a PEER's row must read as `missing` — not `ok` (which would claim serving that
  cannot work) and not `key-missing` (which reads as a local accident and sent
  the whole investigation after a file deleter that never existed);
* the machine that HOLDS the key keeps its legacy `default` row, so migration
  costs the currently-serving machine nothing.
"""

from __future__ import annotations

import pytest

from tools.dashboard import link_serving_supervisor as lss

MINE = "aa" * 32
PEER = "bb" * 32


class _Member:
    def __init__(self, key, payload):
        self.key = key
        self.payload = payload


def _row(key_file, not_after=2_000_000_000):
    return {"cert": "{}", "key_path": key_file, "root_pub": "cc" * 32,
            "viewer_cert": "{}", "dns01_cert": "{}", "not_after": not_after}


@pytest.fixture
def keydir(tmp_path, monkeypatch):
    """Resolve key locators into a temp serving-key directory."""
    def _resolve(locator):
        if not isinstance(locator, str) or not locator:
            return None, "no locator"
        return str(tmp_path / locator), None
    monkeypatch.setattr(lss, "_resolve_key_path", _resolve)
    return tmp_path


def _own(monkeypatch, key):
    monkeypatch.setattr(lss, "local_serve_cert_key", lambda: key)


def test_this_machines_row_is_mine(keydir, monkeypatch):
    _own(monkeypatch, MINE)
    (keydir / "k.key").write_text("x")
    rows = lss._rows_owned_by_this_machine([_Member(MINE, _row("k.key"))])

    assert len(rows) == 1


def test_a_peers_row_is_not_mine_even_though_it_is_valid(keydir, monkeypatch):
    """THE ONE THAT MATTERS. A peer's credential is perfectly valid — right org,
    unexpired, well-formed. It is simply not ours, and claiming it is what made
    every non-minting machine in the fleet unable to serve."""
    _own(monkeypatch, MINE)
    rows = lss._rows_owned_by_this_machine([_Member(PEER, _row("peer.key"))])

    assert rows == []


def test_a_legacy_shared_row_is_mine_when_i_hold_its_key(keydir, monkeypatch):
    """Migration must cost the currently-serving machine nothing: the machine
    that holds the key is the one that minted it, so it keeps the legacy row."""
    _own(monkeypatch, MINE)
    (keydir / "legacy.key").write_text("x")
    rows = lss._rows_owned_by_this_machine(
        [_Member(lss.LEGACY_SERVE_CERT_KEY, _row("legacy.key"))])

    assert len(rows) == 1


def test_a_legacy_shared_row_is_NOT_mine_when_i_lack_its_key(keydir, monkeypatch):
    """SJC's exact situation: it holds home's replicated row and no key. The
    file's absence is the honest ownership test."""
    _own(monkeypatch, MINE)
    rows = lss._rows_owned_by_this_machine(
        [_Member(lss.LEGACY_SERVE_CERT_KEY, _row("home-only.key"))])

    assert rows == []


def test_serve_cert_state_reports_MISSING_for_a_peers_row(keydir, monkeypatch):
    """Not `key-missing`. The status name is a diagnosis, and `key-missing`
    says 'your file was deleted' when the truth is 'this row was never yours'."""
    _own(monkeypatch, MINE)

    class _Result:
        members = [_Member(PEER, _row("peer.key"))]

    monkeypatch.setattr(lss.settings_ops, "read_owned_set",
                        lambda *a, **k: _Result())
    assert lss.serve_cert_state("acme")["status"] == "missing"


def test_a_mint_on_one_machine_cannot_evict_another(keydir, monkeypatch):
    """The regression that produced the whole outage: home minting a fresh
    credential must leave SJC's own row untouched, and vice versa. With rows
    keyed per machine, each machine sees exactly one row — its own."""
    (keydir / "mine.key").write_text("x")
    members = [_Member(MINE, _row("mine.key", not_after=2_000_000_000)),
               _Member(PEER, _row("peer.key", not_after=2_100_000_000))]

    _own(monkeypatch, MINE)
    mine = lss._rows_owned_by_this_machine(members)
    _own(monkeypatch, PEER)
    theirs = lss._rows_owned_by_this_machine(members)

    assert len(mine) == 1 and mine[0]["key_path"] == "mine.key"
    assert len(theirs) == 1 and theirs[0]["key_path"] == "peer.key"
    # And crucially: the peer's LATER expiry does not win selection for us.
    # The old code took max(not_after) across every row in the fleet.
    assert mine[0]["not_after"] == 2_000_000_000


def test_local_key_falls_back_to_legacy_when_unenrolled(monkeypatch):
    """A single-machine install has no machine id and no peer to collide with;
    it must keep using the legacy key rather than crash or invent one."""
    import tools.network.machine_boot as mb
    monkeypatch.setattr(mb, "machine_id", lambda **k: None)

    assert lss.local_serve_cert_key() == lss.LEGACY_SERVE_CERT_KEY


def test_local_key_is_the_machine_id_when_enrolled(monkeypatch):
    import tools.network.machine_boot as mb
    monkeypatch.setattr(mb, "machine_id", lambda **k: "282ecce1")

    assert lss.local_serve_cert_key() == "282ecce1"
