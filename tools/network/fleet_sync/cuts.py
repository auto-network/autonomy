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
import time
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


#: The delegation a machine key issues to the process that runs its fleet
#: sync (fleet_sync_channel: machine-direct, this one scope, no target
#: types). A cut signed by that process carries the certificate so a
#: receiver can walk from the origin, which IS the machine key, to the
#: signer. The hello's TTL bound is not re-checked here: a cut is a fact
#: about the past, and the chain's own validity window still applies.
PROCESS_DELEGATION_SCOPE = ("fleet:sync",)


def ensure_cut_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fleet_sync_origin_cuts("
        "origin_id INTEGER PRIMARY KEY,"
        "cut_ns INTEGER NOT NULL,"
        "sig TEXT NOT NULL,"
        "signer TEXT,"
        "cert TEXT)"
    )
    columns = {str(r[1]) for r in conn.execute("PRAGMA table_info(fleet_sync_origin_cuts)")}
    for name in ("signer", "cert"):
        if name not in columns:
            conn.execute(f"ALTER TABLE fleet_sync_origin_cuts ADD COLUMN {name} TEXT")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fleet_sync_persona_cuts("
        "persona TEXT PRIMARY KEY,"
        "org TEXT NOT NULL,"
        "cut_ns INTEGER NOT NULL,"
        "record TEXT NOT NULL)"
    )
    # Every machine a persona cut ever listed, with the position it was
    # last listed at; a machine the persona's newest cut omits is retired
    # (auto-0my5i): the persona's own fleet decided it, nothing else.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fleet_sync_persona_machines("
        "persona TEXT NOT NULL,"
        "machine TEXT NOT NULL,"
        "position INTEGER NOT NULL,"
        "listed_cut_ns INTEGER NOT NULL,"
        "retired INTEGER NOT NULL DEFAULT 0,"
        "PRIMARY KEY(persona, machine))"
    )


# ── machine cut ─────────────────────────────────────────────────────────────

def origin_cut_body(origin: str, cut_ns: int, signer: str) -> bytes:
    return canonical_json({
        "kind": ORIGIN_CUT_KIND, "origin": origin, "cut_ns": int(cut_ns), "signer": signer,
    })


def seal_machine_cut(
    conn: sqlite3.Connection, signer: KeyPair, origin: str, now_ns: int,
    *, cert: DelegationCert | None = None,
) -> int | None:
    """Raise this store's write floor to max(last write, now) and record the
    signed cut for *origin* (this machine). Returns the cut, or None when the
    clock is behind the floor: cuts pause and the gate keeps refusing writes
    until the clock passes the floor, so a cut never regresses and never
    exceeds the machine clock. *signer* is the origin's own machine key, or
    a process key the machine key delegated to, in which case *cert* is
    that delegation and travels with the cut. Own BEGIN IMMEDIATE
    transaction."""
    if signer.public_hex != origin and cert is None:
        raise CutError("a cut signed by a delegate must carry the delegation")
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
        sig = signer.sign_hex(origin_cut_body(origin, cut, signer.public_hex))
        cert_json = json.dumps(cert.to_dict(), sort_keys=True) if cert is not None else None
        conn.execute(
            "INSERT INTO fleet_sync_origin_cuts(origin_id,cut_ns,sig,signer,cert) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(origin_id) DO UPDATE SET cut_ns=excluded.cut_ns, sig=excluded.sig, "
            "signer=excluded.signer, cert=excluded.cert "
            "WHERE excluded.cut_ns>fleet_sync_origin_cuts.cut_ns",
            (origin_id, cut, sig, signer.public_hex, cert_json),
        )
        conn.execute("COMMIT")
        return cut
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def verify_origin_cut(record: Mapping, *, now: int | None = None) -> dict:
    """-> {origin, cut_ns, sig, signer, cert} or CutError.

    An origin incarnation is the authoring machine's public key. The
    signer is that key, or a process key the machine key delegated to
    with a machine-direct fleet:sync certificate (the same delegation the
    sync hello carries), in which case the chain must anchor at the origin
    and name the signer as its leaf."""
    try:
        origin = str(record["origin"])
        cut_ns = record["cut_ns"]
        sig = str(record["sig"])
        signer = str(record.get("signer") or origin)
        cert_data = record.get("cert")
    except (KeyError, TypeError) as exc:
        raise CutError(f"origin.cut is malformed: {exc}") from exc
    for what, value in (("origin", origin), ("signer", signer)):
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise CutError(f"origin.cut {what} is malformed")
    if not isinstance(cut_ns, int) or isinstance(cut_ns, bool) or cut_ns < 0:
        raise CutError("origin.cut cut_ns is malformed")
    if signer != origin:
        if cert_data is None:
            raise CutError("origin.cut signed by a delegate carries no delegation")
        try:
            cert = DelegationCert.from_dict(cert_data)
            verified = verify_chain(
                cert, origin, org=str(cert.org),
                now=int(time.time()) if now is None else now,
                required_scope=PROCESS_DELEGATION_SCOPE[0],
            )
        except (IdkitError, ValueError, TypeError, KeyError) as exc:
            raise CutError(f"origin.cut delegation failed: {exc}") from exc
        if (
            cert.parent_cert is not None
            or tuple(cert.scope) != PROCESS_DELEGATION_SCOPE
            or cert.target_types is not None
            or verified.leaf_pub != signer
        ):
            raise CutError("origin.cut delegation does not name the signer machine-direct")
    try:
        verify_signature(signer, sig, origin_cut_body(origin, cut_ns, signer))
    except (IdkitError, ValueError) as exc:
        raise CutError(f"origin.cut signature does not verify: {exc}") from exc
    return {"origin": origin, "cut_ns": int(cut_ns), "sig": sig, "signer": signer,
            "cert": cert_data if signer != origin else None}


def adopt_origin_cut(conn: sqlite3.Connection, record: Mapping) -> bool:
    """Verify and store an origin's cut if newer than the one held. Returns
    True when the stored cut moved. Own transaction."""
    verified = verify_origin_cut(record)
    origin, cut_ns, sig = verified["origin"], verified["cut_ns"], verified["sig"]
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
                "INSERT INTO fleet_sync_origin_cuts(origin_id,cut_ns,sig,signer,cert) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(origin_id) DO UPDATE SET cut_ns=excluded.cut_ns, sig=excluded.sig, "
                "signer=excluded.signer, cert=excluded.cert",
                (origin_id, cut_ns, sig, verified["signer"],
                 json.dumps(verified["cert"], sort_keys=True) if verified["cert"] else None),
            )
        conn.execute("COMMIT")
        return moved
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def origin_cuts(conn: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    """``{origin: (cut_ns, sig)}`` for every verified cut this store holds."""
    return {origin: (r["cut_ns"], r["sig"]) for origin, r in origin_cut_records(conn).items()}


def origin_cut_records(conn: sqlite3.Connection) -> dict[str, dict]:
    """``{origin: {cut_ns, sig, signer, cert}}``, the full records. A cut
    recorded before the signer column existed is not returned: it cannot
    be verified by a peer, and the origin re-seals every round."""
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fleet_sync_origin_cuts'"
    ).fetchone() is not None
    if not present:
        return {}
    columns = {str(r[1]) for r in conn.execute("PRAGMA table_info(fleet_sync_origin_cuts)")}
    if "signer" not in columns:
        return {}
    out = {}
    for row in conn.execute(
        "SELECT o.incarnation, c.cut_ns, c.sig, c.signer, c.cert FROM fleet_sync_origin_cuts c "
        "JOIN fleet_sync_origins o ON o.id=c.origin_id WHERE c.signer IS NOT NULL"
    ):
        origin = str(row[0])
        out[origin] = {
            "cut_ns": int(row[1]), "sig": str(row[2]), "signer": str(row[3]),
            "cert": json.loads(row[4]) if row[4] else None,
        }
    return out


def origin_cut_frames(conn: sqlite3.Connection, version: int, watermarks: Mapping[str, int]) -> list[bytes]:
    """The origin.cut control frames to send a puller that advertised
    *watermarks*: every held cut above the puller's watermark for its
    origin. Emitted after the transaction stream, never before."""
    frames = []
    for origin, record in sorted(origin_cut_records(conn).items()):
        if record["cut_ns"] > int(watermarks.get(origin, -1)):
            body = {
                "v": version, "kind": ORIGIN_CUT_KIND, "origin": origin,
                "cut_ns": record["cut_ns"], "sig": record["sig"], "signer": record["signer"],
            }
            if record["cert"] is not None:
                body["cert"] = record["cert"]
            frames.append(canonical_json(body))
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
            machines = {str(m): int(p) for m, p in (record.get("machines") or {}).items()}
            for machine, position in machines.items():
                conn.execute(
                    "INSERT INTO fleet_sync_persona_machines(persona,machine,position,"
                    "listed_cut_ns,retired) VALUES(?,?,?,?,0) ON CONFLICT(persona,machine) "
                    "DO UPDATE SET position=excluded.position, "
                    "listed_cut_ns=excluded.listed_cut_ns, retired=0",
                    (persona, machine, position, cut_ns),
                )
            if machines:
                holders = ",".join("?" * len(machines))
                conn.execute(
                    "UPDATE fleet_sync_persona_machines SET retired=1 "
                    f"WHERE persona=? AND machine NOT IN ({holders})",
                    (persona, *machines),
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


def persona_machines(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
    """-> (listed, retired): machines any held persona cut currently lists,
    and machines a persona listed before and its newest cut omits."""
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fleet_sync_persona_machines'"
    ).fetchone() is not None
    if not present:
        return set(), set()
    listed, retired = set(), set()
    for machine, flag in conn.execute("SELECT machine, retired FROM fleet_sync_persona_machines"):
        (retired if int(flag) else listed).add(str(machine))
    return listed, retired - listed


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
