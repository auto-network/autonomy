"""Every reference to a static file names which build it wants.

``/static/`` grants a long cache lifetime only to a request that names a
version, and rechecks anything else on every page load. That rule is what
makes the fast path safe -- a changed file is a different address, so a
browser can never be handed a stale copy it has no way to release.

It also makes an omission silent. A new reference written without a version
is not broken: it simply takes the slow path forever, on every navigation,
and nothing says so. These fail instead.

Two ways of naming a build, and which one applies is not a preference:

- A file we write and change carries ``?v=``. A whole page gets it
  substituted in when the server renders it. A page FRAGMENT cannot: it is
  rendered by a different path that leaves the token alone, so a fragment
  writing it literally would ship that text to the browser. Fragments, and
  anything fetched after render, read the value the shell publishes.
- A pinned third-party library carries its version in its filename. Nothing
  has to substitute anything, which is the only thing that works inside the
  frames Present and Design Studio build as strings in JavaScript, where no
  template pass ever runs.
"""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1]
STATIC_REF = re.compile(r'["\']/static/([^"\'?\s]+)')

#: Served by URL patterns the browser is not asked to cache aggressively, or
#: fetched by something that has no page to read a version from.
EXEMPT = {
    "manifest.json",       # the browser refetches this on its own schedule
    "icon-192.png",        # named in the manifest, resolved by the browser
    "icon.svg",
}


#: How far past a reference to look for the version. One markup attribute can
#: span several lines -- a conditional src picking between two files puts the
#: reference and the version on different lines of the same expression -- so a
#: line-scoped check reports the first branch and misses that it is covered.
_WINDOW = 240


def _references(root: Path, suffixes: set[str]) -> list[tuple[Path, str, str]]:
    out = []
    for path in root.rglob("*"):
        if path.suffix not in suffixes or "/tests/" in str(path):
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for m in STATIC_REF.finditer(text):
            out.append((path, m.group(1), text[m.end():m.end() + _WINDOW]))
    return out


def _names_a_build(ref: str, following: str) -> bool:
    name = ref.rsplit("/", 1)[-1]
    if name in EXEMPT:
        return True
    # A pinned library carries its version in the filename: name-1.2.3.min.js
    if re.search(r"-\d+\.\d+\.\d+", name):
        return True
    if following.startswith("?v="):
        return True
    # Or the surrounding expression appends it.
    return ("__STATIC_VERSION__" in following
            or "staticVersion()" in following
            or "STATIC_V" in following)


def test_every_template_reference_names_a_build():
    """A template is rendered by the server, so it substitutes the version."""
    bad = [
        f"{p.relative_to(DASHBOARD)}: {ref}"
        for p, ref, following in _references(DASHBOARD / "templates", {".html"})
        if not _names_a_build(ref, following)
    ]
    assert not bad, (
        "these load a static file without naming a build, so a browser "
        "rechecks them on every page load forever:\n  " + "\n  ".join(bad)
    )


def test_every_vendored_library_carries_its_version_in_its_name():
    """The one form that works with no substitution anywhere, including
    inside a frame whose markup was built as a string in JavaScript."""
    vendor = DASHBOARD / "static" / "vendor"
    bad = [
        f.name for f in vendor.rglob("*")
        if f.is_file()
        and f.suffix in {".js", ".css", ".mjs"}
        and not re.search(r"-\d+\.\d+\.\d+", f.name)
    ]
    assert not bad, (
        "vendored libraries with no version in the filename; a version bump "
        "would reuse the same address and browsers would keep the old one:\n  "
        + "\n  ".join(sorted(bad))
    )


def test_every_vendored_library_is_recorded():
    """The manifest exists to say what a file is and where it came from.
    Two libraries sat in that directory for months without an entry."""
    vendor = DASHBOARD / "static" / "vendor"
    manifest = (vendor / "VENDOR.md").read_text()
    missing = [
        f.name for f in vendor.rglob("*")
        if f.is_file()
        and f.suffix in {".js", ".css", ".mjs"}
        and f.name not in manifest
    ]
    assert not missing, (
        "vendored but absent from VENDOR.md, so nothing records the version, "
        "the source, or the checksum:\n  " + "\n  ".join(sorted(missing))
    )
