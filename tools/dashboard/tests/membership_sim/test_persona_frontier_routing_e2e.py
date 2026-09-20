"""Acceptance for persona-frontier routing on the simulator (auto-xs9hz,
graph://d9153c5a-76e O-C, constitution graph://6ad52a52-f75 piece 5).

Everything real: a registry subprocess, two admitted member personas A and
B each with its own authenticated connector, and a real viewer dialing the
link with only its fragment key. A publishes a note link OVER ITS TUNNEL
(create-link carrying R naming its own persona at the note's
write time), so the requirement is recorded the way a dashboard records
it. B holds the link key too, so any dial that reached B would serve; the
only thing keeping B out is routing.

What "behind" means here. The simulator has no org stores and no fleet
sync, so B's lag is represented at the one point the relay can observe
it: B's own sync-frontier advert names A's persona at a timestamp older
than the note. A dashboard derives that advert from its persona cut
records; this scenario states it directly. Healing B is B advertising a
frontier that covers the note.

What the registry log shows. The registry runs with ``--log-level info``
(auto-0tfuz), so besides the control-op result lines
(``control ... op=create-link ... result=ok``) and the refusal
warnings, every dial's ``relay dial routed`` line names the machine it
was routed to. Routing is asserted both there and at the tunnels: A's
connector counts ten channel opens and B's counts zero.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
import uuid

import pytest

from tools.network.idkit import KeyPair
from tools.network.relaykit.close_codes import CLOSE_NO_COVERING_MEMBER
from tools.network.relaykit.connector import TunnelConnector
from tools.network.relaykit.viewer import ViewerChannel

from ._harness import (
    CONTENT, Org, Registry, RoleSpec, _proof_resolver, _serve_cert, assert_member, fetch,
    run_connector,
)


@pytest.fixture
def registry(tmp_path):
    reg = Registry(tmp_path, log_level="info")
    try:
        yield reg
    finally:
        reg.stop()


class _Member:
    """One member dashboard's connector: its persona, its machine key, the
    link keys it holds, and a count of every channel the relay opened on
    it (the authorization callback runs once per routed open)."""

    def __init__(self, registry, org, persona, *, seq: int):
        self.persona = persona
        self.serve_key, self.machine_key = KeyPair.generate(), KeyPair.generate()
        self.held: dict = {}
        self.opens = 0

        def authorization(token):
            self.opens += 1
            key = self.held.get(token)
            if key is None:
                raise PermissionError("link unavailable")
            return {"protocol": "public-link", "key": key}

        async def handler(token, message):
            return b'{"v": 1, "status": "ok"}\n' + CONTENT

        self.conn = TunnelConnector(
            registry.ws, registry.org, self.serve_key,
            _serve_cert(persona, self.serve_key, registry.org),
            handler=handler, machine_key=self.machine_key, caps=(),
            membership_proof_for=_proof_resolver(org.member_pubs(), persona, seq),
            channel_authorization_for=authorization, min_backoff=0.1, max_backoff=1.0,
        )

    @property
    def machine16(self) -> str:
        return self.machine_key.public_hex[:16]


def _wait_log(path, pattern: str, *, timeout: float = 10.0) -> str:
    """Block until the registry log carries a line matching *pattern*."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = path.read_text(errors="replace")
        match = re.search(pattern, text)
        if match:
            return match.group(0)
        time.sleep(0.05)
    raise AssertionError(f"registry log never showed {pattern!r}:\n{path.read_text(errors='replace')[-2000:]}")


def _close_frame(exc: BaseException):
    """The (code, reason) of the close that ended a viewer dial, from the
    websockets exception chain."""
    seen = exc
    while seen is not None:
        rcvd = getattr(seen, "rcvd", None)
        if rcvd is not None:
            return int(rcvd.code), str(rcvd.reason)
        seen = seen.__cause__ or seen.__context__
    return None


def test_a_link_is_served_only_by_members_whose_frontier_covers_its_authors(registry, tmp_path):
    log = tmp_path / "registry.log"
    org = Org.found(roles={"member": RoleSpec(scope_set=("link:publish",), requires="self")})
    persona_b = org.admit("bob", "member")
    assert_member(org.fold(), persona_b, roles=["member"])
    registry.register(org.sim.root)
    registry.commit_checkpoint(org.sim, seq=0)

    a = _Member(registry, org, org.founder, seq=0)
    b = _Member(registry, org, persona_b, seq=0)
    link_key = KeyPair.generate()
    persona_a = org.founder.public_hex
    note_uuid = str(uuid.uuid4())
    evidence: dict = {}

    async def run():
        # B registers first: after the fresh-link pin window least-loaded
        # ordering would try it first, so nothing but routing keeps it out.
        async with run_connector(b.conn):
            async with run_connector(a.conn):
                # A writes the note and publishes its link over its own tunnel.
                written_at_ns = time.time_ns()
                # The grant row committed; R names the author persona at the
                # newest timestamp of the rows the link serves, and rides in
                # the one create-link with the publisher-minted grant id (O-C).
                stamp = time.time_ns()
                assert stamp >= written_at_ns
                made = await a.conn.control("create-link", {
                    "target_uuid": note_uuid, "target_type": "note",
                    "requires": {persona_a: stamp}, "grant_id": "cd" * 16,
                })
                assert made.get("ok") is True, made
                token = made["token"]
                a.held[token] = link_key
                b.held[token] = link_key
                evidence["requires_line"] = _wait_log(
                    log, rf"control org=\S+ op=create-link id={made['id']} result=ok",
                )
                # A's frontier covers its own write; B's advert is behind on
                # A's persona (its store has not received A's persona cut).
                assert (await a.conn.control("sync-frontier", {
                    "org_uuid": registry.org, "frontiers": {persona_a: stamp},
                })).get("ok") is True
                behind = await b.conn.control("sync-frontier", {
                    "org_uuid": registry.org, "frontiers": {persona_a: written_at_ns - 1},
                })
                assert behind.get("ok") is True

                for _ in range(10):
                    assert await fetch(registry, token, link_key.public_hex) == CONTENT
                routed = re.findall(
                    r"relay dial routed: token=\S+ org=\S+ tunnel machine=(\S+) persona=(\S+) ",
                    log.read_text(errors="replace"),
                )
                evidence["routed_before_heal"] = routed
                evidence["opens_a_before_heal"] = a.opens
                evidence["opens_b_before_heal"] = b.opens
                evidence["token"] = token
                evidence["stamp"] = stamp
                evidence["written_at_ns"] = written_at_ns

            # A's connector is gone while B is still behind: the pool is not
            # empty, nobody covers the link, and its publisher has no tunnel.
            _wait_log(log, rf"tunnel\.unregister org=\S+ persona=\S+ machine={a.machine16}")
            try:
                channel = await ViewerChannel.connect(
                    registry.ws, token, link_pub=link_key.public_hex, org=registry.org,
                )
                await channel.send_message(b'{"v": 1, "op": "fetch"}')
                await channel.recv_message()
            except Exception as exc:  # noqa: BLE001 - the close IS the result
                evidence["refused_close"] = _close_frame(exc)
            else:
                raise AssertionError("a dial with no covering member was served")
            evidence["refused_line"] = _wait_log(log, r"relay dial refused \(4431\)[^\n]*")
            assert b.opens == evidence["opens_b_before_heal"], "the refusal never dialed B"

            # B heals: its advert now covers A's persona at the note's stamp.
            healed = await b.conn.control("sync-frontier", {
                "org_uuid": registry.org, "frontiers": {persona_a: stamp},
            })
            assert healed.get("ok") is True
            evidence["heal_line"] = _wait_log(
                log, rf"control org=\S+ op=sync-frontier id={healed['id']} result=ok",
            )
            for _ in range(10):
                assert await fetch(registry, token, link_key.public_hex) == CONTENT
            evidence["opens_b_after_heal"] = b.opens

    asyncio.run(run())

    # Ten of ten dials were served by A alone while B was behind: the
    # relay's own routing lines name A's machine ten times and B's never.
    assert evidence["opens_a_before_heal"] == 10, evidence
    assert evidence["opens_b_before_heal"] == 0, evidence
    routed_machines = [machine for machine, _persona in evidence["routed_before_heal"]]
    assert routed_machines == [a.machine16] * 10, evidence["routed_before_heal"]
    assert all(persona == persona_a[:16] for _m, persona in evidence["routed_before_heal"])
    assert b.machine16 not in routed_machines
    # With A gone and B behind, the one dial closed 4431 with the reason.
    assert evidence["refused_close"] == (
        CLOSE_NO_COVERING_MEMBER, "no member has synced this link yet",
    ), evidence
    assert "none covers the link's 1 persona(s), and its publisher has no tunnel" in evidence["refused_line"]
    # After B's advert covered A's persona, dials reached B.
    assert evidence["opens_b_after_heal"] >= 1, evidence
    assert evidence["opens_b_after_heal"] == 10, evidence

    # The link row's requires column names A's persona at a timestamp at or
    # after the note's write.
    with sqlite3.connect(f"file:{registry._db}?mode=ro", uri=True) as conn:
        raw = conn.execute(
            "SELECT requires FROM links WHERE token=?", (evidence["token"],)
        ).fetchone()[0]
    assert json.loads(raw) == {persona_a: evidence["stamp"]}
    assert evidence["stamp"] >= evidence["written_at_ns"]

    text = log.read_text(errors="replace")
    assert text.count("relay dial refused (4431)") == 1, text[-2000:]
    # After the heal every dial was routed to B, A being gone.
    routed_all = re.findall(r"relay dial routed: token=\S+ org=\S+ tunnel machine=(\S+) ", text)
    assert routed_all[10:] == [b.machine16] * 10, routed_all
    assert "relay viewer failover" not in text, "no candidate refused after being dialed"
    (tmp_path / "persona-frontier-evidence.json").write_text(json.dumps(evidence, indent=1, sort_keys=True))
