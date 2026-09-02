"""The perf package must stay invisible to every plain test selection.

The opt-in benchmark suite (tools/network/fleet_sync/perf) is excluded
from default sweeps, directory runs, and agent-test's changed-line plan by
one structural rule: it contains no pytest-collectable test files.  The
changed-plan globs ``test_*.py`` repo-wide and pytest only collects
``test_*.py``/``*_test.py``, so keeping such names out of perf/ is the
whole guarantee.  This tripwire fails the plain suite the moment someone
adds one — without importing (and therefore without executing) anything
under perf/.
"""

import tomllib
from pathlib import Path

PERF_DIR = Path(__file__).resolve().parents[1] / "perf"
REPO_ROOT = Path(__file__).resolve().parents[4]


def test_perf_package_has_no_collectable_test_files() -> None:
    assert PERF_DIR.is_dir(), "perf package moved without updating this guard"
    collectable = sorted(
        path.relative_to(PERF_DIR).as_posix()
        for pattern in ("test_*.py", "*_test.py")
        for path in PERF_DIR.rglob(pattern)
    )
    assert collectable == [], (
        "pytest-collectable files inside the opt-in perf package would put "
        f"benchmarks into plain and changed-plan runs: {collectable}"
    )


def test_perf_package_is_not_a_default_testpath() -> None:
    config = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    testpaths = config["tool"]["pytest"]["ini_options"]["testpaths"]
    offenders = [
        entry for entry in testpaths
        if "fleet_sync/perf" in entry or entry.endswith("fleet_sync")
    ]
    assert offenders == [], (
        f"default testpaths must never reach the perf package: {offenders}"
    )
