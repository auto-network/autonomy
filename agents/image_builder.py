"""Build ``<org>/<workspace-id>`` session images from Settings.

The build input is the ``dockerfile`` field of
``autonomy.workspace.provision#1`` (resolved with the same per-field
personal shadow the launcher uses, so a personal Dockerfile builds a
personal variant on this operator's machines — coherent because docker
image stores are per-machine). The image name is derived from the org
and the row's key, never stored anywhere.

Everything else the build needs is derived too: rebuilds trigger on a
content-hash mismatch against the machine-homed
``autonomy.workspace.image-build#1`` status row, and the build context
is the Dockerfile ALONE — no repo tree, no sibling files. Startup
scripts are runtime-mounted now, which removes the old reason
per-project builds shipped the whole ``agents/projects/`` tree into the
context, and a minimal context is what keeps an org-writable Dockerfile
from reaching anything at build time.

A disk Dockerfile at ``agents/projects/<workspace>/Dockerfile`` and a
provision row for the same workspace is a hard error, never a silent
precedence: the migration is move-then-delete.

Runs host-side only (the dashboard worker, or
``agents/build.sh --from-settings`` → ``python -m agents.image_builder``).
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tools.graph import cross_org, ops

from agents.workspace_settings import PROVISION_SET_ID, resolve_provision

IMAGE_BUILD_SET_ID = "autonomy.workspace.image-build"
IMAGE_BUILD_REVISION = 1
BUILD_TIMEOUT_S = 1800


def derive_image_name(org: str, workspace_id: str) -> str:
    """``<org>/<workspace-id>`` — the whole naming scheme, from key alone."""
    return f"{org}/{workspace_id}"


@dataclass
class BuildResult:
    org: str
    workspace_id: str
    image: str
    action: str          # built | failed | skipped | collision
    detail: str = ""


def _status_row(org: str, workspace_id: str) -> dict:
    row = ops.read_set_key(
        IMAGE_BUILD_SET_ID, f"{org}:{workspace_id}",
        org="machine", peers=[],
    )
    return (row or {}).get("payload") or {}


def _write_status(org: str, workspace_id: str, payload: dict) -> None:
    ops.upsert_by_key(
        IMAGE_BUILD_SET_ID, IMAGE_BUILD_REVISION,
        f"{org}:{workspace_id}", payload, org="machine",
    )


def _docker(args: list[str], *, runner, timeout: int = BUILD_TIMEOUT_S):
    return runner(
        ["docker", *args],
        capture_output=True, text=True, timeout=timeout,
    )


# The background sweep worker and the launch-time building_image stage
# share one process; a per-image lock coalesces their build attempts —
# the second caller waits, re-reads the freshly-written status row, and
# its hash gate turns the duplicate build into a skip.
_image_locks: dict[str, threading.Lock] = {}
_image_locks_guard = threading.Lock()


def _lock_for(image: str) -> threading.Lock:
    with _image_locks_guard:
        return _image_locks.setdefault(image, threading.Lock())


def image_staleness(
    org: str,
    workspace_id: str,
    *,
    runner=subprocess.run,
) -> str | None:
    """Why this workspace's built image can't launch as-is, or None.

    None means either "current" or "this workspace doesn't build an
    image at all" — in both cases launch proceeds without the
    building_image stage. The check is cheap (two settings reads and a
    docker inspect), so the launch path can afford it every time.
    """
    dockerfile = resolve_provision(workspace_id, org=org).get("dockerfile")
    if not dockerfile:
        return None
    last = _status_row(org, workspace_id)
    content_hash = hashlib.sha256(dockerfile.encode()).hexdigest()
    if last.get("content_hash") != content_hash or not last.get("digest"):
        return "dockerfile changed since the last successful build here"
    image = derive_image_name(org, workspace_id)
    inspect = _docker(
        ["image", "inspect", "--format", "{{.Id}}", image],
        runner=runner, timeout=60,
    )
    if inspect.returncode != 0:
        return "image absent on this machine"
    return None


def build_workspace(
    org: str,
    workspace_id: str,
    *,
    repo_root: Path,
    force: bool = False,
    runner=subprocess.run,
) -> BuildResult | None:
    """Build one workspace's image if its resolved dockerfile changed.

    Returns None when the workspace has no dockerfile content at all.
    """
    dockerfile = resolve_provision(workspace_id, org=org).get("dockerfile")
    if not dockerfile:
        return None
    image = derive_image_name(org, workspace_id)
    with _lock_for(image):
        return _build_locked(
            org, workspace_id, image, dockerfile,
            repo_root=repo_root, force=force, runner=runner,
        )


def _build_locked(
    org: str,
    workspace_id: str,
    image: str,
    dockerfile: str,
    *,
    repo_root: Path,
    force: bool,
    runner,
) -> BuildResult:
    disk = repo_root / "agents" / "projects" / workspace_id / "Dockerfile"
    if disk.exists():
        detail = (
            f"{disk} still exists beside the provision row; migration is "
            f"move-then-delete, and two sources for one image never race"
        )
        _write_status(org, workspace_id, {
            "content_hash": hashlib.sha256(dockerfile.encode()).hexdigest(),
            "built_at": _now(),
            "error": detail,
        })
        return BuildResult(org, workspace_id, image, "collision", detail)

    content_hash = hashlib.sha256(dockerfile.encode()).hexdigest()
    last = _status_row(org, workspace_id)
    if not force and last.get("content_hash") == content_hash \
            and last.get("digest"):
        return BuildResult(org, workspace_id, image, "skipped", "unchanged")

    with tempfile.TemporaryDirectory(prefix="wsimg-") as ctx:
        (Path(ctx) / "Dockerfile").write_text(dockerfile)
        proc = _docker(
            ["build", "-t", image, "-f", str(Path(ctx) / "Dockerfile"), ctx],
            runner=runner,
        )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-2000:]
        _write_status(org, workspace_id, {
            "content_hash": content_hash,
            "built_at": _now(),
            "error": detail or "docker build failed",
        })
        return BuildResult(org, workspace_id, image, "failed", detail)

    inspect = _docker(
        ["image", "inspect", "--format", "{{.Id}}", image],
        runner=runner, timeout=60,
    )
    digest = (inspect.stdout or "").strip() or "unknown"
    _write_status(org, workspace_id, {
        "content_hash": content_hash,
        "built_at": _now(),
        "digest": digest,
    })
    return BuildResult(
        org, workspace_id, image, "built",
        digest + _image_drift(org, workspace_id, image),
    )


def _image_drift(org: str, workspace_id: str, image: str) -> str:
    """A workspace whose provision row builds an image should launch it.

    Cross-SET agreement can't live in schema validate (it sees one
    payload), so the builder reports it where the mismatch bites.
    """
    from tools.graph.schemas.workspace import WORKSPACE_SET_ID
    row = ops.read_set_key(WORKSPACE_SET_ID, workspace_id, org=org, peers=[])
    declared = ((row or {}).get("payload") or {}).get("image")
    if declared and declared != image:
        return (f"  DRIFT: workspace launches image {declared!r}, "
                f"not the {image!r} just built")
    return ""


def sweep(
    *,
    repo_root: Path,
    orgs: list[str] | None = None,
    force: bool = False,
    runner=subprocess.run,
) -> list[BuildResult]:
    """Build every changed workspace image across the given orgs.

    ``orgs=None`` sweeps every organization on this machine. The personal
    store is not swept as an org: personal rows act only as per-field
    shadows inside each org resolution, exactly as at launch.
    """
    results: list[BuildResult] = []
    for org in (orgs if orgs is not None else cross_org.list_org_slugs()):
        members = ops.read_set(PROVISION_SET_ID, org=org, peers=[]).members
        for member in members:
            result = build_workspace(
                org, member.key,
                repo_root=repo_root, force=force, runner=runner,
            )
            if result is not None:
                results.append(result)
    return results


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build <org>/<workspace-id> images from "
                    "autonomy.workspace.provision rows",
    )
    parser.add_argument("--org", help="sweep one org (default: all)")
    parser.add_argument("--workspace", help="build one workspace (needs --org)")
    parser.add_argument("--force", action="store_true",
                        help="rebuild even when the content hash is unchanged")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    if args.workspace:
        if not args.org:
            parser.error("--workspace requires --org")
        result = build_workspace(
            args.org, args.workspace, repo_root=repo_root, force=args.force,
        )
        results = [result] if result else []
        if not results:
            print(f"{args.org}/{args.workspace}: no dockerfile in its "
                  f"provision row")
    else:
        results = sweep(
            repo_root=repo_root,
            orgs=[args.org] if args.org else None,
            force=args.force,
        )
    for r in results:
        print(f"{r.action:9s} {r.image}  {r.detail[:100]}")
    return 1 if any(r.action in ("failed", "collision") for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
