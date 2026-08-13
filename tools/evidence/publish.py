"""Publish a capture to the knowledge graph as the row's permanent evidence.

Per the mission controller's ruling (2026-08-13): captured evidence lives as
GRAPH ATTACHMENTS — content-addressed, searchable, surviving session teardown.
This step creates one graph note per capture carrying every step image as an
attachment, then writes the attachment ids back into the capture's
manifest.json so the manifest is the complete, portable index of the proof.

Run OUTSIDE any isolated-instance environment: the note must land in the real
org graph, not the test instance's stores.

Usage:
    python3 -m tools.evidence.publish <capture-dir> [--org autonomy]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("capture_dir", type=Path)
    ap.add_argument("--tags", default="evidence,register-row")
    args = ap.parse_args()

    manifest_path = args.capture_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    inst = manifest["instance"]

    lines = [
        f"# Evidence: register row {manifest['row']} — {manifest['row_title']}",
        "",
        f"Captured {manifest['captured_at']} by the evidence-capture pipeline "
        f"(driver {manifest['driver']}, capture id {manifest['capture_id']}) on a "
        f"{inst.get('kind', 'real')} instance at {inst.get('base_url', '?')}.",
        "",
    ]
    attach_args: list[str] = []
    for n, step in enumerate(manifest["steps"], start=1):
        path = args.capture_dir / step["file"]
        attach_args += ["--attach", str(path)]
        status = " (FAILED)" if step.get("status") == "failed" else ""
        lines.append(f"**Step {step['seq']}{status}** — {step['caption']}")
        lines.append("")
        lines.append(f"![step {step['seq']}]({{{n}}})")
        lines.append("")

    cmd = ["graph", "note", "-c", "-",
           "--tags", f"{args.tags},row-{manifest['row']}"] + attach_args
    proc = subprocess.run(cmd, input="\n".join(lines), text=True,
                          capture_output=True, timeout=120)
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        return 1
    out = proc.stdout
    print(out.strip())
    src_id = None
    for token in out.replace("(", " ").replace(")", " ").split():
        if token.startswith("src:"):
            src_id = token[len("src:"):]
    if not src_id:
        print("could not parse note source id from graph output", file=sys.stderr)
        return 1

    # The attachment store renames files, so map by order: attachments are
    # listed in the order they were attached, which is step order. Sizes are
    # cross-checked against the local files to catch any reordering.
    listing = subprocess.run(["graph", "attachments", src_id],
                             capture_output=True, text=True, timeout=60)
    att_rows = []
    for line in listing.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1].startswith("image/"):
            att_rows.append({"id": parts[0], "size": int(parts[2])})

    manifest["evidence_note"] = src_id
    if len(att_rows) == len(manifest["steps"]):
        for step, att in zip(manifest["steps"], att_rows):
            local_size = (args.capture_dir / step["file"]).stat().st_size
            if att["size"] == local_size:
                step["attachment"] = att["id"]
            else:
                print(f"size mismatch on step {step['seq']}: local {local_size} "
                      f"vs attachment {att['size']} — id not recorded", file=sys.stderr)
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"manifest updated: evidence_note={src_id}, "
          f"{sum(1 for s in manifest['steps'] if 'attachment' in s)}/"
          f"{len(manifest['steps'])} steps carry attachment ids")
    return 0


if __name__ == "__main__":
    sys.exit(main())
