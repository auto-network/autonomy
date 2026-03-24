#!/usr/bin/env python3
"""Session card data tests — mock fixtures + agent-browser assertions.

Reproduces known session card data bugs:
- Missing recency timer for idle sessions after page reload
- Empty last message for idle sessions
- Row 3 (_hasData) hidden when no session:messages received

Two tiers of checks:
  Expected-pass: cards render, labels present, session count > 0,
                 turn count and context tokens visible (via registry SSE)
  Known-bug:     recency timer empty, last message empty, Row 3 hidden
                 (registry omits last_activity/last_message, store never set)

Exit code 0 — known-bug failures are expected, not hard failures.

Usage:
    python tools/dashboard/tests/test_session_cards.py
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time


# ── Fixture data ──────────────────────────────────────────────────────

def _make_fixtures() -> dict:
    now = int(time.time())
    return {
        "beads": [
            {"id": "auto-test1", "title": "Test bead for session cards", "priority": 1, "status": "open"},
        ],
        "active_sessions": [
            {
                "session_id": "mock-host-001",
                "project": "autonomy",
                "type": "host",
                "tmux_session": "mock-host-001",
                "is_live": True,
                "started_at": now - 3600,
                "label": "Host session alpha",
                "entry_count": 42,
                "context_tokens": 125000,
            },
            {
                "session_id": "mock-container-002",
                "project": "autonomy",
                "type": "container",
                "tmux_session": "mock-container-002",
                "is_live": True,
                "started_at": now - 7200,
                "label": "Container session beta",
                "entry_count": 774,
                "context_tokens": 890000,
            },
        ],
    }


def _make_registry_event() -> dict:
    """SSE event matching get_registry() output shape."""
    now = int(time.time())
    return {
        "topic": "session:registry",
        "data": [
            {
                "session_id": "mock-host-001",
                "project": "autonomy",
                "type": "host",
                "tmux_session": "mock-host-001",
                "is_live": True,
                "started_at": now - 3600,
                "label": "Host session alpha",
                "entry_count": 42,
                "context_tokens": 125000,
                "topics": [],
                "nag_enabled": False,
                "nag_interval": 15,
                "nag_message": "",
            },
            {
                "session_id": "mock-container-002",
                "project": "autonomy",
                "type": "container",
                "tmux_session": "mock-container-002",
                "is_live": True,
                "started_at": now - 7200,
                "label": "Container session beta",
                "entry_count": 774,
                "context_tokens": 890000,
                "topics": ["polishing bead auto-xyz"],
                "nag_enabled": True,
                "nag_interval": 5,
                "nag_message": "Still working?",
            },
        ],
    }


# ── Check helper (matches smoke.py pattern) ──────────────────────────

def _check(name: str, fn, known_bug: str | None = None) -> dict:
    """Execute fn(), return a check result dict.

    If known_bug is set, a failure is expected and reported as [KNOWN-BUG].
    """
    try:
        result = fn()
        if result is True:
            return {"name": name, "pass": True, "known_bug": known_bug}
        else:
            return {"name": name, "pass": False, "detail": str(result), "known_bug": known_bug}
    except Exception as e:
        return {"name": name, "pass": False, "detail": str(e), "known_bug": known_bug}


# ── Server lifecycle ──────────────────────────────────────────────────

def _start_server(fixture_path: str, events_path: str, port: int) -> subprocess.Popen:
    """Boot dashboard on mock fixtures. Returns the Popen handle."""
    env = {
        **os.environ,
        "DASHBOARD_MOCK": fixture_path,
        "DASHBOARD_MOCK_EVENTS": events_path,
        "DASHBOARD_PORT": str(port),
        "PYTHONPATH": os.getcwd(),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "tools.dashboard.server:app",
         "--host", "0.0.0.0", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


def _wait_for_server(port: int, timeout: float = 8.0) -> bool:
    """Poll until the server responds on the given port."""
    import http.client
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            conn = http.client.HTTPConnection("localhost", port, timeout=2)
            conn.request("GET", "/")
            resp = conn.getresponse()
            conn.close()
            if resp.status in (200, 307):
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _stop_server(proc: subprocess.Popen) -> None:
    """Gracefully stop the server."""
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
        proc.wait(timeout=3)


# ── Browser helpers ───────────────────────────────────────────────────

def _browser_cmd(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["agent-browser", *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _browser_eval(js: str) -> str:
    r = _browser_cmd("eval", js)
    return r.stdout.strip()


def _browser_eval_json(js: str):
    """Eval JS and parse the result as JSON.

    agent-browser eval already JSON-encodes results:
      - strings → "hello"
      - arrays → [1, 2, 3]
      - objects → {"a": 1}
      - numbers/bools → plain
    So do NOT use JSON.stringify() in the JS expression — just return
    the value directly and let agent-browser serialize it.
    """
    raw = _browser_eval(js)
    return json.loads(raw)


# ── Main test runner ──────────────────────────────────────────────────

def run_tests() -> dict:
    port = 8091
    tmpdir = tempfile.mkdtemp(prefix="session_card_test_")
    fixture_path = os.path.join(tmpdir, "fixtures.json")
    events_path = os.path.join(tmpdir, "events.jsonl")

    # Write fixtures
    with open(fixture_path, "w") as f:
        json.dump(_make_fixtures(), f)

    # Write SSE registry event (pre-populated so it fires on server start)
    with open(events_path, "w") as f:
        f.write(json.dumps(_make_registry_event()) + "\n")

    checks: list[dict] = []
    proc = None

    try:
        # ── Boot server ───────────────────────────────────────────
        proc = _start_server(fixture_path, events_path, port)
        if not _wait_for_server(port):
            stderr = ""
            try:
                proc.kill()
                _, stderr_bytes = proc.communicate(timeout=3)
                stderr = stderr_bytes.decode(errors="replace")[-500:]
            except Exception:
                pass
            return {"pass": False, "error": f"Server failed to start on port {port}: {stderr}", "checks": []}

        base_url = f"http://localhost:{port}"

        # ── Open sessions page ────────────────────────────────────
        _browser_cmd("open", f"{base_url}/sessions")
        _browser_cmd("wait", "--load", "networkidle")

        # Give SSE registry event time to arrive and Alpine to render
        # Mock watcher polls every 0.5s, Alpine polls store every 0.5s
        time.sleep(2.5)

        # ── Expected-pass checks ──────────────────────────────────
        # Note: agent-browser eval JSON-encodes results automatically.
        # Strings come back quoted ("hello"), arrays/objects as JSON.
        # Use _browser_eval_json() to parse, or _browser_eval() for raw.
        # Do NOT use JSON.stringify() inside JS expressions — it double-encodes.

        # 1. Session cards exist
        def check_cards_exist():
            count = _browser_eval_json(
                'document.querySelectorAll("[data-testid=\\"session-card\\"]").length'
            )
            if count and count > 0:
                return True
            return f"no session cards found (count={count})"
        checks.append(_check("cards_exist", check_cards_exist))

        # 2. Session count matches expected (2 sessions)
        def check_session_count():
            count = _browser_eval_json(
                'document.querySelectorAll("[data-testid=\\"session-card\\"]").length'
            )
            if count == 2:
                return True
            return f"expected 2 session cards, got {count}"
        checks.append(_check("session_count", check_session_count))

        # 3. Labels render (non-empty text in card titles)
        def check_labels_render():
            labels = _browser_eval_json(
                '(() => { '
                'var cards = document.querySelectorAll("[data-testid=\\"session-card\\"]"); '
                'if (cards.length === 0) return []; '
                'var labels = []; '
                'cards.forEach(c => { '
                '  var el = c.querySelector(".font-medium"); '
                '  if (el) labels.push(el.textContent.trim()); '
                '}); '
                'return labels; '
                '})()'
            )
            if len(labels) >= 2 and all(len(l) > 0 for l in labels):
                return True
            return f"labels not all present: {labels}"
        checks.append(_check("labels_render", check_labels_render))

        # 4. data-session-id attributes present
        def check_session_ids():
            ids = _browser_eval_json(
                '(() => { '
                'var cards = document.querySelectorAll("[data-testid=\\"session-card\\"]"); '
                'var ids = []; '
                'cards.forEach(c => ids.push(c.getAttribute("data-session-id"))); '
                'return ids; '
                '})()'
            )
            if "mock-host-001" in ids and "mock-container-002" in ids:
                return True
            return f"expected mock-host-001 and mock-container-002, got {ids}"
        checks.append(_check("session_ids", check_session_ids))

        # 5. tmux session names rendered
        def check_tmux_names():
            names = _browser_eval_json(
                '(() => { '
                'var els = document.querySelectorAll("[data-testid=\\"session-card\\"] .font-mono"); '
                'var names = []; '
                'els.forEach(e => names.push(e.textContent.trim())); '
                'return names; '
                '})()'
            )
            if "mock-host-001" in names and "mock-container-002" in names:
                return True
            return f"tmux names not found: {names}"
        checks.append(_check("tmux_names", check_tmux_names))

        # 6. Turn count visible (entry_count from registry → _turnsStr)
        def check_turn_count():
            texts = _browser_eval_json(
                '(() => { '
                'var chips = document.querySelectorAll("[data-testid=\\"session-card\\"] .session-stat-chip"); '
                'var texts = []; '
                'chips.forEach(c => texts.push(c.textContent.trim())); '
                'return texts; '
                '})()'
            )
            # Expect "42 · 125K" and "774 · 890K"
            has_42 = any("42" in t for t in texts)
            has_774 = any("774" in t for t in texts)
            if has_42 and has_774:
                return True
            return f"turn counts not found in stat chips: {texts}"
        checks.append(_check("turn_count", check_turn_count))

        # 7. Context tokens visible
        def check_context_tokens():
            texts = _browser_eval_json(
                '(() => { '
                'var chips = document.querySelectorAll("[data-testid=\\"session-card\\"] .session-stat-chip"); '
                'var texts = []; '
                'chips.forEach(c => texts.push(c.textContent.trim())); '
                'return texts; '
                '})()'
            )
            # 125000 → "125K", 890000 → "890K"
            has_125k = any("125K" in t for t in texts)
            has_890k = any("890K" in t for t in texts)
            if has_125k and has_890k:
                return True
            return f"context tokens not found in stat chips: {texts}"
        checks.append(_check("context_tokens", check_context_tokens))

        # 8. HOST badge on host session
        def check_host_badge():
            val = _browser_eval_json(
                '(() => { '
                'var card = document.querySelector("[data-session-id=\\"mock-host-001\\"]"); '
                'if (!card) return "card not found"; '
                'var badge = card.querySelector(".session-badge-host"); '
                'return badge ? badge.textContent.trim() : "no badge"; '
                '})()'
            )
            if val == "HOST":
                return True
            return f"HOST badge: {val}"
        checks.append(_check("host_badge", check_host_badge))

        # 9. Nag indicator on container session (nag_enabled=True)
        def check_nag_indicator():
            val = _browser_eval_json(
                '(() => { '
                'var card = document.querySelector("[data-session-id=\\"mock-container-002\\"]"); '
                'if (!card) return "card not found"; '
                'var nag = card.querySelector(".session-nag-indicator"); '
                'return nag ? nag.textContent.trim() : "no nag"; '
                '})()'
            )
            if val and "\U0001f514" in val:
                return True
            return f"nag indicator: {val}"
        checks.append(_check("nag_indicator", check_nag_indicator))

        # 10. Container card uses container class (not host)
        def check_container_class():
            val = _browser_eval_json(
                '(() => { '
                'var card = document.querySelector("[data-session-id=\\"mock-container-002\\"]"); '
                'if (!card) return "card not found"; '
                'return card.classList.contains("session-card-container") ? true : '
                '  "classes: " + card.className; '
                '})()'
            )
            if val is True:
                return True
            return f"container class: {val}"
        checks.append(_check("container_class", check_container_class))

        # ── Known-bug checks ──────────────────────────────────────

        # KB1. Recency timer empty for idle sessions
        #   Bug: session:registry handler (session-store.js:118-136) never sets
        #   store.lastActivity. Only session:messages handler sets it (line 111).
        #   For idle sessions (no new messages after page load), lastActivity
        #   stays 0 → _formatRecency(0) returns '' → recency timer empty.
        def check_recency_timer():
            val = _browser_eval_json(
                '(() => { '
                'var card = document.querySelector("[data-session-id=\\"mock-host-001\\"]"); '
                'if (!card) return "card not found"; '
                'var recency = card.querySelector("[class*=\\"recency-\\"]"); '
                'if (!recency) return "no recency element"; '
                'var text = recency.textContent.trim(); '
                'return text.length > 0 ? text : "empty"; '
                '})()'
            )
            if val and val not in ("empty", "no recency element"):
                return True
            return f"recency timer is {val}"
        checks.append(_check(
            "recency_timer",
            check_recency_timer,
            known_bug="session:registry handler does not set store.lastActivity — "
                      "only session:messages sets it (session-store.js:111). "
                      "Idle sessions after reload show no recency timer."
        ))

        # KB2. Last message empty for idle sessions
        #   Bug: registry has no message content. latest is derived from
        #   store.entries[last] in _updateFromStore (sessions.js:132), which is
        #   empty when no session:messages have been received.
        def check_last_message():
            val = _browser_eval_json(
                '(() => { '
                'var card = document.querySelector("[data-session-id=\\"mock-host-001\\"]"); '
                'if (!card) return "card not found"; '
                'var msgEl = card.querySelector(".text-gray-400.truncate"); '
                'if (!msgEl) return "no message element"; '
                'var text = msgEl.textContent.trim(); '
                'return text.length > 0 && text !== "..." ? text : "empty"; '
                '})()'
            )
            if val and val not in ("empty", "no message element"):
                return True
            return f"last message is {val}"
        checks.append(_check(
            "last_message",
            check_last_message,
            known_bug="session:registry contains no message content. "
                      "latest is derived from store.entries (sessions.js:132) "
                      "which is empty for idle sessions with no session:messages SSE."
        ))

        # KB3. Row 3 (_hasData) hidden for idle sessions
        #   Bug: _hasData = entries.length > 0 || sizeMB > 0 (sessions.js:124).
        #   For idle sessions with no entries and sizeMB='0', _hasData is false,
        #   so <template x-if="s._hasData"> hides the entire Row 3.
        def check_row3_visible():
            row_count = _browser_eval_json(
                '(() => { '
                'var card = document.querySelector("[data-session-id=\\"mock-host-001\\"]"); '
                'if (!card) return -1; '
                'return card.querySelectorAll("div.flex.items-center").length; '
                '})()'
            )
            # If Row 3 is visible, we expect 3 rows (title, tmux+stats, message+recency)
            if row_count >= 3:
                return True
            return f"Row 3 not visible (row_count={row_count}) — _hasData is false for idle sessions"
        checks.append(_check(
            "row3_visible",
            check_row3_visible,
            known_bug="_hasData = entries.length > 0 || sizeMB > 0 (sessions.js:124). "
                      "Idle sessions with no entries and default sizeMB='0' have _hasData=false, "
                      "hiding Row 3 (last message + recency timer) entirely."
        ))

    finally:
        # Clean up browser
        try:
            _browser_cmd("close", timeout=5)
        except Exception:
            pass
        # Stop server
        if proc:
            _stop_server(proc)
        # Clean up temp files
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

    return {"checks": checks}


def main():
    start = time.monotonic()

    if not shutil.which("agent-browser"):
        print("ERROR: agent-browser not on PATH", file=sys.stderr)
        sys.exit(1)

    result = run_tests()
    duration_s = time.monotonic() - start

    if "error" in result:
        print(f"FATAL: {result['error']}", file=sys.stderr)
        sys.exit(1)

    checks = result["checks"]
    expected_pass = [c for c in checks if not c.get("known_bug")]
    known_bugs = [c for c in checks if c.get("known_bug")]

    # ── Print results ─────────────────────────────────────────────
    print("=== Session Card Data Tests ===", file=sys.stderr)
    print(f"Duration: {duration_s:.1f}s", file=sys.stderr)
    print("", file=sys.stderr)

    print("── Expected-pass ──", file=sys.stderr)
    for c in expected_pass:
        status = "PASS" if c["pass"] else "FAIL"
        detail = f" — {c['detail']}" if c.get("detail") else ""
        print(f"  [{status}] {c['name']}{detail}", file=sys.stderr)

    print("", file=sys.stderr)
    print("── Known-bug (expected failures) ──", file=sys.stderr)
    for c in known_bugs:
        if c["pass"]:
            status = "FIXED!"
            detail = " — bug appears to be fixed"
        else:
            status = "KNOWN-BUG"
            detail = f" — {c.get('detail', '')}"
        explanation = f"\n    Explanation: {c['known_bug']}" if c.get("known_bug") else ""
        print(f"  [{status}] {c['name']}{detail}{explanation}", file=sys.stderr)

    # ── Summary ───────────────────────────────────────────────────
    print("", file=sys.stderr)
    ep_pass = sum(1 for c in expected_pass if c["pass"])
    ep_total = len(expected_pass)
    kb_pass = sum(1 for c in known_bugs if c["pass"])
    kb_total = len(known_bugs)
    print(f"Expected-pass: {ep_pass}/{ep_total}", file=sys.stderr)
    print(f"Known-bug: {kb_pass}/{kb_total} fixed", file=sys.stderr)

    all_expected_pass = all(c["pass"] for c in expected_pass)
    overall = "PASS" if all_expected_pass else "FAIL"
    print(f"Overall: {overall}", file=sys.stderr)

    # Output JSON summary
    print(json.dumps({
        "pass": all_expected_pass,
        "duration_s": round(duration_s, 1),
        "expected_pass": {"pass": all_expected_pass, "passed": ep_pass, "total": ep_total},
        "known_bugs": {"fixed": kb_pass, "total": kb_total},
    }))

    # Exit 0 — known-bug failures are expected
    sys.exit(0 if all_expected_pass else 1)


if __name__ == "__main__":
    main()
