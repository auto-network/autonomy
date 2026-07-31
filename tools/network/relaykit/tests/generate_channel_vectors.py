"""Generate the cross-language channel vectors — tools/.../fixtures/channel_v1.json.

One security protocol, implemented once. Python, browser JavaScript, and Go all
validate against these exact bytes. The fixture is DETERMINISTIC (fixed test
keys, no sockets, no wall clock) and is built by CALLING the real channel.py /
idkit primitives with fixed inputs, so it can never drift from the code it
freezes. See decision note graph://8b745204-91e and bead auto-25pky.

Flag semantics are the deployed c31xb 2-bit mask STREAM_FINAL(0x01)|MSG_END(0x02):
valid record flags are {0x00,0x01,0x02,0x03}; a reserved bit (0x04) is rejected.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tools.network.idkit import KeyPair, Subject, canonical_json, issue_cert
from tools.network.relaykit.channel import (
    CHUNK_SIZE,
    DIR_C2S,
    DIR_S2C,
    HANDSHAKE_DOMAIN,
    HANDSHAKE_VERSION,
    KEYS_INFO,
    MAX_MESSAGE_SIZE,
    ChannelCrypto,
    _derive_keys,
    _signed_payload,
    _transcript_hash,
    _MAX_SEQ,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "channel_v1.json"

# Deterministic TEST material — visibly labelled; never real key material.
ORG = "00000000-0000-4000-8000-0000000000aa"        # fixed test org uuid
TOKEN = "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"           # fixed 32-hex test token
NOW = 1_800_000_000                                  # fixed test clock (seconds)
FLAG_STREAM_FINAL = 0x01
FLAG_MSG_END = 0x02
KNOWN_FLAGS = FLAG_STREAM_FINAL | FLAG_MSG_END


def _seed(label: str) -> bytes:
    """A deterministic, clearly-labelled 32-byte TEST seed."""
    return hashlib.sha256(b"autonomy.network.channel.vectors.v1/TEST/" + label.encode()).digest()


def _x25519(label: str) -> X25519PrivateKey:
    return X25519PrivateKey.from_private_bytes(_seed(label))


def _x_pub_hex(priv: X25519PrivateKey) -> str:
    return priv.public_key().public_bytes_raw().hex()


def _keypair(label: str) -> KeyPair:
    return KeyPair.from_private_hex(_seed(label).hex())


def _client_hello_bytes(client_pub: str) -> bytes:
    return canonical_json({"v": HANDSHAKE_VERSION, "eph_pub": client_pub})


def _server_hello_bytes(server_pub: str, cert_wire: str, sig: str) -> bytes:
    return canonical_json(
        {"v": HANDSHAKE_VERSION, "eph_pub": server_pub, "cert": cert_wire, "sig": sig}
    )


# ── handshakes ──────────────────────────────────────────────────────


def _build_handshake(name: str, *, chain: str):
    """One positive handshake with a fixed-key cert chain.

    chain='single' -> root issues one tunnel:serve delegate.
    chain='two-hop' -> root -> mid (broad) -> leaf (strictly narrowed tunnel:serve).
    Returns (fixture_dict, derived) where derived carries transcript + keys for
    the record/stream vectors to reuse.
    """
    client_priv = _x25519(name + "/client")
    server_priv = _x25519(name + "/server")
    client_pub = _x_pub_hex(client_priv)
    server_pub = _x_pub_hex(server_priv)

    root = _keypair(name + "/root")
    delegates = []           # ordered root-to-leaf, fixture form
    delegate_kps = []
    if chain == "single":
        leaf = _keypair(name + "/leaf")
        cert = issue_cert(
            root, leaf.public_hex, scope=("tunnel:serve",), org=ORG,
            subject=Subject("operator", "op-serve"),
            not_before=NOW - 3600, not_after=NOW + 86400,
        )
        delegate_kps = [leaf]
        certs = [cert]
    else:
        mid = _keypair(name + "/mid")
        leaf = _keypair(name + "/leaf")
        cert_mid = issue_cert(
            root, mid.public_hex, scope=("tunnel:serve", "link:read"), org=ORG,
            subject=Subject("operator", "op-mid"),
            not_before=NOW - 3600, not_after=NOW + 172800,
        )
        cert_leaf = issue_cert(
            mid, leaf.public_hex, scope=("tunnel:serve",), org=ORG,
            subject=Subject("operator", "op-serve"),
            not_before=NOW - 1800, not_after=NOW + 86400,
            parent_cert=cert_mid,
        )
        delegate_kps = [mid, leaf]
        certs = [cert_mid, cert_leaf]

    signer = delegate_kps[-1]
    leaf_cert = certs[-1]
    cert_wire = leaf_cert.to_json().decode("ascii")
    sig = signer.sign_hex(_signed_payload(ORG, TOKEN, client_pub, server_pub))

    client_hello = _client_hello_bytes(client_pub)
    server_hello = _server_hello_bytes(server_pub, cert_wire, sig)
    transcript = _transcript_hash(ORG, TOKEN, client_pub, server_pub, cert_wire)

    # ECDH + HKDF, exactly as the real handshake derives them.
    shared = client_priv.exchange(server_priv.public_key())
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes
    okm = HKDF(algorithm=hashes.SHA256(), length=64, salt=transcript, info=KEYS_INFO).derive(shared)
    key_c2s, key_s2c = okm[:32], okm[32:]

    for kp, c in zip(delegate_kps, certs):
        delegates.append({
            "private_hex": kp.private_hex,
            "public_hex": kp.public_hex,
            "cert_wire_utf8_hex": c.to_json().decode("ascii").encode("utf-8").hex(),
        })

    fixture = {
        "name": name,
        "now": NOW,
        "org": ORG,
        "token": TOKEN,
        "root": {"private_hex": root.private_hex, "public_hex": root.public_hex},
        "delegates": delegates,
        "client_private_hex": _seed(name + "/client").hex(),
        "client_public_hex": client_pub,
        "server_private_hex": _seed(name + "/server").hex(),
        "server_public_hex": server_pub,
        "client_hello_utf8_hex": client_hello.hex(),
        "signed_payload_hex": _signed_payload(ORG, TOKEN, client_pub, server_pub).hex(),
        "hello_signature_hex": sig,
        "server_hello_utf8_hex": server_hello.hex(),
        "transcript_hash_hex": transcript.hex(),
        "shared_secret_hex": shared.hex(),
        "hkdf_okm_hex": okm.hex(),
        "key_c2s_hex": key_c2s.hex(),
        "key_s2c_hex": key_s2c.hex(),
    }
    derived = {
        "transcript": transcript, "key_c2s": key_c2s, "key_s2c": key_s2c,
        "client_pub": client_pub, "server_pub": server_pub, "root_pub": root.public_hex,
        "server_hello": server_hello, "cert_wire": cert_wire,
    }
    return fixture, derived


# ── records ─────────────────────────────────────────────────────────


def _seal_record(transcript: bytes, key_send: bytes, key_recv: bytes,
                 send_dir: bytes, recv_dir: bytes, seq: int, flags: int, chunk: bytes) -> dict:
    crypto = ChannelCrypto(key_send, key_recv, transcript, send_dir, recv_dir)
    crypto._send_seq = seq
    wire = crypto._seal_record(flags, chunk)
    nonce = send_dir + seq.to_bytes(8, "big")
    aad = transcript + send_dir + seq.to_bytes(8, "big")
    return {
        "name": None,  # filled by caller
        "handshake": None,
        "direction": "c2s" if send_dir == DIR_C2S else "s2c",
        "sequence": seq,
        "flags": flags,
        "chunk_hex": chunk.hex(),
        "nonce_hex": nonce.hex(),
        "aad_hex": aad.hex(),
        "ciphertext_hex": wire[8:].hex(),
        "wire_record_hex": wire.hex(),
    }


def _records(handshake: str, d: dict) -> list:
    t, kc, ks = d["transcript"], d["key_c2s"], d["key_s2c"]
    specs = [
        ("c2s_seq0_nonfinal", DIR_C2S, DIR_S2C, kc, ks, 0, 0x00, b"first-chunk-not-final"),
        ("c2s_seq1_oneshot_empty", DIR_C2S, DIR_S2C, kc, ks, 1, 0x03, b""),
        ("c2s_seq2_msg_end_one_byte", DIR_C2S, DIR_S2C, kc, ks, 2, 0x02, b"Z"),
        ("c2s_seq3_stream_final_only", DIR_C2S, DIR_S2C, kc, ks, 3, 0x01, b"legacy-final"),
        ("s2c_seq0_oneshot", DIR_S2C, DIR_C2S, ks, kc, 0, 0x03, b"server-one-shot"),
        ("s2c_seq1_msg_end", DIR_S2C, DIR_C2S, ks, kc, 1, 0x02, b"server-msg-end"),
    ]
    out = []
    for name, sd, rd, ksend, krecv, seq, flags, chunk in specs:
        rec = _seal_record(t, ksend, krecv, sd, rd, seq, flags, chunk)
        rec["name"] = name
        rec["handshake"] = handshake
        out.append(rec)
    return out


# ── streams ─────────────────────────────────────────────────────────


def _seal_message_records(transcript, key_send, key_recv, send_dir, recv_dir,
                          start_seq: int, message: bytes) -> list:
    crypto = ChannelCrypto(key_send, key_recv, transcript, send_dir, recv_dir)
    crypto._send_seq = start_seq
    return [r.hex() for r in crypto.seal_message(message)]


def _streams(handshake: str, d: dict) -> list:
    t, kc, ks = d["transcript"], d["key_c2s"], d["key_s2c"]

    def client_step(seq, msg):
        return {"sender": "client", "message_hex": msg.hex(), "start_sequence": seq,
                "records_hex": _seal_message_records(t, kc, ks, DIR_C2S, DIR_S2C, seq, msg)}

    def server_step(seq, msg):
        return {"sender": "server", "message_hex": msg.hex(), "start_sequence": seq,
                "records_hex": _seal_message_records(t, ks, kc, DIR_S2C, DIR_C2S, seq, msg)}

    streams = []
    streams.append({"name": "empty_message", "handshake": handshake,
                    "steps": [client_step(0, b"")]})
    streams.append({"name": "single_record", "handshake": handshake,
                    "steps": [client_step(0, b"a short note")]})
    streams.append({"name": "chunk_plus_one", "handshake": handshake,
                    "steps": [client_step(0, b"x" * (CHUNK_SIZE + 1))]})
    streams.append({"name": "exact_chunk_multiple", "handshake": handshake,
                    "steps": [client_step(0, b"y" * (2 * CHUNK_SIZE))]})
    # Alternating request/response: directional sequences advance independently.
    streams.append({"name": "alternating", "handshake": handshake, "steps": [
        client_step(0, b"request one"),
        server_step(0, b"response one"),
        client_step(1, b"request two"),
        server_step(1, b"response two"),
    ]})
    return streams


# ── negative cases ──────────────────────────────────────────────────


def _aesgcm_record(key: bytes, transcript: bytes, direction: bytes, seq: int,
                   flags: int, chunk: bytes) -> bytes:
    """Seal a record bypassing _seal_record's flag guard (to craft reserved-flag
    and other malformed records that OPEN must still reject)."""
    nonce = direction + seq.to_bytes(8, "big")
    aad = transcript + direction + seq.to_bytes(8, "big")
    ct = AESGCM(key).encrypt(nonce, bytes([flags]) + chunk, aad)
    return seq.to_bytes(8, "big") + ct


def _negatives(d: dict) -> list:
    t, kc, ks = d["transcript"], d["key_c2s"], d["key_s2c"]
    cp, sp = d["client_pub"], d["server_pub"]
    hello = d["server_hello"]
    root_pub = d["root_pub"]
    cases: list[dict] = []

    def hello_case(name, wire_bytes, expected, *, root=root_pub, org=ORG, token=TOKEN,
                   client_pub=cp, now=NOW):
        cases.append({"name": name, "stage": "hello", "expected_error": expected,
                      "input": {"kind": "server_hello", "wire_utf8_hex": wire_bytes.hex(),
                                "root_public_hex": root, "org": org, "token": token,
                                "client_public_hex": client_pub, "now": now}})

    # Wrong pin / scoping — the valid hello, verified against the wrong inputs.
    hello_case("wrong_root", hello, "wrong_root", root=_keypair("attacker/root").public_hex)
    hello_case("wrong_org", hello, "wrong_org", org="00000000-0000-4000-8000-0000000000bb")
    hello_case("wrong_token", hello, "invalid_signature", token="b" * 32)
    hello_case("substituted_client_eph", hello, "invalid_signature",
               client_pub=_x_pub_hex(_x25519("attacker/client")))

    # Substituted server ephemeral: rewrite eph_pub in the wire, sig unchanged.
    sub = json.loads(hello)
    sub["eph_pub"] = _x_pub_hex(_x25519("attacker/server"))
    hello_case("substituted_server_eph", canonical_json(sub), "invalid_signature")

    forged = json.loads(hello)
    forged["sig"] = "0" * 128
    hello_case("forged_hello_signature", canonical_json(forged), "invalid_signature")

    hello_case("malformed_not_json", b"{not json", "invalid_encoding")
    noncanon = b'{"eph_pub":"' + sp.encode() + b'","v":1,"cert":"x","sig":"y"}'  # keys unsorted
    hello_case("noncanonical_hello", noncanon, "invalid_encoding")

    badver = json.loads(hello); badver["v"] = 2
    hello_case("wrong_version", canonical_json(badver), "unsupported_version")

    missing = json.loads(hello); del missing["sig"]
    hello_case("missing_hello_field", canonical_json(missing), "invalid_fields")
    extra = json.loads(hello); extra["extra"] = 1
    hello_case("extra_hello_field", canonical_json(extra), "invalid_fields")

    badeph = json.loads(hello); badeph["eph_pub"] = "zz" * 32
    hello_case("invalid_ephemeral", canonical_json(badeph), "invalid_ephemeral")

    # Certificate defects — rebuild the hello with a defective leaf cert.
    def hello_with_cert(cert, signer, name, expected, *, now=NOW):
        cw = cert.to_json().decode("ascii")
        s = signer.sign_hex(_signed_payload(ORG, TOKEN, cp, sp))
        hello_case(name, _server_hello_bytes(sp, cw, s), expected, now=now)

    root2 = _keypair("cert/root"); leaf2 = _keypair("cert/leaf")
    no_scope = issue_cert(root2, leaf2.public_hex, scope=("link:read",), org=ORG,
                          subject=Subject("operator", "op"), not_before=NOW - 60, not_after=NOW + 3600)
    # wrong_root because this cert chains to root2, not the pinned root.
    hello_with_cert(no_scope, leaf2, "missing_tunnel_serve_scope_wrong_root", "wrong_root")

    expired = issue_cert(root2, leaf2.public_hex, scope=("tunnel:serve",), org=ORG,
                         subject=Subject("operator", "op"), not_before=NOW - 7200, not_after=NOW - 3600)
    hello_with_cert(expired, leaf2, "expired_cert", "invalid_time")
    future = issue_cert(root2, leaf2.public_hex, scope=("tunnel:serve",), org=ORG,
                        subject=Subject("operator", "op"), not_before=NOW + 3600, not_after=NOW + 7200)
    hello_with_cert(future, leaf2, "not_yet_valid_cert", "invalid_time")

    noncanon_cert = json.loads(hello)
    noncanon_cert["cert"] = '{"child_pub":"x"}'   # not a canonical cert wire
    hello_case("noncanonical_cert", canonical_json(noncanon_cert), "noncanonical_cert")

    # ---- record_open negatives ----
    def rec_open(name, records, expected, *, key=ks, transcript=t, direction="s2c",
                 start=0, maxm=MAX_MESSAGE_SIZE):
        cases.append({"name": name, "stage": "record_open", "expected_error": expected,
                      "input": {"kind": "records", "records_hex": [r.hex() if isinstance(r, bytes) else r for r in records],
                                "recv_key_hex": key.hex(), "transcript_hash_hex": transcript.hex(),
                                "recv_direction": direction, "start_sequence": start,
                                "max_message_size": maxm}})

    good = ChannelCrypto(ks, kc, t, DIR_S2C, DIR_C2S)
    r0 = good._seal_record(0x03, b"hello")            # s2c seq0 one-shot
    good2 = ChannelCrypto(ks, kc, t, DIR_S2C, DIR_C2S); good2._send_seq = 1
    r1 = good2._seal_record(0x03, b"second")          # s2c seq1

    tampered = bytearray(r0); tampered[-1] ^= 0x01
    rec_open("bad_gcm_tag", [bytes(tampered)], "authentication_failed")
    rec_open("replay_same_sequence", [r0, r0], "sequence_mismatch")
    rec_open("reorder_sequence", [r1], "sequence_mismatch", start=0)  # expects seq0, gets seq1
    rec_open("wrong_direction", [r0], "authentication_failed", direction="c2s")
    rec_open("different_transcript", [r0], "authentication_failed",
             transcript=hashlib.sha256(b"other").digest())
    reserved = _aesgcm_record(ks, t, DIR_S2C, 0, 0x04, b"reserved")
    rec_open("reserved_flags", [reserved], "reserved_flags")
    big0 = ChannelCrypto(ks, kc, t, DIR_S2C, DIR_C2S)
    b0 = big0._seal_record(0x00, b"x" * CHUNK_SIZE)
    big1 = ChannelCrypto(ks, kc, t, DIR_S2C, DIR_C2S); big1._send_seq = 1
    b1 = big1._seal_record(0x03, b"y" * CHUNK_SIZE)
    rec_open("message_too_large", [b0, b1], "message_too_large", maxm=CHUNK_SIZE + 1)

    # ---- record_seal negatives ----
    def rec_seal(name, messages, expected, *, key=kc, transcript=t, direction="c2s",
                 start=0, maxm=MAX_MESSAGE_SIZE):
        cases.append({"name": name, "stage": "record_seal", "expected_error": expected,
                      "input": {"kind": "messages", "messages_hex": [m.hex() for m in messages],
                                "send_key_hex": key.hex(), "transcript_hash_hex": transcript.hex(),
                                "send_direction": direction, "start_sequence": start,
                                "max_message_size": maxm}})

    rec_seal("seal_message_too_large", [b"z" * (CHUNK_SIZE + 1)], "message_too_large",
             maxm=CHUNK_SIZE)
    rec_seal("seal_sequence_exhausted", [b"tiny"], "sequence_exhausted", start=_MAX_SEQ)
    return cases


# ── assemble + write ────────────────────────────────────────────────


def build_vectors() -> dict:
    hs_single, d_single = _build_handshake("single_hop", chain="single")
    hs_two, d_two = _build_handshake("two_hop_narrowed", chain="two-hop")
    return {
        "schema": "autonomy.network.channel.vectors.v1",
        "encoding": "lowercase-hex",
        "constants": {
            "handshake_domain_utf8_hex": HANDSHAKE_DOMAIN.hex(),
            "keys_info_utf8_hex": KEYS_INFO.hex(),
            "handshake_version": HANDSHAKE_VERSION,
            "dir_c2s_hex": DIR_C2S.hex(),
            "dir_s2c_hex": DIR_S2C.hex(),
            "chunk_size": CHUNK_SIZE,
            "max_message_size": MAX_MESSAGE_SIZE,
            "max_sequence": _MAX_SEQ,
            "flag_stream_final": FLAG_STREAM_FINAL,
            "flag_msg_end": FLAG_MSG_END,
            "known_flags_mask": KNOWN_FLAGS,
            "canonical_json": "utf-8, sorted keys, no insignificant whitespace, integers only",
        },
        "handshakes": [hs_single, hs_two],
        "records": _records("single_hop", d_single),
        "streams": _streams("single_hop", d_single),
        "negative_cases": _negatives(d_single),
    }


def render(vectors: dict) -> str:
    return json.dumps(vectors, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    text = render(build_vectors())
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(text, encoding="utf-8")
    print(f"wrote {FIXTURE} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
