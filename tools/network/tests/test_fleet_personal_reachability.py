"""Personal-fleet reachability descriptor: schema, writer, verifier (auto-iipt7).

The descriptor is a HINT, and the whole patch rests on one property: it can
never admit. The roster decides membership; a descriptor only says where a
member listens. So the first test here is the falsifying one — a perfectly
valid, correctly signed descriptor from a machine outside the roster must be
ignored. A test that only round-trips a valid row would pass against an
implementation that never consults the roster at all.

The rest pin the things a plausible implementation gets wrong: diffing addresses
while ignoring a changed serving slot, sorting an address list whose order IS
the sender's priority, and repairing untrusted input into a shape that verifies.
"""

from __future__ import annotations

import pytest

from tools.graph.schemas.personal_fleet_reachability import (
    MAX_ADDRESSES,
    MAX_ADDRESS_BYTES,
    MAX_ROW_BYTES,
    PersonalFleetReachabilityV1,
    row_bytes,
)
from tools.graph.schemas.registry import SchemaValidationError
from tools.network import fleet_personal_reachability as pr
from tools.network.idkit import KeyPair

ORG_UUID = "2d4b90cb-1e89-452b-82cb-68ca44fd8e52"
ORIGIN = "wss://relay.auto.network"


@pytest.fixture
def machine():
    return KeyPair.generate()


def _relay(serving_pub: str | None = None) -> dict:
    return {
        "relay_base": ORIGIN,
        "org_uuid": ORG_UUID,
        "persona_pub": "aa" * 32,
        "serving_machine_pub": serving_pub or ("bb" * 32),
    }


def test_a_valid_row_from_a_machine_outside_the_roster_is_ignored(machine):
    """THE ONE THAT MATTERS. Correct signature, correct shape, correct row key —
    and the signer is not an active roster machine. Reachability must never
    admit; only the roster does. An implementation that skips the roster check
    passes every other test in this file."""
    row = pr.build_row(machine, ["wss://a.example/s"])

    assert pr.verify_row(row, machine.public_hex, active_machine_pubs=[]) is None
    assert pr.verify_row(
        row, machine.public_hex, active_machine_pubs=[machine.public_hex]) is not None


def test_a_row_stored_under_another_machines_key_is_rejected(machine):
    """Row key and signing key must be the same durable machine. Otherwise a
    roster member could publish a DIFFERENT member's location under their key
    and redirect traffic aimed at them."""
    other = KeyPair.generate()
    row = pr.build_row(machine, ["wss://a.example/s"])

    assert pr.verify_row(
        row, other.public_hex,
        active_machine_pubs=[machine.public_hex, other.public_hex]) is None


def test_a_relay_only_change_is_a_change(machine):
    """Identical addresses, different serving slot. An implementation diffing
    addresses alone reports no change and the peer keeps dialling a slot that
    moved."""
    a = pr.build_row(machine, ["wss://a.example/s"], _relay("cc" * 32), now=100)
    b = pr.build_row(machine, ["wss://a.example/s"], _relay("dd" * 32), now=100)

    assert a["addresses"] == b["addresses"]
    assert pr.semantic_tuple(a) != pr.semantic_tuple(b)


def test_relay_becoming_absent_is_a_change(machine):
    """Removal of the relay is a route change, not an absence of news."""
    with_relay = pr.build_row(machine, ["wss://a.example/s"], _relay(), now=100)
    without = pr.build_row(machine, ["wss://a.example/s"], None, now=100)

    assert pr.semantic_tuple(with_relay) != pr.semantic_tuple(without)


def test_an_unchanged_tuple_is_not_a_change(machine):
    """Change-driven, never a heartbeat: a later timestamp alone is not news."""
    a = pr.build_row(machine, ["wss://a.example/s"], _relay(), now=100)
    b = pr.build_row(machine, ["wss://a.example/s"], _relay(), now=999)

    assert a["updated_at"] != b["updated_at"]
    assert pr.semantic_tuple(a) == pr.semantic_tuple(b)


def test_address_order_is_preserved_and_never_sorted(machine):
    """Order is signed candidate priority. Sorting would still verify while
    silently discarding the sender's intended rank."""
    ordered = ["wss://z.example/s", "wss://a.example/s", "ws://m.example/s"]
    row = pr.build_row(machine, ordered)

    assert row["addresses"] == ordered
    assert row["addresses"] != sorted(ordered)


def test_writer_dedupes_preserving_first_occurrence(machine):
    row = pr.build_row(machine, [
        "wss://a.example/s", "wss://b.example/s", "wss://a.example/s"])

    assert row["addresses"] == ["wss://a.example/s", "wss://b.example/s"]


def test_a_received_row_with_duplicate_addresses_is_refused_not_repaired(machine):
    """Untrusted input is never normalized before verification. Deduplicating a
    received row and then checking the signature would verify a body the signer
    never produced."""
    row = pr.build_row(machine, ["wss://a.example/s"])
    row["addresses"] = ["wss://a.example/s", "wss://a.example/s"]

    with pytest.raises(SchemaValidationError):
        PersonalFleetReachabilityV1.validate(row)
    assert pr.verify_row(
        row, machine.public_hex, active_machine_pubs=[machine.public_hex]) is None


def test_a_tampered_row_yields_no_descriptor(machine):
    """Any edit after signing invalidates it, and the failure is None rather
    than a repaired value — a caller must never treat None as licence to
    replace what it already verified."""
    row = pr.build_row(machine, ["wss://a.example/s"])
    row["addresses"] = ["wss://attacker.example/s"]

    assert pr.verify_row(
        row, machine.public_hex, active_machine_pubs=[machine.public_hex]) is None


def test_an_unknown_field_is_refused(machine):
    """An unknown field would be covered by the signature on one side and
    dropped on the other, so two honest parties would disagree about what was
    signed."""
    row = pr.build_row(machine, ["wss://a.example/s"])
    row["extra"] = "x"

    with pytest.raises(SchemaValidationError):
        PersonalFleetReachabilityV1.validate(row)


def test_the_largest_valid_descriptor_fits_the_byte_ceiling(machine):
    """Builds the biggest legal row and proves it fits. If this ever fails, a
    bound changes explicitly — nothing is silently truncated."""
    host = "w" * (MAX_ADDRESS_BYTES - len("wss://") - len(".example/s"))
    addresses = [f"wss://{host[:-2]}{i:02d}.example/s" for i in range(MAX_ADDRESSES)]
    for address in addresses:
        assert len(address.encode("utf-8")) <= MAX_ADDRESS_BYTES
    row = pr.build_row(machine, addresses, _relay())

    assert len(row["addresses"]) == MAX_ADDRESSES
    assert row_bytes(row) <= MAX_ROW_BYTES, (
        f"largest valid descriptor is {row_bytes(row)} bytes, over {MAX_ROW_BYTES}")


def test_an_over_bound_address_is_dropped_by_the_writer(machine):
    over = "wss://" + ("x" * MAX_ADDRESS_BYTES) + ".example/s"
    row = pr.build_row(machine, [over, "wss://ok.example/s"])

    assert row["addresses"] == ["wss://ok.example/s"]


def test_removal_is_a_live_row_not_a_delete(machine):
    """A machine that stopped listening states that fact verifiably, rather
    than vanishing and leaving peers to guess from an absence."""
    row = pr.build_row(machine, [], None)

    assert row["addresses"] == [] and row["relay"] is None
    assert pr.verify_row(
        row, machine.public_hex, active_machine_pubs=[machine.public_hex]) is not None


def test_publication_without_signing_authority_is_unavailable(machine):
    """Absent machine key or reachability cert reports unavailable and
    preserves the last valid row. It never signs with a process delegate."""
    with pytest.raises(pr.ReachabilityUnavailable):
        pr.publish_if_changed(None, object(), ["wss://a.example/s"])
    with pytest.raises(pr.ReachabilityUnavailable):
        pr.publish_if_changed(machine, None, ["wss://a.example/s"])


def test_a_relay_naming_an_unconfigured_origin_is_rejected(machine):
    """The locator is routing only, and it may not name a relay this node never
    agreed to use."""
    row = pr.build_row(machine, [], _relay())

    assert pr.verify_row(
        row, machine.public_hex, active_machine_pubs=[machine.public_hex],
        configured_relay_origin="wss://other.relay.invalid") is None
    assert pr.verify_row(
        row, machine.public_hex, active_machine_pubs=[machine.public_hex],
        configured_relay_origin=ORIGIN) is not None


def test_a_relay_org_uuid_must_match_the_local_binding(machine):
    row = pr.build_row(machine, [], _relay())

    assert pr.verify_row(
        row, machine.public_hex, active_machine_pubs=[machine.public_hex],
        configured_org_uuid="11111111-1111-1111-1111-111111111111") is None


@pytest.mark.parametrize("bad", [
    "wss://user:pw@relay.auto.network",
    "wss://relay.auto.network/path",
    "wss://relay.auto.network/?q=1",
    "wss://relay.auto.network/#f",
    "http://relay.auto.network",
])
def test_a_relay_base_that_is_not_a_bare_origin_is_refused(bad):
    """Refused rather than stripped: stripping would silently accept a row
    whose signer meant something else."""
    with pytest.raises(SchemaValidationError):
        pr.canonical_relay_origin(bad)


# -- storage-backed writer behaviour -------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A real settings store, so publish_if_changed is exercised end to end."""
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.setenv("GRAPH_ORG", "personal")
    (tmp_path / "orgs").mkdir(parents=True, exist_ok=True)
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()
    try:
        yield tmp_path
    finally:
        GraphDB.close_all_pooled()


def test_repeated_identical_publication_is_a_no_op(store, machine):
    """Change-driven, not a heartbeat: the second call must write nothing, so an
    unchanged fleet produces no replication traffic at all."""
    cert = object()

    assert pr.publish_if_changed(machine, cert, ["wss://a.example/s"]) is True
    assert pr.publish_if_changed(machine, cert, ["wss://a.example/s"]) is False


def test_a_relay_only_change_does_publish(store, machine):
    """The end-to-end form of the semantic-diff test: same addresses, moved
    serving slot, must reach the store."""
    cert = object()
    pr.publish_if_changed(machine, cert, ["wss://a.example/s"], _relay("cc" * 32))

    assert pr.publish_if_changed(
        machine, cert, ["wss://a.example/s"], _relay("dd" * 32)) is True


def test_a_corrupt_stored_row_causes_a_republish_not_a_suppressed_write(store, machine):
    """The comparison baseline is the last VERIFIED row. A tampered stored row
    must never suppress a legitimate publication."""
    cert = object()
    pr.publish_if_changed(machine, cert, ["wss://a.example/s"])
    corrupt = dict(pr.stored_row(machine.public_hex))
    corrupt["sig"] = "0" * 128
    from tools.graph import settings_ops
    from tools.graph.schemas.personal_fleet_reachability import (
        PERSONAL_FLEET_REACHABILITY_REVISION as REV,
        PERSONAL_FLEET_REACHABILITY_SET_ID as SET_ID,
    )
    settings_ops.upsert_by_key(
        SET_ID, REV, machine.public_hex, corrupt, org="personal")

    assert pr.publish_if_changed(machine, cert, ["wss://a.example/s"]) is True


def test_a_row_one_byte_over_the_ceiling_is_refused(machine):
    """The ceiling is enforced on the final canonical bytes, not estimated."""
    row = pr.build_row(machine, ["wss://a.example/s"])
    room = MAX_ROW_BYTES - row_bytes(row)
    row["addresses"] = ["wss://" + ("x" * (room + 8)) + ".example/s"]

    with pytest.raises(SchemaValidationError):
        PersonalFleetReachabilityV1.validate(row)


@pytest.mark.parametrize("field,value", [
    ("machine_pub", "AA" * 32),
    ("machine_pub", "aa" * 31),
    ("sig", "ff" * 63),
])
def test_noncanonical_hex_is_refused(machine, field, value):
    """Uppercase or wrong-length hex is refused rather than normalized, so two
    parties cannot disagree about the signed bytes."""
    row = pr.build_row(machine, ["wss://a.example/s"])
    row[field] = value

    with pytest.raises(SchemaValidationError):
        PersonalFleetReachabilityV1.validate(row)


@pytest.mark.parametrize("bad_uuid", [
    "2D4B90CB-1E89-452B-82CB-68CA44FD8E52", "not-a-uuid", "2d4b90cb1e89452b82cb68ca44fd8e52",
])
def test_a_noncanonical_relay_uuid_is_refused(machine, bad_uuid):
    row = pr.build_row(machine, [], _relay())
    row["relay"]["org_uuid"] = bad_uuid

    with pytest.raises(SchemaValidationError):
        PersonalFleetReachabilityV1.validate(row)
