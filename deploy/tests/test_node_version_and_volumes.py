"""Contract tests for auto-m7vh7: the three daemon volume names are pinned, and
the image self-stamps its version from the checkout's .git at build time (no
build arg, nothing passed in, no .git in the final image)."""

from __future__ import annotations

from pathlib import Path

import re
import yaml


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "docker-compose.yml"
DOCKERFILE = ROOT / "deploy" / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"

FORBIDDEN_ALIASES = ("autonomy-state", "autonomy-mounts", "autonomy-workspace-data")
EXPECTED_MOUNTS = {
    "autonomy-code": "/app",
    "autonomy-data": "/app/data",
    "autonomy-orgs": "/app/orgs",
}


def _compose():
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


# ── the three volumes ──────────────────────────────────────────────────────
def test_dashboard_mounts_the_three_volumes_at_the_locked_destinations():
    svc = _compose()["services"]["dashboard"]
    # volumes mixes short "src:dst[:opts]" strings with long-form dicts
    # (e.g. the rslave keycache bind); only the short named-volume entries
    # are candidates for the three locked mounts.
    mounts = {}
    for entry in svc["volumes"]:
        if isinstance(entry, str) and ":" in entry:
            src, dst = entry.split(":")[:2]
            mounts[src] = dst
    for vol, dest in EXPECTED_MOUNTS.items():
        assert mounts.get(vol) == dest, f"{vol} must mount at {dest}, got {mounts.get(vol)}"


def test_daemon_volume_names_are_pinned_not_project_scoped():
    """Explicit `name:` so `docker volume inspect autonomy-orgs` (the resolver's
    translation primitive) resolves, instead of `<project>_autonomy-orgs`."""
    vols = _compose()["volumes"]
    for vol in EXPECTED_MOUNTS:
        assert vol in vols, f"{vol} missing from top-level volumes"
        assert (vols[vol] or {}).get("name") == vol, (
            f"{vol} must set an explicit `name: {vol}` to pin the daemon name"
        )


def test_no_forbidden_volume_aliases_anywhere_in_compose():
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    for alias in FORBIDDEN_ALIASES:
        assert alias not in text, f"stale volume alias {alias!r} present in compose"


# ── the self-stamped version ───────────────────────────────────────────────
def test_compose_passes_no_version_build_arg():
    """The version is not passed in — the build reads it from .git itself."""
    args = _compose()["services"]["dashboard"]["build"].get("args", {})
    assert "AUTONOMY_VERSION" not in args
    assert "AUTONOMY_BUILD_TIME" not in args


def test_dockerignore_does_not_exclude_dot_git():
    """The builder stage needs .git in the context to read the commit; a line
    that excludes it (not a comment mentioning it) would blind the build."""
    lines = [
        ln.strip()
        for ln in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert ".git" not in lines, ".dockerignore must not exclude .git (build reads it)"


def test_dockerfile_self_stamps_commit_and_date_and_drops_git():
    df = DOCKERFILE.read_text(encoding="utf-8")
    # multi-stage: a named builder stage, and the runtime /app comes from it.
    assert "AS appsrc" in df, "expected a builder stage that reads .git"
    assert re.search(
        r"COPY (--chown=\S+ )?--from=appsrc /app /app", df
    ), "runtime /app must come from the builder"
    # reads BOTH the commit hash and the commit date from the checkout.
    assert "rev-parse HEAD" in df
    assert "show -s --format=%cI HEAD" in df
    assert "commit=%s" in df and "commit_date=%s" in df
    # KEEPS .git (the launcher clones REPO_ROOT for session worktrees —
    # NODE-VOLUME-MODEL.md) but severs the remote so the node never phones
    # home, and never takes a version arg.
    assert "remote remove origin" in df
    assert "rm -rf /app/.git" not in df
    assert "ARG AUTONOMY_VERSION" not in df
    assert "ARG AUTONOMY_BUILD_TIME" not in df
    # The runtime (last) stage must not `COPY . /app` — that would drag .git back
    # into the final image. The builder stage does, and that's fine. Its /app
    # comes only from the cleaned builder stage.
    runtime_stage = "FROM " + df.split("\nFROM ")[-1]
    assert "COPY . /app" not in runtime_stage, "runtime stage must not COPY the raw context"
    assert re.search(
        r"COPY (--chown=\S+ )?--from=appsrc /app /app", runtime_stage
    ), "runtime /app must come from the cleaned builder stage"


def test_no_build_wrapper_script_remains():
    """The self-stamp removes any need for a manual build wrapper."""
    assert not (ROOT / "deploy" / "build.sh").exists()
