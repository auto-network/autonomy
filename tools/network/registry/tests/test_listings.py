"""L1 listing directory — signed cards, key-continuity updates, attestations.

Acceptance (bead ``auto-8nk01``, design ``graph://29ff28a8-b39``):

- publish / fetch / list / revoke round-trip;
- a listing not signed by a key chaining to the registered publisher root
  is rejected (unbound org → 404, unsigned/wrong key → 403);
- an update is accepted ONLY if it chains to the same publisher
  key-continuity — a UUID reclaimed after expiry can neither extend nor
  revoke the old chain (hijack-resistance), while a legitimate
  recovery-key rebind CAN continue it;
- names are labels: two orgs both claim the same name, both stored,
  neither privileged;
- attestations are stored on signature validity alone and served per
  subject key — content is the client's problem.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.registry.listings import (
    build_attestation_payload,
    build_listing_payload,
    parse_listing_claim,
    sign_attestation_record,
    sign_listing,
)

from .conftest import DAY, NOW, ORG, register, signed

ORG_B = "22222222-2222-4222-8222-222222222222"
BUNDLE = "ab" * 32


def make_claim(key, publisher=ORG, name="studio", version="1.0.0", *,
               bundle_hash=BUNDLE, prev=None, ts=NOW, cert=None,
               description="a thing", icon="", provider_hints=None):
    payload = build_listing_payload(
        publisher=publisher, name=name, version=version,
        bundle_hash=bundle_hash, description=description, icon=icon,
        provider_hints=provider_hints or [], prev=prev, ts=ts,
        signer=key.public_hex,
    )
    return sign_listing(key, payload, cert=cert)


def publish(client, clock, env_key, claim, *, cert=None, expect=None):
    return signed(client, "POST", "/v1/listings", env_key, {"claim": claim},
                  clock, cert=cert, expect=expect)


@pytest.fixture
def publish_cert(root):
    """Delegated publisher key: root -> publish key, listing scopes only."""
    key = KeyPair.generate()
    cert = issue_cert(
        root, key.public_hex,
        scope=("listing:publish", "listing:revoke"),
        org=ORG, subject=Subject("agent", "publisher-1"),
        not_before=NOW - 100, not_after=NOW + 300 * DAY,
    )
    return key, cert


# -- round-trip ---------------------------------------------------------------


def test_publish_fetch_list_revoke_round_trip(client, clock, root, bound_org):
    claim = make_claim(root)
    created = publish(client, clock, root, claim, expect=201).json()
    lid = created["listing_id"]
    assert created["publisher"] == ORG and created["name"] == "studio"
    assert created["seq"] == 1

    detail = client.get(f"/v1/listings/{ORG}/studio")
    assert detail.status_code == 200
    head = detail.json()["head"]
    assert head["listing_id"] == lid
    assert head["revoked_at"] is None
    # The stored card is the exact wire claim, still verifiable offline.
    assert head["claim"] == claim
    assert parse_listing_claim(head["claim"])["payload"]["bundle_hash"] == BUNDLE

    index = client.get("/v1/listings").json()["listings"]
    assert [e["listing_id"] for e in index] == [lid]
    by_name = client.get("/v1/listings", params={"name": "studio"}).json()["listings"]
    assert [e["listing_id"] for e in by_name] == [lid]

    revoked = signed(client, "DELETE", f"/v1/listings/{ORG}/studio", root, {},
                     clock, expect=200).json()
    assert revoked["revoked"] == 1
    assert client.get("/v1/listings").json()["listings"] == []
    # Fetch still shows the chain, with the withdrawal visible.
    after = client.get(f"/v1/listings/{ORG}/studio").json()
    assert after["head"]["revoked_at"] == clock.now


def test_fetch_unknown_listing_is_404(client, bound_org):
    assert client.get(f"/v1/listings/{ORG}/nope").status_code == 404


def test_delegated_key_publishes_with_scope(client, clock, bound_org, publish_cert):
    key, cert = publish_cert
    claim = make_claim(key, cert=cert)
    publish(client, clock, key, claim, cert=cert, expect=201)


def test_scope_is_required_to_publish(client, clock, root, bound_org):
    key = KeyPair.generate()
    cert = issue_cert(
        root, key.public_hex, scope=("link:publish",), org=ORG,
        subject=Subject("agent", "narrow"),
        not_before=NOW - 100, not_after=NOW + 300 * DAY,
    )
    claim = make_claim(key, cert=cert)
    publish(client, clock, key, claim, cert=cert, expect=403)


# -- signature / registration gating ------------------------------------------


def test_unregistered_publisher_is_rejected(client, clock, root):
    # No binding for ORG at all: nothing to chain to.
    publish(client, clock, root, make_claim(root), expect=404)


def test_claim_signed_by_unregistered_key_is_rejected(client, clock, root, bound_org):
    # Valid envelope from the org root, but the CARD itself is signed by
    # a key with no chain to the bound root — the durable artifact is the
    # thing that must verify.
    stranger = KeyPair.generate()
    publish(client, clock, root, make_claim(stranger), expect=403)


def test_other_orgs_root_cannot_publish_as_publisher(client, clock, root, bound_org):
    root_b = KeyPair.generate()
    register(client, clock, root_b, org_uuid=ORG_B)
    # B claims to publish under A: B's signature never chains to A's root.
    claim = make_claim(root_b, publisher=ORG)
    publish(client, clock, root_b, claim, expect=403)


def test_tampered_claim_is_rejected(client, clock, root, bound_org):
    import json

    claim = json.loads(make_claim(root))
    claim["payload"]["bundle_hash"] = "cd" * 32  # swap the artifact
    tampered = json.dumps(claim, sort_keys=True, separators=(",", ":"))
    publish(client, clock, root, tampered, expect=403)


def test_oversized_description_is_rejected(client, clock, root, bound_org):
    import json

    claim = json.loads(make_claim(root))
    claim["payload"]["description"] = "x" * 5000
    resigned = json.dumps(claim, sort_keys=True, separators=(",", ":"))
    publish(client, clock, root, resigned, expect=400)


# -- updates: key-continuity ---------------------------------------------------


def test_update_chains_and_supersedes(client, clock, root, bound_org):
    v1 = publish(client, clock, root, make_claim(root), expect=201).json()
    v2_claim = make_claim(root, version="1.1.0", prev=v1["listing_id"],
                          ts=NOW + 60)
    v2 = publish(client, clock, root, v2_claim, expect=201).json()
    assert v2["seq"] == 2

    body = client.get(f"/v1/listings/{ORG}/studio").json()
    assert body["head"]["listing_id"] == v2["listing_id"]
    assert [h["seq"] for h in body["history"]] == [1, 2]
    assert body["history"][1]["prev"] == v1["listing_id"]
    # The index shows only the head.
    index = client.get("/v1/listings").json()["listings"]
    assert [e["version"] for e in index] == ["1.1.0"]


def test_duplicate_claim_is_conflict(client, clock, root, bound_org):
    claim = make_claim(root)
    publish(client, clock, root, claim, expect=201)
    publish(client, clock, root, claim, expect=409)


def test_second_root_listing_without_prev_is_conflict(client, clock, root, bound_org):
    publish(client, clock, root, make_claim(root), expect=201)
    publish(client, clock, root, make_claim(root, version="2.0.0", ts=NOW + 5),
            expect=409)


def test_update_must_reference_current_head(client, clock, root, bound_org):
    v1 = publish(client, clock, root, make_claim(root), expect=201).json()
    v2 = publish(
        client, clock, root,
        make_claim(root, version="1.1.0", prev=v1["listing_id"], ts=NOW + 60),
        expect=201,
    ).json()
    # Chaining to the superseded v1 again is stale; to a nonexistent id, worse.
    publish(client, clock, root,
            make_claim(root, version="1.2.0", prev=v1["listing_id"], ts=NOW + 120),
            expect=409)
    publish(client, clock, root,
            make_claim(root, version="1.2.0", prev="00" * 32, ts=NOW + 120),
            expect=409)
    assert client.get(f"/v1/listings/{ORG}/studio").json()["head"]["listing_id"] \
        == v2["listing_id"]


def test_update_from_different_org_key_is_refused(client, clock, root, bound_org):
    v1 = publish(client, clock, root, make_claim(root), expect=201).json()
    root_b = KeyPair.generate()
    register(client, clock, root_b, org_uuid=ORG_B)
    # B mints a well-formed "update" of A's listing. Its signature cannot
    # chain to A's bound root, so it dies at the claim gate.
    fake = make_claim(root_b, publisher=ORG, version="6.6.6",
                      prev=v1["listing_id"], ts=NOW + 60)
    publish(client, clock, root_b, fake, expect=403)


def test_rebind_continues_the_chain(client, clock, root, recovery, bound_org):
    v1 = publish(client, clock, root, make_claim(root), expect=201).json()
    new_root = KeyPair.generate()
    signed(client, "POST", f"/v1/orgs/{ORG}/rebind", recovery,
           {"new_root_pub": new_root.public_hex}, clock, expect=200)
    # The recovery rebind is the pre-declared continuity path: the new
    # root extends the chain the old root started.
    v2 = make_claim(new_root, version="1.1.0", prev=v1["listing_id"], ts=NOW + 60)
    publish(client, clock, new_root, v2, expect=201)


def test_expiry_reclaimer_cannot_touch_the_old_chain(client, clock, root):
    register(client, clock, root, ttl=DAY)
    v1 = publish(client, clock, root, make_claim(root), expect=201).json()

    clock.advance(2 * DAY)  # binding expires
    hijacker = KeyPair.generate()
    register(client, clock, hijacker, ttl=DAY)  # reclaims the UUID

    # The reclaimer IS the bound root now — but has no rebind edge to the
    # root that accepted v1, so the chain is out of reach: no update...
    fake = make_claim(hijacker, version="1.0.1", prev=v1["listing_id"],
                      ts=clock.now)
    publish(client, clock, hijacker, fake, expect=403)
    # ...no fresh shadow listing under the occupied name...
    publish(client, clock, hijacker, make_claim(hijacker, ts=clock.now + 1),
            expect=409)
    # ...and no delisting of the old continuity's card.
    signed(client, "DELETE", f"/v1/listings/{ORG}/studio", hijacker, {},
           clock, expect=403)
    # A different name is untouched territory.
    publish(client, clock, hijacker,
            make_claim(hijacker, name="fresh", ts=clock.now + 2), expect=201)


# -- names are labels ----------------------------------------------------------


def test_two_orgs_share_a_display_name(client, clock, root, bound_org):
    root_b = KeyPair.generate()
    register(client, clock, root_b, org_uuid=ORG_B)
    a = publish(client, clock, root, make_claim(root), expect=201).json()
    b = publish(client, clock, root_b,
                make_claim(root_b, publisher=ORG_B, ts=NOW + 1), expect=201).json()
    assert a["name"] == b["name"] == "studio"

    hits = client.get("/v1/listings", params={"name": "studio"}).json()["listings"]
    assert {e["publisher"] for e in hits} == {ORG, ORG_B}
    # Neither is privileged: each publisher's chain is fetchable under its
    # own key, and revoking one leaves the other standing.
    signed(client, "DELETE", f"/v1/listings/{ORG}/studio", root, {}, clock,
           expect=200)
    hits = client.get("/v1/listings", params={"name": "studio"}).json()["listings"]
    assert [e["publisher"] for e in hits] == [ORG_B]


# -- attestations ---------------------------------------------------------------


def make_attestation(attestor, subject_pub, *, claim_type="display_name",
                     claim_value="Studio Inc", ts=NOW, ttl=30 * DAY,
                     evidence_type="domain-probe", evidence=""):
    payload = build_attestation_payload(
        attestor=attestor.public_hex, subject=subject_pub,
        claim_type=claim_type, claim_value=claim_value,
        evidence_type=evidence_type, evidence=evidence, ts=ts, ttl=ttl,
    )
    return sign_attestation_record(attestor, payload)


def test_attestation_round_trip_and_expiry(client, clock, root, bound_org):
    attestor = KeyPair.generate()  # needs no org binding: the key IS the identity
    record = make_attestation(attestor, root.public_hex)
    created = client.post("/v1/attestations", json={"record": record})
    assert created.status_code == 201, created.json()
    assert created.json()["subject"] == root.public_hex

    got = client.get(f"/v1/attestations/{root.public_hex}").json()
    assert len(got["attestations"]) == 1
    entry = got["attestations"][0]
    assert entry["record"] == record  # served verbatim: the client re-verifies
    assert entry["claim_value"] == "Studio Inc"

    # Redelivery is idempotent (content-addressed).
    again = client.post("/v1/attestations", json={"record": record})
    assert again.status_code == 201
    assert len(client.get(f"/v1/attestations/{root.public_hex}")
               .json()["attestations"]) == 1

    clock.advance(31 * DAY)  # past ts + ttl
    assert client.get(f"/v1/attestations/{root.public_hex}") \
        .json()["attestations"] == []


def test_attestation_bad_signature_is_rejected(client, clock, root):
    import json

    attestor = KeyPair.generate()
    record = json.loads(make_attestation(attestor, root.public_hex))
    record["payload"]["claim_value"] = "Someone Else"  # tamper, keep sig
    tampered = json.dumps(record, sort_keys=True, separators=(",", ":"))
    response = client.post("/v1/attestations", json={"record": tampered})
    assert response.status_code == 403


def test_attestation_malformed_and_expired_are_rejected(client, clock, root):
    attestor = KeyPair.generate()
    assert client.post("/v1/attestations", json={"record": "{not json"}) \
        .status_code == 400
    dead = make_attestation(attestor, root.public_hex, ts=NOW - 40 * DAY,
                            ttl=30 * DAY)
    assert client.post("/v1/attestations", json={"record": dead}) \
        .status_code == 400
    future = make_attestation(attestor, root.public_hex, ts=NOW + DAY)
    assert client.post("/v1/attestations", json={"record": future}) \
        .status_code == 400


def test_attestations_keyed_by_subject(client, clock, root):
    attestor = KeyPair.generate()
    other = KeyPair.generate()
    client.post("/v1/attestations", json={
        "record": make_attestation(attestor, root.public_hex)})
    client.post("/v1/attestations", json={
        "record": make_attestation(attestor, other.public_hex,
                                   claim_type="domain",
                                   claim_value="studio.example")})
    mine = client.get(f"/v1/attestations/{root.public_hex}").json()["attestations"]
    theirs = client.get(f"/v1/attestations/{other.public_hex}").json()["attestations"]
    assert [e["claim_type"] for e in mine] == ["display_name"]
    assert [e["claim_type"] for e in theirs] == ["domain"]
    assert client.get(f"/v1/attestations/{'zz' * 32}").status_code == 400
