"""auto-a51qv: a machine offers itself to an organization as a session runner
with a self-certified autonomy.org.session-runner row; members list the
organization's verified offers, live when the machine is in the org's relay
slots. Withdrawing deprecates the row."""

from __future__ import annotations

import copy
import time
from pathlib import Path
from types import SimpleNamespace

from tools.network import fleet_org_reachability as reach
from tools.network import org_session_runner as runner
from tools.network.fleet_sync.tests.test_org_channel_routing import ORG, OTHER_ORG, Member, _cert
from tools.network.idkit import KeyPair


def _offer(member, now, **kw):
    fields = {"label": "SJC-2", "capacity": 4, "harnesses": ["claude", "codex"],
              "images": ["alpha/dev", "alpha/dev"], **kw}
    return runner.build_row(member.machine, member.persona_cert, now=now, **fields)


def test_row_roundtrip_and_every_refusal(tmp_path: Path) -> None:
    pa, px = KeyPair.generate(), KeyPair.generate()
    a = Member(tmp_path, "a", pa, [pa.public_hex])
    now = int(time.time())
    row = _offer(a, now)
    key = f"{pa.public_hex}:{a.machine.public_hex}"
    offer = runner.verify_row(key, row, org=ORG, now=now, is_member=lambda p: p == pa.public_hex)
    assert (offer["label"], offer["capacity"], offer["images"]) == ("SJC-2", 4, ["alpha/dev"])
    assert offer["persona_pub"] == pa.public_hex and offer["machine_pub"] == a.machine.public_hex
    # The key's persona is not the row's.
    assert runner.verify_row(f"{px.public_hex}:{a.machine.public_hex}", row, org=ORG, now=now) is None
    # Tampered capacity: the machine signature no longer covers it.
    tampered = copy.deepcopy(row); tampered["capacity"] = 64
    assert runner.verify_row(key, tampered, org=ORG, now=now) is None
    # The key names another machine.
    assert runner.verify_row(f"{pa.public_hex}:{'00' * 32}", row, org=ORG, now=now) is None
    # Another organization.
    assert runner.verify_row(key, row, org=OTHER_ORG, now=now) is None
    # A persona outside the member set.
    assert runner.verify_row(key, row, org=ORG, now=now, is_member=lambda p: False) is None
    # A certificate from a persona that is not the row's.
    forged = copy.deepcopy(row); forged["persona_cert"] = _cert(px, a.machine).to_dict()
    assert runner.verify_row(key, forged, org=ORG, now=now) is None
    assert runner.verify_row(key, "junk", org=ORG, now=now) is None


def test_a_reachability_signature_is_not_a_runner_offer(tmp_path: Path) -> None:
    """Domain separation: the machine's signature over one kind of row never
    certifies the other."""
    pa = KeyPair.generate()
    a = Member(tmp_path, "a", pa, [pa.public_hex])
    now = int(time.time())
    row = _offer(a, now)
    body = {**row, "machine_pub": a.machine.public_hex}
    body["sig"] = reach.sign_certified(a.machine, body, reach.ROW_DOMAIN)
    del body["machine_pub"]
    assert runner.verify_row(f"{pa.public_hex}:{a.machine.public_hex}", body, org=ORG, now=now) is None


def _dashboard(monkeypatch, member, slots):
    from tools.dashboard import org_runners, org_sync_channels

    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(member.orgs_dir))
    channel = SimpleNamespace(
        machine_key=member.machine, persona_cert=member.persona_cert,
        machine_pub=member.machine.public_hex, persona_pub=member.persona.public_hex, org=ORG,
        is_member=lambda p: p in member.members)
    monkeypatch.setattr(org_runners, "_channel", lambda slug: channel if slug == "alpha" else None)
    monkeypatch.setattr(org_runners, "_label", lambda: "SJC-2")
    monkeypatch.setattr(org_runners, "_default_capacity", lambda: 10)
    monkeypatch.setattr(org_runners, "local_images", lambda slug: ["alpha/dev"])
    monkeypatch.setattr(org_sync_channels, "relay_slots_provider",
                        lambda: (lambda: {"alpha": slots}))
    return org_runners


def test_offer_lists_live_and_withdraw_removes_it(tmp_path: Path, monkeypatch) -> None:
    pa = KeyPair.generate()
    a = Member(tmp_path, "a", pa, [pa.public_hex])
    org_runners = _dashboard(monkeypatch, a, [{"machine": a.machine.public_hex}])
    assert org_runners.runners("alpha")["runners"] == []
    assert org_runners.offer("alpha")["ok"] is True
    (listed,) = org_runners.runners("alpha")["runners"]
    assert (listed["label"], listed["capacity"], listed["live"], listed["this_machine"]) == (
        "SJC-2", 10, True, True)
    assert org_runners.offered_here("alpha") is True
    assert org_runners.withdraw("alpha")["withdrawn"] == 1
    assert org_runners.runners("alpha")["runners"] == []
    assert org_runners.offered_here("alpha") is False


def test_a_runner_not_in_the_relay_slots_is_not_live(tmp_path: Path, monkeypatch) -> None:
    pa = KeyPair.generate()
    a = Member(tmp_path, "a", pa, [pa.public_hex])
    org_runners = _dashboard(monkeypatch, a, [])
    org_runners.offer("alpha", capacity=3)
    (listed,) = org_runners.runners("alpha")["runners"]
    assert (listed["capacity"], listed["live"]) == (3, False)


def test_an_unconfirmed_persona_is_not_listed(tmp_path: Path, monkeypatch) -> None:
    pa = KeyPair.generate()
    a = Member(tmp_path, "a", pa, [pa.public_hex])
    org_runners = _dashboard(monkeypatch, a, [])
    org_runners.offer("alpha")
    channel = org_runners._channel("alpha")
    monkeypatch.setattr(org_runners, "_channel", lambda slug: SimpleNamespace(
        **{**vars(channel), "is_member": lambda p: None}))
    assert org_runners.runners("alpha")["runners"] == []


def test_a_revision_1_row_upconverts_and_still_verifies(tmp_path: Path) -> None:
    """Revision 2 drops persona_pub from the payload (auto-ctunt); the key
    carries it. A stored revision-1 row, which repeated it, upconverts on read
    and its signature still verifies, because the signed bytes include
    persona_pub put back from the key either way."""
    from tools.graph.schemas.org_session_runner import (
        ORG_SESSION_RUNNER_REVISION, ORG_SESSION_RUNNER_SET_ID, OrgSessionRunnerV1)
    from tools.graph.schemas.registry import upconvert_payload

    pa = KeyPair.generate()
    a = Member(tmp_path, "a", pa, [pa.public_hex])
    now = int(time.time())
    row = _offer(a, now)
    assert "persona_pub" not in row
    key = f"{pa.public_hex}:{a.machine.public_hex}"
    v1_row = {**row, "persona_pub": pa.public_hex}
    OrgSessionRunnerV1.validate(v1_row)
    upgraded = upconvert_payload(ORG_SESSION_RUNNER_SET_ID, 1, ORG_SESSION_RUNNER_REVISION, v1_row)
    assert upgraded == row
    offer = runner.verify_row(key, upgraded, org=ORG, now=now)
    assert offer is not None and offer["persona_pub"] == pa.public_hex
    # A revision-2 payload that repeats the key's persona is refused.
    assert runner.verify_row(key, v1_row, org=ORG, now=now) is None
