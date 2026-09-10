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
import re
import time

from tools.network.registry.store import RegistryStore


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


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", required=True)
    p.add_argument("--org", required=True, help="org genesis/uuid")
    p.add_argument("--machine", required=True,
                   help="machine id (for the audit line only)")
    p.add_argument("--serving-pub", required=True,
                   help="64-hex serving machine PUBLIC key, derived by the "
                        "operator with derive_serving_machine_key")
    a = p.parse_args()
    _require_org_uuid(a.org)
    _require_serving_pub(a.serving_pub)
    store = RegistryStore(a.db)
    store.register_serving_machine_key(
        a.org, a.serving_pub, now=int(time.time()))
    # READ BACK THROUGH THE FUNCTION RELAY.PY CALLS, and report what was
    # ACHIEVED rather than what was attempted. A refusal above catches the
    # mistakes we know about; this catches the ones we do not -- suggested by
    # host-0906-222509, who pointed out that the same shape (a tool reporting
    # its own action instead of the resulting state) is what let deploy.sh
    # print success without restarting the service and let a docker rmtree
    # return zero having deleted nothing.
    allowed = store.registered_serving_keys(a.org)
    if a.serving_pub not in allowed:
        raise SystemExit(
            "READ-BACK FAILED: wrote the row, then looked the allow-set up the "
            f"way relay.py does (registered_serving_keys({a.org!r})) and the "
            f"key is not in it. Found {len(allowed)} key(s). Enforcement would "
            "NOT be on for this org. Do not treat this run as a backfill."
        )
    print(f"registered serving key for org_uuid={a.org} machine={a.machine[:8]}"
          f"\n  read back via registered_serving_keys(): {len(allowed)} key(s) "
          f"for this org, including {a.serving_pub}"
          f"\n  relay.py will now ENFORCE the allow-set for this org "
          f"(transitional accept closed)")


if __name__ == "__main__":
    main()
