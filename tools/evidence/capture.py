"""Evidence capture runner — drive a real UI flow, capture each step, emit a manifest.

One capture = one directory:

    <out>/row-<NNN>/<capture_id>/
        manifest.json      # the evidence-manifest contract (provisional v1)
        steps/01-<slug>.png
        gallery.html       # static step gallery, relative image refs

The flow is described by a JSON spec (see specs/*.json). Each step is a list of
agent-browser argv fragments followed by one screenshot. There are no fixed
sleeps anywhere in this runner: waiting is done through agent-browser's own
condition waits (`wait --load`, `wait --text`, `wait <selector>`), which fail
loudly on timeout.

Usage:
    python -m tools.evidence.capture <flow-spec.json> [--out DIR] [--keep-open]

Exit status is non-zero if any step fails; the failing step's screenshot is
still captured and recorded in the manifest with status "failed" — a failure
capture is evidence too.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_VERSION = 1


def run_ab(args: list[str], session: str) -> subprocess.CompletedProcess:
    """Run one agent-browser command in the capture's dedicated session."""
    cmd = ["agent-browser", *args, "--session", session]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def load_spec(path: Path) -> dict:
    spec = json.loads(path.read_text())
    for key in ("row", "row_title", "base_url", "steps"):
        if key not in spec:
            raise SystemExit(f"flow spec missing required key: {key}")
    return spec


def make_capture_id(row: int, out_root: Path) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    base = f"e{row}-{stamp}"
    n = 1
    while (out_root / f"row-{row:03d}" / f"{base}-{n:02d}").exists():
        n += 1
    return f"{base}-{n:02d}"


def substitute(fragment: str, spec: dict) -> str:
    """Expand ``{base}`` and ``{env:NAME}`` placeholders in an action fragment.

    ``{env:NAME}`` lets an orchestrator hand the runner values that only
    exist mid-ceremony (an approval id, a just-minted link URL) without
    them being baked into the committed spec.
    """
    out = fragment.replace("{base}", spec["base_url"])
    for name in re.findall(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}", out):
        value = os.environ.get(name)
        if value is None:
            raise SystemExit(f"flow spec needs environment variable {name}, which is unset")
        out = out.replace("{env:" + name + "}", value)
    return out


def write_gallery(capture_dir: Path, manifest: dict) -> None:
    inst = manifest["instance"]
    is_mock = inst.get("kind") != "real"
    banner = (
        '<div style="background:#7a5c14;color:#ffe9b0;padding:.5rem 1rem;'
        'font-weight:700">MOCK INSTANCE — this gallery is illustrative, not proof</div>'
        if is_mock
        else ""
    )
    cards = []
    for step in manifest["steps"]:
        status = step.get("status", "ok")
        badge = (
            '<span style="color:#e08a8a;font-weight:700"> — FAILED</span>'
            if status == "failed"
            else ""
        )
        cards.append(
            f'<figure style="margin:0 0 1.5rem;background:#171e26;border:1px solid #2a3540;'
            f'border-radius:10px;overflow:hidden">'
            f'<img src="{step["file"]}" alt="{step["caption"]}" style="width:100%;display:block">'
            f'<figcaption style="padding:.6rem .9rem;color:#d6dde4">'
            f'<strong>Step {step["seq"]}</strong>{badge} — {step["caption"]}</figcaption></figure>'
        )
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Row {manifest["row"]} — {manifest["row_title"]}</title></head>
<body style="margin:0;background:#0f1419;font:16px/1.5 system-ui,sans-serif">
{banner}
<main style="max-width:56rem;margin:0 auto;padding:1rem">
<h1 style="color:#d6dde4;font-size:1.2rem">Row {manifest["row"]} — {manifest["row_title"]}</h1>
<p style="color:#8b98a5">Captured {manifest["captured_at"]} · instance {inst.get("base_url", "?")}
· commit {inst.get("commit") or "unknown"} · driver {manifest["driver"]}</p>
{"".join(cards)}
</main></body></html>
"""
    (capture_dir / "gallery.html").write_text(html)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("spec", type=Path)
    ap.add_argument("--out", type=Path, default=Path("/workspace/output/evidence"))
    ap.add_argument(
        "--keep-open",
        action="store_true",
        help="leave the browser session open for post-mortem inspection",
    )
    ap.add_argument(
        "--continue-capture",
        type=Path,
        metavar="CAPTURE_DIR",
        help="append this spec's steps to an existing capture (multi-phase "
        "ceremonies whose later steps depend on values minted mid-flow)",
    )
    args = ap.parse_args()

    spec = load_spec(args.spec)
    row = int(spec["row"])
    if args.continue_capture:
        capture_dir = args.continue_capture
        manifest = json.loads((capture_dir / "manifest.json").read_text())
        capture_id = manifest["capture_id"]
        steps_dir = capture_dir / "steps"
        start_seq = len(manifest["steps"]) + 1
    else:
        capture_id = spec.get("capture_id") or make_capture_id(row, args.out)
        capture_dir = args.out / f"row-{row:03d}" / capture_id
        steps_dir = capture_dir / "steps"
        steps_dir.mkdir(parents=True, exist_ok=True)
        start_seq = 1
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "row": row,
            "row_title": spec["row_title"],
            "capture_id": capture_id,
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": spec.get("kind", "step_gallery"),
            "instance": spec.get(
                "instance", {"kind": "real", "base_url": spec["base_url"], "commit": None}
            ),
            "driver": "agent-browser",
            "steps": [],
            "artifacts": [],
            "provenance": {"session": spec.get("provenance_session")},
        }
    session = spec.get("session", f"evidence-{capture_id}")

    failed = False
    for i, step in enumerate(spec["steps"], start=start_seq):
        slug = step.get("slug", f"step{i}")
        shot = steps_dir / f"{i:02d}-{slug}.png"
        status = "ok"
        for action in step.get("actions", []):
            argv = [substitute(a, spec) for a in action]
            proc = run_ab(argv, session)
            if proc.returncode != 0:
                status = "failed"
                step_err = proc.stderr.strip() or proc.stdout.strip()
                print(
                    f"step {i} ({slug}): agent-browser {' '.join(argv)} failed: {step_err}",
                    file=sys.stderr,
                )
                break
        shot_proc = run_ab(["screenshot", str(shot)], session)
        if shot_proc.returncode != 0 and status == "ok":
            status = "failed"
            print(f"step {i} ({slug}): screenshot failed: {shot_proc.stderr.strip()}", file=sys.stderr)
        entry = {
            "seq": i,
            "file": f"steps/{shot.name}",
            "caption": step["caption"],
        }
        if status != "ok":
            entry["status"] = "failed"
        manifest["steps"].append(entry)
        if status == "failed":
            failed = True
            break

    if not args.keep_open:
        run_ab(["close"], session)

    (capture_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    write_gallery(capture_dir, manifest)
    print(capture_dir)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
