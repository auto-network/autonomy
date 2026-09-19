"""Idle cuts: an origin's watermark advances without writes (auto-mmwgu).

A MACHINE CUT is a signed promise by one origin machine that no future write
of that origin will carry a timestamp at or below ``cut_ns``. It is sealed
at the end of every scheduler round under BEGIN IMMEDIATE, so no local
write is mid-flight, by raising ``fleet_sync_state.write_floor`` to
max(last write, now). The existing write gate refuses later writes at or
below the floor, which is what makes the promise true.

A PERSONA CUT is sealed by one machine of a persona's fleet once it holds a
position (cursor or verified cut) for every machine in the persona's
root-signed roster: F is the minimum of those positions. It is signed by
the sealing machine's key and carries the DelegationCert the persona issued
to that machine (scope fleet:sync), the same chain the org hello proves
membership with; a machine never holds the persona's private key.

Both travel as control frames on the pull reply (origin.cut, persona.cut),
adopted after verification and forwarded unchanged by any holder. A holder
emits an origin's cut only after it has served every transaction of that
origin it holds, and a receiver records a cut only after committing every
group received before the frame, so every holder of a cut holds every
transaction at or below it and adopting it as the watermark skips nothing
(graph://d9153c5a-76e, comment e71077e6-306).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Mapping

from tools.network.idkit import DelegationCert, IdkitError, canonical_json
from tools.network.idkit.keys import KeyPair, verify_signature
from tools.network.idkit.verify import verify_chain

ORIGIN_CUT_KIND = "origin.cut"
PERSONA_CUT_KIND = "persona.cut"
#: The DelegationCert scope a persona issues to its machines for fleet sync;
#: the same value fleet_org_channel.ORG_SYNC_SCOPE names.
PERSONA_CUT_SCOPE = "fleet:sync"


class CutError(ValueError):
    """A cut record that does not verify, or a seal that cannot be made."""


def ensure_cut_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fleet_sync_origin_cuts("
        "origin_id INTEGER PRIMARY KEY,"
        "cut_ns INTEGER NOT NULL,"
        "sig TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fleet_sync_persona_cuts("
        "persona TEXT PRIMARY KEY,"
        "org TEXT NOT NULL,"
        "cut_ns INTEGER NOT NULL,"
        "record TEXT NOT NULL)"
    )


# ── machine cut ─────────────────────────────────────────────────────────────

def origin_cut_body(origin: str, cut_ns: int) -> bytes:
    return canonical_json({"kind": ORIGIN_CUT_KIND, "origin": origin, "cut_ns": int(cut_ns)})


def seal_machine_cut(
    conn: sqlite3.Connection, signer: KeyPair, origin: str, now_ns: int,
) -> int | None:
    """Raise this store's write floor to max(last write, now) and record the
    signed cut for *origin* (this machine). Returns the cut, or None when the
    clock is behind the floor: cuts pause and the gate keeps refusing writes
    until the clock passes the floor, so a cut never regresses and never
    exceeds the machine clock. Own BEGIN IMMEDIATE transaction."""
    if conn.in_transaction:
        raise CutError("cannot seal a cut inside another transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        ensure_cut_schema(conn)
        floor, last = conn.execute(
            "SELECT write_floor,last_timestamp FROM fleet_sync_state WHERE singleton=1"
        ).fetchone()
        cut = max(int(last), int(now_ns))
        if cut < int(floor) or int(now_ns) < int(floor):
            conn.execute("ROLLBACK")
            return None
        conn.execute(
            "UPDATE fleet_sync_state SET write_floor=? WHERE singleton=1", (cut,)
        )
        conn.execute("INSERT OR IGNORE INTO fleet_sync_origins(incarnation) VALUES(?)", (origin,))
        origin_id = int(conn.execute(
            "SELECT id FROM fleet_sync_origins WHERE incarnation=?", (origin,)
        ).fetchone()[0])
        sig = signer.sign_hex(origin_cut_body(origin, cut))
        conn.execute(
            "INSERT INTO fleet_sync_origin_cuts(origin_id,cut_ns,sig) VALUES(?,?,?) "
            "ON CONFLICT(origin_id) DO UPDATE SET cut_ns=excluded.cut_ns, sig=excluded.sig "
            "WHERE excluded.cut_ns>fleet_sync_origin_cuts.cut_ns",
            (origin_id, cut, sig),
        )
        conn.execute("COMMIT")
        return cut
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def verify_origin_cut(record: Mapping) -> tuple[str, int, str]:
    """-> (origin, cut_ns, sig) or CutError. The signing key IS the origin:
    an origin incarnation is the authoring machine's public key."""
    try:
        origin = str(record["origin"])
        cut_ns = record["cut_ns"]
        sig = str(record["sig"])
    except (KeyError, TypeError) as exc:
        raise CutError(f"origin.cut is malformed: {exc}") from exc
    if len(origin) != 64 or any(c not in "0123456789abcdef" for c in origin):
        raise CutError("origin.cut origin is malformed")
    if not isinstance(cut_ns, int) or isinstance(cut_ns, bool) or cut_ns < 0:
        raise CutError("origin.cut cut_ns is malformed")
    try:
        verify_signature(origin, sig, origin_cut_body(origin, cut_ns))
    except (IdkitError, ValueError) as exc:
        raise CutError(f"origin.cut signature does not verify: {exc}") from exc
    return origin, int(cut_ns), sig


def adopt_origin_cut(conn: sqlite3.Connection, record: Mapping) -> bool:
    """Verify and store an origin's cut if newer than the one held. Returns
    True when the stored cut moved. Own transaction."""
    origin, cut_ns, sig = verify_origin_cut(record)
    if conn.in_transaction:
        raise CutError("cannot adopt a cut inside another transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        ensure_cut_schema(conn)
        conn.execute("INSERT OR IGNORE INTO fleet_sync_origins(incarnation) VALUES(?)", (origin,))
        origin_id = int(conn.execute(
            "SELECT id FROM fleet_sync_origins WHERE incarnation=?", (origin,)
        ).fetchone()[0])
        before = conn.execute(
            "SELECT cut_ns FROM fleet_sync_origin_cuts WHERE origin_id=?", (origin_id,)
        ).fetchone()
        moved = before is None or int(before[0]) < cut_ns
        if moved:
            conn.execute(
                "INSERT INTO fleet_sync_origin_cuts(origin_id,cut_ns,sig) VALUES(?,?,?) "
                "ON CONFLICT(origin_id) DO UPDATE SET cut_ns=excluded.cut_ns, sig=excluded.sig",
                (origin_id, cut_ns, sig),
            )
        conn.execute("COMMIT")
        return moved
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def origin_cuts(conn: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    """``{origin: (cut_ns, sig)}`` for every verified cut this store holds."""
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fleet_sync_origin_cuts'"
    ).fetchone() is not None
    if not present:
        return {}
    return {
        str(row[0]): (int(row[1]), str(row[2]))
        for row in conn.execute(
            "SELECT o.incarnation, c.cut_ns, c.sig FROM fleet_sync_origin_cuts c "
            "JOIN fleet_sync_origins o ON o.id=c.origin_id"
        )
    }


def origin_cut_frames(conn: sqlite3.Connection, version: int, watermarks: Mapping[str, int]) -> list[bytes]:
    """The origin.cut control frames to send a puller that advertised
    *watermarks*: every held cut above the puller's watermark for its
    origin. Emitted after the transaction stream, never before."""
    frames = []
    for origin, (cut_ns, sig) in sorted(origin_cuts(conn).items()):
        if cut_ns > int(watermarks.get(origin, -1)):
            frames.append(canonical_json({
                "v": version, "kind": ORIGIN_CUT_KIND,
                "origin": origin, "cut_ns": cut_ns, "sig": sig,
            }))
    return frames


# ── persona cut ─────────────────────────────────────────────────────────────

def persona_cut_body(persona: str, org: str, cut_ns: int, machines: Mapping[str, int],
                     signer: str) -> bytes:
    return canonical_json({
        "kind": PERSONA_CUT_KIND, "persona": persona, "org": org,
        "cut_ns": int(cut_ns), "machines": {k: int(v) for k, v in sorted(machines.items())},
        "signer": signer,
    })


def seal_persona_cut(
    conn: sqlite3.Connection, *, signer: KeyPair, persona_cert: DelegationCert,
    org: str, roster_machines: set[str], positions: Mapping[str, int],
) -> dict | None:
    """Seal {persona, org, cut_ns, machines, signer, cert, sig} when every
    machine of the persona's roster has a known position; F is their
    minimum. Returns the record when a NEWER persona cut was stored, else
    None: an unconverged machine (no position) blocks the seal, and a
    minimum no higher than the held cut seals nothing."""
    if not roster_machines or any(m not in positions for m in roster_machines):
        return None
    machines = {m: int(positions[m]) for m in sorted(roster_machines)}
    cut_ns = min(machines.values())
    persona = str(persona_cert.subject.id)
    record = {
        "persona": persona, "org": org, "cut_ns": cut_ns, "machines": machines,
        "signer": signer.public_hex, "cert": persona_cert.to_dict(),
    }
    record["sig"] = signer.sign_hex(persona_cut_body(persona, org, cut_ns, machines, signer.public_hex))
    return record if store_persona_cut(conn, record) else None


def verify_persona_cut(record: Mapping, *, org: str, now: int) -> tuple[str, int]:
    """-> (persona, cut_ns) or CutError. The cert chain anchors at the
    persona the record names, must be for *org* and scope fleet:sync, and
    must name the signer as its leaf; the signature is the signer's."""
    try:
        persona = str(record["persona"])
        cut_ns = record["cut_ns"]
        machines = record["machines"]
        signer = str(record["signer"])
        sig = str(record["sig"])
        cert = DelegationCert.from_dict(record["cert"])
    except (KeyError, TypeError, ValueError, IdkitError) as exc:
        raise CutError(f"persona.cut is malformed: {exc}") from exc
    if str(record.get("org")) != org:
        raise CutError("persona.cut is for another organization")
    if not isinstance(cut_ns, int) or isinstance(cut_ns, bool) or cut_ns < 0:
        raise CutError("persona.cut cut_ns is malformed")
    if (
        not isinstance(machines, dict) or not machines
        or any(not isinstance(v, int) or isinstance(v, bool) or v < cut_ns for v in machines.values())
    ):
        raise CutError("persona.cut machines must be positions at or above cut_ns")
    if cert.org != org or cert.subject.kind != "persona" or str(cert.subject.id) != persona:
        raise CutError("persona.cut cert does not name this persona for this organization")
    try:
        verified = verify_chain(cert, persona, org=org, now=now, required_scope=PERSONA_CUT_SCOPE)
    except IdkitError as exc:
        raise CutError(f"persona.cut cert chain failed: {exc}") from exc
    if verified.leaf_pub != signer:
        raise CutError("persona.cut cert names another machine than the signer")
    try:
        verify_signature(signer, sig, persona_cut_body(persona, org, cut_ns, machines, signer))
    except (IdkitError, ValueError) as exc:
        raise CutError(f"persona.cut signature does not verify: {exc}") from exc
    return persona, int(cut_ns)


def store_persona_cut(conn: sqlite3.Connection, record: Mapping) -> bool:
    """Store a persona cut the caller has verified (or just sealed) if newer
    than the one held. Own transaction."""
    persona, org, cut_ns = str(record["persona"]), str(record["org"]), int(record["cut_ns"])
    if conn.in_transaction:
        raise CutError("cannot store a persona cut inside another transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        ensure_cut_schema(conn)
        before = conn.execute(
            "SELECT cut_ns FROM fleet_sync_persona_cuts WHERE persona=?", (persona,)
        ).fetchone()
        moved = before is None or int(before[0]) < cut_ns
        if moved:
            conn.execute(
                "INSERT INTO fleet_sync_persona_cuts(persona,org,cut_ns,record) VALUES(?,?,?,?) "
                "ON CONFLICT(persona) DO UPDATE SET org=excluded.org, cut_ns=excluded.cut_ns, "
                "record=excluded.record",
                (persona, org, cut_ns, json.dumps(dict(record), sort_keys=True)),
            )
        conn.execute("COMMIT")
        return moved
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def adopt_persona_cut(conn: sqlite3.Connection, record: Mapping, *, org: str, now: int) -> bool:
    verify_persona_cut(record, org=org, now=now)
    return store_persona_cut(conn, record)


def persona_cuts(conn: sqlite3.Connection) -> dict[str, dict]:
    """``{persona: record}`` for every persona cut this store holds."""
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fleet_sync_persona_cuts'"
    ).fetchone() is not None
    if not present:
        return {}
    return {
        str(row[0]): json.loads(row[1])
        for row in conn.execute("SELECT persona, record FROM fleet_sync_persona_cuts")
    }


def persona_frontiers(conn: sqlite3.Connection) -> dict[str, int]:
    """``{persona: cut_ns}``: what an org member advertises per persona."""
    return {persona: int(record["cut_ns"]) for persona, record in persona_cuts(conn).items()}


def persona_cut_frames(conn: sqlite3.Connection, version: int, known: Mapping[str, int]) -> list[bytes]:
    """persona.cut frames for every held persona cut newer than what the
    puller said it knows."""
    frames = []
    for persona, record in sorted(persona_cuts(conn).items()):
        if int(record["cut_ns"]) > int(known.get(persona, -1)):
            frames.append(canonical_json({"v": version, "kind": PERSONA_CUT_KIND, **record}))
    return frames
