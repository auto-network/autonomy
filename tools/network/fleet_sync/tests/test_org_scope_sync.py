"""Organization databases synchronize across the personal fleet."""

import sqlite3
import time
from pathlib import Path

import pytest

from tools.network.fleet_sync.harness import HarnessFleet
from tools.network.fleet_sync_scheduler import (
    FleetSyncProtocolError,
    decode_pull_request,
    encode_pull_request,
)


def test_scope_field_roundtrip_and_personal_bytes_unchanged() -> None:
    """The request carries no checkpoint concept: sync checkpoints are gone.

    ``accept_checkpoint`` used to ride here so a founded origin could refuse a
    checkpoint install. There is no checkpoint to refuse, so the field and its
    tuple slot are removed rather than left as a permanently-true vestige.
    """
    epoch = "ab" * 32
    compat = "cd" * 32
    personal = encode_pull_request(epoch, compat=compat)
    assert b"scope" not in personal  # request shape shared with v3 fleets
    assert b"accept_checkpoint" not in personal
    assert decode_pull_request(personal) == (
        epoch, (), compat, "personal", False, 4, None
    )
    scoped = encode_pull_request(epoch, compat=compat, scope="alpha")
    assert decode_pull_request(scoped) == (
        epoch, (), compat, "alpha", False, 4, None
    )
    boot = encode_pull_request(epoch, compat=compat, bootstrap=True)
    assert decode_pull_request(boot) == (
        epoch, (), compat, "personal", True, 4, None
    )
    marked = encode_pull_request(epoch, compat=compat, watermarks={"ab" * 32: 5})
    assert decode_pull_request(marked)[6] == {"ab" * 32: 5}
    legacy = encode_pull_request(epoch, compat=compat, version=3)
    assert decode_pull_request(legacy) == (
        epoch, (), compat, "personal", False, 3, None
    )
    with pytest.raises(FleetSyncProtocolError):
        encode_pull_request(epoch, compat=compat, scope="bad:scope")
    with pytest.raises(FleetSyncProtocolError):
        encode_pull_request(epoch, compat=compat, version=2)


def test_org_databases_sync_with_isolation(tmp_path: Path) -> None:
    fleet = HarnessFleet(
        tmp_path / "fleet", size=2, org_scopes=("alpha", "beta")
    ).build()
    try:
        fleet.start_all()
        fleet.write(0, "p-note", "personal crossing")
        fleet.write_org(0, "alpha", "a-note", "alpha crossing")
        fleet.write_org(1, "beta", "b-note", "beta crossing")

        fleet.wait(
            lambda: fleet.has_org(1, "alpha", "a-note"),
            timeout=120.0, label="alpha crossing",
        )
        fleet.wait(
            lambda: fleet.has_org(0, "beta", "b-note"),
            timeout=120.0, label="beta crossing",
        )
        fleet.wait(
            lambda: fleet.has(1, "p-note"),
            timeout=120.0, label="personal crossing",
        )

        # Isolation: rows never leak across scopes.
        assert not fleet.has(1, "a-note")
        assert not fleet.has_org(1, "beta", "a-note")
        assert not fleet.has_org(0, "alpha", "b-note")
        assert not fleet.has(0, "b-note")
    finally:
        fleet.shutdown()


def test_schema_mismatch_pauses_only_that_org(tmp_path: Path) -> None:
    def diverge(index: int, slug: str, path: Path) -> None:
        if index == 1 and slug == "alpha":
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "ALTER TABLE sources ADD COLUMN harness_extra TEXT"
                )

    fleet = HarnessFleet(
        tmp_path / "fleet", size=2, org_scopes=("alpha", "beta"),
        org_customize=diverge,
    ).build()
    try:
        fleet.start_all()
        fleet.write(0, "p-1", "personal still flows")
        fleet.write_org(0, "alpha", "a-1", "must pause")
        fleet.write_org(0, "beta", "b-1", "must flow")

        fleet.wait(
            lambda: fleet.has(1, "p-1") and fleet.has_org(1, "beta", "b-1"),
            timeout=120.0, label="unaffected scopes",
        )
        # The mismatched scope stays paused: give it ample opportunity.
        time.sleep(1.0)
        assert not fleet.has_org(1, "alpha", "a-1")
    finally:
        fleet.shutdown()


def test_a_legacy_peer_sending_accept_checkpoint_is_still_admitted() -> None:
    """An un-updated peer puts accept_checkpoint on the wire; we must admit it.

    The old encoder sends the field ONLY when False, so every peer holding a
    founded ledger sends it. The request decoder's allow-list is strict, so
    removing the key from that list refused those requests before admission
    and took fleet sync down with every peer that had not updated (live
    2026-09-09T17:31Z, four scopes, 20 minutes; the one scope that kept
    working was the one where the field is omitted).

    Deleting the field from the ENCODER was safe. Deleting it from the DECODER
    is a wire break. It is accepted, shape-checked, and ignored.
    """
    import json

    from tools.network.fleet_sync_scheduler import canonical_json

    epoch, compat = "ab" * 32, "cd" * 32
    body = json.loads(encode_pull_request(epoch, compat=compat, version=3))
    body["accept_checkpoint"] = False          # exactly what an old peer sends
    decoded = decode_pull_request(canonical_json(body))
    assert decoded[0] == epoch and decoded[5] == 3
    assert len(decoded) == 7, "the legacy key must not re-enter the tuple"

    # True is equally legal on the wire.
    body["accept_checkpoint"] = True
    assert decode_pull_request(canonical_json(body))[5] == 3

    # Shape is still enforced, and unknown fields are still refused.
    body["accept_checkpoint"] = "no"
    with pytest.raises(FleetSyncProtocolError):
        decode_pull_request(canonical_json(body))
    body.pop("accept_checkpoint")
    body["invented_field"] = 1
    with pytest.raises(FleetSyncProtocolError):
        decode_pull_request(canonical_json(body))

    # And we never emit it ourselves.
    assert b"accept_checkpoint" not in encode_pull_request(epoch, compat=compat)


#: Optional request fields that peers in the field still SEND, and which this
#: decoder must therefore keep accepting even after we stop emitting them.
#: Removing an entry here is a WIRE BREAK for every peer that has not updated.
RETIRED_BUT_STILL_ACCEPTED = frozenset({"accept_checkpoint"})


def test_retired_request_fields_stay_accepted_by_the_decoder() -> None:
    """A strict allow-list makes every removed optional field a breaking change.

    ``decode_pull_request`` refuses any request whose key set is not a subset
    of the allow-list. So a field can be dropped from the ENCODER freely -- it
    only governs what we send -- but dropping it from the DECODER refuses every
    peer that still sends it. Those are two changes, and only the send half is
    ever safe to make unilaterally: the receive half has to wait until no peer
    emits the field, which means a fleet-wide version in between.

    Doing both at once took fleet sync down for 23 minutes across four scopes
    (2026-09-09T17:31Z). This guard fails if someone repeats it.
    """
    from tools.network.fleet_sync_scheduler import _REQUEST_OPTIONAL_FIELDS

    missing = RETIRED_BUT_STILL_ACCEPTED - set(_REQUEST_OPTIONAL_FIELDS)
    assert not missing, (
        f"removed from the decoder allow-list: {sorted(missing)}. Peers still "
        "send these; refusing them breaks sync with every un-updated machine. "
        "Retire the ENCODER first, wait for the fleet, then the decoder."
    )
