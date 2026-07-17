"""Self-contained UI guard: no CDN in any dashboard load path (H5 review).

The sovereign-distribution stance (DEPLOY.md) promises a fresh install
serves its whole UI from the deployment itself — offline-capable, no
third-party origin in the CSP. Libraries belong in
``tools/dashboard/static/vendor/`` (see VENDOR.md there), never behind
a CDN URL.
"""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1]

CDN_HOSTS = re.compile(
    r"cdn\.jsdelivr\.net|unpkg\.com|cdnjs\.cloudflare\.com|"
    r"fonts\.googleapis\.com|ajax\.googleapis\.com",
)

# Runtime UI surfaces: what the server sends to browsers.
SCAN_ROOTS = ("templates", "static", "plugins", "server.py")

# Documentation of the vendoring itself may name the source URLs.
ALLOWED = {"static/vendor/VENDOR.md"}


def _scan_files():
    for root in SCAN_ROOTS:
        path = DASHBOARD / root
        if path.is_file():
            yield path
            continue
        for f in path.rglob("*"):
            if f.is_file() and f.suffix in (
                ".html", ".js", ".css", ".py", ".md", ".json",
            ):
                yield f


def test_no_cdn_hosts_in_dashboard_surfaces():
    offenders = []
    for f in _scan_files():
        rel = str(f.relative_to(DASHBOARD))
        if rel in ALLOWED:
            continue
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if CDN_HOSTS.search(line):
                offenders.append(f"{rel}:{i}: {line.strip()[:100]}")
    assert not offenders, (
        "CDN reference(s) in dashboard surfaces — vendor into "
        "static/vendor/ instead (see VENDOR.md):\n" + "\n".join(offenders)
    )


def test_vendored_libs_present_and_nonempty():
    vendor = DASHBOARD / "static" / "vendor"
    for name in (
        "marked.min.js", "xterm.min.js", "xterm.min.css",
        "addon-fit.min.js", "addon-clipboard.min.js", "purify.min.js",
        "alpine.min.js", "html2canvas.min.js", "tailwind-browser.min.js",
    ):
        f = vendor / name
        assert f.is_file() and f.stat().st_size > 1000, name


def test_csp_has_no_third_party_origins():
    from tools.dashboard import server

    for csp in (server._CSPMiddleware._CSP, server._CSPMiddleware._CSP_FRAMEABLE):
        for directive in csp.split(";"):
            directive = directive.strip()
            if not directive.startswith(("script-src", "style-src", "default-src")):
                continue
            for source in directive.split()[1:]:
                assert source.startswith("'") or source in ("data:",), (
                    f"third-party origin {source!r} in CSP directive {directive!r}"
                )
