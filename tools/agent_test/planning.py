"""Diff-aware test selection and changed-line coverage summaries."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .store import list_manifests, read_json


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_lines(repo: Path) -> dict[str, list[int]]:
    changed: dict[str, set[int]] = {}
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD", "--unified=0", "--no-color", "--", "*.py"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        current: str | None = None
        for line in result.stdout.splitlines():
            if line.startswith("+++ b/"):
                current = line[6:]
                changed.setdefault(current, set())
                continue
            match = _HUNK_RE.match(line)
            if current and match:
                start = int(match.group(1))
                count = int(match.group(2) or "1")
                changed[current].update(range(start, start + max(1, count)))
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", "*.py"],
            cwd=repo,
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout.split(b"\0")
        for raw in untracked:
            if not raw:
                continue
            name = raw.decode(errors="replace")
            try:
                count = len((repo / name).read_text(encoding="utf-8").splitlines())
            except OSError:
                continue
            changed[name] = set(range(1, count + 1))
    except (OSError, subprocess.SubprocessError):
        for path in repo.rglob("*.py"):
            if any(part in {".venv", "venv", "env"} for part in path.parts):
                continue
            try:
                count = len(path.read_text(encoding="utf-8").splitlines())
                changed[path.relative_to(repo).as_posix()] = set(range(1, count + 1))
            except OSError:
                continue
    return {name: sorted(lines) for name, lines in sorted(changed.items()) if lines}


def _test_files(repo: Path) -> list[Path]:
    return sorted(
        path
        for path in repo.rglob("test_*.py")
        if not any(part in {".venv", "venv", "env"} for part in path.parts)
    )[:5000]


def build_plan(repo: Path, state_root: Path) -> dict[str, Any]:
    changed = changed_lines(repo)
    candidates: dict[str, dict[str, Any]] = {}
    covered_files: set[str] = set()

    def add(selector: str, reason: str, confidence: int, source: str) -> None:
        record = candidates.setdefault(
            selector,
            {"selector": selector, "confidence": confidence, "reasons": [], "sources": set()},
        )
        record["confidence"] = max(record["confidence"], confidence)
        if reason not in record["reasons"]:
            record["reasons"].append(reason)
        record["sources"].add(source)
        covered_files.add(source)

    for source in changed:
        source_path = Path(source)
        if source_path.name.startswith("test_") or "tests" in source_path.parts:
            if (repo / source).is_file():
                add(source, "the test file itself changed", 100, source)
        stem = source_path.stem
        direct = [
            source_path.parent / "tests" / f"test_{stem}.py",
            source_path.parent / f"test_{stem}.py",
            Path("tests") / f"test_{stem}.py",
        ]
        for candidate in direct:
            if (repo / candidate).is_file():
                add(candidate.as_posix(), f"naming match for {source}", 80, source)

    changed_sets = {name: set(lines) for name, lines in changed.items()}
    for manifest in list_manifests(state_root)[:100]:
        coverage = read_json(Path(manifest["_directory"]) / "coverage.json", {})
        tests = coverage.get("tests", {}) if isinstance(coverage, dict) else {}
        if not isinstance(tests, dict):
            continue
        for nodeid, files in tests.items():
            if not isinstance(files, dict):
                continue
            for source, wanted in changed_sets.items():
                executed = set(files.get(source) or [])
                if executed & wanted:
                    add(str(nodeid), f"previously executed changed lines in {source}", 95, source)

    tests = _test_files(repo)
    cached_text: dict[Path, str] = {}
    for source in changed:
        if source in covered_files:
            continue
        module = source.removesuffix(".py").replace("/", ".")
        token = Path(source).stem
        reference_matches = 0
        for test_path in tests:
            try:
                if test_path not in cached_text:
                    cached_text[test_path] = test_path.read_text(encoding="utf-8")
                text = cached_text[test_path]
            except OSError:
                continue
            if module in text or re.search(rf"\b{re.escape(token)}\b", text):
                selector = test_path.relative_to(repo).as_posix()
                add(selector, f"references {module}", 60, source)
                reference_matches += 1
                if reference_matches >= 25:
                    break

    recommendations = []
    for record in candidates.values():
        record["sources"] = sorted(record["sources"])
        recommendations.append(record)
    recommendations.sort(key=lambda item: (-item["confidence"], item["selector"]))
    auto_selectable = len(recommendations) <= 50
    return {
        "changed_lines": changed,
        "changed_files": len(changed),
        "recommendations": recommendations,
        "auto_selectable": auto_selectable,
        "gaps": sorted(set(changed) - covered_files),
    }


def changed_coverage(manifest: dict[str, Any]) -> dict[str, Any]:
    changed = manifest.get("changed_lines") or {}
    coverage = read_json(Path(manifest["_directory"]) / "coverage.json", {})
    files = coverage.get("files", {}) if isinstance(coverage, dict) else {}
    details: list[dict[str, Any]] = []
    total = covered = 0
    for name, raw_lines in sorted(changed.items()):
        wanted = {int(value) for value in raw_lines}
        executed = set((files.get(name) or {}).get("lines") or [])
        hit = wanted & executed
        missing = sorted(wanted - executed)
        total += len(wanted)
        covered += len(hit)
        details.append(
            {"file": name, "changed": len(wanted), "covered": len(hit), "missing": missing}
        )
    return {
        "changed": total,
        "covered": covered,
        "percent": round(100 * covered / total, 1) if total else None,
        "files": details,
    }
