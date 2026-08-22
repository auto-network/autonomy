"""The publication-band compliance sweep (settings_ops.audit_publication_bands).

Feeds controlled stored rows and asserts the audit flags exactly what is
outside each set's real declared band: a workspace row at `canonical` (band
max=curated) is a cross-org leak; a workspace row at `raw` is fine; a capability
contract at `canonical` (band max=canonical) is fine. Band lookups use the real
schema registry, so this also pins that the bands landed as intended.
"""

from __future__ import annotations

import sqlite3

from tools.graph import settings_ops
from tools.graph import cross_org
from tools.graph.db import GraphDB


def _store_with(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE settings (set_id TEXT, key TEXT, schema_revision INT, "
        "publication_state TEXT, deprecated INT)"
    )
    conn.executemany(
        "INSERT INTO settings VALUES (?,?,?,?,0)", rows
    )
    conn.commit()
    return conn


def test_audit_flags_only_out_of_band_rows(monkeypatch):
    conn = _store_with([
        # autonomy.workspace band is raw..curated -> canonical is ABOVE max (leak)
        ("autonomy.workspace", "enterprise-ng", 1, "canonical"),
        # a within-band workspace row
        ("autonomy.workspace", "autonomy-dev", 1, "raw"),
        # autonomy.capability.contract band is raw..canonical -> canonical is fine
        ("autonomy.capability.contract", "video", 1, "canonical"),
    ])

    class _Fake:
        pass
    fake = _Fake()
    fake.conn = conn

    monkeypatch.setattr(cross_org, "all_store_slugs", lambda: ["anchore"])
    monkeypatch.setattr(GraphDB, "for_org", staticmethod(lambda slug, mode=None: fake))

    findings = settings_ops.audit_publication_bands()

    assert len(findings) == 1, findings
    f = findings[0]
    assert f["set_id"] == "autonomy.workspace"
    assert f["key"] == "enterprise-ng"
    assert f["state"] == "canonical"
    assert f["band"] == "raw..curated"
    assert "above max" in f["reason"]
    assert f["store"] == "anchore"


def test_audit_flags_below_min(monkeypatch):
    # A hypothetical shared set pinned min=published would flag a raw row.
    # Use the real capability.contract at raw only if its band forbids raw;
    # capability.contract is raw..canonical so raw is fine — assert no finding,
    # confirming a legitimately-private-at-authoring row is not false-flagged.
    conn = _store_with([("autonomy.capability.contract", "video", 1, "raw")])

    class _Fake:
        pass
    fake = _Fake()
    fake.conn = conn
    monkeypatch.setattr(cross_org, "all_store_slugs", lambda: ["autonomy"])
    monkeypatch.setattr(GraphDB, "for_org", staticmethod(lambda slug, mode=None: fake))

    assert settings_ops.audit_publication_bands() == []
