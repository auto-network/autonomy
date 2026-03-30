#!/usr/bin/env python3
"""Validate rich-content note rendering at desktop and mobile widths.

Usage:
    python3 tools/dashboard/tests/validate_rich_content.py <graph-id>
    python3 tools/dashboard/tests/validate_rich_content.py c8c33197-1de
    python3 tools/dashboard/tests/validate_rich_content.py d99bf3dd-927 --url https://localhost:8080

Runs agent-browser against the live dashboard. Checks:
  - Iframe renders with data-testid="rich-content-iframe"
  - Toggle button present with data-testid="rich-toggle"
  - Nav/sidebar not broken by leaked CSS
  - No vertical scrollbar on iframe
  - At narrow viewport (400px): content wider than iframe (horizontal scroll works)
  - At narrow viewport: overflow-x allows scrolling

Exit code 0 = all checks pass, 1 = failures.
"""
import json
import subprocess
import sys
import time


def _ab(*args, timeout=10):
    r = subprocess.run(["agent-browser", *args], capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()


def _ab_eval(js, timeout=10):
    r = subprocess.run(["agent-browser", "eval", js], capture_output=True, text=True, timeout=timeout)
    out = r.stdout.strip()
    try:
        return json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return out


def run(graph_id, base_url="https://localhost:8080"):
    url = f"{base_url}/graph/{graph_id}"
    results = {}
    failures = []

    print(f"Validating: {url}")
    print()

    # Open page
    _ab("open", url, "--ignore-https-errors", "--no-sandbox")
    time.sleep(2)

    # ── Desktop checks (default viewport) ──
    print("Desktop checks:")

    checks = _ab_eval("""
    (function() {
        var r = {};
        var iframe = document.querySelector('[data-testid="rich-content-iframe"]');
        r.has_iframe = !!iframe;
        r.iframe_visible = !!(iframe && iframe.offsetParent !== null);

        var toggle = document.querySelector('[data-testid="rich-toggle"]');
        r.has_toggle = !!toggle;
        r.toggle_label = toggle ? toggle.textContent.trim() : '';

        var sidebar = document.querySelector('[data-testid="sidebar"]') || document.querySelector('nav') || document.querySelector('aside');
        r.nav_intact = !!(sidebar && sidebar.offsetParent !== null);

        if (iframe) {
            r.no_vertical_scrollbar = iframe.scrollHeight <= iframe.clientHeight + 2;
            try {
                var d = iframe.contentDocument;
                r.iframe_body_width = d.body.clientWidth;
                r.iframe_body_scrollWidth = d.body.scrollWidth;
                r.svg_count = d.querySelectorAll('svg').length;
                // Body must extend to fit its content (fit-content check)
                // If body is narrower than its scrollWidth, backgrounds won't cover content
                r.body_covers_content = d.body.clientWidth >= d.body.scrollWidth - 2;
                // Check body has width: fit-content
                var bw = window.getComputedStyle(d.body).width;
                r.body_computed_width = bw;
            } catch(e) {
                r.iframe_access_error = e.message;
            }
        }
        return JSON.stringify(r);
    })()
    """)

    if isinstance(checks, str):
        try:
            checks = json.loads(checks)
        except Exception:
            print(f"  ERROR: could not parse checks: {checks}")
            _ab("close")
            return 1

    def check(name, value, msg):
        status = "PASS" if value else "FAIL"
        print(f"  [{status}] {name}")
        if not value:
            failures.append(f"{name}: {msg}")

    check("iframe_present", checks.get("has_iframe"), "No iframe[data-testid='rich-content-iframe']")
    check("iframe_visible", checks.get("iframe_visible"), "Iframe not visible")
    check("toggle_present", checks.get("has_toggle"), "No toggle[data-testid='rich-toggle']")
    check("toggle_label", checks.get("toggle_label") == "Show Text", f"Expected 'Show Text', got '{checks.get('toggle_label')}'")
    check("nav_intact", checks.get("nav_intact"), "Nav/sidebar broken")
    check("no_vertical_scrollbar", checks.get("no_vertical_scrollbar"), "Iframe has vertical scrollbar")
    check("body_covers_content", checks.get("body_covers_content"),
          f"Body ({checks.get('iframe_body_width')}px) narrower than content ({checks.get('iframe_body_scrollWidth')}px) — backgrounds won't extend. Use width:fit-content on body.")

    print()

    # ── Mobile checks (narrow viewport) ──
    print("Mobile checks (400px):")
    _ab("set", "viewport", "400", "800")
    time.sleep(1)

    mobile = _ab_eval("""
    (function() {
        var r = {};
        var iframe = document.querySelector('[data-testid="rich-content-iframe"]');
        if (!iframe) { r.error = 'no iframe'; return JSON.stringify(r); }
        r.iframe_width = iframe.clientWidth;
        try {
            var d = iframe.contentDocument;
            r.body_scrollWidth = d.body.scrollWidth;
            r.body_clientWidth = d.body.clientWidth;
            r.content_wider = d.body.scrollWidth > iframe.clientWidth;
            // Body must extend to match content for scrolling to work
            // If body is narrower than scrollWidth, content is clipped (no usable scrollbar)
            r.body_extends = d.body.clientWidth >= d.body.scrollWidth - 2;
            // Check that diagram container extends to match content (no clipped backgrounds)
            var diagram = d.querySelector('.diagram');
            if (diagram) {
                r.diagram_clientWidth = diagram.clientWidth;
                r.diagram_covers_svg = diagram.scrollWidth <= diagram.clientWidth + 2;
            }
            // SVG must render at its intended width (from viewBox), not scaled down
            var svg = d.querySelector('svg');
            if (svg) {
                var vb = svg.getAttribute('viewBox');
                var intended = vb ? parseInt(vb.split(' ')[2]) : 0;
                var actual = svg.getBoundingClientRect().width;
                r.svg_intended_width = intended;
                r.svg_actual_width = Math.round(actual);
                r.svg_not_scaled = actual >= intended * 0.95;
            }
            var ox = window.getComputedStyle(d.documentElement).overflowX;
            r.overflow_x = ox;
            r.scroll_enabled = ox === 'auto' || ox === 'scroll' || ox === 'visible';
            r.scrollable = r.content_wider && r.scroll_enabled;
        } catch(e) {
            r.error = e.message;
        }
        return JSON.stringify(r);
    })()
    """)

    if isinstance(mobile, str):
        try:
            mobile = json.loads(mobile)
        except Exception:
            print(f"  ERROR: could not parse mobile checks: {mobile}")
            _ab("set", "viewport", "1280", "720")
            _ab("close")
            return 1

    check("content_wider_than_iframe", mobile.get("content_wider"),
          f"Content not wider at 400px (scrollWidth={mobile.get('body_scrollWidth')}, iframeWidth={mobile.get('iframe_width')})")
    check("horizontal_scroll_enabled", mobile.get("scroll_enabled"),
          f"overflow-x is '{mobile.get('overflow_x')}', expected auto/scroll/visible")
    check("body_extends_to_content", mobile.get("body_extends"),
          f"Body ({mobile.get('body_clientWidth')}px) narrower than content ({mobile.get('body_scrollWidth')}px) — content clipped, no scrollbar")
    check("diagram_scrollable", mobile.get("scrollable"), "Cannot scroll horizontally at narrow viewport")
    if mobile.get("svg_intended_width"):
        check("svg_not_scaled_down", mobile.get("svg_not_scaled"),
              f"SVG rendered at {mobile.get('svg_actual_width')}px, intended {mobile.get('svg_intended_width')}px — use fixed width attribute, not width=\"100%\"")
    if mobile.get("diagram_clientWidth") is not None:
        check("diagram_background_extends", mobile.get("diagram_covers_svg"),
              f".diagram ({mobile.get('diagram_clientWidth')}px) narrower than its content — background clipped when scrolling right")

    # Restore and close
    _ab("set", "viewport", "1280", "720")
    _ab("close")

    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    else:
        print(f"ALL PASSED (9 checks)")
        return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    graph_id = sys.argv[1]
    base_url = "https://localhost:8080"
    for i, arg in enumerate(sys.argv):
        if arg == "--url" and i + 1 < len(sys.argv):
            base_url = sys.argv[i + 1]

    sys.exit(run(graph_id, base_url))
