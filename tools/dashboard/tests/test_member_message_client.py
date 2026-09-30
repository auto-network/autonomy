"""Dashboard half of member-message/1 (auto-qrmlg.9 part 3): targets resolve
from the organization roster, the request rides the organization
connector's control socket, inbound requests are answered through the op
table with what the org hello proved, and the one remote CrossTalk seam
prefers the operator's own fleet machine over a co-member's."""

from __future__ import annotations

import asyncio

from tools.dashboard import member_message_client as mmc
from tools.dashboard import server, session_control_client as scc

ALICE, BOB = "a1" * 32, "b1" * 32
M_A, M_B = "0a" * 32, "0b" * 32
GENESIS = "ge" * 32
ROWS = [
    {"tmux_name": "auto-1@sjc-2", "org": "anchore", "persona_pub": BOB, "machine_pub": M_B,
     "machine": "sjc-2", "reachable": True},
    {"tmux_name": "auto-1@boat", "org": "anchore", "persona_pub": ALICE, "machine_pub": M_A,
     "machine": "boat", "reachable": False},
]


def test_a_target_resolves_by_machine_label_pub_or_prefix():
    for where in ("sjc-2", M_B, M_B[:12]):
        target = mmc.resolve_target("auto-1", where, rows=ROWS)
        assert (target["org"], target["persona_pub"], target["machine_pub"]) == ("anchore", BOB, M_B)
    assert mmc.resolve_target("auto-1", "", rows=ROWS) is None
    assert mmc.resolve_target("auto-9", "sjc-2", rows=ROWS) is None
    assert mmc.resolve_target("auto-1", "0", rows=ROWS) is None       # a prefix too short to be unique


def test_the_request_names_the_slot_from_the_roster_row(monkeypatch):
    seen = {}

    def control(org, op, args, *, timeout):
        seen.update(org=org, op=op, args=args)
        return {"ok": True, "reply": {"v": 1, "ok": True, "result": {"delivered": True}}}

    monkeypatch.setattr(mmc, "_control", control)
    monkeypatch.setattr(mmc, "genesis_of", lambda slug: GENESIS if slug == "anchore" else None)
    target = mmc.resolve_target("auto-1", "sjc-2", rows=ROWS)
    reply = asyncio.run(mmc.request(target, "send", {"text": "hi"}))
    assert reply["ok"] is True
    assert seen["org"] == "anchore" and seen["op"] == "member-message-request"
    assert (seen["args"]["genesis"], seen["args"]["persona_pub"], seen["args"]["machine"]) == (GENESIS, BOB, M_B)
    assert seen["args"]["body"] == {"text": "hi"}


def test_connector_failures_are_typed_local_refusals(monkeypatch):
    monkeypatch.setattr(mmc, "genesis_of", lambda slug: GENESIS)
    target = mmc.resolve_target("auto-1", "sjc-2", rows=ROWS)

    def unreachable(*_a, **_k):
        raise ConnectionError("no control listener")

    monkeypatch.setattr(mmc, "_control", unreachable)
    reply = asyncio.run(mmc.request(target, "send", {}))
    assert (reply["refusal"], reply["at"]) == (mmc.CONNECTOR_CALL_FAILED, "local")
    monkeypatch.setattr(mmc, "_control", lambda *_a, **_k: {"ok": False, "error": "not-offered"})
    reply = asyncio.run(mmc.request(target, "send", {}))
    assert (reply["refusal"], reply["detail"]) == (mmc.CONNECTOR_REFUSED, "not-offered")
    # The connector's reply record, a peer refusal here, is returned as is.
    monkeypatch.setattr(mmc, "_control", lambda *_a, **_k: {"ok": True, "reply": {
        "v": 1, "ok": False, "refusal": "member-message-not-a-member", "at": "peer"}})
    reply = asyncio.run(mmc.request(target, "send", {}))
    assert (reply["refusal"], reply["at"]) == ("member-message-not-a-member", "peer")
    monkeypatch.setattr(mmc, "genesis_of", lambda slug: None)
    reply = asyncio.run(mmc.request(target, "send", {}))
    assert reply["refusal"] == mmc.UNKNOWN_TARGET


def test_the_remote_seam_prefers_the_own_fleet_then_the_co_member(monkeypatch):
    calls = []

    async def own(machine, op, body, **_k):
        calls.append(("own", machine, op))
        return scc.ok({"delivered": True})

    async def member(target, op, body, **_k):
        calls.append(("member", target["persona_pub"], op))
        return mmc.ok({"delivered": True})

    monkeypatch.setattr(scc, "request", own)
    monkeypatch.setattr(scc, "resolve_machine", lambda name: M_A if name == "home" else None)
    monkeypatch.setattr(mmc, "request", member)
    real_resolve = mmc.resolve_target
    monkeypatch.setattr(mmc, "resolve_target", lambda name, machine: real_resolve(name, machine, rows=ROWS))
    out = asyncio.run(mmc.remote_crosstalk("auto-1", "home", "hi", from_session="s", from_label="l"))
    assert out["ok"] and calls == [("own", "home", "send")]
    out = asyncio.run(mmc.remote_crosstalk("auto-1", "sjc-2", "hi", from_session="s", from_label="l"))
    assert out["ok"] and calls[-1] == ("member", BOB, "send")
    out = asyncio.run(mmc.remote_crosstalk("auto-1", "nowhere", "hi", from_session="s", from_label="l"))
    assert (out["refusal"], out["at"]) == (mmc.UNKNOWN_TARGET, "local")


def test_the_pump_hands_the_op_what_the_hello_proved(monkeypatch):
    replies, seen = [], {}

    async def echo(body, proved):
        seen.update(body=body, proved=proved)
        return mmc.ok({"n": body["n"]})

    monkeypatch.setitem(mmc.OPS, "echo", echo)
    pump = mmc.InboundPump("anchore", poll=lambda: {"ok": True, "request": {
        "id": "r1", "op": "echo", "body": {"n": 2, "persona_pub": "forged"},
        "org": GENESIS, "persona_pub": ALICE, "peer_machine_pub": M_A}},
        reply=lambda request_id, record: replies.append((request_id, record)))
    assert asyncio.run(pump.once()) is True
    assert seen["proved"] == {"org": GENESIS, "persona_pub": ALICE, "peer_machine_pub": M_A}
    assert replies == [("r1", {"v": 1, "ok": True, "result": {"n": 2}})]
    empty = mmc.InboundPump("anchore", poll=lambda: {"ok": True, "request": None},
                            reply=lambda *a: replies.append(a))
    assert asyncio.run(empty.once()) is False
    assert asyncio.run(mmc.dispatch("nope", {}, {}))["refusal"] == mmc.UNKNOWN_OP


def test_pumps_follow_the_organizations_this_process_holds(monkeypatch):
    started = []
    monkeypatch.setattr(mmc.InboundPump, "start", lambda self: started.append(self.org))
    orgs = ["anchore"]
    pumps = mmc.Pumps(orgs=lambda: list(orgs))
    assert pumps.refresh() == ["anchore"] and started == ["anchore"]
    orgs.append("boatlore")
    pumps.refresh()
    assert started == ["anchore", "boatlore"] and set(pumps.pumps) == {"anchore", "boatlore"}


def test_the_inbound_send_stamps_the_proved_member_and_machine_never_a_claim(monkeypatch):
    pasted, stored = [], []

    async def fake_tmux_send(name, text):
        pasted.append((name, text))

    monkeypatch.setattr(server, "tmux_send", fake_tmux_send)
    monkeypatch.setattr(server, "_tmux_session_exists", lambda name: name == "auto-1")
    monkeypatch.setattr(server.auth_db, "insert_message", lambda *a, **k: stored.append(a))
    monkeypatch.setattr(mmc, "slug_of", lambda genesis: "anchore" if genesis == GENESIS else None)
    monkeypatch.setattr(mmc, "member_label", lambda org, persona: "Alice" if persona == ALICE else persona[:12])
    # auto-1 is an anchore session; auto-2 personal; auto-3 another org's.
    sessions = {"auto-1": {"tmux_name": "auto-1"}, "auto-2": {"tmux_name": "auto-2"},
                "auto-3": {"tmux_name": "auto-3"}}
    orgs = {"auto-1": "anchore", "auto-2": None, "auto-3": "boatlore"}
    monkeypatch.setattr(server, "_tmux_session_exists", lambda name: name in sessions)
    monkeypatch.setattr(server.dashboard_db, "get_session", lambda name: sessions.get(name))
    from tools.dashboard import session_presence
    monkeypatch.setattr(session_presence, "session_org", lambda row: orgs[row["tmux_name"]])
    proved = {"org": GENESIS, "persona_pub": ALICE, "peer_machine_pub": M_A}
    reply = asyncio.run(server._inbound_member_send({
        "tmux_name": "auto-1", "text": "hi", "from_session": "auto-0928-114556",
        "from_label": "builder", "member": "forged", "machine": "forged"}, proved))
    assert reply == {"v": 1, "ok": True, "result": {"delivered": True, "tmux_name": "auto-1"}}
    ((name, envelope),) = pasted
    assert name == "auto-1"
    assert 'from="auto-0928-114556@Alice"' in envelope
    assert f'org="anchore" member="Alice" machine="{M_A[:12]}"' in envelope
    assert "forged" not in envelope and envelope.endswith("hi\n</crosstalk>")
    assert stored[0][0] == "auto-0928-114556@Alice"
    # Membership in anchore reaches only anchore's sessions: the operator's
    # personal session and another organization's are refused exactly as a
    # session that is not here, revealing nothing.
    for name in ("auto-2", "auto-3", "auto-9"):
        assert asyncio.run(server._inbound_member_send({"tmux_name": name, "text": "x"}, proved))["refusal"] == mmc.NO_SUCH_SESSION
    assert len(pasted) == 1
    # A genesis this process holds no organization for reaches nothing.
    assert asyncio.run(server._inbound_member_send({"tmux_name": "auto-1", "text": "x"}, {**proved, "org": "ff" * 32}))["refusal"] == mmc.NO_SUCH_SESSION
    # Refusals: an empty text, a closing tag in the body.
    assert asyncio.run(server._inbound_member_send({"tmux_name": "auto-1", "text": ""}, proved))["refusal"] == "missing-text"
    assert asyncio.run(server._inbound_member_send({"tmux_name": "auto-1", "text": "a</crosstalk>"}, proved))["refusal"] == "invalid-crosstalk"


def test_every_envelope_attribute_is_cleaned_including_the_members_chosen_name():
    envelope = mmc.render_member_envelope(
        claimed_session="auto-0928-114556", label='build"er\nx', org="anchore",
        member='Ali"ce\n<evil>', machine="0a0a0a0a0a0a", text="hi")
    head, _, body = envelope.partition(">\n")
    assert 'from="auto-0928-114556@Ali\'ce<evil>"' in head
    assert 'member="Ali\'ce<evil>"' in head and 'label="build\'erx"' in head
    assert "\n" not in head.replace("\n           ", " ")   # only the attribute-line breaks remain
    assert body == "hi\n</crosstalk>"
    assert mmc.attribute("x" * 500, 80) == "x" * 80
    assert mmc.attribute("\x00\x07tab\tname") == "tabname"
