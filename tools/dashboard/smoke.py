#!/usr/bin/env python3
"""Dashboard smoke test — Tier 1 (API sanity) + Tier 2 (browser sweep).

Exit 0 on PASS, 1 on FAIL.

Usage:
    python tools/dashboard/smoke.py
    python tools/dashboard/smoke.py --base-url https://localhost:8080
    python tools/dashboard/smoke.py --tier tier1
    python tools/dashboard/smoke.py --tier tier2
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time

try:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)


def _check(name: str, fn) -> dict:
    """Execute fn(), return a check result dict."""
    try:
        result = fn()
        if result is True:
            return {"name": name, "pass": True}
        else:
            return {"name": name, "pass": False, "detail": str(result)}
    except Exception as e:
        return {"name": name, "pass": False, "detail": str(e)}


def run_tier1(base_url: str) -> dict:
    """Run Tier 1 API sanity checks (pure HTTP, no browser)."""
    session = requests.Session()
    session.verify = False

    checks = []

    # 1. Server reachable — GET / returns 200
    def check_server_reachable():
        r = session.get(f"{base_url}/", timeout=10)
        if r.status_code == 200:
            return True
        return f"HTTP {r.status_code}"
    checks.append(_check("server_reachable", check_server_reachable))

    # 2. Stats API — GET /api/stats returns 200, valid JSON
    def check_stats_api():
        r = session.get(f"{base_url}/api/stats", timeout=10)
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        json.loads(r.text)
        return True
    checks.append(_check("stats_api", check_stats_api))

    # 3. Beads API — GET /api/beads/list returns 200, valid JSON, non-empty list
    def check_beads_api():
        r = session.get(f"{base_url}/api/beads/list", timeout=10)
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        data = json.loads(r.text)
        if not data:
            return "empty response (no beads)"
        return True
    checks.append(_check("beads_api", check_beads_api))

    # 4. Dispatch runs API — GET /api/dispatch/runs returns 200, valid JSON
    def check_dispatch_runs_api():
        r = session.get(f"{base_url}/api/dispatch/runs", timeout=10)
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        json.loads(r.text)
        return True
    checks.append(_check("dispatch_runs_api", check_dispatch_runs_api))

    # 5. Timeline API — GET /api/timeline returns 200, valid JSON
    def check_timeline_api():
        r = session.get(f"{base_url}/api/timeline", timeout=10)
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        json.loads(r.text)
        return True
    checks.append(_check("timeline_api", check_timeline_api))

    # 6. Dispatch fragment — GET /pages/dispatch returns 200, contains x-data
    def check_dispatch_fragment():
        r = session.get(f"{base_url}/pages/dispatch", timeout=10)
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        if "x-data" not in r.text:
            return "x-data not found in response"
        return True
    checks.append(_check("dispatch_fragment", check_dispatch_fragment))

    # 7. Timeline fragment — GET /pages/timeline returns 200, contains x-data
    def check_timeline_fragment():
        r = session.get(f"{base_url}/pages/timeline", timeout=10)
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        if "x-data" not in r.text:
            return "x-data not found in response"
        return True
    checks.append(_check("timeline_fragment", check_timeline_fragment))

    # 8. CSP header — Content-Security-Policy present AND contains 'unsafe-eval'
    def check_csp_header():
        r = session.get(f"{base_url}/", timeout=10)
        csp = r.headers.get("Content-Security-Policy", "")
        if not csp:
            return "Content-Security-Policy header missing"
        if "'unsafe-eval'" not in csp:
            return f"'unsafe-eval' not found in CSP (required by Alpine.js v3): {csp[:200]}"
        return True
    checks.append(_check("csp_header", check_csp_header))

    # 9. Session send — missing tmux_session returns 400
    def check_session_send_no_session():
        r = session.post(
            f"{base_url}/api/session/send",
            json={"message": "hello"},
            timeout=10,
        )
        if r.status_code != 400:
            return f"expected 400, got {r.status_code}"
        d = r.json()
        if "error" not in d:
            return "no error field in 400 response"
        return True
    checks.append(_check("session_send_no_session", check_session_send_no_session))

    # 10. Session send — empty message returns 400
    def check_session_send_empty_message():
        r = session.post(
            f"{base_url}/api/session/send",
            json={"tmux_session": "nonexistent-session", "message": ""},
            timeout=10,
        )
        if r.status_code != 400:
            return f"expected 400, got {r.status_code}"
        d = r.json()
        if "error" not in d:
            return "no error field in 400 response"
        return True
    checks.append(_check("session_send_empty_message", check_session_send_empty_message))

    # 11. Session send — unknown tmux session returns 404 or 503 (tmux not available)
    def check_session_send_unknown_session():
        r = session.post(
            f"{base_url}/api/session/send",
            json={"tmux_session": "no-such-session-xyzzy", "message": "test"},
            timeout=10,
        )
        if r.status_code not in (404, 503):
            return f"expected 404 or 503, got {r.status_code}"
        d = r.json()
        if "error" not in d:
            return "no error field in response"
        return True
    checks.append(_check("session_send_unknown_session", check_session_send_unknown_session))

    passed = all(c["pass"] for c in checks)
    for c in checks:
        status = "PASS" if c["pass"] else "FAIL"
        detail = f" — {c['detail']}" if c.get("detail") else ""
        print(f"  [{status}] {c['name']}{detail}", file=sys.stderr)

    return {"pass": passed, "checks": checks}


def run_tier2(base_url: str) -> dict:
    """Run Tier 2 browser sweep using agent-browser."""
    if not shutil.which("agent-browser"):
        print("  WARN: agent-browser not on PATH — skipping tier 2", file=sys.stderr)
        return {"pass": True, "skipped": True, "reason": "agent-browser not found"}

    pages = ["/dispatch", "/timeline", "/beads"]
    page_results = []

    for page in pages:
        url = f"{base_url}{page}"
        print(f"  Sweeping {page}...", file=sys.stderr)
        page_pass = False
        page_detail = None

        try:
            # Open page
            r1 = subprocess.run(
                ["agent-browser", "open", url, "--ignore-https-errors"],
                capture_output=True, text=True, timeout=30,
            )
            if r1.returncode != 0:
                page_detail = f"open failed: {r1.stderr.strip()}"
            else:
                # Wait for networkidle
                subprocess.run(
                    ["agent-browser", "wait", "--load", "networkidle"],
                    capture_output=True, text=True, timeout=30,
                )

                # Check Alpine x-data element exists and has non-empty value
                r3 = subprocess.run(
                    ["agent-browser", "eval",
                     "document.querySelector('[x-data]').getAttribute('x-data')"],
                    capture_output=True, text=True, timeout=15,
                )
                xdata_val = r3.stdout.strip()
                if not xdata_val or xdata_val.lower() in ("null", "undefined", ""):
                    page_detail = f"x-data missing or null: {xdata_val!r}"
                else:
                    # Check #content has rendered children
                    r4 = subprocess.run(
                        ["agent-browser", "eval",
                         "document.querySelector('#content') ? "
                         "document.querySelector('#content').children.length > 0 : false"],
                        capture_output=True, text=True, timeout=15,
                    )
                    content_val = r4.stdout.strip().lower()
                    if content_val == "true":
                        page_pass = True
                    else:
                        page_detail = (
                            f"#content empty or missing (catches CSP-broken Alpine): "
                            f"eval={content_val!r}"
                        )
        except subprocess.TimeoutExpired:
            page_detail = "agent-browser timed out"
        except Exception as e:
            page_detail = str(e)
        finally:
            subprocess.run(
                ["agent-browser", "close"],
                capture_output=True, text=True, timeout=10,
            )

        status = "PASS" if page_pass else "FAIL"
        detail = f" — {page_detail}" if page_detail else ""
        print(f"  [{status}] {page}{detail}", file=sys.stderr)
        page_results.append({"page": page, "pass": page_pass, "detail": page_detail})

    passed = all(p["pass"] for p in page_results)
    return {"pass": passed, "skipped": False, "pages": page_results}


def main():
    parser = argparse.ArgumentParser(description="Dashboard smoke test")
    parser.add_argument(
        "--base-url", default="https://localhost:8080",
        help="Base URL for the dashboard (default: https://localhost:8080)",
    )
    parser.add_argument(
        "--tier", default="all", choices=["all", "tier1", "tier2"],
        help="Which tier(s) to run (default: all)",
    )
    args = parser.parse_args()

    start_ms = time.time() * 1000
    tier1_result = None
    tier2_result = None

    # Tier 1
    if args.tier in ("all", "tier1"):
        print("=== Tier 1: API Sanity ===", file=sys.stderr)
        tier1_result = run_tier1(args.base_url)
        print(f"Tier 1: {'PASS' if tier1_result['pass'] else 'FAIL'}", file=sys.stderr)

    # Tier 2 — only if tier1 passed (or tier2 requested explicitly)
    if args.tier == "tier2" or (args.tier == "all" and tier1_result and tier1_result["pass"]):
        print("=== Tier 2: Browser Sweep ===", file=sys.stderr)
        tier2_result = run_tier2(args.base_url)
        print(f"Tier 2: {'PASS' if tier2_result['pass'] else 'FAIL'}", file=sys.stderr)
    elif args.tier == "all" and tier1_result and not tier1_result["pass"]:
        print("=== Tier 2: SKIPPED (tier 1 failed) ===", file=sys.stderr)
        tier2_result = {"pass": False, "skipped": True, "reason": "tier1 failed"}

    # Compute overall pass
    if args.tier == "tier1":
        overall_pass = tier1_result["pass"] if tier1_result else False
    elif args.tier == "tier2":
        overall_pass = tier2_result["pass"] if tier2_result else False
    else:
        t1 = tier1_result["pass"] if tier1_result else False
        t2 = tier2_result["pass"] if tier2_result else False
        overall_pass = t1 and t2

    duration_ms = int(time.time() * 1000 - start_ms)

    result: dict = {"pass": overall_pass, "duration_ms": duration_ms}
    if tier1_result is not None:
        result["tier1"] = tier1_result
    if tier2_result is not None:
        result["tier2"] = tier2_result

    print(json.dumps(result))
    sys.exit(0 if overall_pass else 1)


if __name__ == "__main__":
    main()
