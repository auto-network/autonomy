"""Member-gated admission for an organization's sync scope (auto-coea3).

Design of record: graph://c2baad48-0a3. The personal fleet handshake
(fleet_sync_channel) is signed over one operator's personal root and
authorizes a machine against that operator's roster; two members of one
organization share neither, so an org scope is admitted by the ORG HELLO
defined here instead:

    {v, org, machine_pub, eph_pub, persona_cert, membership_proof, ts, sig}

- ``org`` is the organization's genesis id; the signed payload and the
  transcript are bound to it and to the per-connection session.
- ``persona_cert`` is an idkit certificate issued by the member persona to
  the machine key: scope ``fleet:sync``, subject ``("persona", persona)``,
  org = genesis. The chain anchors at the persona the cert names, exactly
  as the registry's v3 tunnel hello does.
- ``membership_proof`` is the registry hello v3 rider byte-for-byte
  ({v, checkpoint_seq, index, path}), verified with
  membership_commitment.verify_inclusion against the members_root of the
  checkpoint this node has ADOPTED under that seq -- never against the raw
  ledger fold, which advances the moment a claim folds while a checkpoint
  is adopted only when published or verified (OrgAdmission.tla).
- ``ts`` gives ±clock.MAX_CLOCK_SKEW freshness (the registry's rule); the
  ephemeral key binds the channel to the party that produced the hello.
- ``sig`` is the machine key's signature over the domain-separated payload.

Admission is mutual: the server answers with the same fields for its own
persona plus ``client_machine_pub``. Every check fails with a typed
HandshakeError naming the check.

Which checkpoint a peer may prove under (OrgAdmission.tla, auto-qrmlg.11,
rules E-any-adm and prover-downgrade; proven live and safe there):

- A proof is accepted under ANY checkpoint this node has adopted and
  retained, provided that checkpoint is at or after the peer's CURRENT
  admission: the peer's claim in the fold at that checkpoint's head is the
  claim that admits it in this node's newest fold. Two members that adopted
  different checkpoints therefore still sync, and a member that adopted a
  newer checkpoint before pulling the events behind it is not shut out.
  The match is by the ROOT the proof recomputes, against every retained
  record; the rider's ``checkpoint_seq`` is the peer's own label for that
  record, tried first and trusted for nothing (a sponsor that bundled a
  genuine root under the wrong seq cannot lock its joiner out).
- Removal is judged against the NEWEST adopted member set, per served
  message (``authorize``, as the personal path does): a removed member's
  old proof stops working the moment this node adopts the checkpoint that
  drops it, with no grace window and nothing to re-prove.
- The server proves back under the root the client proved under, labelled
  with the client's seq (prover-downgrade), so the older side of a pair
  never has to hold the newer side's checkpoint to be admitted by it.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from tools.network.idkit import DelegationCert, IdkitError, KeyPair, canonical_json
from tools.network.idkit.keys import verify_signature
from tools.network.idkit.verify import verify_chain
from tools.network.ledger import membership_commitment as mc
from tools.network.relaykit.channel import HandshakeError
from tools.network.relaykit.hello import HelloError, validate_membership_proof

from tools.network import clock

ORG_HANDSHAKE_VERSION = 1
ORG_HANDSHAKE_DOMAIN = b"autonomy.network.fleet-org-channel.handshake.v1\n"
ORG_SYNC_SCOPE = "fleet:sync"
#: Close code for a peer whose persona is not in the newest adopted member
#: set (the registry's CLOSE_MEMBERSHIP_STALE, tools/network/registry/relay.py).
CLOSE_MEMBERSHIP_STALE = 4417
#: A client hello's ephemeral key is remembered this long: a captured hello
#: replayed inside the ts freshness window is refused by it, and one that
#: arrives later is refused by the ts check.
HELLO_REPLAY_MEMORY_S = 2 * clock.MAX_CLOCK_SKEW
ORG_EPOCH_DOMAIN = b"autonomy.network.fleet-sync.org-epoch.v1\n"
ORG_PEER_STATE_DOMAIN = b"autonomy.network.fleet-sync.org-peer-state.v1\n"


def org_epoch(org: str, seq: int | None) -> str:
    """The org scope's wire epoch (design §2): the membership checkpoint
    seq this node's own proof is verified under, bound to the org, in the
    64-hex shape every roster_epoch field and validator already accepts.
    It rides the org scope's pull request, done frame and checkpoint
    manifest in place of the personal roster hash, which two members of one
    organization compute differently by construction. Observable state,
    never authority: admission is the hello's."""
    return hashlib.sha256(
        ORG_EPOCH_DOMAIN + canonical_json({"org": org, "seq": seq})
    ).hexdigest()


def org_state_key(org: str) -> str:
    """The org scope's fleet_sync_peer_state key (design §2): the org id
    alone, so per-peer state is keyed by (machine pair, org) and a
    membership change -- join, leave, removal, rekey -- neither changes the
    key nor forces a re-checkpoint."""
    return hashlib.sha256(
        ORG_PEER_STATE_DOMAIN + canonical_json({"org": org})
    ).hexdigest()

ORG_CLIENT_FIELDS = frozenset({
    "v", "org", "machine_pub", "eph_pub", "persona_cert", "membership_proof",
    "ts", "addresses", "sig",
})
#: Bound on the addresses a client hello may introduce (fleet_direct_config
#: MAX_ADVERTISE_ADDRS).
MAX_HELLO_ADDRESSES = 8
#: The server answers with its own persona and proof; it introduces no
#: addresses (the client dialled it) and names the client machine instead.
ORG_SERVER_FIELDS = (ORG_CLIENT_FIELDS - {"addresses"}) | {"client_machine_pub"}


def _hex64(value: object, what: str) -> str:
    if (
        not isinstance(value, str) or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise HandshakeError(f"{what} must be 64 lowercase hex chars")
    return value


def _parse(raw: object, fields: frozenset[str], what: str) -> dict[str, Any]:
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HandshakeError(f"{what} is not valid UTF-8") from exc
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HandshakeError(f"{what} is not valid JSON") from exc
    if not isinstance(data, dict) or set(data) != fields:
        raise HandshakeError(f"{what} must carry exactly {sorted(fields)}")
    if data["v"] != ORG_HANDSHAKE_VERSION:
        raise HandshakeError(f"unsupported {what} version: {data['v']!r}")
    if not isinstance(data["org"], str) or not data["org"]:
        raise HandshakeError(f"{what} org must be a non-empty string")
    _hex64(data["machine_pub"], f"{what} machine_pub")
    _hex64(data["eph_pub"], f"{what} eph_pub")
    if not isinstance(data["persona_cert"], dict):
        raise HandshakeError(f"{what} persona_cert must be an object")
    if not isinstance(data["membership_proof"], dict):
        raise HandshakeError(f"{what} membership_proof must be an object")
    if type(data["ts"]) is not int:
        raise HandshakeError(f"{what} ts must be an integer")
    if "addresses" in fields:
        addresses = data["addresses"]
        if (
            not isinstance(addresses, list) or len(addresses) > MAX_HELLO_ADDRESSES
            or any(
                not isinstance(a, str) or not a or len(a) > 256
                or not (a.startswith("ws://") or a.startswith("wss://"))
                for a in addresses
            )
        ):
            raise HandshakeError(
                f"{what} addresses must be at most {MAX_HELLO_ADDRESSES} ws:// or wss:// URLs"
            )
    if not isinstance(data["sig"], str):
        raise HandshakeError(f"{what} sig must be a string")
    if "client_machine_pub" in fields:
        _hex64(data["client_machine_pub"], f"{what} client_machine_pub")
    return data


def _eph_pub(private_key: X25519PrivateKey) -> str:
    return private_key.public_key().public_bytes_raw().hex()


def _payload(side: str, **fields: Any) -> bytes:
    return ORG_HANDSHAKE_DOMAIN + canonical_json(
        {"v": ORG_HANDSHAKE_VERSION, "side": side, **fields}
    )


def _transcript(*, org: str, session: str, client_machine_pub: str,
                client_eph: str, server_machine_pub: str, server_eph: str) -> bytes:
    return hashlib.sha256(ORG_HANDSHAKE_DOMAIN + canonical_json({
        "v": ORG_HANDSHAKE_VERSION, "org": org, "session": session,
        "client_machine_pub": client_machine_pub, "client_eph": client_eph,
        "server_machine_pub": server_machine_pub, "server_eph": server_eph,
    })).digest()


class MembershipStaleError(HandshakeError):
    """The peer's persona is not in the newest member set this node has
    adopted: the connection closes with CLOSE_MEMBERSHIP_STALE. Its proof
    may verify under an older retained checkpoint; removal is judged against
    the newest set, so this is how removal takes effect."""

    close_code = CLOSE_MEMBERSHIP_STALE


@dataclass(frozen=True)
class AdmittedPeer:
    machine_pub: str
    persona_pub: str
    #: The seq the peer labelled its proof with (its own record's label).
    checkpoint_seq: int
    #: The seq of the retained record the proof verified under here; the
    #: admission floor is re-checked on it per served message.
    matched_seq: int = -1
    #: Addresses the peer introduced itself at in its client hello (signed
    #: by its machine key), or () for a server-side peer. Sync is pull-only,
    #: so this is how the machine that is dialled first learns where to
    #: dial back; a hint only, superseded by the peer's replicated
    #: reachability row (auto-mldvv) once that has crossed.
    addresses: tuple[str, ...] = ()


class OrgFleetAuthenticator:
    """Org-scope admission for one endpoint: this machine's own persona
    certificate and proof, and verification of a peer's against the
    checkpoints this node has adopted.

    ``adopted_checkpoint_for(seq)`` returns the adopted checkpoint record
    for *seq* (a mapping with ``seq`` and ``members_root``) or None when this
    node has not retained that seq; ``retained_checkpoints()`` returns every
    retained record; ``newest_adopted_seq()`` returns the newest seq adopted
    (None before the seed). ``membership_proof_for(under=, root=, attempt=)``
    returns this machine's own rider for its persona: under the retained
    record whose members_root is *root*, labelled *under* (the server proving
    back at the client's level), else the prover's *attempt*-th candidate
    root (OrgAdmissionBundleBound.tla ProveOwnFold: retained records first,
    then this node's own fold at heads it holds, newest first). *attempt*
    counts refusals since the last completed hello, so a peer that retains
    any candidate root is reached within as many hellos as there are
    candidates, whichever side dials.
    ``adopted_members_for(seq)`` gives the persona set behind a retained
    checkpoint; the newest set is what removal is judged against.
    ``admission_ok_for(seq, persona)`` says whether retained checkpoint
    *seq* is at or after *persona*'s current admission (None: cannot tell,
    accepted; the test harnesses model no admissions).
    """

    def __init__(
        self,
        machine_key: KeyPair,
        *,
        org: str,
        persona_cert: DelegationCert,
        membership_proof_for: Callable[..., dict],
        adopted_checkpoint_for: Callable[[int], dict | None],
        newest_adopted_seq: Callable[[], int | None],
        retained_checkpoints: Callable[[], Iterable[dict]] | None = None,
        now: Callable[[], float] = time.time,
        adopted_members_for: Callable[[int], Iterable[str] | None] | None = None,
        admission_ok_for: Callable[[int, str], bool | None] | None = None,
        advertised_addresses: Callable[[], Sequence[str]] | None = None,
    ) -> None:
        if not isinstance(org, str) or not org:
            raise HandshakeError("org must be a non-empty string")
        self.machine_key = machine_key
        self.machine_pub = machine_key.public_hex
        self.org = org
        self.persona_cert = persona_cert
        self.persona_pub = str(persona_cert.subject.id)
        self._proof_for = membership_proof_for
        self._adopted_for = adopted_checkpoint_for
        self._newest_seq = newest_adopted_seq
        self._retained = retained_checkpoints
        #: Optional: the persona set of an adopted checkpoint (the members
        #: behind its members_root), for readers that filter hints such as
        #: reachability rows by membership. None: unknown to this node.
        self._members_for = adopted_members_for
        #: Optional: whether a retained checkpoint is at or after a persona's
        #: current admission (OrgAdmission.tla E-any-adm's floor).
        self._admission_ok = admission_ok_for
        #: This machine's dialable addresses, introduced in its client hello.
        self._advertised = advertised_addresses
        self._now = now
        #: machine_pub -> AdmittedPeer for peers this endpoint admitted.
        self._admitted: dict[str, AdmittedPeer] = {}
        #: Per peer machine: refusals of this machine's own hello to that
        #: peer since the last one that completed with it. Selects the
        #: prover's candidate root for that peer (Rotate is per prover-verifier
        #: pair: completing hellos with other peers must not reset it).
        self._attempts: dict[str, int] = {}
        #: Observability (auto-qrmlg.2): per peer, the last completed hello
        #: and the last refusal; plus the last failed dial (not a refusal).
        self._last_completed: dict[str, dict[str, Any]] = {}
        self._last_refused: dict[str, dict[str, Any]] = {}
        self._last_dial_error: dict[str, Any] | None = None
        #: client eph_pub -> wall-clock expiry, for replay refusal.
        self._seen_client_eph: dict[str, float] = {}

    # -- verification ---------------------------------------------------

    def _verify_peer(self, data: dict[str, Any], what: str) -> str:
        """Every admission check, in order; returns the peer's persona."""
        if data["org"] != self.org:
            raise HandshakeError(f"{what} is for another organization")
        try:
            cert = DelegationCert.from_dict(data["persona_cert"])
        except (IdkitError, ValueError, TypeError, KeyError) as exc:
            raise HandshakeError(f"{what} persona_cert is malformed: {exc}") from exc
        if cert.org != self.org:
            raise HandshakeError(f"{what} persona_cert is for another organization")
        if cert.subject.kind != "persona":
            raise HandshakeError(f"{what} persona_cert subject must be a persona")
        persona_pub = _hex64(cert.subject.id, f"{what} persona")
        try:
            verified = verify_chain(
                cert, persona_pub, org=self.org, now=int(self._now()),
                required_scope=ORG_SYNC_SCOPE,
            )
        except IdkitError as exc:
            raise HandshakeError(f"{what} persona_cert chain failed: {exc}") from exc
        if verified.leaf_pub != data["machine_pub"]:
            raise HandshakeError(f"{what} persona_cert names another machine")
        if abs(int(self._now()) - data["ts"]) > clock.MAX_CLOCK_SKEW:
            raise HandshakeError(
                f"{what} ts outside ±{clock.MAX_CLOCK_SKEW}s freshness window"
            )
        try:
            rider = validate_membership_proof(data["membership_proof"])
        except HelloError as exc:
            raise HandshakeError(f"{what} membership_proof is malformed: {exc}") from exc
        seq = int(rider["checkpoint_seq"])
        matched = self._match_retained(seq, persona_pub, rider["index"], rider["path"], what)
        # E-any-adm: any retained checkpoint admits, at or after the peer's
        # current admission; removal is judged against the newest set.
        self._require_current_member(persona_pub, what)
        matched_seq = int(matched["seq"])
        if self._admission_ok is not None and self._admission_ok(matched_seq, persona_pub) is False:
            raise HandshakeError(
                f"{what} membership_proof is under checkpoint {matched_seq}, which "
                "predates this persona's current admission"
            )
        return persona_pub, str(matched["members_root"]), matched_seq

    def _candidates(self, seq: int) -> list[dict]:
        """Retained records to try a proof against: the one the peer named
        first, then every other, newest first."""
        named = self._adopted_for(seq)
        out: list[dict] = [named] if named is not None else []
        if self._retained is not None:
            others = [r for r in self._retained() if isinstance(r, dict) and "members_root" in r]
            others.sort(key=lambda r: int(r.get("seq", -1)), reverse=True)
            for record in others:
                if named is None or record.get("members_root") != named.get("members_root"):
                    out.append(record)
        return out

    def _match_retained(
        self, seq: int, persona_pub: str, index: object, path: object, what: str,
    ) -> dict:
        """The retained record the proof verifies under: the root a proof
        recomputes is what is checked, so a record the peer labelled with a
        seq this node keeps under another seq still admits it."""
        candidates = self._candidates(seq)
        if not candidates:
            raise HandshakeError(
                f"{what} membership_proof names checkpoint {seq}, which this "
                "node has not adopted"
            )
        last = "no retained checkpoint"
        for record in candidates:
            try:
                mc.verify_inclusion(str(record["members_root"]), persona_pub, index, path)
                return record
            except (mc.MembershipCommitmentError, KeyError, TypeError) as exc:
                last = str(exc)
        raise HandshakeError(
            f"{what} persona is not in the adopted member set at checkpoint "
            f"{seq}, nor in any other retained one: {last}"
        )

    def _require_current_member(self, persona_pub: str, what: str) -> None:
        if self.is_member(persona_pub) is False:
            raise MembershipStaleError(
                f"{what} persona is not in the newest adopted member set "
                f"(checkpoint {self._newest_seq()}): removed"
            )

    def _refuse_replay(self, client_eph: str) -> None:
        """A client hello carries a fresh ephemeral key by construction; one
        seen before is a replayed capture, refused before any other check.
        (Its signature would still bind a replay to the original session id,
        which the client chooses, so binding alone is not freshness.)"""
        now = self._now()
        expired = [k for k, until in self._seen_client_eph.items() if until <= now]
        for key in expired:
            del self._seen_client_eph[key]
        if client_eph in self._seen_client_eph:
            raise HandshakeError("ORG_CLIENT_HELLO is a replayed capture (eph_pub seen before)")
        self._seen_client_eph[client_eph] = now + HELLO_REPLAY_MEMORY_S

    def _own_fields(
        self, *, under: int | None = None, root: str | None = None, peer: str | None = None,
    ) -> dict[str, Any]:
        """This machine's hello fields. *under*/*root* are the seq the peer
        labelled its proof with and the root that proof verified under: the
        server proves back under that root with that label (prover-downgrade),
        so the peer can verify the reply against its own record. A client
        hello (no root) proves under this node's attempt-th candidate for
        *peer*."""
        attempt = self._attempts.get(peer, 0) if peer is not None else 0
        rider = self._proof_for(under=under, root=root, attempt=attempt)
        return {
            "persona_cert": self.persona_cert.to_dict(),
            "membership_proof": dict(rider),
            "ts": int(self._now()),
        }

    def _completed(self, peer_machine: str, seq: int, root: str) -> None:
        self._attempts.pop(peer_machine, None)
        self._last_completed[peer_machine] = {
            "peer": peer_machine, "checkpoint_seq": int(seq), "members_root": root,
            "at": int(self._now()),
        }

    def note_refusal(self, peer_machine: str, error: object) -> None:
        """This machine's own hello to *peer_machine* did not complete. A
        typed refusal (HandshakeError: the peer answered and refused) rotates
        the candidate root for that peer and is reported; a failed dial
        tested nothing, so it is reported without advancing the rotation. A
        completed hello with that peer resets it."""
        if isinstance(error, HandshakeError):
            attempt = self._attempts.get(peer_machine, 0) + 1
            self._attempts[peer_machine] = attempt
            self._last_refused[peer_machine] = {
                "peer": peer_machine, "error": str(error)[:200], "at": int(self._now()),
                "attempt": attempt,
            }
        else:
            self._last_dial_error = {
                "peer": peer_machine, "error": f"{type(error).__name__}: {error}"[:200],
                "at": int(self._now()),
            }

    def hello_state(self) -> dict[str, Any]:
        """For the org sync report: what this node proves under, per peer
        the last hello that completed and the last refusal with the attempt
        count, and whether any peer is refusing this node's hellos after the
        last completion with it (OrgAdmissionBundleBound.tla ~HelloOK: the
        newest candidate cannot complete a hello there)."""
        peers: dict[str, dict[str, Any]] = {}
        for machine in set(self._last_completed) | set(self._last_refused):
            completed = self._last_completed.get(machine)
            refused = self._last_refused.get(machine)
            attempts = self._attempts.get(machine, 0)
            peers[machine] = {
                "last_completed": dict(completed) if completed else None,
                "last_refused": dict(refused) if refused else None,
                "attempts_since_completed": attempts,
                "stuck": refused is not None and attempts > 0 and (
                    completed is None or refused["at"] >= completed["at"]
                ),
            }
        return {
            "newest_adopted_seq": self._newest_seq(),
            "peers": peers,
            "stuck": any(entry["stuck"] for entry in peers.values()),
            "last_dial_error": dict(self._last_dial_error) if self._last_dial_error else None,
        }

    # -- handshake ------------------------------------------------------

    def build_client_hello(
        self, session: str, *, peer: str | None = None,
    ) -> tuple[X25519PrivateKey, bytes]:
        """*peer* is the machine being dialled: its own rotation counter
        picks which candidate root this hello proves under."""
        private_key = X25519PrivateKey.generate()
        eph_pub = _eph_pub(private_key)
        own = self._own_fields(peer=peer)
        addresses: list[str] = []
        if self._advertised is not None:
            try:
                addresses = list(dict.fromkeys(self._advertised()))[:MAX_HELLO_ADDRESSES]
            except Exception:
                addresses = []
        body = {
            "v": ORG_HANDSHAKE_VERSION, "org": self.org,
            "machine_pub": self.machine_pub, "eph_pub": eph_pub, **own,
            "addresses": addresses,
        }
        body["sig"] = self.machine_key.sign_hex(_payload(
            "client", org=self.org, session=session, machine_pub=self.machine_pub,
            eph_pub=eph_pub, **own, addresses=addresses,
        ))
        return private_key, canonical_json(body)

    def accept_client(
        self, raw: object, *, session: str
    ) -> tuple[str, X25519PrivateKey, bytes, bytes]:
        data = _parse(raw, ORG_CLIENT_FIELDS, "ORG_CLIENT_HELLO")
        client_pub = data["machine_pub"]
        self._refuse_replay(data["eph_pub"])
        persona_pub, matched_root, matched_seq = self._verify_peer(data, "ORG_CLIENT_HELLO")
        try:
            verify_signature(client_pub, data["sig"], _payload(
                "client", org=self.org, session=session, machine_pub=client_pub,
                eph_pub=data["eph_pub"], persona_cert=data["persona_cert"],
                membership_proof=data["membership_proof"], ts=data["ts"],
                addresses=data["addresses"],
            ))
        except IdkitError as exc:
            raise HandshakeError(f"client machine proof failed: {exc}") from exc
        self._admitted[client_pub] = AdmittedPeer(
            client_pub, persona_pub, int(data["membership_proof"]["checkpoint_seq"]),
            matched_seq, tuple(data["addresses"]),
        )
        self._completed(client_pub, int(data["membership_proof"]["checkpoint_seq"]), matched_root)
        private_key = X25519PrivateKey.generate()
        server_eph = _eph_pub(private_key)
        own = self._own_fields(
            under=int(data["membership_proof"]["checkpoint_seq"]), root=matched_root)
        body = {
            "v": ORG_HANDSHAKE_VERSION, "org": self.org,
            "machine_pub": self.machine_pub, "eph_pub": server_eph,
            "client_machine_pub": client_pub, **own,
        }
        body["sig"] = self.machine_key.sign_hex(_payload(
            "server", org=self.org, session=session, client_machine_pub=client_pub,
            client_eph=data["eph_pub"], machine_pub=self.machine_pub,
            eph_pub=server_eph, **own,
        ))
        transcript = _transcript(
            org=self.org, session=session, client_machine_pub=client_pub,
            client_eph=data["eph_pub"], server_machine_pub=self.machine_pub,
            server_eph=server_eph,
        )
        return client_pub, private_key, canonical_json(body), transcript

    def verify_server(
        self, raw: object, *, session: str, client_eph: str, expected_machine_pub: str,
    ) -> tuple[str, bytes]:
        data = _parse(raw, ORG_SERVER_FIELDS, "ORG_SERVER_HELLO")
        if data["client_machine_pub"] != self.machine_pub:
            raise HandshakeError("server hello names another client machine")
        if data["machine_pub"] != expected_machine_pub:
            raise HandshakeError("server hello is from an unexpected machine")
        persona_pub, server_root, server_matched_seq = self._verify_peer(data, "ORG_SERVER_HELLO")
        try:
            verify_signature(data["machine_pub"], data["sig"], _payload(
                "server", org=self.org, session=session,
                client_machine_pub=self.machine_pub, client_eph=client_eph,
                machine_pub=data["machine_pub"], eph_pub=data["eph_pub"],
                persona_cert=data["persona_cert"],
                membership_proof=data["membership_proof"], ts=data["ts"],
            ))
        except IdkitError as exc:
            raise HandshakeError(f"server machine proof failed: {exc}") from exc
        # A server hello introduces no addresses; keep the ones this peer
        # gave us in its own client hello, if it has dialled us before.
        known = self._admitted.get(data["machine_pub"])
        self._admitted[data["machine_pub"]] = AdmittedPeer(
            data["machine_pub"], persona_pub,
            int(data["membership_proof"]["checkpoint_seq"]), server_matched_seq,
            known.addresses if known is not None else (),
        )
        self._completed(data["machine_pub"], int(data["membership_proof"]["checkpoint_seq"]), server_root)
        return data["eph_pub"], _transcript(
            org=self.org, session=session, client_machine_pub=self.machine_pub,
            client_eph=client_eph, server_machine_pub=data["machine_pub"],
            server_eph=data["eph_pub"],
        )

    # -- per-message authorization and membership change -----------------

    def newest_adopted_seq(self) -> int | None:
        """The newest membership checkpoint seq this node has adopted (what
        its own hello proves under); None before the seed."""
        return self._newest_seq()

    def is_member(self, persona_pub: str) -> bool | None:
        """Whether *persona_pub* is in the newest adopted member set; None
        when this node cannot tell (no member list, no adoption yet)."""
        if self._members_for is None:
            return None
        newest = self._newest_seq()
        if newest is None:
            return None
        members = self._members_for(int(newest))
        if members is None:
            return None
        return persona_pub in set(members)

    def admitted(self, machine_pub: str) -> AdmittedPeer | None:
        return self._admitted.get(machine_pub)

    def authorize(self, machine_pub: str) -> None:
        """Called per served message: the peer must be admitted, its persona
        must still be in the newest adopted member set, and the record its
        proof verified under must still be at or after its current admission
        (a removal and a re-admission adopted while the connection was idle
        leave the old proof before the new claim: refused, it must hello
        again; OrgAdmissionLeaves.tla ReAdmitAfterRemoval). Adopting a
        checkpoint that removes a member refuses that member's next message
        (MembershipStaleError, close 4417) with no grace and nothing to
        re-prove; every other admitted peer is unaffected."""
        peer = self._admitted.get(machine_pub)
        if peer is None:
            raise HandshakeError("machine is not admitted to this organization's scope")
        self._require_current_member(peer.persona_pub, "admitted peer's")
        if peer.matched_seq >= 0:
            # The record the proof verified under must still be retained (an
            # evicted record can no longer be placed against the persona's
            # current admission) and still at or after that admission.
            if self._adopted_for(peer.matched_seq) is None:
                raise MembershipStaleError(
                    f"admitted peer's proof is under checkpoint {peer.matched_seq}, "
                    "which this node no longer retains: it must hello again"
                )
            if self._admission_ok is not None \
                    and self._admission_ok(peer.matched_seq, peer.persona_pub) is False:
                raise MembershipStaleError(
                    f"admitted peer's proof is under checkpoint {peer.matched_seq}, which "
                    "predates its current admission: it must hello again"
                )

    def admitted_addresses(self) -> dict[str, tuple[str, ...]]:
        """machine_pub -> addresses for every admitted peer that introduced
        any and is still a member of the newest adopted set."""
        out: dict[str, tuple[str, ...]] = {}
        for peer in self._admitted.values():
            if not peer.addresses:
                continue
            try:
                self.authorize(peer.machine_pub)
            except HandshakeError:
                continue
            out[peer.machine_pub] = peer.addresses
        return out
