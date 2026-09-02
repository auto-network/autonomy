"""CLI for the opt-in fleet sync performance suite.

Run the suite and compare against the retained baseline:

    .venv/bin/python -m tools.network.fleet_sync.perf run --scale quick
    .venv/bin/python -m tools.network.fleet_sync.perf run             # full

Compare two result files offline:

    .venv/bin/python -m tools.network.fleet_sync.perf compare A.json B.json

Exit status reflects benchmark *errors* only; regressions are reported in
the table but never gate.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from . import baseline as baseline_mod
from .suite import BENCHMARKS, SCALES, run_benchmark


def _run(args: argparse.Namespace) -> int:
    scale = SCALES[args.scale]
    names = args.only or list(BENCHMARKS)
    unknown = sorted(set(names) - set(BENCHMARKS))
    if unknown:
        print(f"unknown benchmark(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    store = Path(args.baseline_dir) if args.baseline_dir else (
        baseline_mod.default_store()
    )
    previous = None
    reference = (
        Path(args.baseline) if args.baseline
        else baseline_mod.baseline_path(store, scale.name)
    )
    if reference.is_file():
        previous = baseline_mod.load_result(reference)

    result = baseline_mod.new_result(scale.name)
    scratch = Path(tempfile.mkdtemp(prefix="fleet-sync-perf-"))
    try:
        for name in names:
            print(f"running {name} ({scale.name}) …", flush=True)
            result["benchmarks"][name] = run_benchmark(name, scratch, scale)
            entry = result["benchmarks"][name]
            note = entry.get("reason") or entry.get("error") or (
                f"{entry['duration_s']:.1f}s"
            )
            print(f"  {name}: {entry['status']} ({note})", flush=True)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    print()
    rows = baseline_mod.compare(previous, result)
    print(baseline_mod.render_table(rows, previous))

    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\nresult JSON: {output}")
    if args.no_retain:
        print("baseline not updated (--no-retain)")
    else:
        # Only a complete pass may become the reference other runs compare
        # against; partial (--only) runs would silently blank benchmarks.
        errors = [n for n, e in result["benchmarks"].items()
                  if e["status"] == "error"]
        if args.only:
            print("baseline not updated (partial --only run)")
        elif errors:
            print(f"baseline not updated (errors in: {', '.join(errors)})")
        else:
            history, promoted = baseline_mod.retain(store, result)
            print(f"retained: {history}\nbaseline: {promoted}")
    return 1 if any(
        e["status"] == "error" for e in result["benchmarks"].values()
    ) else 0


def _compare(args: argparse.Namespace) -> int:
    previous = baseline_mod.load_result(Path(args.baseline_file))
    current = baseline_mod.load_result(Path(args.current_file))
    rows = baseline_mod.compare(previous, current)
    print(baseline_mod.render_table(rows, previous))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools.network.fleet_sync.perf")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the suite and compare")
    run.add_argument("--scale", choices=sorted(SCALES), default="full")
    run.add_argument(
        "--only", action="append", metavar="NAME",
        help=f"run a subset (repeatable): {', '.join(BENCHMARKS)}",
    )
    run.add_argument("--baseline", help="explicit result JSON to compare against")
    run.add_argument("--baseline-dir", help="override the retained-baseline store")
    run.add_argument("--output", help="also write the result JSON here")
    run.add_argument("--no-retain", action="store_true",
                     help="do not promote this run to the retained baseline")
    run.set_defaults(handler=_run)

    compare = sub.add_parser("compare", help="compare two result files")
    compare.add_argument("baseline_file")
    compare.add_argument("current_file")
    compare.set_defaults(handler=_compare)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
