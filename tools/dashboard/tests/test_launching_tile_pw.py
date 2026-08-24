"""auto-yfcoc PART 2 — optimistic launching tile + Launching section.

Real-rendering regression test driven by Playwright against a running
dashboard. This exercises the ACTUAL browser path (Alpine store → getters →
template), which is the gap that let the startup-phase UI ship while broken:
the prior suite only validated synthetic derivation states, never the live
render.

Skips cleanly when Playwright isn't installed or the dashboard isn't
reachable at DASHBOARD_URL (default https://localhost:8080), so it never
blocks a hermetic unit run — but it RUNS (and catches real breakage)
wherever a dashboard is live.

What it pins:
  1. Picking a workspace inserts a pending tile instantly, in a dedicated
     "Launching" section, with the "Queued" phase chip.
  2. The pending tile is NOT duplicated into Active Sessions.
  3. When the real session:registry row for the same workspace arrives, the
     pending tile is reconciled away and the real (still-booting) session
     shows in Launching.
"""
from __future__ import annotations

import os
import urllib.request
import ssl

import pytest

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://localhost:8080")


def _dashboard_up() -> bool:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(f"{DASHBOARD_URL}/sessions", timeout=5, context=ctx) as r:
            return r.status == 200
    except Exception:
        return False


playwright = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
pytestmark = pytest.mark.skipif(not _dashboard_up(), reason=f"dashboard not reachable at {DASHBOARD_URL}")


@pytest.fixture(scope="module")
def checks():
    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        pg = b.new_page(ignore_https_errors=True)
        pg.on("pageerror", lambda e: errors.append(f"PAGEERROR: {e}"))
        pg.goto(f"{DASHBOARD_URL}/sessions", wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_function("() => typeof window.getSessionStore === 'function'", timeout=15000)
        pg.wait_for_timeout(1200)

        # 1) optimistic pending tile via the page's own store path
        pg.evaluate(
            """() => {
                const s = window.getSessionStore('pending-pwtest');
                s.isLive=true; s.project='autonomy'; s.label='PW launch test';
                s.sessionType='container'; s.startedAt=Date.now()/1000;
                s.setupPhase='pending'; s.harnessPhase='pending';
                s.activityState='thinking'; s._launching=true;
                window.dispatchEvent(new CustomEvent('sessions:store-changed', {detail:{reason:'pwtest'}}));
            }"""
        )
        pg.wait_for_timeout(700)
        r: dict = {}
        r["launching_visible"] = pg.is_visible('[data-testid="launching-section"]')
        r["launching_cards"] = pg.locator('[data-testid="launching-section"] [data-testid="session-card"]').count()
        chip = pg.locator('[data-testid="launching-section"] .sc-phase-chip')
        r["chip_text"] = chip.first.text_content().strip() if chip.count() else None
        r["pending_in_active"] = pg.locator('[data-testid="active-sessions-section"] [data-session-id="pending-pwtest"]').count()

        # 2) real session arrives → pending reconciled away
        pg.evaluate(
            """() => {
                const r = window.getSessionStore('auto-pwreal-0001');
                r.isLive=true; r.project='autonomy'; r.sessionType='container';
                r.role='Builder'; r.harness='claude'; r.model='claude-pwtest';
                r.startedAt=Date.now()/1000 + 1;
                r.setupPhase='container_starting'; r.harnessPhase='harness_starting';
                window.dispatchEvent(new CustomEvent('sessions:store-changed', {detail:{reason:'pwtest2'}}));
            }"""
        )
        pg.wait_for_timeout(700)
        r["pending_after_reconcile"] = pg.locator('[data-session-id="pending-pwtest"]').count()
        r["real_in_launching"] = pg.locator('[data-testid="launching-section"] [data-session-id="auto-pwreal-0001"]').count()
        launch = pg.locator('[data-testid="launching-section"] [data-session-id="auto-pwreal-0001"]')
        r["launch_role_badges"] = launch.locator('.sc-role:visible').count()
        r["launch_harness_visible"] = launch.locator('[data-testid="session-harness-badge"]:visible').count()
        r["page_errors"] = errors
        # cleanup injected store rows so we don't pollute a live page
        pg.evaluate(
            """() => { try { delete Alpine.store('sessions')['pending-pwtest'];
                       delete Alpine.store('sessions')['auto-pwreal-0001']; } catch(e){} }"""
        )
        b.close()
    return r


def test_optimistic_tile_renders_in_launching(checks):
    assert checks["launching_visible"], "Launching section not visible after workspace pick"
    assert checks["launching_cards"] >= 1, "Optimistic pending tile did not render in Launching"


def test_optimistic_tile_shows_queued_chip(checks):
    assert checks["chip_text"] == "Queued", f"Expected 'Queued' chip on pending tile, got {checks['chip_text']!r}"


def test_pending_tile_not_in_active(checks):
    assert checks["pending_in_active"] == 0, "Pending tile leaked into Active Sessions (double-render)"


def test_real_session_reconciles_pending_away(checks):
    assert checks["pending_after_reconcile"] == 0, "Pending tile not reconciled away after real session arrived"
    assert checks["real_in_launching"] >= 1, "Real booting session not shown in Launching"


def test_launching_card_prioritizes_phase_over_secondary_badges(checks):
    assert checks["launch_role_badges"] == 0, (
        "Launching card kept its type/role badges ahead of the startup phase"
    )
    assert checks["launch_harness_visible"] >= 1, (
        "Launching card hid the model badge even though the test viewport has room"
    )


def test_no_page_errors(checks):
    assert checks["page_errors"] == [], f"JS page errors on sessions page: {checks['page_errors']}"
