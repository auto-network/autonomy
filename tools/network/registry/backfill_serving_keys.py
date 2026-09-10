"""auto-e2ufw backfill: register existing orgs' serving machine keys.

Option B (graph://a374b260-e4a) closes the transitional accept per org as
its serving-key allow-set becomes non-empty. This host-terminal tool, run
by the operator (who holds the personal root), derives each machine's
per-(org, machine) serving pubkey and registers it with the registry's
allow-set. Idempotent: re-running registers nothing new. It writes ONLY
public keys — the private seed never leaves the operator's root context.

Usage (on the registry host, or against its DB):
    python -m tools.network.registry.backfill_serving_keys \\
        --db /var/lib/autonomy-registry/registry.db \\
        --org <ORG_UUID> --machine <machine_id> --serving-pub <hex>

``--org`` IS THE ORG_UUID, NOT THE GENESIS ID. The allow-set column is
literally ``serve_machine_keys.org_uuid``, and the value relay.py looks up is
the one the connector dialed with -- the ``{org}`` of ``/t/{org}``, which is
the org_uuid. Registering under the genesis id writes a row nothing will ever
read: ``registered_serving_keys(org_uuid)`` stays empty, the transitional
accept stays open, and this tool's own success line still prints "org now has
1 registered serving key(s)" -- so the mistake CONFIRMS itself and the
enforcement window silently never closes. This file used to say "<genesis_id>"
and "org genesis/uuid"; it was wrong, and the shape guard below exists because
the two identifiers are both opaque strings and nothing else would catch it.

The two are easy to tell apart and easy to obtain together, per machine:
``fleet_enrollment_routes.serving_org_targets()`` returns ``{scope, org_uuid,
genesis_id}`` per serving org, and ``link_serving_supervisor.control(scope,
"connector-status", {})["serving_slot"]["machine"]`` is the serving public key
that org is ACTUALLY presenting -- which is what should be registered, since
the point is for the allow-set to match reality.

The caller derives <hex> with idkit.derive_serving_machine_key(root,
genesis_id, machine_id).public_hex on the operator's machine -- note the
DERIVATION is keyed by genesis_id while the REGISTRATION is keyed by org_uuid,
which is the whole reason this is confusable. This tool takes the
already-derived PUBLIC key so the root never touches the host.
"""

from __future__ import annotations

import argparse
import json
import re
import time

from tools.network.registry.store import RegistryStore

#: How old a collected manifest may be before `register` refuses it.
#: Serving keys are not stable over hours: on 2026-09-10 home's three org
#: connectors moved from its durable roster key to per-org keys when 76d61b5b
#: landed at 00:14Z, and the four serve-cert delegates rotated again at 01:50Z
#: when the certs were re-minted persona-signed -- two independent rotations in
#: one night, measured from the registry's own log by host-0906-222509. An
#: allow-set registered from values collected before a derivation change
#: hard-gates every connector it covers, and the refusal surfaces at the hello
#: where it reads as an identity fault. So a stale manifest is refused rather
#: than trusted; re-collect and re-run, it is two commands.
MANIFEST_MAX_AGE_S = 15 * 60


#: LOWERCASE only, both of these. The registry stores what it is given and
#: compares it literally: `WHERE org_uuid = ?`, and `data["machine"] not in
#: allowed` against a wire value the hello parser already pinned to
#: `^[0-9a-f]{64}$`. So an uppercase input writes a row that is never matched
#: and never looked up -- a third way for this tool to confirm a no-op. Refused
#: rather than silently lowercased: a tool that reports a value it did not use
#: is the next bug.
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                   r"[0-9a-f]{4}-[0-9a-f]{12}\Z")
_HEX64 = re.compile(r"^[0-9a-f]{64}\Z")


def _require_org_uuid(org: str) -> None:
    """Refuse anything that is not an org_uuid, naming the likely mistake.

    A 64-hex value is a genesis id -- the thing the serving key is DERIVED
    from, and the thing this tool's own documentation used to ask for. Writing
    it registers a row no reader looks at, and the success line below would
    still claim the org has a registered key. A wrong value that confirms
    itself is worse than a crash, so this crashes.
    """
    if _UUID.match(org):
        return
    if len(org) == 64 and all(c in "0123456789abcdefABCDEF" for c in org):
        raise SystemExit(
            f"--org got a 64-hex value ({org[:12]}...), which is a GENESIS ID. "
            "The allow-set is keyed by org_uuid (serve_machine_keys.org_uuid), "
            "the same value the connector dials as /t/{org}. Registering a "
            "genesis id writes a row nothing reads: the transitional accept "
            "would stay open while this tool reported success. Pass the "
            "org_uuid -- serving_org_targets() returns both per serving org."
        )
    if _UUID.match(org.lower()):
        raise SystemExit(
            f"--org must be LOWERCASE, got {org!r}. The lookup is a literal "
            "string comparison (WHERE org_uuid = ?) against the value the "
            "connector dialed, which is lowercase. An uppercase row is written "
            "and never read."
        )
    raise SystemExit(
        f"--org must be an org_uuid, got {org[:32]!r}. The allow-set column is "
        "serve_machine_keys.org_uuid and relay.py looks up the value the "
        "connector dialed with."
    )


def _require_serving_pub(pub: str) -> None:
    """64 LOWERCASE hex. The hello parser pins the wire form to
    ``^[0-9a-f]{64}$`` and enforcement is ``data["machine"] not in allowed``,
    so an uppercase row is written and never matched -- the same no-op that
    reports success."""
    if _HEX64.match(pub):
        return
    if _HEX64.match(pub.lower()):
        raise SystemExit(
            f"--serving-pub must be LOWERCASE hex, got {pub[:12]}... The "
            "allow-set is compared against the hello's machine field, which "
            "the parser pins to lowercase. An uppercase row never matches."
        )
    raise SystemExit(
        f"--serving-pub must be 64 lowercase hex characters, got "
        f"{pub[:32]!r} ({len(pub)} chars)."
    )


def collect_manifest() -> dict:
    """THIS machine's live serving keys, read from the processes serving them.

    Run on a dashboard machine, not on the registry host. Reads each serving
    org's connector-status through the control socket, so the key recorded is
    the one that org is ACTUALLY presenting -- not a derivation done in a fresh
    interpreter, which can differ from what is on the wire and which cannot see
    whether the connector is even up.
    """
    from tools.dashboard import fleet_enrollment_routes as routes
    from tools.dashboard import link_serving_supervisor as supervisor

    targets = [{"scope": None, "org_uuid": None, "genesis_id": None}]
    targets += routes.serving_org_targets()
    entries = []
    commits = set()
    for target in targets:
        scope = target["scope"]
        reply = supervisor.control(scope, "connector-status", {}, timeout=12.0)
        slot = reply.get("serving_slot") or {}
        machine_pub = slot.get("machine")
        org_uuid = target["org_uuid"] or reply.get("org_uuid")
        if not machine_pub or not org_uuid:
            raise SystemExit(
                f"scope {scope or 'personal'} reported no serving slot; its "
                "connector is not serving. Registering a partial set would "
                "gate the orgs it covers and leave this one un-enforced -- "
                "fix the connector and re-collect."
            )
        commits.add(reply.get("boot_commit") or "")
        entries.append({
            "scope": scope or "personal",
            "org_uuid": org_uuid,
            "serving_pub": machine_pub,
            # Recorded for provenance only. The key is DERIVED from this and
            # REGISTERED under org_uuid; it must never reach a register line.
            "genesis_id_do_not_register": target["genesis_id"],
        })
    if len(commits) > 1:
        raise SystemExit(
            f"this machine's connectors are on different commits ({commits}); "
            "they are mid-handoff. Wait for the recycle and re-collect."
        )
    return {
        "collected_at": int(time.time()),
        "boot_commit": commits.pop() if commits else "",
        "entries": entries,
    }


def _manifest_entries(paths: list, now: int) -> list:
    """Load manifests, refusing stale ones and disagreeing commits."""
    out = []
    commits = set()
    for path in paths:
        with open(path) as fh:
            manifest = json.load(fh)
        age = now - int(manifest.get("collected_at") or 0)
        if age > MANIFEST_MAX_AGE_S:
            raise SystemExit(
                f"{path} was collected {age // 60} minutes ago, older than the "
                f"{MANIFEST_MAX_AGE_S // 60}-minute bound. Serving keys rotate "
                "-- registering a stale one hard-gates the connector it was "
                "meant to admit. Re-collect on each machine and re-run."
            )
        commits.add(manifest.get("boot_commit") or "")
        out.extend(manifest["entries"])
    if len(commits) > 1:
        raise SystemExit(
            f"the manifests come from different commits ({sorted(commits)}). "
            "Register only when every machine is on the same commit and "
            "quiescent: a commit touching key derivation or serving identity "
            "invalidates an allow-set, and 76d61b5b already did exactly that "
            "once."
        )
    return out


def live_slots(readout_url: str) -> dict:
    """``{org_uuid: {machine-key prefix, ...}}`` for tunnels serving RIGHT NOW.

    Read from the registry's own loopback readout (``GET /readout`` on the
    private metrics listener, metrics.render_readout), which walks the live
    TunnelHub at call time "so it can never disagree with reality". This is the
    invariant itself rather than a proxy for it: who is serving now, versus who
    the set about to be written would admit.

    WHY THIS REPLACED A FRESHNESS CLOCK as the primary check. The first version
    refused manifests older than 15 minutes. host-0906-222509 measured the real
    propagation: 76d61b5b merged and home's connectors recycled onto it in about
    26 SECONDS, so a manifest collected at 00:13:50 and applied at 00:14:30
    passes any bound you would pick and is already wrong. The hazard is CHANGE,
    not age, and on this fleet a merge lands faster than two ssh round trips.
    The clock survives below as a backstop against a stale terminal resumed
    tomorrow — do not "improve" this tool by tuning that number and think the
    risk is addressed.

    Machine keys in the readout are truncated to 16 hex characters (64 bits) on
    purpose; comparison is therefore by prefix, which is collision-free at this
    scale. The registry's own ``build.commit`` is in the reply too and is
    deliberately NOT compared with the manifests' ``boot_commit``: those are
    different deploys of different codebases, and equating them would assert a
    relationship that does not exist.
    """
    import json as _json
    import urllib.request

    with urllib.request.urlopen(readout_url, timeout=5) as resp:
        data = _json.load(resp)
    slots = {}
    for org, info in (data.get("orgs") or {}).items():
        keys = {
            tunnel.get("machine") or ""
            for tunnel in (info.get("tunnels") or ())
        }
        slots[org] = {key for key in keys if key}
    return slots


def _covered(prefix: str, full_keys) -> bool:
    return any(full.startswith(prefix) for full in full_keys)


def _require_live_slots_covered(slots: dict, entries: list) -> None:
    """Every machine serving an org NOW must be in the set about to be written.

    Catches what no clock can: one machine drifting between its own collect and
    this register, and a derivation change landing in the seconds between them.
    """
    by_org: dict = {}
    for entry in entries:
        by_org.setdefault(entry["org_uuid"], set()).add(entry["serving_pub"])
    for org, manifest_keys in sorted(by_org.items()):
        for prefix in sorted(slots.get(org, ())):
            if not _covered(prefix, manifest_keys):
                raise SystemExit(
                    f"REFUSING: org {org} has a tunnel serving RIGHT NOW under "
                    f"machine key {prefix}..., which is not in the manifest "
                    f"({len(manifest_keys)} key(s) collected). Registering now "
                    "would gate that connector at its next hello. Its serving "
                    "key changed after the manifest was collected — re-collect "
                    "on every machine and re-run."
                )


def _require_live_slots_admitted(slots: dict, store, entries: list) -> None:
    """POST-CONDITION, stronger than the read-back: the gate now admits the
    fleet as it actually stands. Read-back proves the row landed; this proves
    every live tunnel would pass the check relay.py is about to start making.
    If the two ever disagree, that is worth waking someone for."""
    for org in sorted({entry["org_uuid"] for entry in entries}):
        allowed = store.registered_serving_keys(org)
        for prefix in sorted(slots.get(org, ())):
            if not _covered(prefix, allowed):
                raise SystemExit(
                    f"POST-CONDITION FAILED: org {org} is being served NOW by "
                    f"machine key {prefix}..., and the allow-set just written "
                    f"({len(allowed)} key(s)) does not admit it. Enforcement is "
                    "live and that connector will be refused at its next "
                    "hello. Investigate before anything reconnects."
                )


def _register_one(store, org: str, machine: str, serving_pub: str) -> None:
    _require_org_uuid(org)
    _require_serving_pub(serving_pub)
    store.register_serving_machine_key(org, serving_pub, now=int(time.time()))
    # READ BACK THROUGH THE FUNCTION RELAY.PY CALLS, and report what was
    # ACHIEVED rather than what was attempted. A refusal above catches the
    # mistakes we know about; this catches the ones we do not -- suggested by
    # host-0906-222509, who pointed out that the same shape (a tool reporting
    # its own action instead of the resulting state) is what let deploy.sh
    # print success without restarting the service and let a docker rmtree
    # return zero having deleted nothing.
    allowed = store.registered_serving_keys(org)
    if serving_pub not in allowed:
        raise SystemExit(
            "READ-BACK FAILED: wrote the row, then looked the allow-set up the "
            f"way relay.py does (registered_serving_keys({org!r})) and the "
            f"key is not in it. Found {len(allowed)} key(s). Enforcement would "
            "NOT be on for this org. Do not treat this run as a backfill."
        )
    print(f"registered serving key for org_uuid={org} machine={machine[:8]}"
          f"\n  read back via registered_serving_keys(): {len(allowed)} key(s) "
          f"for this org, including {serving_pub}"
          f"\n  relay.py will now ENFORCE the allow-set for this org "
          f"(transitional accept closed)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--collect", action="store_true",
                   help="ON A DASHBOARD MACHINE: print this machine's live "
                        "serving keys as a manifest; registers nothing")
    p.add_argument("--manifest", action="append", default=[],
                   help="ON THE REGISTRY HOST: a manifest from --collect "
                        "(repeatable, one per machine). Refused when stale")
    p.add_argument("--readout",
                   help="the registry's own loopback readout URL "
                        "(http://127.0.0.1:<metrics_port>/readout). THE "
                        "PRIMARY SAFETY CHECK: every machine serving an org "
                        "right now must be in the manifest, and must still be "
                        "admitted afterwards")
    p.add_argument("--no-live-check", action="store_true",
                   help="skip the live-slot check. You are then registering a "
                        "gate without knowing who it will refuse; the only "
                        "remaining detection is the 'serving-key REFUSED' "
                        "audit line, after a connector is already locked out")
    p.add_argument("--db")
    p.add_argument("--org", help="org_uuid, NOT the genesis id")
    p.add_argument("--machine", default="",
                   help="machine id (for the audit line only)")
    p.add_argument("--serving-pub",
                   help="64 lowercase hex serving machine PUBLIC key")
    a = p.parse_args()

    if a.collect:
        print(json.dumps(collect_manifest(), indent=1))
        return
    if not a.db:
        raise SystemExit("--db is required to register")
    store = RegistryStore(a.db)
    if a.manifest:
        entries = _manifest_entries(a.manifest, int(time.time()))
        slots = {}
        if a.no_live_check:
            print("WARNING: --no-live-check. Registering a gate without "
                  "reading who is serving now.")
        elif not a.readout:
            raise SystemExit(
                "--readout is required (or --no-live-check to proceed without "
                "it). The live-slot comparison is the primary check: a "
                "freshness bound cannot catch a derivation change that lands "
                "in the seconds between collect and register, and one did "
                "exactly that on 2026-09-10 in about 26 seconds."
            )
        else:
            slots = live_slots(a.readout)
            _require_live_slots_covered(slots, entries)
        for entry in entries:
            _register_one(store, entry["org_uuid"], a.machine or entry["scope"],
                          entry["serving_pub"])
        if slots:
            _require_live_slots_admitted(slots, store, entries)
            print("\npost-condition OK: every tunnel serving these orgs right "
                  "now is admitted by the allow-set just written.")
        print(f"\n{len(entries)} registration(s) complete. Watch the registry "
              "audit log: 'serving-key transitional-accept' must STOP appearing "
              "for these orgs (that is the positive signal enforcement is on), "
              "and 'serving-key REFUSED' appearing means a serving key rotated "
              "and needs re-registering.")
        return
    if not a.org or not a.serving_pub:
        raise SystemExit(
            "give --manifest (preferred: values read live from the serving "
            "processes) or --org with --serving-pub")
    _register_one(store, a.org, a.machine, a.serving_pub)


if __name__ == "__main__":
    main()
