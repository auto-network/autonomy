"""The shared membership-lifecycle simulation harness (auto-whf4h).

Every scenario in this package runs the same real stack: a registry
SUBPROCESS (its own DB, real HTTP + WebSocket relay); an org ledger built
with the ``Sim`` testkit and committed to the registry as a root-signed
membership checkpoint; independent serving identities — each its own serving
key, persona-signed certificate, and membership-proof resolver — dialing that
one registry over a real ``TunnelConnector``; and a real ``ViewerChannel``
fetching through them. Nothing hard-codes a production host: the relay URL
is ``ws://127.0.0.1:<port>`` of the subprocess.

Verdicts assert on two surfaces (design note graph://3c07f8ca-932):

* the FOLD — ``state.valid[eid]`` / ``state.reasons[eid]``. The fold never
  raises on an unauthorized event; it marks it invalid and records one of the
  ``R_*`` reason strings from ``tools.network.ledger.fold``;
* the REGISTRY — whether an identity's v3 hello is admitted or refused.

Layer: this harness drives the ledger, the registry, and the tunnel. It does
not drive the browser sign-on ceremony (a vector test owns that) and it does
not post events through dashboard HTTP routes — case 2 (auto-liwgd) adds the
over-the-wire ``role.define`` path once that route lands (auto-7l0ku). The
dashboard-layer ``org_authority.authorize`` reads the org ledger from storage
and cannot see a ``Sim``; assert authority here with ``state.holds``.

The seams were lifted from ``test_membership_federation_flow.py`` (the
acceptance suite, left intact) so that scenario modules compose named
building blocks instead of re-standing the stack.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence

import httpx

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.ledger import membership_commitment as mc
from tools.network.ledger.tests.conftest import Sim
from tools.network.registry.signing import sign_request
from tools.network.relaykit.connector import TunnelConnector
from tools.network.relaykit.viewer import ViewerChannel

from ..test_link_serving_tunnel import ORG_UUID, free_port, start_registry

DAY = 86_400
CONTENT = b"<html><body>membership-sim content, byte-exact</body></html>"


# ── the registry subprocess ───────────────────────────────────


class Registry:
    """One real registry subprocess. ``start`` / ``stop`` bracket its life;
    the HTTP helpers are the few registry calls every scenario needs."""

    def __init__(self, tmp_path, org: str = ORG_UUID):
        self.org = org
        self.port = free_port()
        self._proc = start_registry(
            self.port, tmp_path / "registry.db", tmp_path / "registry.log")

    @property
    def http(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def stop(self):
        self._proc.terminate()
        self._proc.wait(timeout=5)

    def register(self, root: KeyPair):
        """Bind the org to *root* — the registry's constitutional anchor."""
        with httpx.Client(base_url=self.http) as client:
            r = client.post("/v1/orgs", json=sign_request(
                root, "POST", "/v1/orgs",
                {"org_uuid": self.org, "root_pub": root.public_hex,
                 "recovery_policy": "none"},
                ts=int(time.time())))
            assert r.status_code == 201, r.text

    def commit_checkpoint(self, sim: Sim, *, seq: int = 0, ts: Optional[int] = None) -> dict:
        """Root-sign and adopt a checkpoint committing the CURRENT fold. A
        fresh org's seed (seq 0) legitimately commits everyone at once; a
        root-signed record at a higher seq is a reset that advances past
        member-signed history (membership_commitment: root strictly advances
        seq)."""
        state = sim.fold()
        record = mc.build_root_checkpoint(
            org=self.org, seq=seq, genesis_id=sim.genesis_id,
            ledger_head=sorted(state.heads)[0] if state.heads else sim.genesis_id,
            members_root_hex=mc.members_root(state),
            checkpointers_root_hex=mc.checkpointers_root(state),
            ts=ts or int(time.time()), root=sim.root)
        with httpx.Client(base_url=self.http) as client:
            r = client.post(f"/v1/orgs/{self.org}/membership-checkpoints", json=record)
            assert r.status_code == 201, r.text
        return record

    def membership_state(self) -> Optional[dict]:
        """The registry's adopted membership tuple, or None before a seed."""
        with httpx.Client(base_url=self.http) as client:
            r = client.get(f"/v1/orgs/{self.org}/membership")
            return r.json() if r.status_code == 200 else None

    def publish(self, root: KeyPair, target_type: str = "note") -> str:
        """Publish a share link as the org root; returns its token."""
        with httpx.Client(base_url=self.http) as client:
            r = client.post("/v1/links", json=sign_request(
                root, "POST", "/v1/links",
                {"org": self.org, "target_uuid": self.org, "target_type": target_type},
                ts=int(time.time())))
            assert r.status_code == 201, r.text
            return r.json()["token"]


# ── the org under test ────────────────────────────────────────


@dataclass(frozen=True)
class RoleSpec:
    """One role definition: scopes the role confers, what admission needs
    (``self`` / ``sponsor`` / ``admin-ack``), and the static approver count
    for admin-ack roles."""
    scope_set: Sequence[str] = ()
    requires: str = "self"
    threshold: Optional[int] = None


@dataclass
class Org:
    """A ``Sim`` ledger plus the bookkeeping a scenario needs: the founder,
    every admitted persona by name, and the invite key that admitted each.

    The root defines roles and sponsors founding invites (the ``Sim`` root is
    the org's constitutional key). Members are admitted by key-bound invites
    unless a scenario passes ``approvers``/``token`` through ``claim``."""
    sim: Sim
    founder: KeyPair
    personas: Dict[str, KeyPair] = field(default_factory=dict)

    @classmethod
    def found(cls, *, org: str = ORG_UUID,
              owner: RoleSpec = RoleSpec(scope_set=("*",), requires="self"),
              roles: Optional[Dict[str, RoleSpec]] = None) -> "Org":
        """Genesis, the owner role, the founder's self-admitting claim, then
        any additional *roles* — the shape every real org starts from."""
        sim = Sim(org=org)
        sim.role_define(sim.root, "owner", list(owner.scope_set),
                        requires=owner.requires, approver_threshold=owner.threshold)
        founder, ik = KeyPair.generate(), KeyPair.generate()
        inv = sim.invite(sim.root, "owner", invite_key=ik)
        sim.claim(inv, ik, founder)
        self = cls(sim=sim, founder=founder, personas={"founder": founder})
        for name, spec in (roles or {}).items():
            self.define_role(name, spec)
        return self

    # -- ledger verbs (each returns the event id) ----------------------------

    def define_role(self, name: str, spec: RoleSpec, *, author: Optional[KeyPair] = None,
                    version: int = 1) -> str:
        return self.sim.role_define(
            author or self.sim.root, name, list(spec.scope_set),
            requires=spec.requires, version=version,
            approver_threshold=spec.threshold)

    def invite(self, role: str, *, sponsor: Optional[KeyPair] = None,
               invite_key: Optional[KeyPair] = None, token_hash: Optional[str] = None,
               max_uses: Optional[int] = None) -> str:
        """Mint an invite for *role*. Key-bound when *invite_key* is given
        (the default), token-bound when *token_hash* is. The sponsor defaults
        to the root."""
        return self.sim.invite(
            sponsor or self.sim.root, role,
            invite_key=invite_key, token_hash=token_hash, max_uses=max_uses)

    def claim(self, invite_id: str, signer: KeyPair, persona: KeyPair, *,
              approvers: Iterable[KeyPair] = (), token: Optional[str] = None) -> str:
        """A persona claims *invite_id*, carrying countersignatures from
        *approvers*. For a key-bound invite *signer* is the invite key."""
        return self.sim.claim(invite_id, signer, persona,
                              approvers=tuple(approvers), token=token)

    def admit(self, name: str, role: str, *, sponsor: Optional[KeyPair] = None,
              approvers: Iterable[KeyPair] = ()) -> KeyPair:
        """Key-bound invite + claim in one step; registers the persona under
        *name*. Whether the claim is actually admitted is the fold's call —
        read it with ``assert_member`` / ``assert_verdict``."""
        persona, ik = KeyPair.generate(), KeyPair.generate()
        inv = self.invite(role, sponsor=sponsor, invite_key=ik)
        self.claim(inv, ik, persona, approvers=approvers)
        self.personas[name] = persona
        return persona

    def grant(self, persona: KeyPair, role: str, *, author: Optional[KeyPair] = None) -> str:
        return self.sim.role_grant(author or self.sim.root, persona, role)

    def delegate(self, child: KeyPair, scope: Sequence[str], *, author: Optional[KeyPair] = None,
                 redelegate: bool = False) -> str:
        return self.sim.delegate(author or self.sim.root, child, list(scope),
                                 redelegate=redelegate)

    def revoke(self, event_id: str, *, author: Optional[KeyPair] = None) -> str:
        return self.sim.revoke_event(author or self.sim.root, event_id)

    def remove_member(self, persona: KeyPair) -> str:
        """Revoke the claim that admitted *persona* (the root's prerogative)."""
        return self.revoke(claim_id_of(self.sim, persona))

    # -- reading the fold ----------------------------------------------------

    def fold(self):
        return self.sim.fold()

    def member_pubs(self):
        return mc.member_pubs(self.fold())


def claim_id_of(sim: Sim, persona: KeyPair) -> str:
    """The ``member.claim`` event id that admitted *persona*."""
    for event in sim.ledger.events():
        if event.type == "member.claim" \
                and event.payload.get("persona_pub") == persona.public_hex:
            return event.event_id
    raise AssertionError("no claim for that persona")


# ── declarative steps ─────────────────────────────────────────


@dataclass(frozen=True)
class Step:
    """One named ledger action: ``run(org)`` returns the event id, which
    ``drive_actions`` records under ``name`` so verdicts can cite it."""
    name: str
    run: Callable[[Org], str]


def drive_actions(org: Org, steps: Iterable[Step]) -> Dict[str, str]:
    """Apply *steps* in order to *org*; return ``{name: event_id}``. A
    scenario reads as a table of steps followed by verdicts on their ids."""
    ids: Dict[str, str] = {}
    for step in steps:
        if step.name in ids:
            raise ValueError(f"duplicate step name {step.name!r}")
        ids[step.name] = step.run(org)
    return ids


# ── verdicts ──────────────────────────────────────────────────


def assert_verdict(state, eid: str, *, admitted: bool, reason: Optional[str] = None):
    """The fold's verdict on one event. An admitted event is valid with no
    reason; a refused one is invalid with exactly *reason* (an ``R_*``
    constant from ``tools.network.ledger.fold``)."""
    ok = state.valid.get(eid)
    assert ok is not None, f"event {eid} is not in the fold"
    if admitted:
        assert ok is True, f"expected admitted, fold refused: {state.reasons.get(eid)!r}"
        assert eid not in state.reasons, f"admitted event carries reason {state.reasons[eid]!r}"
    else:
        assert ok is False, "expected refused, fold admitted it"
        got = state.reasons.get(eid)
        assert got == reason, f"expected reason {reason!r}, got {got!r}"


def assert_member(state, persona: KeyPair, *, roles: Optional[Iterable[str]] = None):
    """*persona* is a live member; optionally with exactly *roles*."""
    view = state.members.get(persona.public_hex)
    assert view is not None, "persona is not a member"
    if roles is not None:
        assert set(view.roles) == set(roles), f"roles {view.roles!r} != {set(roles)!r}"


def assert_not_member(state, persona: KeyPair):
    assert persona.public_hex not in state.members, "persona is a member"


def assert_holds(state, persona: KeyPair, scope: str, expected: bool = True):
    """Whether *persona*'s folded authority covers *scope* — the ledger-layer
    twin of ``org_authority.authorize``."""
    assert state.holds(persona.public_hex, scope) is expected, \
        f"holds({scope!r}) is not {expected}"


# ── identities on the wire ────────────────────────────────────


def _serve_cert(persona: KeyPair, serve_key: KeyPair, org: str):
    return issue_cert(
        persona, serve_key.public_hex, scope=("tunnel:serve",), org=org,
        subject=Subject("persona", persona.public_hex),
        not_before=int(time.time()) - 300, not_after=int(time.time()) + 30 * DAY)


def _proof_resolver(member_pubs: Sequence[str], persona: KeyPair, seq: int):
    async def membership_proof_for():
        try:
            index, path = mc.inclusion_proof(member_pubs, persona.public_hex)
        except mc.MembershipCommitmentError:
            index, path = 0, []  # an outsider fabricates a proof of itself
        return {"v": 1, "checkpoint_seq": seq, "index": index, "path": path}
    return membership_proof_for


def connect_identity(registry: Registry, org: Org, persona: KeyPair, *, seq: int,
                     link_token: Optional[str] = None, link_key: Optional[KeyPair] = None,
                     content: bytes = CONTENT) -> TunnelConnector:
    """One member dashboard as an independent serving identity: its own
    serving key and persona-signed certificate, a v3 hello proving *persona*
    under the committed ``members_root`` at *seq*, a link-key resolver for
    *link_token*, and a handler that serves *content*. The caller runs it
    (``run_connector``)."""
    serve_key, machine_key = KeyPair.generate(), KeyPair.generate()

    def link_key_for(token):
        return link_key if link_key is not None and token == link_token else None

    async def handler(token, message):
        return json.dumps({"v": 1, "status": "ok"}).encode() + b"\n" + content

    return TunnelConnector(
        registry.ws, registry.org, serve_key, _serve_cert(persona, serve_key, registry.org),
        handler=handler, machine_key=machine_key, caps=(),
        membership_proof_for=_proof_resolver(org.member_pubs(), persona, seq),
        link_key_for=link_key_for, min_backoff=0.1, max_backoff=1.0,
    )


@contextlib.asynccontextmanager
async def run_connector(conn: TunnelConnector, *, timeout: float = 15.0):
    """Run *conn* until it authenticates, yield, then tear it down."""
    task = asyncio.create_task(conn.run())
    try:
        await asyncio.wait_for(conn.connected.wait(), timeout=timeout)
        yield conn
    finally:
        conn.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def identity_admitted(registry: Registry, org: Org, persona: KeyPair, *, seq: int,
                      member_pubs_override: Optional[List[str]] = None,
                      timeout: float = 2.5) -> bool:
    """Bring up a v3 tunnel for *persona* and report whether the REGISTRY
    admitted it — the hello reaches ``connected`` — or refused it (never
    authenticates within *timeout*). *member_pubs_override* lets an outsider
    present a fabricated roster."""
    member_pubs = member_pubs_override or org.member_pubs()
    serve_key, machine_key = KeyPair.generate(), KeyPair.generate()
    conn = TunnelConnector(
        registry.ws, registry.org, serve_key, _serve_cert(persona, serve_key, registry.org),
        machine_key=machine_key,
        membership_proof_for=_proof_resolver(member_pubs, persona, seq),
        min_backoff=0.1, max_backoff=0.3)

    async def run():
        task = asyncio.create_task(conn.run())
        try:
            await asyncio.wait_for(conn.connected.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            conn.stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    return asyncio.run(run())


async def fetch(registry: Registry, token: str, link_pub: str, *, timeout: float = 15.0) -> bytes:
    """A real viewer opens *token* with its fragment key and fetches; returns
    the served body (after the status line)."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            ch = await ViewerChannel.connect(registry.ws, token, link_pub=link_pub, org=registry.org)
            break
        except Exception as exc:
            last = exc
            await asyncio.sleep(0.25)
    else:
        raise AssertionError(f"viewer could not connect in {timeout}s: {last!r}")
    try:
        await ch.send_message(json.dumps({"v": 1, "op": "fetch"}).encode())
        return (await ch.recv_message()).partition(b"\n")[2]
    finally:
        await ch.close()
