"""Write floors: an origin's watermark advances without writes (auto-mmwgu).

A MACHINE WRITE FLOOR is a signed promise by one origin machine that no future write
of that origin will carry a timestamp at or below ``write_floor_ns``. It is sealed
at the end of every scheduler round under BEGIN IMMEDIATE, so no local
write is mid-flight, by raising ``fleet_sync_state.write_floor`` to
max(last write, now). The existing write gate refuses later writes at or
below the floor, which is what makes the promise true.

A PERSONA WRITE FLOOR is sealed by one machine of a persona's fleet once its
cursor for every machine in the persona's root-signed roster is known: F is
the minimum of those cursors. It is signed by
the sealing machine's key and carries the DelegationCert the persona issued
to that machine (scope fleet:sync), the same chain the org hello proves
membership with; a machine never holds the persona's private key.

Both travel as control frames on the pull reply (machine.write_floor,
persona.write_floor), after every transaction of the stream. A server's reply
is one snapshot bounded by its own cursor per origin, read once before the
first page: it serves an origin's rows only at or below that cursor and sends
the origin's write floor only when that cursor has reached it. What a server
cannot claim itself, it does not pass on. A receiver stores every verified
floor, and moves its cursor to it (catalog.claim_write_floor) only once every
transaction of that origin at or below the floor is resolved here; zero
transactions resolve, so an idle machine's floor moves the cursor. The pull
request's watermark is the cursor alone. Checked in
tools/network/TLA/FleetSyncWriteFloors.tla (graph://d9153c5a-76e O-K, the
operator's correction of 2026-09-20).
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Mapping

from tools.network.idkit import DelegationCert, IdkitError, canonical_json
from tools.network.idkit.keys import KeyPair, verify_signature
from tools.network.idkit.verify import verify_chain

MACHINE_WRITE_FLOOR_KIND = "machine.write_floor"
PERSONA_WRITE_FLOOR_KIND = "persona.write_floor"
#: The DelegationCert scope a persona issues to its machines for fleet sync;
#: the same value fleet_org_channel.ORG_SYNC_SCOPE names.
PERSONA_WRITE_FLOOR_SCOPE = "fleet:sync"


class WriteFloorError(ValueError):
    """A write floor record that does not verify, or a seal that cannot be made."""


#: The delegation a machine key issues to the process that runs its fleet
#: sync (fleet_sync_channel: machine-direct, this one scope, no target
#: types). A write floor signed by that process carries the certificate so a
#: receiver can walk from the origin, which IS the machine key, to the
#: signer. The hello's TTL bound is not re-checked here: a write floor is a fact
#: about the past, and the chain's own validity window still applies.
PROCESS_DELEGATION_SCOPE = ("fleet:sync",)


def ensure_write_floor_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fleet_sync_machine_write_floors("
        "origin_id INTEGER PRIMARY KEY,"
        "write_floor_ns INTEGER NOT NULL,"
        "sig TEXT NOT NULL,"
        "signer TEXT,"
        "cert TEXT,"
        "sealed_ref INTEGER NOT NULL DEFAULT 0)"
    )
    columns = {str(r[1]) for r in conn.execute("PRAGMA table_info(fleet_sync_machine_write_floors)")}
    for name, kind in (("signer", "TEXT"), ("cert", "TEXT"), ("sealed_ref", "INTEGER NOT NULL DEFAULT 0")):
        if name not in columns:
            conn.execute(f"ALTER TABLE fleet_sync_machine_write_floors ADD COLUMN {name} {kind}")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fleet_sync_persona_write_floors("
        "persona TEXT PRIMARY KEY,"
        "org TEXT NOT NULL,"
        "write_floor_ns INTEGER NOT NULL,"
        "record TEXT NOT NULL)"
    )


# ── machine write floor ─────────────────────────────────────────────────────────────

def machine_write_floor_body(origin: str, write_floor_ns: int, signer: str) -> bytes:
    return canonical_json({
        "kind": MACHINE_WRITE_FLOOR_KIND, "origin": origin, "write_floor_ns": int(write_floor_ns), "signer": signer,
    })


def seal_machine_write_floor(
    conn: sqlite3.Connection, signer: KeyPair, origin: str, now_ns: int,
    *, cert: DelegationCert | None = None,
) -> int | None:
    """Raise this store's write floor to max(last write, now) and record the
    signed write floor for *origin* (this machine). Returns the write floor, or None when the
    clock is behind the floor: write floors pause and the gate keeps refusing writes
    until the clock passes the floor, so a write floor never regresses and never
    exceeds the machine clock. *signer* is the origin's own machine key, or
    a process key the machine key delegated to, in which case *cert* is
    that delegation and travels with the write floor. Own BEGIN IMMEDIATE
    transaction."""
    if signer.public_hex != origin and cert is None:
        raise WriteFloorError("a write floor signed by a delegate must carry the delegation")
    if conn.in_transaction:
        raise WriteFloorError("cannot seal a write floor inside another transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        ensure_write_floor_schema(conn)
        floor, last = conn.execute(
            "SELECT write_floor,last_timestamp FROM fleet_sync_state WHERE singleton=1"
        ).fetchone()
        sealed_ns = max(int(last), int(now_ns))
        if sealed_ns < int(floor) or int(now_ns) < int(floor):
            conn.execute("ROLLBACK")
            return None
        conn.execute(
            "UPDATE fleet_sync_state SET write_floor=? WHERE singleton=1", (sealed_ns,)
        )
        conn.execute("INSERT OR IGNORE INTO fleet_sync_origins(incarnation) VALUES(?)", (origin,))
        origin_id = int(conn.execute(
            "SELECT id FROM fleet_sync_origins WHERE incarnation=?", (origin,)
        ).fetchone()[0])
        # A floor is an acknowledgement: it is sealed only after this machine
        # wrote a row or applied one from a peer since its last floor. Every
        # such row is a fleet_sync_transactions row with a higher id; a
        # received floor is not. A settled fleet therefore seals nothing.
        newest = int(conn.execute("SELECT COALESCE(MAX(id),0) FROM fleet_sync_transactions").fetchone()[0])
        held = conn.execute("SELECT write_floor_ns, sealed_ref FROM fleet_sync_machine_write_floors WHERE origin_id=?", (origin_id,)).fetchone()
        if held is not None and int(held[1]) >= newest:
            conn.execute("ROLLBACK")
            return int(held[0])   # nothing written, nothing applied: the held floor stands
        sig = signer.sign_hex(machine_write_floor_body(origin, sealed_ns, signer.public_hex))
        cert_json = json.dumps(cert.to_dict(), sort_keys=True) if cert is not None else None
        conn.execute(
            "INSERT INTO fleet_sync_machine_write_floors(origin_id,write_floor_ns,sig,signer,cert,sealed_ref) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(origin_id) DO UPDATE SET write_floor_ns=excluded.write_floor_ns, sig=excluded.sig, "
            "signer=excluded.signer, cert=excluded.cert, sealed_ref=excluded.sealed_ref "
            "WHERE excluded.write_floor_ns>fleet_sync_machine_write_floors.write_floor_ns",
            (origin_id, sealed_ns, sig, signer.public_hex, cert_json, newest),
        )
        # This machine holds every row it wrote, so its own cursor is its
        # floor: the watermark it advertises for itself, and the position
        # a persona write floor takes for it.
        from tools.network.fleet_sync.catalog import ensure_origin_cursor_schema
        ensure_origin_cursor_schema(conn)
        conn.execute(
            "INSERT INTO fleet_sync_origin_cursor(origin_id,timestamp_ns,transaction_id) "
            "VALUES(?,?,'') ON CONFLICT(origin_id) DO UPDATE SET "
            "timestamp_ns=excluded.timestamp_ns, transaction_id=excluded.transaction_id "
            "WHERE excluded.timestamp_ns>fleet_sync_origin_cursor.timestamp_ns",
            (origin_id, sealed_ns),
        )
        conn.execute("COMMIT")
        return sealed_ns
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def verify_machine_write_floor(record: Mapping, *, now: int | None = None) -> dict:
    """-> {origin, write_floor_ns, sig, signer, cert} or WriteFloorError.

    An origin incarnation is the authoring machine's public key. The
    signer is that key, or a process key the machine key delegated to
    with a machine-direct fleet:sync certificate (the same delegation the
    sync hello carries), in which case the chain must anchor at the origin
    and name the signer as its leaf."""
    try:
        origin = str(record["origin"])
        write_floor_ns = record["write_floor_ns"]
        sig = str(record["sig"])
        signer = str(record.get("signer") or origin)
        cert_data = record.get("cert")
    except (KeyError, TypeError) as exc:
        raise WriteFloorError(f"machine.write_floor is malformed: {exc}") from exc
    for what, value in (("origin", origin), ("signer", signer)):
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise WriteFloorError(f"machine.write_floor {what} is malformed")
    if not isinstance(write_floor_ns, int) or isinstance(write_floor_ns, bool) or write_floor_ns < 0:
        raise WriteFloorError("machine.write_floor write_floor_ns is malformed")
    if signer != origin:
        if cert_data is None:
            raise WriteFloorError("machine.write_floor signed by a delegate carries no delegation")
        try:
            cert = DelegationCert.from_dict(cert_data)
            verified = verify_chain(
                cert, origin, org=str(cert.org),
                now=int(time.time()) if now is None else now,
                required_scope=PROCESS_DELEGATION_SCOPE[0],
            )
        except (IdkitError, ValueError, TypeError, KeyError) as exc:
            raise WriteFloorError(f"machine.write_floor delegation failed: {exc}") from exc
        if (
            cert.parent_cert is not None
            or tuple(cert.scope) != PROCESS_DELEGATION_SCOPE
            or cert.target_types is not None
            or verified.leaf_pub != signer
        ):
            raise WriteFloorError("machine.write_floor delegation does not name the signer machine-direct")
    try:
        verify_signature(signer, sig, machine_write_floor_body(origin, write_floor_ns, signer))
    except (IdkitError, ValueError) as exc:
        raise WriteFloorError(f"machine.write_floor signature does not verify: {exc}") from exc
    return {"origin": origin, "write_floor_ns": int(write_floor_ns), "sig": sig, "signer": signer,
            "cert": cert_data if signer != origin else None}


def adopt_machine_write_floor(conn: sqlite3.Connection, record: Mapping, *, catalog=None) -> bool:
    """Verify and store an origin's write floor if newer than the one held.
    With *catalog* (the receiving store's MutationCatalog on *conn*), also
    claim it as the cursor in the same transaction where every row of that
    origin at or below it is resolved (catalog.claim_write_floor_locked), so
    no commit lies between holding a floor and claiming it. Returns True
    when the stored write floor moved. Own transaction."""
    verified = verify_machine_write_floor(record)
    origin, write_floor_ns, sig = verified["origin"], verified["write_floor_ns"], verified["sig"]
    if conn.in_transaction:
        raise WriteFloorError("cannot adopt a write floor inside another transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        ensure_write_floor_schema(conn)
        conn.execute("INSERT OR IGNORE INTO fleet_sync_origins(incarnation) VALUES(?)", (origin,))
        origin_id = int(conn.execute(
            "SELECT id FROM fleet_sync_origins WHERE incarnation=?", (origin,)
        ).fetchone()[0])
        before = conn.execute(
            "SELECT write_floor_ns FROM fleet_sync_machine_write_floors WHERE origin_id=?", (origin_id,)
        ).fetchone()
        moved = before is None or int(before[0]) < write_floor_ns
        if moved:
            conn.execute(
                "INSERT INTO fleet_sync_machine_write_floors(origin_id,write_floor_ns,sig,signer,cert) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(origin_id) DO UPDATE SET write_floor_ns=excluded.write_floor_ns, sig=excluded.sig, "
                "signer=excluded.signer, cert=excluded.cert",
                (origin_id, write_floor_ns, sig, verified["signer"],
                 json.dumps(verified["cert"], sort_keys=True) if verified["cert"] else None),
            )
        if catalog is not None:
            catalog.claim_write_floor_locked(origin, write_floor_ns)
        conn.execute("COMMIT")
        return moved
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def machine_write_floors(conn: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    """``{origin: (write_floor_ns, sig)}`` for every verified write floor this store holds."""
    return {origin: (r["write_floor_ns"], r["sig"]) for origin, r in machine_write_floor_records(conn).items()}


def machine_write_floor_records(conn: sqlite3.Connection) -> dict[str, dict]:
    """``{origin: {write_floor_ns, sig, signer, cert}}``, the full records. A write floor
    recorded before the signer column existed is not returned: it cannot
    be verified by a peer, and the origin re-seals every round."""
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fleet_sync_machine_write_floors'"
    ).fetchone() is not None
    if not present:
        return {}
    columns = {str(r[1]) for r in conn.execute("PRAGMA table_info(fleet_sync_machine_write_floors)")}
    if "signer" not in columns:
        return {}
    out = {}
    for row in conn.execute(
        "SELECT o.incarnation, c.write_floor_ns, c.sig, c.signer, c.cert FROM fleet_sync_machine_write_floors c "
        "JOIN fleet_sync_origins o ON o.id=c.origin_id WHERE c.signer IS NOT NULL"
    ):
        origin = str(row[0])
        out[origin] = {
            "write_floor_ns": int(row[1]), "sig": str(row[2]), "signer": str(row[3]),
            "cert": json.loads(row[4]) if row[4] else None,
        }
    return out


def machine_write_floor_frames(
    records: Mapping[str, Mapping], version: int, watermarks: Mapping[str, int],
    bounds: Mapping[str, int],
) -> list[bytes]:
    """The machine.write_floor control frames to send a puller that advertised
    *watermarks*: every write floor in *records* above the puller's watermark
    for its origin AND at or below *bounds* for it. *records* and *bounds*
    are one snapshot, the server's held floors and its own cursor per origin
    read together before the first page (SQLiteFleetSyncStore.serve_snapshot),
    the same bound the transaction pages kept; a floor read later could have
    moved past the bound during the serve. A floor the server's cursor has
    not reached is held but not passed on: what a server cannot claim itself,
    it does not send. Emitted after the transaction stream, never before."""
    frames = []
    for origin, record in sorted(records.items()):
        floor_ns = int(record["write_floor_ns"])
        if floor_ns > int(watermarks.get(origin, -1)) and floor_ns <= int(bounds.get(origin, -1)):
            body = {
                "v": version, "kind": MACHINE_WRITE_FLOOR_KIND, "origin": origin,
                "write_floor_ns": record["write_floor_ns"], "sig": record["sig"], "signer": record["signer"],
            }
            if record["cert"] is not None:
                body["cert"] = record["cert"]
            frames.append(canonical_json(body))
    return frames


# ── persona write floor ─────────────────────────────────────────────────────────────

def persona_write_floor_body(persona: str, org: str, write_floor_ns: int, machines: Mapping[str, int],
                     signer: str) -> bytes:
    return canonical_json({
        "kind": PERSONA_WRITE_FLOOR_KIND, "persona": persona, "org": org,
        "write_floor_ns": int(write_floor_ns), "machines": {k: int(v) for k, v in sorted(machines.items())},
        "signer": signer,
    })


def seal_persona_write_floor(
    conn: sqlite3.Connection, *, signer: KeyPair, persona_cert: DelegationCert,
    org: str, roster_machines: set[str], positions: Mapping[str, int],
) -> dict | None:
    """Seal {persona, org, write_floor_ns, machines, signer, cert, sig} when every
    machine of the persona's roster has a known position; F is their
    minimum. Returns the record when a NEWER persona write floor was stored, else
    None: an unconverged machine (no position) blocks the seal, and a
    minimum no higher than the held write floor seals nothing."""
    if not roster_machines or any(m not in positions for m in roster_machines):
        return None
    machines = {m: int(positions[m]) for m in sorted(roster_machines)}
    write_floor_ns = min(machines.values())
    persona = str(persona_cert.subject.id)
    record = {
        "persona": persona, "org": org, "write_floor_ns": write_floor_ns, "machines": machines,
        "signer": signer.public_hex, "cert": persona_cert.to_dict(),
    }
    record["sig"] = signer.sign_hex(persona_write_floor_body(persona, org, write_floor_ns, machines, signer.public_hex))
    return record if store_persona_write_floor(conn, record) else None


def persona_seal_blocker(
    conn: sqlite3.Connection, *, persona: str, roster_machines: set[str],
    positions: Mapping[str, int],
) -> str | None:
    """Why seal_persona_write_floor would decline right now, in words, or None when
    it would seal: an empty roster, a roster machine with no position in
    this store, or a minimum not above the persona write floor already held."""
    if not roster_machines:
        return "the persona's roster lists no machines"
    missing = sorted(m for m in roster_machines if m not in positions)
    if missing:
        return "no position held for roster machine(s) " + ", ".join(m[:12] for m in missing)
    minimum = min(int(positions[m]) for m in roster_machines)
    held = persona_write_floors(conn).get(persona)
    if held is not None and int(held.get("write_floor_ns", 0)) >= minimum:
        return f"minimum position {minimum} is not above the held persona write floor {int(held['write_floor_ns'])}"
    return None


def verify_persona_write_floor(record: Mapping, *, org: str, now: int) -> tuple[str, int]:
    """-> (persona, write_floor_ns) or WriteFloorError. The cert chain anchors at the
    persona the record names, must be for *org* and scope fleet:sync, and
    must name the signer as its leaf; the signature is the signer's."""
    try:
        persona = str(record["persona"])
        write_floor_ns = record["write_floor_ns"]
        machines = record["machines"]
        signer = str(record["signer"])
        sig = str(record["sig"])
        cert = DelegationCert.from_dict(record["cert"])
    except (KeyError, TypeError, ValueError, IdkitError) as exc:
        raise WriteFloorError(f"persona.write_floor is malformed: {exc}") from exc
    if str(record.get("org")) != org:
        raise WriteFloorError("persona.write_floor is for another organization")
    if not isinstance(write_floor_ns, int) or isinstance(write_floor_ns, bool) or write_floor_ns < 0:
        raise WriteFloorError("persona.write_floor write_floor_ns is malformed")
    if (
        not isinstance(machines, dict) or not machines
        or any(not isinstance(v, int) or isinstance(v, bool) or v < write_floor_ns for v in machines.values())
    ):
        raise WriteFloorError("persona.write_floor machines must be positions at or above write_floor_ns")
    if cert.org != org or cert.subject.kind != "persona" or str(cert.subject.id) != persona:
        raise WriteFloorError("persona.write_floor cert does not name this persona for this organization")
    try:
        verified = verify_chain(cert, persona, org=org, now=now, required_scope=PERSONA_WRITE_FLOOR_SCOPE)
    except IdkitError as exc:
        raise WriteFloorError(f"persona.write_floor cert chain failed: {exc}") from exc
    if verified.leaf_pub != signer:
        raise WriteFloorError("persona.write_floor cert names another machine than the signer")
    try:
        verify_signature(signer, sig, persona_write_floor_body(persona, org, write_floor_ns, machines, signer))
    except (IdkitError, ValueError) as exc:
        raise WriteFloorError(f"persona.write_floor signature does not verify: {exc}") from exc
    return persona, int(write_floor_ns)


def store_persona_write_floor(conn: sqlite3.Connection, record: Mapping) -> bool:
    """Store a persona write floor the caller has verified (or just sealed) if newer
    than the one held. Own transaction."""
    persona, org, write_floor_ns = str(record["persona"]), str(record["org"]), int(record["write_floor_ns"])
    if conn.in_transaction:
        raise WriteFloorError("cannot store a persona write floor inside another transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        ensure_write_floor_schema(conn)
        before = conn.execute(
            "SELECT write_floor_ns FROM fleet_sync_persona_write_floors WHERE persona=?", (persona,)
        ).fetchone()
        moved = before is None or int(before[0]) < write_floor_ns
        if moved:
            conn.execute(
                "INSERT INTO fleet_sync_persona_write_floors(persona,org,write_floor_ns,record) VALUES(?,?,?,?) "
                "ON CONFLICT(persona) DO UPDATE SET org=excluded.org, write_floor_ns=excluded.write_floor_ns, "
                "record=excluded.record",
                (persona, org, write_floor_ns, json.dumps(dict(record), sort_keys=True)),
            )
        conn.execute("COMMIT")
        return moved
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def adopt_persona_write_floor(conn: sqlite3.Connection, record: Mapping, *, org: str, now: int) -> bool:
    verify_persona_write_floor(record, org=org, now=now)
    return store_persona_write_floor(conn, record)


def persona_write_floors(conn: sqlite3.Connection) -> dict[str, dict]:
    """``{persona: record}`` for every persona write floor this store holds."""
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fleet_sync_persona_write_floors'"
    ).fetchone() is not None
    if not present:
        return {}
    return {
        str(row[0]): json.loads(row[1])
        for row in conn.execute("SELECT persona, record FROM fleet_sync_persona_write_floors")
    }


def persona_frontiers(conn: sqlite3.Connection) -> dict[str, int]:
    """``{persona: write_floor_ns}``: what an org member advertises per persona."""
    return {persona: int(record["write_floor_ns"]) for persona, record in persona_write_floors(conn).items()}


def persona_write_floor_frames(conn: sqlite3.Connection, version: int, known: Mapping[str, int]) -> list[bytes]:
    """persona.write_floor frames for every held persona write floor newer than what the
    puller said it knows."""
    frames = []
    for persona, record in sorted(persona_write_floors(conn).items()):
        if int(record["write_floor_ns"]) > int(known.get(persona, -1)):
            frames.append(canonical_json({"v": version, "kind": PERSONA_WRITE_FLOOR_KIND, **record}))
    return frames
