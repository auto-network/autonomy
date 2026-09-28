"""auto-2v6ay.2 guard: no code names the organization "autonomy" as a
target, outside tests and a short allowlist (operator decision D5: the
user's actual org or personal, never a literal org)."""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
ROOTS = ("tools", "agents")
SUFFIXES = {".py", ".js", ".html", ".yaml", ".yml"}

#: Shapes in which "autonomy" is an ORG, not a repo, product, image, reach
#: mode or set id.
PATTERNS = [re.compile(p) for p in (
    r"""\borg\s*=\s*["']autonomy["']""",
    r"""["']org["']\s*:\s*["']autonomy["']""",
    r"""\bor\s+["']autonomy["']""",
    r"""\|\|\s*["']autonomy["']""",
    r"""_ORG\s*=\s*["']autonomy["']""",
    r"""\breturn\s+["']autonomy["']""",
    r"""graph_project\s*[=:]\s*["']autonomy["']""",
    r"""^org:\s*autonomy\s*$""",
    r"""selectedOrg\s*:\s*['"]autonomy['"]""",
)]

#: path -> why the literal is legitimate there.
ALLOWLIST = {
    "tools/dashboard/dao/mock.py": "mock-mode fixture data, not a runtime target",
    "tools/dashboard/server.py": "the DASHBOARD_MOCK branch of the dispatch tail (fixture)",
    "tools/graph/curation/autonomy-bootstrap-allowlist.yaml": (
        "Autonomy's own public-surface curation list: about that org by definition"),
    "tools/dashboard/org_identity.py": (
        "_LEGACY_PATH_ORG: path-derived projects of rows written before this "
        "change, applied only where that org exists"),
}


def _offenders():
    for root in ROOTS:
        for path in (REPO / root).rglob("*"):
            if path.suffix not in SUFFIXES or not path.is_file():
                continue
            rel = path.relative_to(REPO).as_posix()
            if "/tests/" in rel or path.name.startswith("test_") or "node_modules" in rel:
                continue
            if rel in ALLOWLIST:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                code = line.split("#", 1)[0] if path.suffix == ".py" else line
                if any(p.search(code) for p in PATTERNS):
                    yield f"{rel}:{lineno}: {line.strip()}"


def test_no_code_names_autonomy_as_its_target_org():
    found = list(_offenders())
    assert not found, (
        "a literal org 'autonomy' outside the allowlist (use the request's, "
        "session's or asset's org, else personal):\n" + "\n".join(found))


def test_allowlisted_files_still_exist():
    for rel in ALLOWLIST:
        assert (REPO / rel).is_file(), rel
