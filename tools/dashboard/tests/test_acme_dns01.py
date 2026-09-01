from __future__ import annotations

from tools.dashboard import acme_dns01
from tools.network.idkit import KeyPair, Subject, issue_cert


ROOT = KeyPair.from_private_hex("11" * 32)
LEAF = KeyPair.from_private_hex("22" * 32)
PERSONA = "33" * 32
ORG = "019dae5c-d13b-7b95-9e48-408ac581cd75"


def _authority():
    cert = issue_cert(
        ROOT,
        child_pub=LEAF.public_hex,
        scope=("serve:dns-01",),
        org=ORG,
        subject=Subject(kind="persona", id=PERSONA),
        not_before=1_700_000_000,
        not_after=1_700_003_600,
    )
    return acme_dns01.Dns01Authority(key=LEAF, cert=cert)


def test_present_and_cleanup_use_frozen_signed_contract():
    calls = []

    def control(org, op, args):
        calls.append((org, op, args))
        if op == "connector-status":
            return {"ok": True, "serving": True,
                    "accepted_caps": ["host-lease/1", "dns-01/1"]}
        if op == "serve.dns01.present":
            return {"ok": True, "name": "_acme-challenge.p.serve.auto.network",
                    "expires_at": args["expiry"]}
        return {"ok": True}

    client = acme_dns01.Dns01Client(
        ORG, _authority(), control=control, now=lambda: 1_700_000_100,
    )
    shown = client.present("certbot-run-1", "challenge-value", ttl=60,
                           lifetime=600)
    assert shown == {
        "name": "_acme-challenge.p.serve.auto.network",
        "expires_at": 1_700_000_700,
    }
    client.cleanup("certbot-run-1", "challenge-value")

    _, _, present = calls[1]
    assert set(present) == {"order", "value", "ttl", "expiry", "ts", "cert", "sig"}
    assert present["expiry"] == present["ts"] + 600
    assert acme_dns01.verify_request_signature(
        "serve.dns01.present", present, LEAF.public_hex,
    )
    _, _, cleanup = calls[3]
    assert set(cleanup) == {"order", "value", "ts", "cert", "sig"}
    assert acme_dns01.verify_request_signature(
        "serve.dns01.cleanup", cleanup, LEAF.public_hex,
    )


def test_missing_capability_fails_before_dns_mutation():
    calls = []

    def control(org, op, args):
        calls.append(op)
        return {"ok": True, "serving": True, "accepted_caps": ["host-lease/1"]}

    client = acme_dns01.Dns01Client(ORG, _authority(), control=control)
    try:
        client.present("order", "value")
    except acme_dns01.Dns01Unavailable as exc:
        assert str(exc) == "relay did not negotiate dns-01/1"
    else:
        raise AssertionError("missing capability was accepted")
    assert calls == ["connector-status"]


def test_uniform_relay_refusal_is_not_reclassified():
    def control(org, op, args):
        if op == "connector-status":
            return {"ok": True, "serving": True,
                    "accepted_caps": ["dns-01/1"]}
        return {"ok": False, "error": "refused"}

    client = acme_dns01.Dns01Client(ORG, _authority(), control=control)
    try:
        client.present("order", "value")
    except acme_dns01.Dns01Refused as exc:
        assert str(exc) == "DNS-01 operation refused"
    else:
        raise AssertionError("uniform refusal was accepted")


def test_waits_for_challenge_on_every_authoritative_nameserver():
    observations = {
        "ns1.auto.network": [set(), {"challenge"}],
        "ns2.auto.network": [{"challenge"}],
    }
    clock = [0.0]

    def query(server, name):
        values = observations[server]
        return values.pop(0) if len(values) > 1 else values[0]

    def sleep(seconds):
        clock[0] += seconds

    elapsed = acme_dns01.wait_authoritative_txt(
        "_acme-challenge.p.serve.auto.network",
        "challenge",
        query=query,
        now=lambda: clock[0],
        sleep=sleep,
        timeout=5,
        interval=1,
    )
    assert elapsed == 1


def test_authoritative_wait_times_out_if_only_one_ns_observes_value():
    clock = [0.0]

    def query(server, name):
        return {"challenge"} if server == "ns1.auto.network" else set()

    try:
        acme_dns01.wait_authoritative_txt(
            "_acme-challenge.p.serve.auto.network",
            "challenge",
            query=query,
            now=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            timeout=2,
            interval=1,
        )
    except acme_dns01.Dns01Unavailable as exc:
        assert str(exc) == "DNS-01 value did not reach every authoritative nameserver"
    else:
        raise AssertionError("single-authoritative visibility was accepted")
