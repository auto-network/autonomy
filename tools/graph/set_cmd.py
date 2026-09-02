"""``graph set`` subcommand group — Settings primitive CLI.

Spec: graph://0d3f750f-f9c. Thin layer over ``settings_ops``, dispatched
via :func:`tools.graph.client.get_client` so container CLIs route through
the dashboard API instead of opening local sqlite files.

The argparse setup lives in cli.py; per-subcommand handlers live here.
"""

from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any

from .client import get_client
from .schemas.registry import SchemaValidationError


_VALID_PROMOTION_STATES = ("curated", "published", "canonical")
_MAX_SECRET_BYTES = 1024 * 1024


# ── helpers ──────────────────────────────────────────────────


def _read_payload_file(path: str) -> dict:
    """Load a payload from JSON or YAML. Falls back to JSON if PyYAML missing.

    ``path == "-"`` reads from stdin (JSON only — YAML over stdin would
    require sniffing or an explicit format flag).
    """
    if path == "-":
        text = sys.stdin.read()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            print(f"Error: invalid JSON on stdin: {e}", file=sys.stderr)
            sys.exit(1)
        if not isinstance(data, dict):
            print("Error: stdin must contain a JSON object", file=sys.stderr)
            sys.exit(1)
        return data
    text = Path(path).read_text()
    p = path.lower()
    if p.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            print("Error: PyYAML not installed; pass a .json file or install pyyaml.",
                  file=sys.stderr)
            sys.exit(1)
        data = yaml.safe_load(text)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            print(f"Error: invalid JSON in {path}: {e}", file=sys.stderr)
            sys.exit(1)
    if not isinstance(data, dict):
        print(f"Error: {path} must contain a JSON/YAML object", file=sys.stderr)
        sys.exit(1)
    return data


def _resolve_payload_input(args) -> dict:
    """Pick the payload source for ``set add`` / ``set override``.

    Order of precedence (mutually exclusive — checked at parse time):

    * ``--inline JSON`` — small payloads on the command line.
    * ``--from PATH`` (``-`` reads stdin) — file path.
    * neither, and stdin is not a TTY — implicit stdin read.
    * neither, stdin is a TTY — error (no payload supplied).
    """
    inline = getattr(args, "inline", None)
    from_path = getattr(args, "from_file", None)
    if inline is not None and from_path is not None:
        print("Error: --inline and --from are mutually exclusive",
              file=sys.stderr)
        sys.exit(1)
    if inline is not None:
        try:
            data = json.loads(inline)
        except json.JSONDecodeError as e:
            print(f"Error: invalid JSON for --inline: {e}", file=sys.stderr)
            sys.exit(1)
        if not isinstance(data, dict):
            print("Error: --inline must be a JSON object", file=sys.stderr)
            sys.exit(1)
        return data
    if from_path is not None:
        return _read_payload_file(from_path)
    if not sys.stdin.isatty():
        return _read_payload_file("-")
    print("Error: no payload supplied — pass --inline JSON, --from FILE, "
          "--from -, or pipe JSON on stdin", file=sys.stderr)
    sys.exit(1)


def _parse_set_at_rev(spec: str) -> tuple[str, int]:
    """Parse 'autonomy.workspace#1' into ('autonomy.workspace', 1)."""
    if "#" not in spec:
        print(f"Error: expected <set_id>#<rev>, got {spec!r}", file=sys.stderr)
        sys.exit(1)
    set_id, rev = spec.rsplit("#", 1)
    try:
        return set_id, int(rev)
    except ValueError:
        print(f"Error: revision must be an integer in {spec!r}", file=sys.stderr)
        sys.exit(1)


def _print_table(rows: list[dict], cols: list[tuple[str, str, int]]) -> None:
    """Print a table. ``cols`` = (key, header, width)."""
    header = "  ".join(f"{h:<{w}}" for _, h, w in cols)
    sep = "  ".join("─" * w for _, _, w in cols)
    print(header)
    print(sep)
    for r in rows:
        line = "  ".join(
            f"{str(r.get(k, '') or '')[:w]:<{w}}" for k, _, w in cols
        )
        print(line)


# ── address resolution ──────────────────────────────────────


def _looks_like_set_id(value: str) -> bool:
    """Heuristic: dotted set_id (``autonomy.workspace``) vs UUID/prefix.

    UUIDs/prefixes are hex with dashes only; set_ids contain dots. Used
    only to decide between addressing modes when two positionals were
    given — we still attempt resolution and surface a clear error if the
    guess was wrong.
    """
    return "." in value


def _scope_phrase(org) -> str:
    """Human suffix naming the search scope of a failed lookup.

    ``org`` may be the :data:`ops.CALLER_ORG` sentinel (no ``--org`` given);
    rendering its repr into an error produced the baffling
    ``in org <settings_ops.CALLER_ORG>`` — say what it means instead.
    """
    if not org:
        return ""
    from . import ops
    if org is ops.CALLER_ORG:
        return " in the caller's organization scope"
    return f" in org {org!r}"


def _resolve_address(args, *, accept_key: bool = True) -> str:
    """Resolve the ``id [key]`` positional pair to a single Setting id.

    * 1 positional → treat as full id or prefix; call
      ``client.resolve_setting_strict``.
    * 2 positionals (and ``accept_key`` is True) → treat as
      ``(set_id, key)``; call ``client.read_set`` and pick the member
      with matching key.

    On miss / ambiguity the function prints a candidate list (for
    ambiguity) or a "no match" error and ``sys.exit(1)``s. On success
    it returns the canonical id string.
    """
    org = _org(args)
    parts: list[str] = list(getattr(args, "id_parts", []) or [])
    if not parts:
        print("Error: missing positional argument: id (or set_id key)",
              file=sys.stderr)
        sys.exit(1)
    if len(parts) > 2:
        print(f"Error: too many positional arguments: {parts!r}",
              file=sys.stderr)
        sys.exit(1)

    if len(parts) == 2:
        if not accept_key:
            print(f"Error: this command takes one positional id, got {parts!r}",
                  file=sys.stderr)
            sys.exit(1)
        set_id, key = parts
        return _resolve_set_key(set_id, key, org=org)

    # Single positional — id or prefix.
    return _resolve_id_or_prefix(parts[0], org=org)


def _resolve_id_or_prefix(value: str, *, org: str | None) -> str:
    """Resolve an id or id-prefix to a single Setting id.

    Prints diagnostics + ``sys.exit(1)`` on miss or ambiguity.
    """
    client = get_client()
    hit = client.resolve_setting_strict(value, org=org)
    if hit is None:
        scope = _scope_phrase(org)
        print(
            f"Error: no Setting with id starting with {value!r}{scope}",
            file=sys.stderr,
        )
        sys.exit(1)
    if isinstance(hit, list):
        print(
            f"Error: ambiguous prefix {value!r} matches {len(hit)} Settings:",
            file=sys.stderr,
        )
        for r in hit:
            sid = r.get("id", "")
            sset = r.get("set_id", "")
            skey = r.get("key", "")
            print(f"  {sid}  {sset}  key={skey}", file=sys.stderr)
        sys.exit(1)
    return hit["id"]


def _resolve_set_key(set_id: str, key: str, *, org: str | None) -> str:
    """Resolve ``(set_id, key)`` to the winning base Setting id.

    Uses ``client.read_set`` so the answer matches what consumers see.
    Errors with ``sys.exit(1)`` if no member matches.
    """
    client = get_client()
    members = client.read_set(set_id, org=org)
    for m in members.members:
        if m.key == key:
            return m.id
    scope = _scope_phrase(org)
    print(
        f"Error: no Setting with set_id={set_id!r} key={key!r}{scope}",
        file=sys.stderr,
    )
    sys.exit(1)


# ── list / members / show / read ────────────────────────────


def _org(args):
    """Return the explicit ``--org`` slug or :data:`ops.CALLER_ORG`.

    The Settings public API requires ``org=`` (auto-cfb8u). When the CLI
    operator did not pass ``--org``, the historical behavior was the
    env-cascade (``GRAPH_ORG`` env → scopeless default). The
    :data:`CALLER_ORG` sentinel preserves that behavior.
    """
    val = getattr(args, "org", None)
    if val:
        return val
    from . import ops
    return ops.CALLER_ORG


def cmd_set_list(args) -> None:
    set_ids = get_client().list_set_ids(org=_org(args))
    if not set_ids:
        print("(no Settings yet)")
        return
    for s in set_ids:
        print(s)


def _resolve_read_flags(args) -> tuple[int | None, int | None, int | None, bool]:
    """Returns (target_revision, min_revision, stored_revision, no_upconvert)."""
    return (
        getattr(args, "as_rev", None),
        getattr(args, "min_rev", None),
        getattr(args, "stored_rev", None),
        bool(getattr(args, "no_upconvert", False)),
    )


def cmd_set_members(args) -> None:
    target, minrev, stored, no_upconvert = _resolve_read_flags(args)
    if no_upconvert:
        target = None  # explicit "show stored shape"
    members = get_client().read_set(
        args.set_id, target_revision=target, min_revision=minrev,
        org=_org(args),
    )
    rows = []
    for m in members.members:
        if stored is not None and m.stored_revision != stored:
            continue
        target_disp = (
            str(m.target_revision) if m.target_revision is not None else "-"
        )
        rows.append({
            "id": m.id[:11],
            "key": m.key,
            "org": m.org or "-",
            "stored_rev": str(m.stored_revision),
            "target_rev": target_disp,
            "state": m.state,
        })
    if not rows:
        print(f"(no Settings in {args.set_id})")
    else:
        # ORG is the DB each row is surfaced from — the caller org or one of
        # its peers. It matters because resolvers differ on peer visibility
        # (e.g. capability enable/install read the workspace's own org ONLY,
        # so a row surfaced here from a peer will NOT resolve for a workspace
        # in a different org). Pin with --only-org to see one DB in isolation.
        _print_table(rows, [
            ("id", "ID", 12),
            ("key", "KEY", 28),
            ("org", "ORG", 16),
            ("stored_rev", "STORED", 6),
            ("target_rev", "TARGET", 6),
            ("state", "STATE", 10),
        ])
    if any(members.dropped.values()):
        print()
        print(f"Dropped: {members.dropped.to_dict()}")


def _fmt_signed_at(ms) -> str:
    if not ms:
        return "-"
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%SZ"
    )


def cmd_set_contested(args) -> None:
    """Contended slots: keys where more than one member's signed slot sits
    at the winning rung and store. Stats across sets by default; per-slot
    detail for one exact set_id."""
    import fnmatch

    client = get_client()
    org = _org(args)
    target = getattr(args, "set_id", None)
    key_glob = getattr(args, "key", None)
    limit = getattr(args, "limit", None)
    is_glob = bool(target) and any(c in target for c in "*?[")
    detail = bool(target) and not is_glob

    if detail:
        set_ids = [target]
    else:
        set_ids = client.list_set_ids(org=org)
        if target:
            set_ids = [s for s in set_ids if fnmatch.fnmatch(s, target)]

    per_set: list[tuple[str, list[dict]]] = []
    for set_id in sorted(set_ids):
        entries = client.contested_keys(set_id, org=org) or []
        if key_glob:
            entries = [
                e for e in entries if fnmatch.fnmatch(e.get("key", ""), key_glob)
            ]
        if entries:
            per_set.append((set_id, entries))

    if getattr(args, "json", False):
        print(json.dumps(
            {set_id: entries for set_id, entries in per_set}, indent=2,
        ))
        return

    if not per_set:
        scope = target or "any set"
        print(f"(no contested keys in {scope})")
        return

    if detail:
        entries = per_set[0][1]
        shown = entries[:limit] if limit else entries
        for entry in shown:
            print(f"{entry['key']}  ({len(entry['slots'])} slots)")
            for slot in entry["slots"]:
                marker = "→" if slot.get("resolves") else " "
                persona = (slot.get("terminal_persona") or "")[:16]
                print(
                    f"  {marker} {persona:<16}  {slot.get('state', '-'):<10}"
                    f"  {_fmt_signed_at(slot.get('signed_at')):<21}"
                    f"  {slot.get('org') or '-'}"
                )
        if limit and len(entries) > limit:
            more = len(entries) - limit
            noun = "key" if more == 1 else "keys"
            print(f"… {more} more contested {noun} (raise --limit)")
        return

    rows = []
    for set_id, entries in per_set:
        slots = [s for e in entries for s in e["slots"]]
        rows.append({
            "set_id": set_id,
            "keys": str(len(entries)),
            "slots": str(len(slots)),
            "signers": str(len({s.get("terminal_persona") for s in slots})),
            "latest": _fmt_signed_at(
                max((s.get("signed_at") or 0) for s in slots)
            ),
        })
    shown = rows[:limit] if limit else rows
    _print_table(shown, [
        ("set_id", "SET_ID", 40),
        ("keys", "KEYS", 5),
        ("slots", "SLOTS", 5),
        ("signers", "SIGNERS", 7),
        ("latest", "LATEST SIGNED", 20),
    ])
    if limit and len(rows) > limit:
        more = len(rows) - limit
        noun = "set" if more == 1 else "sets"
        print(f"… {more} more {noun} (raise --limit)")
    print()
    print("One exact set_id for per-slot detail; globs and --key filter.")


def cmd_set_show(args) -> None:
    target, _, _, no_upconvert = _resolve_read_flags(args)
    if no_upconvert:
        target = None
    sid = _resolve_address(args)
    got = get_client().get_setting(
        sid, target_revision=target, org=_org(args),
    )
    if got is None:
        suffix = " (or dropped by --as-rev)" if target is not None else ""
        print(f"Error: Setting not found: {sid}{suffix}",
              file=sys.stderr)
        sys.exit(1)
    out = {
        "id": got.id,
        "set_id": got.set_id,
        "stored_revision": got.stored_revision,
        "target_revision": got.target_revision,
        "key": got.key,
        "state": got.state,
        "supersedes": got.supersedes,
        "excludes": got.excludes,
        "deprecated": got.deprecated,
        "successor_id": got.successor_id,
        "created_at": got.created_at,
        "updated_at": got.updated_at,
        "payload": got.payload,
    }
    print(json.dumps(out, indent=2))


def _print_composition(set_id: str, key: str, org) -> None:
    """Say, on stderr, how many rows produced the value just printed.

    Every read surface returns a merged payload, so the store presents as a
    dictionary of key to value while it is really rows and layers. Hundreds
    of readings teach the dictionary, and then a write appears to do nothing
    and there is nowhere to look -- not a gap in anyone's knowledge, a gap in
    what anything will tell them.

    One line, and only when there is more than one row, so the common case
    stays quiet. On stderr so piping the payload into a file or `jq` is
    unaffected: the value is the output, this is commentary about it.
    """
    try:
        from tools.graph import settings_ops
        layers = settings_ops.layers_for(set_id, key, org=org)
    except Exception:
        return
    parts = []
    if layers.get("overrides"):
        parts.append(f"{len(layers['overrides'])} override(s)")
    if layers.get("deprecated"):
        parts.append(f"{len(layers['deprecated'])} retired row(s), not applied")
    if not parts:
        return
    print(f"  composed from a base plus {', '.join(parts)} — "
          f"`graph set layers {set_id} --key {key}` shows each",
          file=sys.stderr)


def cmd_set_read(args) -> None:
    """``graph set read <id-or-prefix> | <set_id> <key>`` — resolved payload.

    Mirrors ``graph read <src_id>`` semantics: returns the effective
    content the consumer sees at runtime. With ``--chain``, walks the
    supersedes chain and shows per-layer contributions.
    """
    parts: list[str] = list(getattr(args, "id_parts", []) or [])
    org = _org(args)
    if not parts:
        print("Error: missing positional argument: id (or set_id key)",
              file=sys.stderr)
        sys.exit(1)
    if len(parts) > 2:
        print(f"Error: too many positional arguments: {parts!r}",
              file=sys.stderr)
        sys.exit(1)

    chain_mode = bool(getattr(args, "chain", False))

    if len(parts) == 2:
        set_id, key = parts
        # A read never needs a revision — read_set, the vault-open dispatch,
        # and chain_setting all key on the bare set name. Tolerate a `#<rev>`
        # suffix (the form the docs and `graph set add` use) instead of
        # letting it silently miss declared_vault_tier and fall through to a
        # failing plain read.
        set_id = set_id.split("#", 1)[0]
    else:
        # Single positional — resolve to row, then derive (set_id, key).
        client = get_client()
        hit = client.resolve_setting_strict(parts[0], org=org)
        if hit is None:
            scope = _scope_phrase(org)
            print(
                f"Error: no Setting with id starting with {parts[0]!r}{scope}",
                file=sys.stderr,
            )
            sys.exit(1)
        if isinstance(hit, list):
            print(
                f"Error: ambiguous prefix {parts[0]!r} matches "
                f"{len(hit)} Settings:",
                file=sys.stderr,
            )
            for r in hit:
                print(f"  {r.get('id','')}  {r.get('set_id','')}  "
                      f"key={r.get('key','')}", file=sys.stderr)
            sys.exit(1)
        set_id = hit["set_id"]
        key = hit["key"]

    if chain_mode:
        chain = get_client().chain_setting(set_id, key, org=org)
        if chain is None:
            scope = _scope_phrase(org)
            print(
                f"Error: no member matches ({set_id!r}, {key!r}){scope}",
                file=sys.stderr,
            )
            sys.exit(1)
        print(json.dumps(chain, indent=2))
        return

    client = get_client()
    # An organization-scoped caller cannot enumerate or directly read a
    # personal-homed set.  For a schema that explicitly declares the derived
    # org namespace, go straight to the narrow approval/release seam: the
    # server turns this suffix into ``<bearer-org>:<suffix>`` and returns only
    # a ramfs receipt after the operator ceremony.
    from tools.graph import schemas
    if (
        schemas.declared_vault_tier(set_id) == "secured"
        and schemas.declared_home(set_id) == "personal"
        and schemas.declared_org_writeback_key_strategy(set_id)
        == "org_slug:credential_name"
    ):
        opener = getattr(client, "request_vault_open", None)
        if opener is None:
            print(
                "Error: secured Settings require dashboard approval; "
                "retry without --force-host",
                file=sys.stderr,
            )
            sys.exit(1)
        receipt = opener(set_id, key, org=org,
                          ttl_seconds=int(getattr(args, "ttl", 0) or 0))
        print(receipt["path"])
        return
    members = client.read_set(set_id, org=org)
    for m in members.members:
        if m.key == key:
            # A vault secret that did not open has no payload, and printing
            # its `null` would read as "this setting's value is null" — the
            # one thing a refusal must never look like.
            failure = getattr(m, "vault_error", None)
            if failure is not None:
                print(
                    f"Error: {failure.reason}: {failure.message}", file=sys.stderr,
                )
                sys.exit(1)
            sealed = getattr(m, "sealed_content_key", None)
            if sealed is not None:
                # A secured read is a delivered release, not a key-export
                # operation.  The normal HTTP client creates a vault_open
                # rendezvous; direct-host recovery mode deliberately cannot
                # bypass the human ceremony or print the sealed CEK.
                opener = getattr(client, "request_vault_open", None)
                if opener is None:
                    print(
                        "Error: secured Settings require dashboard approval; "
                        "retry without --force-host",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                receipt = opener(set_id, key, org=org,
                                 ttl_seconds=int(getattr(args, "ttl", 0) or 0))
                # The common tool result contains only a path.  Secret bytes
                # remain in the session's non-swappable ramfs and therefore do
                # not enter the transcript/model context.
                print(receipt["path"])
                return
            print(json.dumps(m.payload, indent=2, default=str))
            _print_composition(set_id, key, org)
            return
    scope = _scope_phrase(org)
    print(
        f"Error: no member matches ({set_id!r}, {key!r}){scope}",
        file=sys.stderr,
    )
    sys.exit(1)


# ── add / override / exclude ────────────────────────────────


def _report_shadowed_write(set_id: str, key: str, client=None) -> None:
    """Say loudly when the row just written is not the row that will be read.

    A row stored below the row resolution returns is written, reports
    success, and is read by nothing -- so silence here means the caller
    believes they changed a value they did not change.

    The report comes from the WRITE RESPONSE, because the write does not
    happen here. With ``GRAPH_API`` set it happens in the dashboard, and an
    earlier version read a module-global that only an in-process write
    populates -- so this printed nothing, for every container session, for
    every setting, since the day it was written. The server had detected the
    condition and put it in the response; the response was thrown away.

    The in-process fallback is for ``--force-host``, where the write really
    did happen here and there is no response to read.
    """
    message = None
    report = getattr(client, "last_write_report", None)
    if isinstance(report, dict):
        shadowed = report.get("shadowed_by")
        if isinstance(shadowed, dict):
            message = shadowed.get("message") or str(shadowed)
    if message is None:
        try:
            from tools.graph import settings_ops
            shadow = settings_ops.take_shadowed_write(set_id, key)
        except Exception:
            return
        if shadow is None:
            return
        message = str(shadow)
    print("")
    print("  ****************************************************************")
    print(f"  {message}")
    print("  ****************************************************************")
    print("")


def _report_unresolved_references(set_id: str, rev: int, payload, org) -> None:
    """Say which referenced keys are not provisioned yet, right after the write.

    A field that declares a reference names a key in another set. Whether that
    key exists is knowable at the moment the row is written, and the moment it
    is written is when someone is in a position to do something about it --
    rather than at launch, as a capability that quietly does not work.

    Reported, never fatal: writing the row before provisioning what it names is
    a legitimate order to work in. The point is that nobody has to remember to
    check.
    """
    try:
        from tools.graph import settings_ops
        missing = settings_ops.unresolved_references(
            set_id, rev, payload, org=settings_ops._resolve_settings_caller(org),
        )
    except Exception:
        return
    unknown = [(t, k) for t, k in missing if k.startswith("<no schema")]
    provisionable = [(t, k) for t, k in missing if not k.startswith("<no schema")]
    for target, key in provisionable:
        print(f"  ! not provisioned: {target}  key={key}")
    if provisionable:
        target, key = provisionable[0]
        print(f"    provision with: graph set add {target}#1 "
              f"--key {key} --from <file>")
        # Which store answered. This read goes to the databases THIS process
        # can open, and a container's are not the host's -- so "not
        # provisioned" here can mean "provisioned somewhere I cannot see".
        # Naming the frame is the difference between a fact and a guess
        # wearing a fact's clothes.
        print(f"    (looked in the databases this process can open; from a "
              f"container that is not the host's store)")
    for target, _ in unknown:
        print(f"  ! this row references {target}, and no schema for it is "
              f"registered here.")
        print(f"    Nothing can satisfy that: a write to an unregistered "
              f"schema is refused. Either the")
        print(f"    reference names a set that does not exist, or that set's "
              f"module is not imported")
        print(f"    in this process.")


def cmd_set_layers(args) -> None:
    """Every stored row behind one resolved value, and what each contributes.

    The command the other messages point at. Every read surface returns a
    merged payload, so when a write appears to do nothing there is nowhere
    to look -- not a gap in anyone's knowledge, a gap in what anything will
    tell them. This is the view where a base, its overrides, the fields each
    one changes, and any row that resolves to nothing are all visible at
    once.
    """
    from tools.graph import settings_ops

    set_id = args.set_at_rev.split("#", 1)[0]
    org = _org(args) or "personal"
    layers = settings_ops.layers_for(set_id, args.key, org=org)

    if layers["base"] is None and not layers["orphans"]:
        print(f"  no rows under {set_id} key={args.key!r} in {org!r}")
        return

    if layers["base"] is not None:
        base = layers["base"]
        print(f"  base       {base['id']}  [{base['state']}]  "
              f"rev {base['schema_revision']}")
        for name in sorted(base["payload"]):
            print(f"               {name} = "
                  f"{json.dumps(base['payload'][name], default=str)[:70]}")

    for entry in layers["overrides"]:
        print(f"  override   {entry['id']}  [{entry['state']}]"
              f"{'  DEPRECATED' if entry['deprecated'] else ''}")
        print(f"               changes {', '.join(entry['changes']) or '(nothing)'}")

    for entry in layers["deprecated"]:
        kind = "override" if entry["is_override"] else "base"
        print(f"  retired    {entry['id']}  [{entry['state']}]  {kind} — "
              f"not applied by resolution")

    for entry in layers["orphans"]:
        print(f"  ORPHAN     {entry['id']}  [{entry['state']}]  supersedes "
              f"{entry['supersedes'][:11]}, which is not here — resolves to "
              f"nothing and is only reachable by this id")

    if layers["resolved"] is not None:
        print(f"\n  resolved   {json.dumps(layers['resolved'], sort_keys=True, default=str)[:600]}")


def cmd_set_orphans(args) -> None:
    """Rows in a set whose key names an entity that has no row."""
    from tools.graph import settings_ops

    set_id = args.set_at_rev.split("#", 1)[0]
    org = _org(args) or "personal"
    findings = settings_ops.orphans_of(set_id, org=org)
    if not findings:
        print(f"  ✓ {set_id} — every key names something that exists")
        return
    print(f"  {len(findings)} orphaned key(s) in {set_id}:\n")
    for finding in findings:
        print(f"      {finding.address}")
        print(f"        {finding.detail}")
    sys.exit(1)


def cmd_set_band_audit(args) -> None:
    """Report every stored settings row whose publication_state is outside its
    schema's declared band — the estate-wide compliance sweep.

    Run on the HOST (it opens every org DB plus the local stores directly). A
    finding above the band max is a cross-org disclosure; below the min is a
    federation outage. Exit 1 when anything is noncompliant, so CI/cron can gate.
    """
    from tools.graph import settings_ops

    only = getattr(args, "org", None)
    findings = settings_ops.audit_publication_bands(org=only)
    if getattr(args, "json", False):
        print(json.dumps(findings, indent=2))
        sys.exit(1 if findings else 0)
    if not findings:
        scope = f"store {only}" if only else "every store"
        print(f"  ✓ {scope}: every settings row is within its declared publication band")
        return
    print(f"  {len(findings)} row(s) outside the declared band:\n")
    for f in findings:
        band = f["band"] if f["band"] is not None else "(none declared)"
        print(f"      {f['store']} · {f['set_id']}#{f['revision']} · key={f['key']}")
        print(f"        state={f['state']}  band={band}  — {f['reason']}")
    sys.exit(1)


def cmd_set_check(args) -> None:
    """Is this row satisfied, and everything it declares it depends on?

    Metadata-driven end to end: it follows fields declaring ``references``
    and asks fields declaring ``exists``. Schemas may also contribute
    cross-row constraints whose lower-level validator is shared with runtime.
    """
    # A revision is not needed to check a row: the stored row carries its
    # own, and the check reads what is there rather than asserting a shape.
    set_id = args.set_at_rev.split("#", 1)[0]
    org = _org(args) or "personal"
    try:
        findings, satisfied = get_client().inspect_setting(
            set_id, args.key, org=org,
        )
    except Exception as exc:
        print(f"Error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)

    def _emit_satisfied() -> None:
        if not satisfied:
            return
        print("\n  Verified checks:")
        for item in satisfied:
            print(f"      ✓ {item.kind}: {item.detail}")

    if not findings:
        print(f"  ✓ {set_id} key={args.key} — satisfied, with everything it "
              f"declares it depends on")
        _emit_satisfied()
        return

    import textwrap

    blocking = [f for f in findings if f.severity != "advisory"]
    advisory = [f for f in findings if f.severity == "advisory"]

    def _emit(group, marker):
        for finding in group:
            print(f"  {marker} {finding.kind}")
            print(f"      at      {finding.address}")
            # A finding that says what to do about it is longer than one line,
            # and the instruction is the part worth reading.
            wrapped = textwrap.wrap(finding.detail, width=72) or [""]
            print(f"      what    {wrapped[0]}")
            for line in wrapped[1:]:
                print(f"              {line}")
            print(f"      looked  {finding.looked_in}")

    # Separated because they answer different questions. "What is unsatisfied"
    # and "can this run" are not the same list, and printing them as one made
    # a missing local clone source read exactly like a missing credential --
    # so the reader either treats every finding as fatal or learns to treat
    # none of them as fatal.
    if blocking:
        print(f"  {len(blocking)} unsatisfied requirement"
              f"{'' if len(blocking) == 1 else 's'} for {set_id} "
              f"key={args.key} — this cannot run until each is resolved:\n")
        _emit(blocking, "!")
    if advisory:
        if blocking:
            print("")
        print(f"  {len(advisory)} thing{'' if len(advisory) == 1 else 's'} "
              f"declared and not present, which do NOT stop it running:\n")
        _emit(advisory, "-")
    if not blocking:
        print(f"\n  ✓ nothing blocks {set_id} key={args.key}")
    _emit_satisfied()
    print("\n  The walk follows declared fields, keys, and schema readiness "
          "hooks only.\n  An undeclared relationship is not checked or "
          "reported.")
    # Exit status answers the question a script asks, which is whether this
    # can run -- not whether every declared thing is present.
    sys.exit(1 if blocking else 0)


def _report_effective_value(set_id: str, key: str, org, written: dict) -> None:
    """Say what readers will see, every time, not only when it surprises.

    "Setting written" and "this is the value now" are different facts, and
    only the second is what the caller came for. Reporting the row alone let
    a write land, print success, and change nothing anyone reads -- and the
    only way to find out was to go looking, which is a thing people do after
    they have already stopped trusting the output.

    Printed unconditionally. A report that appears only when something is
    wrong teaches the reader that silence means agreement, and silence is
    exactly what the failing case produced.
    """
    try:
        from tools.graph import settings_ops
        row = settings_ops.read_set_key(set_id, key, org=org)
    except Exception:
        return
    if row is None:
        print("      effective: nothing — this key does not resolve")
        return
    effective = row.get("payload")
    if effective == written:
        print("      effective: exactly what you wrote")
        return
    differing = sorted(
        name for name in set(written) | set(effective or {})
        if (written.get(name) != (effective or {}).get(name))
    )
    print(f"      effective: differs from what you wrote in "
          f"{', '.join(differing)}")
    print(f"                 {json.dumps(effective, sort_keys=True)[:300]}")


def cmd_set_add(args) -> None:
    set_id, rev = _parse_set_at_rev(args.set_at_rev)
    payload = _resolve_payload_input(args)
    client = get_client()
    try:
        sid = client.add_setting(
            set_id, rev, args.key, payload, state=args.state,
            org=_org(args),
            vault_policy_class_id=getattr(args, "policy_class", None),
        )
    except SchemaValidationError as e:
        # Surface the validator's message verbatim — schemas already name
        # the failing field, expected shape, and observed value. Wrapping
        # with a generic prefix would mask that signal.
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  ✓ Setting: {sid[:11]}  {set_id}#{rev}  key={args.key}  [{args.state}]")
    _report_effective_value(set_id, args.key, _org(args), payload)
    _report_shadowed_write(set_id, args.key, client)
    _report_unresolved_references(set_id, rev, payload, _org(args))


def _read_limited_secret(stream) -> bytearray:
    value = bytearray(stream.read(_MAX_SECRET_BYTES + 1))
    if len(value) > _MAX_SECRET_BYTES:
        value[:] = b"\x00" * len(value)
        print(
            f"Error: secret exceeds {_MAX_SECRET_BYTES} bytes",
            file=sys.stderr,
        )
        sys.exit(1)
    return value


def _read_secret_bytes(args) -> bytearray:
    """Read one secret without accepting it in argv or an environment value."""
    source = getattr(args, "secret_file", None)
    fd = getattr(args, "secret_fd", None)
    prompt = bool(getattr(args, "secret_prompt", False))
    if prompt:
        value = bytearray(getpass.getpass("Secret value: ").encode("utf-8"))
        if len(value) > _MAX_SECRET_BYTES:
            value[:] = b"\x00" * len(value)
            print(
                f"Error: secret exceeds {_MAX_SECRET_BYTES} bytes",
                file=sys.stderr,
            )
            sys.exit(1)
        return value
    if fd is not None:
        if fd < 0:
            print("Error: --from-fd must be non-negative", file=sys.stderr)
            sys.exit(1)
        with os.fdopen(os.dup(fd), "rb") as stream:
            return _read_limited_secret(stream)
    if source is not None:
        if source == "-":
            return _read_limited_secret(sys.stdin.buffer)
        with Path(source).open("rb") as stream:
            return _read_limited_secret(stream)
    if not sys.stdin.isatty():
        return _read_limited_secret(sys.stdin.buffer)
    print(
        "Error: no secret supplied — use --from-file PATH, --from-fd N, "
        "--prompt, or pipe the value on stdin",
        file=sys.stderr,
    )
    sys.exit(1)


def cmd_set_seal(args) -> None:
    """Write one personal raw value without argv/env or cross-store access."""
    from .schemas.vault_credential import (
        VAULT_CREDENTIAL_REVISION,
        VAULT_SECURED_SET_ID,
    )

    secret = _read_secret_bytes(args)
    payload: dict[str, str] = {}
    try:
        if not secret:
            print("Error: refusing to vault an empty secret", file=sys.stderr)
            sys.exit(1)
        try:
            payload["value"] = secret.decode("utf-8")
        except UnicodeDecodeError:
            print(
                "Error: secured Setting values are UTF-8 text; encode binary "
                "material before sealing",
                file=sys.stderr,
            )
            sys.exit(1)
        client = get_client()
        if hasattr(client, "seal_personal_setting"):
            if getattr(args, "org", None) not in (None, "personal"):
                print(
                    "Error: graph set seal currently targets the personal "
                    "credential store; organization-held vault schemas are "
                    "not available yet",
                    file=sys.stderr,
                )
                sys.exit(1)
            sid = client.seal_personal_setting(
                args.key,
                payload["value"],
                policy_class_id=args.policy_class,
            )
        else:
            # Disaster-recovery direct mode has no HTTP routing layer. Keep
            # the same Settings primitive while the caller explicitly owns
            # local store selection.
            sid = client.add_setting(
                VAULT_SECURED_SET_ID,
                VAULT_CREDENTIAL_REVISION,
                args.key,
                payload,
                state="raw",
                org=_org(args),
                vault_policy_class_id=args.policy_class,
            )
    finally:
        payload["value"] = ""
        secret[:] = b"\x00" * len(secret)
    if not isinstance(sid, str) or not sid:
        print("Error: secured Setting write returned no identifier", file=sys.stderr)
        sys.exit(1)
    report = getattr(client, "last_write_report", None)
    stored_key = report.get("key") if isinstance(report, dict) else args.key
    stored_policy = (
        report.get("policy_class_id") if isinstance(report, dict) else None
    ) or args.policy_class
    print(
        f"  ✓ Secured Setting: {sid[:11]}  key={stored_key}  "
        f"policy-class={stored_policy}"
    )


def cmd_set_override(args) -> None:
    payload = _resolve_payload_input(args)
    target_id = _resolve_target_address(args)
    client = get_client()
    try:
        sid = client.override_setting(
            target_id, payload, state=args.state,
            org=_org(args),
        )
    except LookupError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except SchemaValidationError as e:
        # Surface the validator's message verbatim (see cmd_set_add).
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  ✓ Override: {sid[:11]}  supersedes={target_id[:11]}  [{args.state}]")


def cmd_set_exclude(args) -> None:
    target_id = _resolve_target_address(args)
    try:
        sid = get_client().exclude_setting(
            target_id, state=args.state, org=_org(args),
        )
    except LookupError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  ✓ Exclude: {sid[:11]}  excludes={target_id[:11]}  [{args.state}]")


def _resolve_target_address(args) -> str:
    """Resolve the target positional (id-or-prefix or set_id+key) for
    override/exclude. The two argparse positionals come in as ``target``
    and (optionally) ``target_key``."""
    target = getattr(args, "target", None)
    target_key = getattr(args, "target_key", None)
    org = _org(args)
    if not target:
        print("Error: missing target id (or set_id key)", file=sys.stderr)
        sys.exit(1)
    if target_key is not None:
        return _resolve_set_key(target, target_key, org=org)
    return _resolve_id_or_prefix(target, org=org)


# ── promote / deprecate / remove ────────────────────────────


def cmd_set_promote(args) -> None:
    if args.to not in _VALID_PROMOTION_STATES:
        print(
            f"Error: --to must be one of {_VALID_PROMOTION_STATES}, got {args.to!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    sid = _resolve_address(args)
    try:
        get_client().promote_setting(sid, args.to, org=_org(args))
    except LookupError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  ✓ Promoted: {sid[:11]} → {args.to}")


def cmd_set_deprecate(args) -> None:
    sid = _resolve_address(args)
    try:
        get_client().deprecate_setting(
            sid, successor_id=args.successor,
            org=_org(args),
        )
    except LookupError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    suc = f" successor={args.successor[:11]}" if args.successor else ""
    print(f"  ✓ Deprecated: {sid[:11]}{suc}")


def cmd_set_undeprecate(args) -> None:
    sid = _resolve_address(args)
    try:
        get_client().undeprecate_setting(sid, org=_org(args))
    except LookupError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  ✓ Undeprecated: {sid[:11]}")


def _report_rows_left_for(set_id: str, key: str, org) -> None:
    """After removing one row, say what is still under that key.

    A key can hold several rows, and removing one reported unqualified
    success while the rest stayed. Worse, addressing runs through
    resolution: once a base is gone the survivors resolve to nothing, so
    the same command that had just left them behind then answered "no
    Setting with that key" while holding one. The store denying its own
    contents is worse than saying nothing.

    So the count is reported, and anything left is named by id -- which is
    the only address that still works once a row stops resolving.
    """
    try:
        from tools.graph import settings_ops
        layers = settings_ops.layers_for(set_id, key, org=org)
    except Exception:
        return
    leftover = list(layers.get("overrides") or []) + list(
        layers.get("orphans") or [])
    if layers.get("base") is None and not leftover:
        return
    if layers.get("base") is None and leftover:
        print(f"      ! the base is gone and {len(leftover)} row(s) remain "
              f"under this key. They resolve to nothing and cannot be "
              f"addressed by key — remove them by id:")
        for entry in leftover:
            print(f"          {entry['id']}")
        return
    if leftover:
        print(f"      {len(leftover)} row(s) still under this key")


def cmd_set_remove(args) -> None:
    # Removing BY KEY is refused when the key is ambiguous. Resolution picks
    # the winning row, and during a revision migration the winner is the row
    # you just migrated TO — so "remove the old row by key" silently deleted
    # the new one and left the legacy row resolving again (host finding,
    # 2026-08-30). More than one live base under the key → name an id.
    parts = list(getattr(args, "id_parts", []) or [])
    if len(parts) == 2:
        # A vault credential's stored key carries a server-derived org
        # namespace the caller never types (and must not guess), so removal
        # goes through the same routing seam seal and vault_open use: the
        # bare name in, the server derives the rest.
        from tools.graph import schemas
        vault_set_id = parts[0].split("#", 1)[0]
        if (
            schemas.declared_vault_tier(vault_set_id) in ("secured", "audited")
            and schemas.declared_home(vault_set_id) == "personal"
        ):
            remover = getattr(get_client(), "remove_vault_credential", None)
            if remover is not None:
                from .client import GraphHttpError
                try:
                    receipt = remover(vault_set_id, parts[1], org=_org(args))
                except (LookupError, ValueError) as e:
                    print(f"Error: {e}", file=sys.stderr)
                    sys.exit(1)
                except GraphHttpError as e:
                    detail = (e.body or {}).get("error") or str(e)
                    print(f"Error: {detail}", file=sys.stderr)
                    sys.exit(1)
                removed_key = (
                    receipt.get("key") if isinstance(receipt, dict) else None
                ) or parts[1]
                print(f"  ✓ Removed vault credential: {removed_key}")
                return
            # Disaster-recovery direct mode has no vault routing seam; the
            # caller explicitly owns local store selection, so fall through
            # to the generic path.
    if len(parts) == 2:
        try:
            from tools.graph import settings_ops
            layers = settings_ops.layers_for(parts[0], parts[1], org=_org(args))
        except Exception:
            layers = None
        shadowed = (layers or {}).get("shadowed_bases") or []
        if shadowed:
            base = (layers or {}).get("base") or {}
            print(
                f"Error: {parts[1]!r} holds {1 + len(shadowed)} live base "
                f"row(s); removing 'whichever resolves' is never what a "
                f"migration wants. Name the id:",
                file=sys.stderr,
            )
            for entry in [base, *shadowed]:
                if entry.get("id"):
                    print(
                        f"    {entry['id']}  rev={entry.get('schema_revision')} "
                        f" [{entry.get('state')}]"
                        + ("  <- currently resolves" if entry is base else ""),
                        file=sys.stderr,
                    )
            sys.exit(1)
    sid = _resolve_address(args)
    # Captured BEFORE the removal: afterwards the row is gone and, if it was
    # the base, its key stops resolving -- so there would be nothing left to
    # ask what else is under it.
    try:
        _row = get_client().resolve_setting_strict(sid, org=_org(args))
        if isinstance(_row, list):
            _row = None
    except Exception:
        _row = None
    try:
        get_client().remove_setting(sid, org=_org(args))
    except LookupError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  ✓ Removed: {sid[:11]}")
    if _row is not None:
        _report_rows_left_for(_row["set_id"], _row["key"], _org(args))


# ── schema / example / find ─────────────────────────────────


def _split_schema_addr(spec: str) -> tuple[str, int | None]:
    """Parse ``autonomy.workspace`` or ``autonomy.workspace#1`` into
    ``(set_id, revision-or-None)``. Errors with ``sys.exit(1)`` if the
    revision suffix is malformed.
    """
    if "#" not in spec:
        return spec, None
    set_id, rev = spec.rsplit("#", 1)
    try:
        return set_id, int(rev)
    except ValueError:
        print(f"Error: revision must be an integer in {spec!r}",
              file=sys.stderr)
        sys.exit(1)


def _resolve_schema_member(
    spec: str, *, org: str | None,
):
    """Look up the ``autonomy.schema`` Setting that holds *spec*'s json-schema.

    *spec* is ``set_id`` (latest revision wins) or ``set_id#rev`` (exact).
    On miss prints to stderr and exits 1; on success returns the
    :class:`ResolvedSetting` member whose payload is the json-schema.
    """
    set_id, rev = _split_schema_addr(spec)
    members = get_client().read_set("autonomy.schema", org=org)
    matches = [
        m for m in members.members
        if m.key.startswith(f"{set_id}#")
    ]
    if not matches:
        scope = _scope_phrase(org)
        print(
            f"Error: no schema registered for {set_id!r}{scope}",
            file=sys.stderr,
        )
        sys.exit(1)
    if rev is None:
        # Latest revision wins. Tie-break is unlikely (revisions are unique
        # per set_id), but sort numerically just in case.
        matches.sort(key=lambda m: _key_revision(m.key))
        return matches[-1]
    target_key = f"{set_id}#{rev}"
    chosen = next((m for m in matches if m.key == target_key), None)
    if chosen is None:
        print(
            f"Error: no schema for {target_key} (registered: "
            f"{[m.key for m in matches]})",
            file=sys.stderr,
        )
        sys.exit(1)
    return chosen


def _key_revision(key: str) -> int:
    """Pull the integer revision out of a ``set_id#N`` key, defaulting to 0."""
    try:
        return int(key.rsplit("#", 1)[-1])
    except (ValueError, IndexError):
        return 0


def _format_property(
    name: str, prop: dict, *, indent: str = "    ",
) -> list[str]:
    """Render one property as a list of pretty-printed lines."""
    type_str = prop.get("type", "any")
    parts = [f"{name} ({type_str})"]
    if "enum" in prop:
        parts.append(f"[enum: {', '.join(map(str, prop['enum']))}]")
    if "default" in prop:
        parts.append(f"[default: {json.dumps(prop['default'])}]")
    head = f"{indent}{' '.join(parts)}"
    lines = [head]
    desc = prop.get("description")
    if desc:
        lines.append(f"{indent}  {desc}")
    element = prop.get("element")
    if isinstance(element, dict):
        # Two flavors of element shape:
        #   1) a per-field dict of {field_name: meta_dict}
        #   2) a single shape dict (e.g. {"type": "string"})
        if all(isinstance(v, dict) for v in element.values()):
            lines.append(f"{indent}  Element shape:")
            for sub_name, sub_meta in element.items():
                sub_lines = _format_property(
                    sub_name, sub_meta, indent=indent + "    ",
                )
                # Mark required sub-fields inline.
                if sub_meta.get("required"):
                    sub_lines[0] = sub_lines[0] + "  [required]"
                lines.extend(sub_lines)
        else:
            lines.append(f"{indent}  Element type: {element.get('type','any')}")
    return lines


def _print_schema(key: str, payload: Any) -> None:
    """Pretty-print a json-schema payload for ``graph set schema``."""
    if not isinstance(payload, dict):
        print(json.dumps(payload, indent=2))
        return
    set_id = payload.get("set_id", key.rsplit("#", 1)[0])
    rev = payload.get("schema_revision", _key_revision(key))
    properties: dict = payload.get("properties") or {}
    required_list: list = list(payload.get("required") or [])
    required_set = set(required_list)

    print(f"{set_id}#{rev}")
    print()
    if not properties:
        print("  (no fields declared in _field_metadata)")
        return

    # Walk the canonical ``required`` list first to preserve the schema's
    # declared field order — sorting is for storage, not for display.
    req_props = [(n, properties[n]) for n in required_list if n in properties]
    opt_props = sorted(
        ((n, p) for n, p in properties.items() if n not in required_set),
        key=lambda kv: kv[0],
    )

    if req_props:
        print("  Required fields:")
        for n, p in req_props:
            for line in _format_property(n, p):
                print(line)
        print()
    if opt_props:
        print("  Optional fields:")
        for n, p in opt_props:
            for line in _format_property(n, p):
                print(line)


def cmd_set_schema(args) -> None:
    member = _resolve_schema_member(args.spec, org=_org(args))
    _print_schema(member.key, member.payload)


def _placeholder(name: str, prop: dict) -> Any:
    """Pick a placeholder value for ``set example`` JSON output.

    Order: explicit ``default`` > first ``enum`` value > type-driven
    placeholder (``"<name>"`` for strings; type-appropriate empty for
    arrays/objects/booleans/numbers).
    """
    if "default" in prop:
        return prop["default"]
    if prop.get("enum"):
        return prop["enum"][0]
    t = prop.get("type", "string")
    if t == "string":
        return f"<{name}>"
    if t == "integer":
        return 0
    if t == "number":
        return 0
    if t == "boolean":
        return False
    if t == "array":
        # If the element shape declares required sub-fields, hand the
        # operator a single populated stub element instead of an empty
        # list. Required-array fields with rich element shapes are the
        # ones operators actually need a starter for (e.g. workspace
        # `repos` -> `[{"url": "<url>", "mount": "<mount>"}]`).
        element = prop.get("element")
        if (isinstance(element, dict)
                and all(isinstance(v, dict) for v in element.values())
                and any(v.get("required") for v in element.values())):
            stub = {}
            for sub_name, sub_meta in element.items():
                if sub_meta.get("required"):
                    stub[sub_name] = _placeholder(sub_name, sub_meta)
            return [stub]
        return []
    if t == "object":
        return {}
    return None


def _build_example(json_schema: dict) -> dict:
    """Build a stub payload from an exported json-schema.

    Includes only required fields — optional fields are omitted so the
    output is a minimal valid starting point. Operators add optional
    fields by reading ``set schema`` for descriptions.
    """
    out: dict = {}
    required = json_schema.get("required") or []
    properties = json_schema.get("properties") or {}
    for name in required:
        prop = properties.get(name) or {}
        out[name] = _placeholder(name, prop)
    return out


def cmd_set_example(args) -> None:
    member = _resolve_schema_member(args.spec, org=_org(args))
    payload = member.payload if isinstance(member.payload, dict) else {}
    stub = _build_example(payload)
    print(json.dumps(stub, indent=2))


def _haystack_for_synopsis(
    key: str,
    synopsis: Any,
    schema_payload: Any,
) -> str:
    """Build the lowercase haystack used by ``set find`` ranking.

    Combines (a) the synopsis's summary + nouns + related_set_ids and
    (b) flattened schema field names, descriptions, and enum values.
    Schema-side text is what lets ``find harness codex`` hit
    ``autonomy.workspace#1`` even though "codex" only appears as an
    enum value.
    """
    parts: list[str] = [key.lower()]
    if isinstance(synopsis, dict):
        s = synopsis.get("summary")
        if isinstance(s, str):
            parts.append(s.lower())
        for n in synopsis.get("nouns") or []:
            if isinstance(n, str):
                parts.append(n.lower())
        for r in synopsis.get("related_set_ids") or []:
            if isinstance(r, str):
                parts.append(r.lower())
    if isinstance(schema_payload, dict):
        for name, prop in (schema_payload.get("properties") or {}).items():
            parts.append(str(name).lower())
            if isinstance(prop, dict):
                d = prop.get("description")
                if isinstance(d, str):
                    parts.append(d.lower())
                for ev in prop.get("enum") or []:
                    parts.append(str(ev).lower())
    return " ".join(parts)


def _all_terms_score(haystack: str, terms: list[str]) -> int:
    """Return total occurrence count if ALL terms appear, else 0."""
    score = 0
    for t in terms:
        c = haystack.count(t)
        if c == 0:
            return 0
        score += c
    return score


def cmd_set_find(args) -> None:
    terms = [t.lower() for t in (args.terms or []) if t]
    if not terms:
        print("Error: at least one search term required", file=sys.stderr)
        sys.exit(1)
    org = _org(args)
    syn = get_client().read_set("autonomy.schema.synopsis", org=org)
    sch = get_client().read_set("autonomy.schema", org=org)
    schema_by_key = {
        m.key: m.payload for m in sch.members if isinstance(m.payload, dict)
    }
    ranked: list[tuple[int, str, str]] = []
    for m in syn.members:
        haystack = _haystack_for_synopsis(
            m.key, m.payload, schema_by_key.get(m.key),
        )
        score = _all_terms_score(haystack, terms)
        if score > 0:
            summary = ""
            if isinstance(m.payload, dict):
                summary = m.payload.get("summary") or ""
            ranked.append((score, m.key, summary))
    if not ranked:
        print(f"(no schema matches: {' '.join(terms)})")
        return
    # Highest score first; ties broken by key for deterministic output.
    ranked.sort(key=lambda r: (-r[0], r[1]))
    width = max(len(k) for _, k, _ in ranked)
    for _score, key, summary in ranked:
        print(f"{key:<{width}}  {summary}")


# ── migrate ─────────────────────────────────────────────────


def cmd_set_migrate(args) -> None:
    try:
        report = get_client().migrate_setting_revisions(
            args.set_id, args.to_rev, dry_run=args.dry_run,
            org=_org(args),
        )
    except Exception as e:  # noqa: BLE001 — surface to operator
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    mode = "DRY RUN" if args.dry_run else "WRITE"
    print(f"[{mode}] migrate {args.set_id} → rev {args.to_rev}")
    print(f"  rewrote:            {report.rewrote}")
    print(f"  already_at_target:  {report.already_at_target}")
    print(f"  above_target:       {report.above_target}")
    print(f"  no_upconvert_path:  {report.no_upconvert_path}")
    if report.affected_ids:
        print("  affected:")
        for sid in report.affected_ids[:20]:
            print(f"    {sid}")
        if len(report.affected_ids) > 20:
            print(f"    ... ({len(report.affected_ids) - 20} more)")


# ── argparse setup ──────────────────────────────────────────


def add_read_flags(parser) -> None:
    parser.add_argument("--as-rev", type=int, dest="as_rev",
                        help="Target revision: shape every returned row at rev N")
    parser.add_argument("--min-rev", type=int, dest="min_rev",
                        help="Floor: drop rows with stored_revision < N")
    parser.add_argument("--stored-rev", type=int, dest="stored_rev",
                        help="Filter: only rows whose stored_revision is exactly N")
    parser.add_argument("--no-upconvert", action="store_true",
                        dest="no_upconvert",
                        help="Show stored payloads (the default; explicit form)")


def _add_org_arg(parser) -> None:
    parser.add_argument(
        "--org", dest="org", default=None,
        help="Route to the per-org DB at data/orgs/<slug>.db "
             "(default: autonomy / GRAPH_DB env)",
    )


def _add_address_arg(parser, *, help_text: str) -> None:
    """Wire the dual positional ``id [key]`` form onto a subparser."""
    parser.add_argument(
        "id_parts", nargs="+", metavar="ID",
        help=help_text,
    )


def attach_set_subparser(sub) -> None:
    """Wire up ``graph set ...`` subcommands onto an existing subparsers obj."""
    p_set = sub.add_parser(
        "set",
        help="Settings primitive: layered configuration (graph://0d3f750f-f9c)",
    )
    set_sub = p_set.add_subparsers(dest="set_subcmd", required=True)

    # list
    p_list = set_sub.add_parser("list", help="List known set_ids visible to caller")
    _add_org_arg(p_list)
    p_list.set_defaults(func=cmd_set_list)

    # members
    p_members = set_sub.add_parser(
        "members", help="List resolved members of a SET (default: stored revisions)",
    )
    p_members.add_argument("set_id")
    add_read_flags(p_members)
    _add_org_arg(p_members)
    p_members.set_defaults(func=cmd_set_members)

    # contested — contended slot report (auto-y2ubq)
    p_contested = set_sub.add_parser(
        "contested",
        help="Keys where members' signed slots contest the resolved value",
    )
    p_contested.add_argument(
        "set_id", nargs="?", default=None, metavar="SET_ID",
        help="Exact set_id for per-slot detail; a glob (e.g. 'autonomy.org.*') "
             "or nothing for high-level stats across sets",
    )
    p_contested.add_argument(
        "--key", metavar="GLOB",
        help="Only keys matching this glob (e.g. 'workspace:*')",
    )
    p_contested.add_argument("--limit", type=int, help="Cap output rows")
    p_contested.add_argument(
        "--json", action="store_true", help="Raw JSON instead of tables",
    )
    _add_org_arg(p_contested)
    p_contested.set_defaults(func=cmd_set_contested)

    # show
    p_show = set_sub.add_parser("show", help="Show a single Setting in detail")
    _add_address_arg(
        p_show,
        help_text="Setting id (full or unique prefix), or `set_id key`",
    )
    add_read_flags(p_show)
    _add_org_arg(p_show)
    p_show.set_defaults(func=cmd_set_show)

    # read — resolved payload by (set_id, key) or id-prefix
    p_read = set_sub.add_parser(
        "read",
        help="Resolved effective payload (post-merge) for a Setting member",
    )
    _add_address_arg(
        p_read,
        help_text="`set_id key`, or a Setting id (full or unique prefix)",
    )
    p_read.add_argument(
        "--chain", action="store_true", dest="chain",
        help="Show per-layer contributions through the supersedes chain",
    )
    p_read.add_argument(
        "--ttl", type=int, default=0, dest="ttl", metavar="SECONDS",
        help="For a secured-vault read: how long the delivered credential "
             "lives in the session ramfs. 0 (default) = the full container "
             "lifespan (destroyed only when the container stops); a positive "
             "value destroys the file that many seconds after delivery.",
    )
    _add_org_arg(p_read)
    p_read.set_defaults(func=cmd_set_read)

    # seal — raw secret input, deliberately unavailable through argv/env.
    p_seal = set_sub.add_parser(
        "seal",
        help="Store a raw value in the personal human-gated secured vault",
        description=(
            "Read a UTF-8 secret from a file descriptor, file, hidden prompt, "
            "or stdin and seal it as autonomy.vault.secured#1. The secret is "
            "never accepted as a command-line or environment value."
        ),
    )
    p_seal.add_argument("--key", required=True, help="Stable credential name")
    p_seal.add_argument(
        "--policy-class",
        default="personal-root",
        help=(
            "Policy class whose public sealing key receives this value "
            "(default: personal-root)"
        ),
    )
    secret_source = p_seal.add_mutually_exclusive_group()
    secret_source.add_argument(
        "--from-file", dest="secret_file", metavar="PATH",
        help="Read exact bytes from PATH ('-' means stdin)",
    )
    secret_source.add_argument(
        "--from-fd", dest="secret_fd", type=int, metavar="N",
        help="Read exact bytes from an already-open file descriptor",
    )
    secret_source.add_argument(
        "--prompt", dest="secret_prompt", action="store_true",
        help="Read one hidden line interactively (not suitable for multiline keys)",
    )
    p_seal.add_argument(
        "--org",
        choices=("personal",),
        help=(
            "Explicitly name the personal destination (the default). "
            "Organization-held vault schemas are not available yet."
        ),
    )
    p_seal.set_defaults(func=cmd_set_seal)

    # add
    p_add = set_sub.add_parser("add", help="Create a base Setting")
    p_add.add_argument("set_at_rev",
                       help="set_id#schema_revision, e.g. autonomy.workspace#1")
    p_add.add_argument("--key", required=True, help="Identity within (set_id, this DB)")

    p_check = set_sub.add_parser(
        "check",
        help="Ask the dashboard to verify a row and its declared dependencies",
        description=(
            "Run the dashboard's schema-driven readiness inspection for one "
            "Setting row. The same inspection feeds organization workspace "
            "health; this command renders its failures and positive evidence."
        ),
    )
    p_check.add_argument("set_at_rev", metavar="set_id[#rev]")
    p_check.add_argument("--key", required=True, help="Which row to check")
    p_check.add_argument("--org", default=None, help="Organization to read as")
    p_check.set_defaults(func=cmd_set_check)

    p_orphans = set_sub.add_parser(
        "orphans",
        help="Rows whose key names an entity that no longer exists",
    )
    p_orphans.add_argument("set_at_rev", metavar="set_id[#rev]")
    p_orphans.add_argument("--org", default=None, help="Organization to read as")
    p_orphans.set_defaults(func=cmd_set_orphans)

    # band-audit — estate-wide publication-band compliance sweep
    p_band = set_sub.add_parser(
        "band-audit",
        help="Report settings rows whose publication_state is outside the "
             "schema's declared band (host-run; scans every store)",
    )
    p_band.add_argument(
        "--org", default=None,
        help="Scan a single store slug instead of every store on the machine",
    )
    p_band.add_argument(
        "--json", action="store_true", help="Emit findings as JSON",
    )
    p_band.set_defaults(func=cmd_set_band_audit)

    p_layers = set_sub.add_parser(
        "layers",
        help="Every stored row behind one resolved value, and what each adds",
    )
    p_layers.add_argument("set_at_rev", metavar="set_id[#rev]")
    p_layers.add_argument("--key", required=True, help="Which key to open up")
    p_layers.add_argument("--org", default=None, help="Organization to read as")
    p_layers.set_defaults(func=cmd_set_layers)
    p_add.add_argument("--from", dest="from_file",
                       help="Path to JSON or YAML payload file (use '-' for stdin)")
    p_add.add_argument("--inline", dest="inline",
                       help="Inline JSON object (mutually exclusive with --from)")
    p_add.add_argument("--state", default="raw",
                       choices=("raw", "curated", "published", "canonical"))
    _add_org_arg(p_add)
    p_add.set_defaults(func=cmd_set_add)

    # override
    p_over = set_sub.add_parser("override", help="Create an override Setting")
    p_over.add_argument(
        "target", metavar="ID",
        help="Target Setting id (full or unique prefix)",
    )
    p_over.add_argument(
        "target_key", metavar="KEY", nargs="?",
        help="Optional: when given, treat the previous arg as set_id and "
             "look up by (set_id, key)",
    )
    p_over.add_argument("--from", dest="from_file",
                        help="Path to partial JSON/YAML payload (use '-' for stdin)")
    p_over.add_argument("--inline", dest="inline",
                        help="Inline JSON object (mutually exclusive with --from)")
    p_over.add_argument("--state", default="raw",
                        choices=("raw", "curated", "published", "canonical"))
    _add_org_arg(p_over)
    p_over.set_defaults(func=cmd_set_override)

    # exclude
    p_excl = set_sub.add_parser("exclude", help="Create an exclude Setting")
    p_excl.add_argument(
        "target", metavar="ID",
        help="Target Setting id (full or unique prefix)",
    )
    p_excl.add_argument(
        "target_key", metavar="KEY", nargs="?",
        help="Optional: when given, treat the previous arg as set_id and "
             "look up by (set_id, key)",
    )
    p_excl.add_argument("--state", default="raw",
                        choices=("raw", "curated", "published", "canonical"))
    _add_org_arg(p_excl)
    p_excl.set_defaults(func=cmd_set_exclude)

    # promote
    p_prom = set_sub.add_parser("promote", help="Transition publication_state")
    _add_address_arg(
        p_prom,
        help_text="Setting id (full or unique prefix), or `set_id key`",
    )
    p_prom.add_argument("--to", required=True,
                        choices=_VALID_PROMOTION_STATES)
    _add_org_arg(p_prom)
    p_prom.set_defaults(func=cmd_set_promote)

    # deprecate
    p_dep = set_sub.add_parser("deprecate", help="Mark a Setting deprecated")
    _add_address_arg(
        p_dep,
        help_text="Setting id (full or unique prefix), or `set_id key`",
    )
    p_dep.add_argument("--successor", help="Optional successor Setting id")
    _add_org_arg(p_dep)
    p_dep.set_defaults(func=cmd_set_deprecate)

    # undeprecate
    p_undep = set_sub.add_parser(
        "undeprecate", help="Reverse a previous deprecate"
    )
    _add_address_arg(
        p_undep,
        help_text="Setting id (full or unique prefix), or `set_id key`",
    )
    _add_org_arg(p_undep)
    p_undep.set_defaults(func=cmd_set_undeprecate)

    # remove
    p_rem = set_sub.add_parser("remove", help="Hard-delete a raw Setting")
    _add_address_arg(
        p_rem,
        help_text="Setting id (full or unique prefix), or `set_id key`",
    )
    _add_org_arg(p_rem)
    p_rem.set_defaults(func=cmd_set_remove)

    # schema — pretty-print field shape for a registered schema
    p_schema = set_sub.add_parser(
        "schema",
        help="Print the registered schema for set_id (or set_id#rev)",
    )
    p_schema.add_argument(
        "spec", metavar="SET_ID[#REV]",
        help="Schema set_id (latest revision) or set_id#rev (exact)",
    )
    _add_org_arg(p_schema)
    p_schema.set_defaults(func=cmd_set_schema)

    # example — emit a JSON stub matching the schema's required fields
    p_example = set_sub.add_parser(
        "example",
        help="Emit a stub JSON payload for a registered schema",
    )
    p_example.add_argument(
        "spec", metavar="SET_ID[#REV]",
        help="Schema set_id (latest revision) or set_id#rev (exact)",
    )
    _add_org_arg(p_example)
    p_example.set_defaults(func=cmd_set_example)

    # find — search curated synopses for a noun-layer match
    p_find = set_sub.add_parser(
        "find",
        help="Find schemas by noun: searches synopsis + schema fields",
    )
    p_find.add_argument("terms", nargs="+", metavar="TERM",
                        help="One or more search terms (all must match)")
    _add_org_arg(p_find)
    p_find.set_defaults(func=cmd_set_find)

    # migrate
    p_mig = set_sub.add_parser(
        "migrate", help="Rewrite stored rows up to a target revision",
    )
    p_mig.add_argument("set_id")
    p_mig.add_argument("--to-rev", type=int, required=True, dest="to_rev")
    p_mig.add_argument("--dry-run", action="store_true", dest="dry_run")
    _add_org_arg(p_mig)
    p_mig.set_defaults(func=cmd_set_migrate)

    # typegen — schema-derived TypeScript codegen
    from .typegen_cmd import attach_typegen_subparser
    attach_typegen_subparser(set_sub)
