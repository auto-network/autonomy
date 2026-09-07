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
  is adopted only at the operator's root ceremony.
- ``ts`` gives ±ORG_HELLO_MAX_SKEW_S freshness (the registry's rule); the
  ephemeral key binds the channel to the party that produced the hello.
- ``sig`` is the machine key's signature over the domain-separated payload.

Admission is mutual: the server answers with the same fields for its own
persona plus ``client_machine_pub``. Every check fails with a typed
HandshakeError naming the check.

Removal: when this node adopts a newer checkpoint it calls
``note_adoption``; peers admitted under an older seq have
REPROVE_DEADLINE_S to ``reprove`` under the new root, after which
``authorize`` (called per served message, as the personal path does)
refuses them. A removed member cannot produce a proof under the new root.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from tools.network.idkit import DelegationCert, IdkitError, KeyPair, canonical_json
from tools.network.idkit.keys import verify_signature
from tools.network.idkit.verify import verify_chain
from tools.network.ledger import membership_commitment as mc
from tools.network.relaykit.channel import HandshakeError
from tools.network.relaykit.hello import HelloError, validate_membership_proof

ORG_HANDSHAKE_VERSION = 1
ORG_HANDSHAKE_DOMAIN = b"autonomy.network.fleet-org-channel.handshake.v1\n"
ORG_SYNC_SCOPE = "fleet:sync"
#: ±freshness on the hello's ts (the registry's MAX_CLOCK_SKEW discipline).
ORG_HELLO_MAX_SKEW_S = 300
#: Grace for a peer admitted under an older adopted checkpoint to re-prove
#: under the newer one (the registry's MEMBERSHIP_REPROVE_DEADLINE_S).
REPROVE_DEADLINE_S = 5.0
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
    "ts", "sig",
})
ORG_SERVER_FIELDS = ORG_CLIENT_FIELDS | {"client_machine_pub"}


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


@dataclass(frozen=True)
class AdmittedPeer:
    machine_pub: str
    persona_pub: str
    checkpoint_seq: int


class OrgFleetAuthenticator:
    """Org-scope admission for one endpoint: this machine's own persona
    certificate and proof, and verification of a peer's against the
    checkpoints this node has adopted.

    ``adopted_checkpoint_for(seq)`` returns the adopted checkpoint record
    for *seq* (a mapping with ``members_root``) or None when this node has
    not adopted that seq; ``newest_adopted_seq()`` returns the newest seq
    adopted (None before the seed). ``membership_proof_for()`` returns this
    machine's own rider for its persona under the newest adopted seq.
    """

    def __init__(
        self,
        machine_key: KeyPair,
        *,
        org: str,
        persona_cert: DelegationCert,
        membership_proof_for: Callable[[], dict],
        adopted_checkpoint_for: Callable[[int], dict | None],
        newest_adopted_seq: Callable[[], int | None],
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
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
        self._now = now
        self._monotonic = monotonic
        #: machine_pub -> AdmittedPeer for peers this endpoint admitted.
        self._admitted: dict[str, AdmittedPeer] = {}
        #: (newest seq, monotonic deadline) after note_adoption().
        self._reprove_window: tuple[int, float] | None = None

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
        if abs(int(self._now()) - data["ts"]) > ORG_HELLO_MAX_SKEW_S:
            raise HandshakeError(
                f"{what} ts outside ±{ORG_HELLO_MAX_SKEW_S}s freshness window"
            )
        try:
            rider = validate_membership_proof(data["membership_proof"])
        except HelloError as exc:
            raise HandshakeError(f"{what} membership_proof is malformed: {exc}") from exc
        seq = int(rider["checkpoint_seq"])
        adopted = self._adopted_for(seq)
        if adopted is None:
            raise HandshakeError(
                f"{what} membership_proof names checkpoint {seq}, which this "
                "node has not adopted"
            )
        newest = self._newest_seq()
        if newest is not None and seq != newest and not self._within_reprove_window(seq):
            raise HandshakeError(
                f"{what} membership_proof is for checkpoint {seq}; this node "
                f"has adopted {newest}: re-prove required"
            )
        try:
            mc.verify_inclusion(
                str(adopted["members_root"]), persona_pub,
                rider["index"], rider["path"],
            )
        except (mc.MembershipCommitmentError, KeyError, TypeError) as exc:
            raise HandshakeError(
                f"{what} persona is not in the adopted member set at "
                f"checkpoint {seq}: {exc}"
            ) from exc
        return persona_pub

    def _within_reprove_window(self, seq: int) -> bool:
        window = self._reprove_window
        return (
            window is not None and seq < window[0]
            and self._monotonic() < window[1]
        )

    def _own_fields(self) -> dict[str, Any]:
        return {
            "persona_cert": self.persona_cert.to_dict(),
            "membership_proof": dict(self._proof_for()),
            "ts": int(self._now()),
        }

    # -- handshake ------------------------------------------------------

    def build_client_hello(self, session: str) -> tuple[X25519PrivateKey, bytes]:
        private_key = X25519PrivateKey.generate()
        eph_pub = _eph_pub(private_key)
        own = self._own_fields()
        body = {
            "v": ORG_HANDSHAKE_VERSION, "org": self.org,
            "machine_pub": self.machine_pub, "eph_pub": eph_pub, **own,
        }
        body["sig"] = self.machine_key.sign_hex(_payload(
            "client", org=self.org, session=session, machine_pub=self.machine_pub,
            eph_pub=eph_pub, **own,
        ))
        return private_key, canonical_json(body)

    def accept_client(
        self, raw: object, *, session: str
    ) -> tuple[str, X25519PrivateKey, bytes, bytes]:
        data = _parse(raw, ORG_CLIENT_FIELDS, "ORG_CLIENT_HELLO")
        client_pub = data["machine_pub"]
        persona_pub = self._verify_peer(data, "ORG_CLIENT_HELLO")
        try:
            verify_signature(client_pub, data["sig"], _payload(
                "client", org=self.org, session=session, machine_pub=client_pub,
                eph_pub=data["eph_pub"], persona_cert=data["persona_cert"],
                membership_proof=data["membership_proof"], ts=data["ts"],
            ))
        except IdkitError as exc:
            raise HandshakeError(f"client machine proof failed: {exc}") from exc
        self._admitted[client_pub] = AdmittedPeer(
            client_pub, persona_pub, int(data["membership_proof"]["checkpoint_seq"]),
        )
        private_key = X25519PrivateKey.generate()
        server_eph = _eph_pub(private_key)
        own = self._own_fields()
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
        persona_pub = self._verify_peer(data, "ORG_SERVER_HELLO")
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
        self._admitted[data["machine_pub"]] = AdmittedPeer(
            data["machine_pub"], persona_pub,
            int(data["membership_proof"]["checkpoint_seq"]),
        )
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

    def admitted(self, machine_pub: str) -> AdmittedPeer | None:
        return self._admitted.get(machine_pub)

    def authorize(self, machine_pub: str) -> None:
        """Called per served message: the peer must be admitted, and its
        proof must be under the newest adopted checkpoint or inside the
        re-prove window that follows an adoption."""
        peer = self._admitted.get(machine_pub)
        if peer is None:
            raise HandshakeError("machine is not admitted to this organization's scope")
        newest = self._newest_seq()
        if newest is not None and peer.checkpoint_seq != newest \
                and not self._within_reprove_window(peer.checkpoint_seq):
            raise HandshakeError(
                f"membership proof is for checkpoint {peer.checkpoint_seq}; this "
                f"node has adopted {newest}: re-prove required"
            )

    def note_adoption(self, seq: int, *, deadline_s: float = REPROVE_DEADLINE_S) -> None:
        """This node adopted checkpoint *seq*: peers proven under an older
        seq have *deadline_s* to re-prove before authorize() refuses them."""
        self._reprove_window = (int(seq), self._monotonic() + float(deadline_s))

    def reprove(self, machine_pub: str, rider: object) -> AdmittedPeer:
        """Re-stamp an admitted peer under a newer adopted checkpoint from a
        fresh rider it sent; raises when the rider does not verify."""
        peer = self._admitted.get(machine_pub)
        if peer is None:
            raise HandshakeError("machine is not admitted to this organization's scope")
        try:
            proof = validate_membership_proof(rider)
        except HelloError as exc:
            raise HandshakeError(f"re-prove rider is malformed: {exc}") from exc
        seq = int(proof["checkpoint_seq"])
        adopted = self._adopted_for(seq)
        if adopted is None:
            raise HandshakeError(f"re-prove names checkpoint {seq}, not adopted here")
        newest = self._newest_seq()
        if newest is not None and seq != newest:
            raise HandshakeError(f"re-prove must be under checkpoint {newest}, got {seq}")
        try:
            mc.verify_inclusion(
                str(adopted["members_root"]), peer.persona_pub,
                proof["index"], proof["path"],
            )
        except (mc.MembershipCommitmentError, KeyError, TypeError) as exc:
            raise HandshakeError(
                f"persona is not in the adopted member set at checkpoint {seq}: {exc}"
            ) from exc
        updated = AdmittedPeer(machine_pub, peer.persona_pub, seq)
        self._admitted[machine_pub] = updated
        return updated

    def stale_peers(self) -> list[AdmittedPeer]:
        """Peers whose proof is older than the newest adopted checkpoint."""
        newest = self._newest_seq()
        if newest is None:
            return []
        return [p for p in self._admitted.values() if p.checkpoint_seq != newest]
