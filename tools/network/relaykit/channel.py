"""E2E channel crypto — X25519 handshake + AES-256-GCM records (§5.2, I5).

The relay between the two ends of a channel is UNTRUSTED: it sees (and
may rewrite) every byte. Security rests on two facts:

1. **The org key pins the server end.** The dashboard signs its
   ephemeral X25519 key with a ``tunnel:serve``-scoped idkit delegation
   chain; the viewer verifies that chain against the org root public key
   it fetched from the registry ENVELOPE — a value the bootloader holds
   *before* any channel bytes flow. A relay that substitutes its own
   ECDH key cannot produce that signature; a relay that substitutes the
   whole cert cannot chain it to the pinned root. Either way the
   handshake fails closed (I5).
2. **Everything after the handshake is AEAD ciphertext.** Keys are
   HKDF-derived from the ECDH secret and bound to the handshake
   transcript (org, token, both ephemeral keys, and the server cert),
   so a spliced or replayed channel decrypts to nothing.

The viewer end stays anonymous (rung 1): only the server signs.

Handshake wire (JSON inside opaque channel messages)::

    CLIENT_HELLO  {"v": 1, "eph_pub": <64 hex>}
    SERVER_HELLO  {"v": 1, "eph_pub": <64 hex>, "cert": <wire JSON str>,
                   "sig": <128 hex over HANDSHAKE_DOMAIN ||
                           canonical_json({v, org, token,
                                           client_eph, server_eph})>}

Key schedule::

    transcript_hash = SHA256(HANDSHAKE_DOMAIN ||
                             canonical_json({v, org, token, client_eph,
                                             server_eph, cert}))
    k = HKDF-SHA256(X25519(eph_c, eph_s), salt=transcript_hash,
                    info=KEYS_INFO, 64 bytes)
    key_c2s, key_s2c = k[:32], k[32:]

Record layer (Q3: chunking + backpressure)::

    record    = [8B seq BE][AES-256-GCM ciphertext]
    nonce     = direction_tag (4B) || seq (8B BE)      # never reused
    AAD       = transcript_hash || direction_tag || seq
    plaintext = [1B flags][chunk]

Flags preserve the original ``0x01 = final`` meaning for deployed v1
clients while separating message and exchange boundaries:

    0x01 STREAM_FINAL  no message follows in this response exchange
    0x02 MSG_END       this record completes the current message

One-shot messages set both bits.  A legacy peer that sends only 0x01 is
accepted as ending both its message and exchange.

Messages are chunked at ``CHUNK_SIZE`` (128 KiB) so a 1.5 MB+ artifact
never occupies one giant WS message anywhere on the path; senders await
each record's transmission, which surfaces TCP backpressure naturally.
Receivers enforce strictly sequential ``seq`` — reorder, replay, or drop
by the relay is detected, not tolerated.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterator, Optional, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from tools.network.idkit import (
    DelegationCert,
    IdkitError,
    KeyPair,
    canonical_json,
    verify_chain,
    verify_signature,
)

HANDSHAKE_DOMAIN = b"autonomy.network.channel.handshake.v1\n"
KEYS_INFO = b"autonomy.network.channel.keys.v1"
HANDSHAKE_VERSION = 1

DIR_C2S = b"c2s\x00"
DIR_S2C = b"s2c\x00"

CHUNK_SIZE = 128 * 1024
MAX_MESSAGE_SIZE = 64 * 1024 * 1024
_SEQ_LEN = 8
_FLAG_STREAM_FINAL = 0x01
_FLAG_MSG_END = 0x02
_KNOWN_FLAGS = _FLAG_STREAM_FINAL | _FLAG_MSG_END
# Compatibility name used by the original record protocol and its browser
# implementation.  STREAM_FINAL deliberately keeps this bit assignment.
_FLAG_FINAL = _FLAG_STREAM_FINAL
_MAX_SEQ = 2**63


class HandshakeError(Exception):
    """The channel handshake failed — the peer is not who the org key says."""


class RecordError(Exception):
    """A record failed authentication, ordering, or size checks."""


@dataclass(frozen=True)
class OpenedRecord:
    """One authenticated plaintext record in streaming receive mode.

    ``chunk`` may be handed to a bounded staging sink immediately.  A caller
    commits the staged message only after ``message_end``; teardown before
    that boundary therefore discards an incomplete message.  ``stream_final``
    marks the last message in the current response exchange, not the lifetime
    of the bidirectional channel.
    """

    chunk: bytes
    message_end: bool
    stream_final: bool


def _eph_pub_hex(private_key: X25519PrivateKey) -> str:
    return private_key.public_key().public_bytes_raw().hex()


def _decode_eph_pub(value: object, what: str) -> X25519PublicKey:
    if not isinstance(value, str) or len(value) != 64 or value != value.lower():
        raise HandshakeError(f"{what} must be 64 lowercase hex chars")
    try:
        return X25519PublicKey.from_public_bytes(bytes.fromhex(value))
    except ValueError as exc:
        raise HandshakeError(f"{what} is not a valid X25519 public key") from exc


def _parse_hello(raw, expected_fields: frozenset, what: str) -> dict:
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HandshakeError(f"{what} is not valid UTF-8") from exc
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HandshakeError(f"{what} is not valid JSON") from exc
    if not isinstance(data, dict) or set(data) != expected_fields:
        raise HandshakeError(f"{what} must carry exactly {sorted(expected_fields)}")
    if data["v"] != HANDSHAKE_VERSION:
        raise HandshakeError(f"unsupported {what} version: {data['v']!r}")
    return data


# -- hello construction / verification ----------------------------------------


def build_client_hello() -> Tuple[X25519PrivateKey, bytes]:
    """Mint the viewer's ephemeral key and its CLIENT_HELLO bytes."""
    private_key = X25519PrivateKey.generate()
    hello = canonical_json({"v": HANDSHAKE_VERSION, "eph_pub": _eph_pub_hex(private_key)})
    return private_key, hello


def parse_client_hello(raw) -> str:
    """Dashboard side: returns the client's ephemeral public key hex."""
    data = _parse_hello(raw, frozenset({"v", "eph_pub"}), "CLIENT_HELLO")
    _decode_eph_pub(data["eph_pub"], "client eph_pub")
    return data["eph_pub"]


def _signed_payload(org: str, token: str, client_eph: str, server_eph: str) -> bytes:
    return HANDSHAKE_DOMAIN + canonical_json(
        {
            "v": HANDSHAKE_VERSION,
            "org": org,
            "token": token,
            "client_eph": client_eph,
            "server_eph": server_eph,
        }
    )


def _transcript_hash(org: str, token: str, client_eph: str, server_eph: str,
                     cert_wire: str) -> bytes:
    return hashlib.sha256(
        HANDSHAKE_DOMAIN
        + canonical_json(
            {
                "v": HANDSHAKE_VERSION,
                "org": org,
                "token": token,
                "client_eph": client_eph,
                "server_eph": server_eph,
                "cert": cert_wire,
            }
        )
    ).digest()


def build_server_hello(
    signing_key: KeyPair,
    cert: DelegationCert,
    *,
    org: str,
    token: str,
    client_eph: str,
) -> Tuple[X25519PrivateKey, bytes, bytes]:
    """Dashboard side: mint the server ephemeral key and the signed
    SERVER_HELLO. Returns ``(eph_private, hello_bytes, transcript_hash)``.

    *signing_key* must be the ``tunnel:serve`` delegate that *cert*
    delegates to — the signature is what lets the anonymous viewer pin
    this end to the org key (I5).
    """
    if cert.child_pub != signing_key.public_hex:
        raise HandshakeError("cert does not delegate to the signing key")
    private_key = X25519PrivateKey.generate()
    server_eph = _eph_pub_hex(private_key)
    cert_wire = cert.to_json().decode("ascii")
    sig = signing_key.sign_hex(_signed_payload(org, token, client_eph, server_eph))
    hello = canonical_json(
        {
            "v": HANDSHAKE_VERSION,
            "eph_pub": server_eph,
            "cert": cert_wire,
            "sig": sig,
        }
    )
    return private_key, hello, _transcript_hash(org, token, client_eph, server_eph, cert_wire)


def verify_server_hello(
    raw,
    *,
    root_pub: str,
    org: str,
    token: str,
    client_eph: str,
    now: Optional[int] = None,
) -> Tuple[str, bytes]:
    """Viewer side: the I5 gate.

    *root_pub* is the org root public key the bootloader fetched from the
    registry envelope BEFORE opening the channel — the pin. Verifies, in
    order: the cert parses in canonical form and chains to *root_pub*
    with scope ``tunnel:serve``; then the hello signature (over org,
    token, and BOTH ephemeral keys) verifies against the chain's leaf.

    Raises :class:`HandshakeError` on any failure. Returns
    ``(server_eph_hex, transcript_hash)``.
    """
    data = _parse_hello(raw, frozenset({"v", "eph_pub", "cert", "sig"}), "SERVER_HELLO")
    _decode_eph_pub(data["eph_pub"], "server eph_pub")
    if not isinstance(data["cert"], str) or not isinstance(data["sig"], str):
        raise HandshakeError("SERVER_HELLO cert and sig must be strings")

    try:
        cert = DelegationCert.from_json(data["cert"])
        result = verify_chain(
            cert, root_pub, org=org, now=now, required_scope="tunnel:serve"
        )
        verify_signature(
            result.leaf_pub,
            data["sig"],
            _signed_payload(org, token, client_eph, data["eph_pub"]),
        )
    except IdkitError as exc:
        raise HandshakeError(f"{type(exc).__name__}: {exc}") from exc

    return data["eph_pub"], _transcript_hash(org, token, client_eph, data["eph_pub"],
                                             data["cert"])


# -- record layer ---------------------------------------------------------------


def _derive_keys(private_key: X25519PrivateKey, peer_pub_hex: str,
                 transcript_hash: bytes) -> Tuple[bytes, bytes]:
    shared = private_key.exchange(_decode_eph_pub(peer_pub_hex, "peer eph_pub"))
    okm = HKDF(
        algorithm=hashes.SHA256(), length=64, salt=transcript_hash, info=KEYS_INFO
    ).derive(shared)
    return okm[:32], okm[32:]


class ChannelCrypto:
    """One end of an established channel: seal outgoing, open incoming.

    Build with :meth:`client` or :meth:`server` after the handshake.
    ``seal_message`` chunks a full message into records; feed incoming
    records to ``open_record`` and it returns a completed message when
    its ``MSG_END`` arrives (else ``None``).  Large-message consumers use
    ``open_stream_record`` to receive each authenticated record without the
    compatibility path's whole-message buffer.
    """

    def __init__(self, send_key: bytes, recv_key: bytes, transcript_hash: bytes,
                 send_dir: bytes, recv_dir: bytes,
                 max_message_size: int = MAX_MESSAGE_SIZE):
        self._send = AESGCM(send_key)
        self._recv = AESGCM(recv_key)
        self._transcript_hash = transcript_hash
        self._send_dir = send_dir
        self._recv_dir = recv_dir
        self._send_seq = 0
        self._recv_seq = 0
        self._buffer = bytearray()
        self._incoming_message_size = 0
        self._peak_buffered_bytes = 0
        self._max_message_size = max_message_size

    @classmethod
    def client(cls, private_key: X25519PrivateKey, server_eph_hex: str,
               transcript_hash: bytes, **kw) -> "ChannelCrypto":
        key_c2s, key_s2c = _derive_keys(private_key, server_eph_hex, transcript_hash)
        return cls(key_c2s, key_s2c, transcript_hash, DIR_C2S, DIR_S2C, **kw)

    @classmethod
    def server(cls, private_key: X25519PrivateKey, client_eph_hex: str,
               transcript_hash: bytes, **kw) -> "ChannelCrypto":
        key_c2s, key_s2c = _derive_keys(private_key, client_eph_hex, transcript_hash)
        return cls(key_s2c, key_c2s, transcript_hash, DIR_S2C, DIR_C2S, **kw)

    def _seal_record(self, flags: int, chunk: bytes) -> bytes:
        if flags & ~_KNOWN_FLAGS:
            raise RecordError("record has unknown flags")
        if len(chunk) > CHUNK_SIZE:
            raise RecordError("record chunk exceeds maximum size")
        if self._send_seq >= _MAX_SEQ:
            raise RecordError("send sequence exhausted; channel must be re-keyed")
        seq = self._send_seq.to_bytes(_SEQ_LEN, "big")
        nonce = self._send_dir + seq
        aad = self._transcript_hash + self._send_dir + seq
        ciphertext = self._send.encrypt(nonce, bytes([flags]) + chunk, aad)
        self._send_seq += 1
        return seq + ciphertext

    def iter_seal_message(
        self, plaintext: bytes, *, stream_final: bool = True
    ) -> Iterator[bytes]:
        """Yield sealed records for one message without buffering ciphertext.

        ``stream_final=False`` ends this message with ``MSG_END`` while
        leaving the response exchange open for another message.  At least one
        record is produced, including for an empty message.
        """
        if not isinstance(plaintext, (bytes, bytearray, memoryview)):
            raise TypeError("channel message must be bytes-like")
        plaintext_size = (
            plaintext.nbytes if isinstance(plaintext, memoryview) else len(plaintext)
        )
        if plaintext_size > self._max_message_size:
            raise RecordError("message exceeds maximum size")
        plaintext = bytes(plaintext)
        offset = 0
        while True:
            chunk = plaintext[offset:offset + CHUNK_SIZE]
            offset += CHUNK_SIZE
            message_end = offset >= len(plaintext)
            flags = 0
            if message_end:
                flags |= _FLAG_MSG_END
                if stream_final:
                    flags |= _FLAG_STREAM_FINAL
            yield self._seal_record(flags, chunk)
            if message_end:
                return

    def seal_message(self, plaintext: bytes, *, stream_final: bool = True) -> list:
        """Compatibility wrapper returning all records for one message."""
        return list(self.iter_seal_message(plaintext, stream_final=stream_final))

    def open_stream_record(self, record: bytes) -> OpenedRecord:
        """Authenticate one record and return its plaintext and boundaries.

        Raises :class:`RecordError` on tamper, replay, reorder, or
        oversize — after which the channel must be torn down.  This mode does
        not append plaintext to the whole-message compatibility buffer.
        """
        if not isinstance(record, (bytes, bytearray)) or len(record) <= _SEQ_LEN:
            raise RecordError("record too short")
        record = bytes(record)
        seq_bytes, ciphertext = record[:_SEQ_LEN], record[_SEQ_LEN:]
        if int.from_bytes(seq_bytes, "big") != self._recv_seq:
            raise RecordError("record out of sequence")
        nonce = self._recv_dir + seq_bytes
        aad = self._transcript_hash + self._recv_dir + seq_bytes
        try:
            plaintext = self._recv.decrypt(nonce, ciphertext, aad)
        except InvalidTag as exc:
            raise RecordError("record failed authentication") from exc
        self._recv_seq += 1

        if not plaintext:
            raise RecordError("record missing flags byte")
        flags, chunk = plaintext[0], plaintext[1:]
        if flags & ~_KNOWN_FLAGS:
            raise RecordError("record has unknown flags")
        if len(chunk) > CHUNK_SIZE:
            raise RecordError("record chunk exceeds maximum size")

        self._incoming_message_size += len(chunk)
        if self._incoming_message_size > self._max_message_size:
            raise RecordError("message exceeds maximum size")
        stream_final = bool(flags & _FLAG_STREAM_FINAL)
        # Original clients use 0x01 as their sole final/message-end bit.
        message_end = bool(flags & _FLAG_MSG_END) or stream_final
        if message_end:
            self._incoming_message_size = 0
        return OpenedRecord(
            chunk=bytes(chunk),
            message_end=message_end,
            stream_final=stream_final,
        )

    def open_record(self, record: bytes) -> Optional[bytes]:
        """Compatibility whole-message receive path.

        Returns the completed message at ``MSG_END`` (or a legacy 0x01
        boundary), and ``None`` while mid-message.
        """
        opened = self.open_stream_record(record)
        chunk = opened.chunk
        self._buffer.extend(chunk)
        self._peak_buffered_bytes = max(self._peak_buffered_bytes, len(self._buffer))
        if opened.message_end:
            message = bytes(self._buffer)
            self._buffer = bytearray()
            return message
        return None

    @property
    def buffered_bytes(self) -> int:
        """Bytes held by the compatibility whole-message receive path."""
        return len(self._buffer)

    @property
    def peak_buffered_bytes(self) -> int:
        """Peak compatibility-buffer occupancy (zero in pure stream mode)."""
        return self._peak_buffered_bytes
