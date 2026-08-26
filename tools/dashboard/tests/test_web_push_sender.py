"""Protocol and safe-egress proofs for the production Web Push sender."""

from __future__ import annotations

import base64
import json
import os
import ssl
import sys
import types

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tools.dashboard import web_push_sender as sender


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class StaticResolver:
    def __init__(self, *, addresses=("93.184.216.34",), cnames=None):
        self._addresses = addresses
        self._cnames = cnames or {}
        self.address_calls = []
        self.cname_calls = []

    def cname(self, host):
        self.cname_calls.append(host)
        return self._cnames.get(host)

    def addresses(self, host):
        self.address_calls.append(host)
        return list(self._addresses)


class CaptureTransport:
    def __init__(self, status=201, headers=None):
        self.status = status
        self.response_headers = headers or {}
        self.calls = []

    def post(self, destination, *, headers, body, timeout):
        self.calls.append({
            "destination": destination,
            "headers": dict(headers),
            "body": body,
            "timeout": timeout,
        })
        return sender.TransportResponse(self.status, self.response_headers)


class _VapidSigner:
    """Independent ES256 signer with the small py_vapid interface we consume."""

    def __init__(self):
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.public_key = self.private_key.public_key()

    def sign(self, claims):
        header = _b64url(b'{"typ":"JWT","alg":"ES256"}')
        payload = _b64url(json.dumps(
            claims, separators=(",", ":"), sort_keys=True,
        ).encode("utf-8"))
        signing_input = f"{header}.{payload}".encode("ascii")
        der = self.private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        signature = _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        public = self.public_key.public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint,
        )
        return {
            "Authorization": f"vapid t={header}.{payload}.{signature},k={_b64url(public)}",
        }


def _hkdf(*, key_material, salt, info, length):
    return HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=salt,
        info=info,
    ).derive(key_material)


class _Rfc8291Encoder:
    """Independent RFC 8291 encoder used because unit Python is dependency-light."""

    def encode(self, *, endpoint, p256dh, auth_secret, plaintext):
        del endpoint
        receiver_public = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), _decode(p256dh),
        )
        ephemeral = ec.generate_private_key(ec.SECP256R1())
        ephemeral_public = ephemeral.public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint,
        )
        receiver_bytes = receiver_public.public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint,
        )
        shared = ephemeral.exchange(ec.ECDH(), receiver_public)
        ikm = _hkdf(
            key_material=shared,
            salt=_decode(auth_secret),
            info=b"WebPush: info\x00" + receiver_bytes + ephemeral_public,
            length=32,
        )
        salt = os.urandom(16)
        content_key = _hkdf(
            key_material=ikm,
            salt=salt,
            info=b"Content-Encoding: aes128gcm\x00",
            length=16,
        )
        nonce = _hkdf(
            key_material=ikm,
            salt=salt,
            info=b"Content-Encoding: nonce\x00",
            length=12,
        )
        ciphertext = AESGCM(content_key).encrypt(nonce, plaintext + b"\x02", None)
        return salt + (4096).to_bytes(4, "big") + bytes([65]) + ephemeral_public + ciphertext


def _decrypt_rfc8291(body, *, receiver, auth_secret):
    salt = body[:16]
    record_size = int.from_bytes(body[16:20], "big")
    key_length = body[20]
    ephemeral_bytes = body[21:21 + key_length]
    ciphertext = body[21 + key_length:]
    assert record_size == 4096
    assert key_length == 65
    ephemeral = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), ephemeral_bytes,
    )
    receiver_bytes = receiver.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint,
    )
    shared = receiver.exchange(ec.ECDH(), ephemeral)
    ikm = _hkdf(
        key_material=shared,
        salt=auth_secret,
        info=b"WebPush: info\x00" + receiver_bytes + ephemeral_bytes,
        length=32,
    )
    content_key = _hkdf(
        key_material=ikm,
        salt=salt,
        info=b"Content-Encoding: aes128gcm\x00",
        length=16,
    )
    nonce = _hkdf(
        key_material=ikm,
        salt=salt,
        info=b"Content-Encoding: nonce\x00",
        length=12,
    )
    padded = AESGCM(content_key).decrypt(nonce, ciphertext, None)
    assert padded.endswith(b"\x02")
    return padded[:-1]


@pytest.fixture
def keys():
    receiver = ec.generate_private_key(ec.SECP256R1())
    public = receiver.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint,
    )
    auth = b"0123456789abcdef"
    vapid = _VapidSigner()
    return receiver, _b64url(public), auth, vapid


def _send(keys, **overrides):
    receiver, p256dh, auth, vapid = keys
    resolver = overrides.pop("resolver", StaticResolver())
    transport = overrides.pop("transport", CaptureTransport())
    arguments = {
        "endpoint": "https://push.example.test/send/opaque?x=1",
        "p256dh": p256dh,
        "auth_secret": _b64url(auth),
        "payload": '{"event_id":"opaque","title":"Autonomy needs attention"}',
        "vapid_key": vapid,
        "vapid_subject": "mailto:ops@example.test",
        "ttl": 3600,
        "urgency": "normal",
        "topic": "approval_opaque",
        "resolver": resolver,
        "transport": transport,
        "encoder": _Rfc8291Encoder(),
        "timeout": 7.5,
        "now": 1_800_000_000.25,
    }
    arguments.update(overrides)
    result = sender.send_encrypted_web_push(**arguments)
    return result, transport, resolver, receiver, auth, vapid


class TestEncryptedWebPushSender:
    def test_maintained_encoder_boundary_selects_only_aes128gcm(self, monkeypatch):
        observed = {}

        class FakePusher:
            def __init__(self, subscription):
                observed["subscription"] = subscription

            def encode(self, plaintext, *, content_encoding):
                observed["plaintext"] = plaintext
                observed["encoding"] = content_encoding
                return {"body": b"encrypted"}

        monkeypatch.setitem(
            sys.modules, "pywebpush", types.SimpleNamespace(WebPusher=FakePusher),
        )
        body = sender.PyWebPushPayloadEncoder().encode(
            endpoint="https://push.example.test/x",
            p256dh="receiver",
            auth_secret="secret",
            plaintext=b'{"x":1}',
        )
        assert body == b"encrypted"
        assert observed == {
            "subscription": {
                "endpoint": "https://push.example.test/x",
                "keys": {"p256dh": "receiver", "auth": "secret"},
            },
            "plaintext": b'{"x":1}',
            "encoding": "aes128gcm",
        }

    def test_emits_decryptable_rfc8291_and_verified_vapid(self, keys):
        result, transport, resolver, receiver, auth, vapid = _send(keys)
        assert result == sender.SendResult(201, "accepted", False, False, None)
        assert resolver.cname_calls == ["push.example.test"]
        assert resolver.address_calls == ["push.example.test"]
        assert len(transport.calls) == 1
        publication = transport.calls[0]
        destination = publication["destination"]
        assert destination.address == "93.184.216.34"
        assert destination.endpoint.host == "push.example.test"
        assert destination.endpoint.request_target == "/send/opaque?x=1"
        assert destination.endpoint.audience == "https://push.example.test"
        assert publication["timeout"] == 7.5

        headers = publication["headers"]
        assert headers["Content-Encoding"] == "aes128gcm"
        assert headers["Content-Type"] == "application/octet-stream"
        assert headers["Content-Length"] == str(len(publication["body"]))
        assert headers["TTL"] == "3600"
        assert headers["Urgency"] == "normal"
        assert headers["Topic"] == "approval_opaque"
        assert set(headers) == {
            "Authorization", "Content-Encoding", "Content-Type",
            "Content-Length", "TTL", "Urgency", "Topic",
        }

        plaintext = _decrypt_rfc8291(
            publication["body"], receiver=receiver, auth_secret=auth,
        )
        assert json.loads(plaintext) == {
            "event_id": "opaque",
            "title": "Autonomy needs attention",
        }
        assert plaintext not in publication["body"]

        match = sender._AUTHORIZATION.fullmatch(headers["Authorization"])
        assert match is not None
        token, encoded_key = match.groups()
        signing_input, signature_text = token.rsplit(".", 1)
        header_text, claims_text = signing_input.split(".", 1)
        assert json.loads(_decode(header_text)) == {"typ": "JWT", "alg": "ES256"}
        claims = json.loads(_decode(claims_text))
        assert claims == {
            "aud": "https://push.example.test",
            "sub": "mailto:ops@example.test",
            "exp": 1_800_043_200,
        }
        public = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), _decode(encoded_key),
        )
        raw_signature = _decode(signature_text)
        public.verify(
            encode_dss_signature(
                int.from_bytes(raw_signature[:32], "big"),
                int.from_bytes(raw_signature[32:], "big"),
            ),
            signing_input.encode("ascii"),
            ec.ECDSA(hashes.SHA256()),
        )
        expected_vapid = vapid.public_key.public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint,
        )
        assert _decode(encoded_key) == expected_vapid
        # RFC 8292 forbids reusing the VAPID signing key as the RFC 8291
        # ephemeral ECDH key.  aes128gcm's keyid begins after salt/rs/idlen.
        assert publication["body"][21:86] != expected_vapid

    @pytest.mark.parametrize("status, outcome, retryable, retire", [
        (200, "accepted", False, False),
        (202, "accepted", False, False),
        (204, "accepted", False, False),
        (301, "redirect_refused", False, False),
        (307, "redirect_refused", False, False),
        (400, "permanent_failure", False, False),
        (401, "permanent_failure", False, False),
        (403, "permanent_failure", False, False),
        (404, "retired", False, True),
        (408, "retryable", True, False),
        (410, "retired", False, True),
        (425, "retryable", True, False),
        (429, "retryable", True, False),
        (500, "retryable", True, False),
        (503, "retryable", True, False),
    ])
    def test_classifies_push_service_responses(
        self, keys, status, outcome, retryable, retire,
    ):
        result, transport, *_ = _send(
            keys, transport=CaptureTransport(status, {"Retry-After": "90"}),
        )
        assert len(transport.calls) == 1
        assert result.status == status
        assert result.outcome == outcome
        assert result.retryable is retryable
        assert result.retire_subscription is retire
        assert result.retry_after == (90.0 if retryable else None)

    def test_redirect_is_returned_not_followed(self, keys):
        transport = CaptureTransport(302, {"Location": "http://127.0.0.1/private"})
        result, *_ = _send(keys, transport=transport)
        assert result.outcome == "redirect_refused"
        assert len(transport.calls) == 1

    @pytest.mark.parametrize("endpoint", [
        "http://push.example.test/send",
        "https://user@push.example.test/send",
        "https://push.example.test:444/send",
        "https://push.example.test/send#fragment",
        "https://127.0.0.1/send",
        "https://[::1]/send",
        " https://push.example.test/send",
        "https://push.example.test/send\nX-Test: yes",
        "https://push.example.test/unicode-✓",
        "https://push.example.test/a\\b",
    ])
    def test_refuses_malformed_destination_before_dns_or_send(self, keys, endpoint):
        resolver = StaticResolver()
        transport = CaptureTransport()
        with pytest.raises(sender.WebPushEgressPolicyError):
            _send(keys, endpoint=endpoint, resolver=resolver, transport=transport)
        assert resolver.cname_calls == []
        assert transport.calls == []

    @pytest.mark.parametrize("address", [
        "0.0.0.0",
        "10.0.0.1",
        "100.64.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "192.0.2.1",
        "224.0.0.1",
        "255.255.255.255",
        "::",
        "::1",
        "fe80::1",
        "fc00::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:93.184.216.34",
        "64:ff9b::7f00:1",
        "64:ff9b:1::7f00:1",
        "2002:7f00:1::",
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",
    ])
    def test_refuses_every_non_global_or_mapped_answer(self, keys, address):
        transport = CaptureTransport()
        with pytest.raises(sender.WebPushEgressPolicyError):
            _send(
                keys,
                resolver=StaticResolver(addresses=(address,)),
                transport=transport,
            )
        assert transport.calls == []

    def test_mixed_public_private_answers_fail_closed(self, keys):
        transport = CaptureTransport()
        with pytest.raises(sender.WebPushEgressPolicyError):
            _send(
                keys,
                resolver=StaticResolver(addresses=("93.184.216.34", "127.0.0.1")),
                transport=transport,
            )
        assert transport.calls == []

    def test_cname_chain_is_bounded_and_loop_safe(self, keys):
        loop = StaticResolver(cnames={
            "push.example.test": "alias.example.test",
            "alias.example.test": "push.example.test",
        })
        with pytest.raises(sender.WebPushEgressPolicyError, match="loop"):
            _send(keys, resolver=loop)

        cnames = {
            f"n{number}.example.test": f"n{number + 1}.example.test"
            for number in range(10)
        }
        deep = StaticResolver(cnames=cnames)
        with pytest.raises(sender.WebPushEgressPolicyError, match="too deep"):
            _send(keys, endpoint="https://n0.example.test/x", resolver=deep)
        assert deep.address_calls == []

    def test_dns_answer_is_used_once_and_pinned_for_the_attempt(self, keys):
        class RebindingResolver(StaticResolver):
            def addresses(self, host):
                self.address_calls.append(host)
                return ("93.184.216.34",) if len(self.address_calls) == 1 else ("127.0.0.1",)

        resolver = RebindingResolver()
        result, transport, *_ = _send(keys, resolver=resolver)
        assert result.outcome == "accepted"
        assert resolver.address_calls == ["push.example.test"]
        assert transport.calls[0]["destination"].address == "93.184.216.34"

    def test_exact_address_connect_keeps_original_sni_and_tls_failure(self, monkeypatch):
        calls = []

        class RawSocket:
            def close(self):
                calls.append(("close",))

        class Context:
            def wrap_socket(self, raw, *, server_hostname):
                calls.append(("sni", raw, server_hostname))
                raise ssl.SSLCertVerificationError("hostname mismatch")

        def connector(address, timeout, source_address):
            calls.append(("connect", address, timeout, source_address))
            return RawSocket()

        monkeypatch.setattr(sender.socket, "create_connection", connector)
        connection = sender._PinnedHTTPSConnection(
            "push.example.test",
            "93.184.216.34",
            timeout=4.0,
            context=Context(),
        )
        with pytest.raises(ssl.SSLCertVerificationError):
            connection.connect()
        assert calls[0] == ("connect", ("93.184.216.34", 443), 4.0, None)
        assert calls[1][0] == "sni"
        assert calls[1][2] == "push.example.test"
        assert calls[2] == ("close",)

    @pytest.mark.parametrize("field, value", [
        ("payload", "x" * 2049),
        ("payload", "[]"),
        ("payload", '{"number":NaN}'),
        ("ttl", -1),
        ("ttl", 86401),
        ("ttl", True),
        ("urgency", "urgent"),
        ("topic", "not allowed"),
        ("topic", "x" * 33),
        ("timeout", 0),
        ("now", float("inf")),
        ("now", 1e300),
        ("vapid_subject", "http://example.test/contact"),
    ])
    def test_refuses_payload_and_header_policy_before_send(self, keys, field, value):
        transport = CaptureTransport()
        with pytest.raises(sender.WebPushInputError):
            _send(keys, transport=transport, **{field: value})
        assert transport.calls == []

    def test_refuses_malformed_subscription_keys_before_send(self, keys):
        transport = CaptureTransport()
        with pytest.raises(sender.WebPushInputError):
            _send(keys, p256dh=_b64url(b"\x04" + b"0" * 32), transport=transport)
        with pytest.raises(sender.WebPushInputError):
            _send(keys, auth_secret="not+base64", transport=transport)
        assert transport.calls == []

    def test_vapid_signature_uses_the_supplied_immutable_key(self, keys):
        _receiver, _p256dh, _auth, original = keys
        replacement = _VapidSigner()
        result, transport, *_ = _send(keys, vapid_key=replacement)
        assert result.outcome == "accepted"
        match = sender._AUTHORIZATION.fullmatch(
            transport.calls[0]["headers"]["Authorization"],
        )
        assert match is not None
        expected = replacement.public_key.public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint,
        )
        old = original.public_key.public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint,
        )
        assert _decode(match.group(2)) == expected
        assert expected != old

    def test_refuses_vapid_key_or_signature_confusion(self, keys):
        class ConfusedSigner(_VapidSigner):
            def sign(self, claims):
                signed = super().sign(claims)
                authorization = signed["Authorization"]
                token_part, key_part = authorization.rsplit(",k=", 1)
                signed_material, signature = token_part.rsplit(".", 1)
                signature = (
                    "A" if signature[0] != "A" else "B"
                ) + signature[1:]
                signed["Authorization"] = (
                    signed_material + "." + signature + ",k=" + key_part
                )
                return signed

        transport = CaptureTransport()
        with pytest.raises(sender.WebPushInputError, match="token"):
            _send(keys, vapid_key=ConfusedSigner(), transport=transport)
        assert transport.calls == []

    def test_retry_after_http_date_is_bounded(self):
        result = sender.classify_response(
            429,
            {"Retry-After": "Wed, 15 Jan 2027 08:00:00 GMT"},
            now=1_700_000_000,
        )
        assert result.retry_after == 3600.0

    def test_retry_after_huge_integer_is_capped(self):
        result = sender.classify_response(429, {"Retry-After": "9" * 128})
        assert result.retry_after == 3600.0

    def test_transport_refuses_a_nonverifying_tls_context(self):
        context = ssl.create_default_context()
        context.check_hostname = False
        with pytest.raises(ValueError, match="verify"):
            sender.PinnedHTTPSPostTransport(context=context)
