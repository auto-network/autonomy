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
