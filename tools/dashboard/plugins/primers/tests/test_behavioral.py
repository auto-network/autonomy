"""L2.B behavioral sweep for the Primers UI plugin (bead auto-9fyy0).

One navigation + ONE batched JS eval per test that returns a results
dict; Python asserts on the values. Per the bead's acceptance
criteria: ``data-testid`` / ``offsetParent !== null`` / fetch-spy
assertions only — never CSS-class structure.

The tests stub ``window.fetch`` in the browser so the page exercises
its real API-wiring code without needing a server-side fixture for
``/api/primers/workspaces`` / ``/api/primers/workspace/<id>``. The stub
captures every call into ``window.__primersFetchSpy`` for fetch-spy
assertions.
"""
from __future__ import annotations

import json

import pytest

from tools.dashboard.test_lib.l2b_harness import (
    _ab_eval_batch,
    _navigate_and_check,
)


# ── Fetch-stub installer ────────────────────────────────────────────────
#
# Installed once per page load (idempotent). Captures every call into
# ``window.__primersFetchSpy``; routes URL prefixes to canned responses
# so the page renders deterministically. Tests can override the canned
# data by setting ``window.__primersStubData`` before navigating.

_FETCH_STUB_JS = r"""
window.__primersStubData = window.__primersStubData || {
    orgs: ['autonomy', 'anchore', 'personal'],
    workspaces: [
        {id: 'autonomy',       name: 'Autonomy Network', org: 'autonomy',
         image: 'autonomy-agent:dashboard',     writable: false},
        {id: 'enterprise-ng',  name: 'Enterprise NG',    org: 'anchore',
         image: 'autonomy-agent:enterprise-ng', writable: true},
    ],
    primers: {
        'autonomy': {
            markdown: '# Autonomy Network\n\nDashboard primer body.',
            token_estimate: 420,
            workspace: {id: 'autonomy', name: 'Autonomy Network',
                        org: 'autonomy',
                        image: 'autonomy-agent:dashboard',
                        writable: false},
        },
        'enterprise-ng': {
            markdown: '# Enterprise NG — Workspace Environment\n\n' +
                'You are running inside the enterprise-ng container.\n\n' +
                'Lots of additional content for the token estimate to be ' +
                'meaningful in the page chrome.',
            token_estimate: 1234,
            workspace: {id: 'enterprise-ng', name: 'Enterprise NG',
                        org: 'anchore',
                        image: 'autonomy-agent:enterprise-ng',
                        writable: true},
        },
    },
    // Per-id error overrides — set to a {status, body} pair to make the
    // stub respond with that error envelope for the named workspace.
    primerErrors: {},
};

if (!window.__primersFetchInstalled) {
    window.__primersOriginalFetch = window.fetch;
    window.__primersFetchSpy = [];
    window.fetch = function (input, init) {
        var url = typeof input === 'string'
            ? input
            : (input && input.url) || '';
        var method = (init && init.method) || 'GET';
        var orgHeader = null;
        try {
            orgHeader = new Headers((init && init.headers) || {})
                .get('X-Graph-Org');
        } catch (e) { orgHeader = null; }
        window.__primersFetchSpy.push({
            url: url, method: method, org: orgHeader,
        });

        var stub = window.__primersStubData;

        function jsonResp(body, status) {
            return Promise.resolve(new Response(
                JSON.stringify(body),
                {status: status || 200,
                 headers: {'Content-Type': 'application/json'}},
            ));
        }

        if (url === '/api/orgs') {
            return jsonResp({orgs: (stub.orgs || []).map(function (slug) {
                return {org: {slug: slug}};
            })}, 200);
        }

        if (url === '/api/primers/workspaces') {
            var rows = stub.workspaces || [];
            if (orgHeader) {
                rows = rows.filter(function (ws) { return ws.org === orgHeader; });
            }
            return jsonResp({workspaces: rows}, 200);
        }

        if (url.indexOf('/api/primers/workspace/') === 0
            && method === 'GET') {
            var wid = decodeURIComponent(
                url.replace('/api/primers/workspace/', '').split('?')[0],
            );
            var err = stub.primerErrors && stub.primerErrors[wid];
            if (err) return jsonResp(err.body || {error: 'rendering failed'},
                                     err.status || 500);
            var p = (stub.primers || {})[wid];
            if (!p) return jsonResp(
                {error: 'unknown workspace: ' + JSON.stringify(wid)}, 404);
            if (orgHeader && p.workspace && p.workspace.org !== orgHeader) {
                return jsonResp(
                    {error: 'unknown workspace: ' + JSON.stringify(wid)}, 404);
            }
            return jsonResp(p, 200);
        }

        // Pass through anything else (plugin discovery, fragment loads,
        // version checks, /api/dao/active_sessions, etc).
        return window.__primersOriginalFetch.apply(this, arguments);
    };

    window.__primersFetchInstalled = true;
}

window.__primersFetchSpy = [];
"""


def _install_stub(stub_overrides: dict | None = None) -> None:
    """Install fetch stub (once per session). *stub_overrides* replaces
    ``window.__primersStubData`` wholesale before installation runs, so
    each test can swap canned responses without redefining the stub
    body."""
    install_js = _FETCH_STUB_JS
    if stub_overrides is not None:
        install_js = (
            "window.__primersStubData = "
            + json.dumps(stub_overrides)
            + "; "
            + install_js
        )
    _ab_eval_batch(install_js)


def _navigate_to_primers_and_check(
    js_checks: str,
    *,
    stub_overrides: dict | None = None,
    wait_ms: int = 900,
    pre_path: str | None = "/sessions",
) -> dict:
    """Bounce off *pre_path* (so navigateTo('/primers') re-runs init),
    install / refresh the stub, navigate to ``/primers`` and run
    *js_checks*."""
    if pre_path:
        _navigate_and_check(pre_path, "", wait_ms=400)
    _install_stub(stub_overrides)
    return _navigate_and_check("/primers", js_checks, wait_ms=wait_ms)


# ── Tests ───────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("browser")
class TestPrimersPlugin:
    """L2.B sweep — Primers UI v1 (bead auto-9fyy0).

    One batched JS eval per method per the L2.B contract.
    """

    @pytest.fixture(autouse=True)
    def _clear_state(self):
        """Reset localStorage selection + stub data before every test
        so persistence between methods doesn't bleed."""
        _ab_eval_batch(
            "try { localStorage.removeItem("
            "'autonomy.plugin.primers.selection'); } catch (e) {} "
            "if (window.__primersFetchSpy) "
            "  window.__primersFetchSpy.length = 0; "
            "window.__primersStubData = null;"
        )
        yield

    # State matrix row 1 — plugin enabled, navigated to /primers.
    def test_sidebar_entry_present(self):
        """Plugin loaded by default; /primers route returns
        ``primers-fragment-root``; sidebar entry is visible."""
        result = _navigate_to_primers_and_check(
            """
            r.frag_present = !!document.querySelector(
                '[data-testid="primers-fragment-root"]');
            var nav = document.querySelector('[data-page="primers"]');
            r.nav_present = nav !== null;
            r.nav_visible = nav !== null && nav.offsetParent !== null;
            var empty = document.querySelector(
                '[data-testid="primers-empty"]');
            r.empty_visible = empty !== null
                && empty.offsetParent !== null;
            """,
        )
        assert result.get("nav_present"), result
        assert result.get("nav_visible"), result
        assert result.get("frag_present"), result
        assert result.get("empty_visible"), result

    # State matrix row 1 (continued) — workspace list populates.
    def test_workspace_list_populates(self):
        """Left rail shows ≥1 workspace row; row count matches the API
        response filtered to the selected org."""
        _navigate_to_primers_and_check("")
        result = _ab_eval_batch(
            """
            var r = {};
            return new Promise(function (resolve) {
                setTimeout(function () {
                    var rows = document.querySelectorAll(
                        '[data-testid="workspace-row"]');
                    r.workspace_row_count = rows.length;
                    var root = document.querySelector(
                        '[data-testid="primers-fragment-root"]');
                    var data = window.Alpine ? Alpine.$data(root) : null;
                    r.selected_org = data ? data.selectedOrg : null;
                    r.expected_count = (window.__primersStubData.workspaces || [])
                        .filter(function (ws) { return ws.org === r.selected_org; })
                        .length;
                    r.workspace_ids = Array.from(rows).map(function (b) {
                        return b.dataset.workspaceId;
                    });
                    r.org_picker_present =
                        !!document.querySelector('[data-testid="org-picker"]');
                    resolve(r);
                }, 600);
            });
            """
        )
        assert result.get("workspace_row_count") == result.get(
            "expected_count"
        ), f"row count != API workspaces.length: {result}"
        assert result.get("workspace_row_count") >= 1, result
        assert result.get("selected_org") == "autonomy", result
        assert result.get("org_picker_present"), result
        assert "autonomy" in (result.get("workspace_ids") or []), result

    # State matrix row 2 — clicking a workspace renders its primer.
    def test_click_workspace_renders_primer(self):
        """Click a row, fetch fires, rendered markdown appears in the
        right pane. Markdown text matches the API response prefix."""
        _navigate_to_primers_and_check("")
        result = _ab_eval_batch(
            """
            var r = {};
            return new Promise(function (resolve) {
                setTimeout(function () {
                    var picker = document.querySelector(
                        '[data-testid="org-picker"]');
                    if (picker) {
                        picker.value = 'anchore';
                        picker.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                    setTimeout(function () {
                    window.__primersFetchSpy.length = 0;
                    var target = Array.from(document.querySelectorAll(
                        '[data-testid="workspace-row"]')).find(function (b) {
                        return b.dataset.workspaceId === 'enterprise-ng';
                    });
                    if (target) target.click();
                    setTimeout(function () {
                        var md = document.querySelector(
                            '[data-testid="primer-markdown"]');
                        r.markdown_present = md !== null;
                        r.markdown_visible = md !== null
                            && md.offsetParent !== null;
                        r.markdown_text = md
                            ? md.textContent.trim() : '';
                        var primer = window.__primersStubData
                            .primers['enterprise-ng'];
                        r.expected_prefix = primer.markdown.slice(0, 50);
                        var calls = window.__primersFetchSpy.filter(
                            function (c) {
                                return c.url ===
                                    '/api/primers/workspace/enterprise-ng';
                            });
                        r.workspace_fetch_count = calls.length;
                        r.workspace_fetch_orgs = calls.map(function (c) {
                            return c.org;
                        });
                        resolve(r);
                    }, 700);
                    }, 700);
                }, 600);
            });
            """
        )
        assert result.get("markdown_present"), result
        assert result.get("markdown_visible"), result
        assert result.get("workspace_fetch_count", 0) >= 1, (
            f"render-route fetch did not fire on row click: {result}"
        )
        assert "anchore" in (result.get("workspace_fetch_orgs") or []), result
        # The rendered markdown text contains the expected leading body.
        # The leading "# " becomes an <h1> header, so plain-textContent
        # matches the body without the hash.
        prefix = (result.get("expected_prefix") or "").lstrip("# ").strip()
        body = (result.get("markdown_text") or "").strip()
        assert prefix in body or body.startswith(prefix.split("\n")[0]), (
            f"rendered markdown does not contain API prefix: {result}"
        )

    # State matrix row 3 — token count visible and matches API.
    def test_token_count_visible(self):
        """``token-count`` element shows the estimate from the API
        response (formatted with ``toLocaleString``)."""
        _navigate_to_primers_and_check("")
        result = _ab_eval_batch(
            r"""
            var r = {};
            return new Promise(function (resolve) {
                setTimeout(function () {
                    var picker = document.querySelector(
                        '[data-testid="org-picker"]');
                    if (picker) {
                        picker.value = 'anchore';
                        picker.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                    setTimeout(function () {
                    var target = Array.from(document.querySelectorAll(
                        '[data-testid="workspace-row"]')).find(function (b) {
                        return b.dataset.workspaceId === 'enterprise-ng';
                    });
                    if (target) target.click();
                    setTimeout(function () {
                        var tc = document.querySelector(
                            '[data-testid="token-count"]');
                        r.token_count_present = tc !== null;
                        r.token_count_text = tc
                            ? tc.textContent.replace(/\s+/g, ' ').trim() : '';
                        var expected = window.__primersStubData
                            .primers['enterprise-ng'].token_estimate;
                        r.expected_estimate = expected;
                        // toLocaleString in en-US splits thousands with
                        // commas; check both the raw + locale forms.
                        r.matches_raw =
                            r.token_count_text.indexOf(String(expected)) !== -1;
                        r.matches_locale =
                            r.token_count_text.indexOf(
                                expected.toLocaleString()) !== -1;
                        resolve(r);
                    }, 700);
                    }, 700);
                }, 600);
            });
            """
        )
        assert result.get("token_count_present"), result
        assert (
            result.get("matches_raw") or result.get("matches_locale")
        ), f"token count did not surface API value: {result}"

    # State matrix row 4 — render failure shows error, no markdown.
    def test_render_failure_shows_error(self):
        """API stubbed to 404 for the picked workspace; the page shows
        ``primers-error`` text and does NOT render the markdown."""
        _install_stub({
            "workspaces": [
                {"id": "broken", "name": "Broken", "org": "autonomy",
                 "image": "autonomy-agent:broken", "writable": False},
            ],
            "primers": {},
            "primerErrors": {
                "broken": {
                    "status": 500,
                    "body": {"error": "rendering failed"},
                },
            },
        })
        _navigate_and_check("/sessions", "", wait_ms=400)
        _navigate_and_check("/primers", "", wait_ms=900)
        result = _ab_eval_batch(
            """
            var r = {};
            return new Promise(function (resolve) {
                setTimeout(function () {
                    var target = Array.from(document.querySelectorAll(
                        '[data-testid="workspace-row"]')).find(function (b) {
                        return b.dataset.workspaceId === 'broken';
                    });
                    if (target) target.click();
                    setTimeout(function () {
                        var err = document.querySelector(
                            '[data-testid="primers-error"]');
                        r.error_present = err !== null;
                        r.error_visible = err !== null
                            && err.offsetParent !== null;
                        r.error_text = err
                            ? err.textContent.trim() : '';
                        var md = document.querySelector(
                            '[data-testid="primer-markdown"]');
                        r.markdown_present = md !== null;
                        resolve(r);
                    }, 700);
                }, 600);
            });
            """
        )
        assert result.get("error_present"), result
        assert result.get("error_visible"), result
        assert "rendering failed" in (result.get("error_text") or ""), result
        assert not result.get("markdown_present"), (
            f"markdown should NOT render when the renderer fails: {result}"
        )

    # State matrix row 5 — selection persists across SPA navigation.
    def test_selection_persists_across_navigation(self):
        """Select a workspace, navigate ``/sessions``, navigate back —
        selection restored from localStorage."""
        _install_stub()
        _navigate_and_check("/sessions", "", wait_ms=400)
        _navigate_and_check("/primers", "", wait_ms=900)
        # Step 1: select enterprise-ng on /primers.
        _ab_eval_batch(
            """
            return new Promise(function (resolve) {
                setTimeout(function () {
                    var picker = document.querySelector(
                        '[data-testid="org-picker"]');
                    if (picker) {
                        picker.value = 'anchore';
                        picker.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                    setTimeout(function () {
                    var target = Array.from(document.querySelectorAll(
                        '[data-testid="workspace-row"]')).find(function (b) {
                        return b.dataset.workspaceId === 'enterprise-ng';
                    });
                    if (target) target.click();
                    setTimeout(function () { resolve(true); }, 500);
                    }, 700);
                }, 500);
            });
            """
        )

        # Step 2: navigate away then back.
        _navigate_and_check("/sessions", "", wait_ms=400)
        _navigate_and_check("/primers", "", wait_ms=900)

        result = _ab_eval_batch(
            """
            var r = {};
            return new Promise(function (resolve) {
                setTimeout(function () {
                    try {
                        var raw = localStorage.getItem(
                            'autonomy.plugin.primers.selection');
                        r.persisted = raw ? JSON.parse(raw) : null;
                    } catch (e) { r.persisted = null; }
                    var rows = document.querySelectorAll(
                        '[data-testid="workspace-row"]');
                    var selected = Array.from(rows).find(function (b) {
                        return b.dataset.workspaceId === 'enterprise-ng';
                    });
                    // Read the Alpine ``selectedId`` directly so we don't
                    // assert against CSS classes.
                    var root = document.querySelector(
                        '[data-testid="primers-fragment-root"]');
                    var data = window.Alpine
                        ? Alpine.$data(root) : null;
                    r.alpine_selected_id = data ? data.selectedId : null;
                    var md = document.querySelector(
                        '[data-testid="primer-markdown"]');
                    r.markdown_visible = md !== null
                        && md.offsetParent !== null;
                    resolve(r);
                }, 600);
            });
            """
        )
        persisted = result.get("persisted") or {}
        assert persisted.get("org") == "anchore", persisted
        assert persisted.get("workspace_id") == "enterprise-ng", persisted
        assert result.get("alpine_selected_id") == "enterprise-ng", result
        assert result.get("markdown_visible"), (
            f"primer markdown did not re-render after returning to "
            f"/primers — selection was not restored: {result}"
        )

    # State matrix row 6 — Dispatch tab placeholder disabled.
    def test_dispatch_tab_disabled_in_v1(self):
        """``tab-dispatch`` is rendered with ``disabled`` /
        ``aria-disabled`` so v1 ships a clear v2 placeholder."""
        result = _navigate_to_primers_and_check(
            r"""
            var tab = document.querySelector('[data-testid="tab-dispatch"]');
            r.tab_present = tab !== null;
            r.tab_disabled = tab !== null && tab.disabled === true;
            r.tab_aria_disabled = tab !== null
                && tab.getAttribute('aria-disabled') === 'true';
            r.tab_text = tab
                ? tab.textContent.replace(/\s+/g, ' ').trim() : '';
            """,
        )
        assert result.get("tab_present"), result
        # Either the HTML ``disabled`` attribute or aria-disabled is
        # acceptable; the design ships both.
        assert (
            result.get("tab_disabled") or result.get("tab_aria_disabled")
        ), f"Dispatch tab is not disabled: {result}"
        assert "v2" in (result.get("tab_text") or "").lower(), (
            f"Dispatch tab missing v2 marker: {result}"
        )

    def test_topbar_org_switch_scopes_workspace_requests(self):
        """Changing the topbar org picker refetches the rail as that org."""
        _navigate_to_primers_and_check("")
        result = _ab_eval_batch(
            """
            var r = {};
            return new Promise(function (resolve) {
                setTimeout(function () {
                    window.__primersFetchSpy.length = 0;
                    var picker = document.querySelector('[data-testid="org-picker"]');
                    r.picker_present = !!picker;
                    if (picker) {
                        picker.value = 'anchore';
                        picker.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                    setTimeout(function () {
                        var rows = document.querySelectorAll(
                            '[data-testid="workspace-row"]');
                        r.workspace_ids = Array.from(rows).map(function (b) {
                            return b.dataset.workspaceId;
                        });
                        r.workspace_fetch_orgs = window.__primersFetchSpy
                            .filter(function (c) {
                                return c.url === '/api/primers/workspaces';
                            })
                            .map(function (c) { return c.org; });
                        var root = document.querySelector(
                            '[data-testid="primers-fragment-root"]');
                        var data = window.Alpine ? Alpine.$data(root) : null;
                        r.alpine_selected_org = data ? data.selectedOrg : null;
                        resolve(r);
                    }, 800);
                }, 600);
            });
            """
        )
        assert result.get("picker_present"), result
        assert result.get("alpine_selected_org") == "anchore", result
        assert result.get("workspace_ids") == ["enterprise-ng"], result
        assert "anchore" in (result.get("workspace_fetch_orgs") or []), result

    def test_topbar_search_collapses_until_icon_click(self):
        """Structured topbar search starts collapsed behind the shell
        search icon, then expands to a filter input."""
        _navigate_to_primers_and_check("")
        result = _ab_eval_batch(
            """
            var r = {};
            return new Promise(function (resolve) {
                setTimeout(function () {
                    var icon = document.getElementById('global-search-icon');
                    r.topbar_present =
                        !!document.querySelector('[data-testid="app-structured-topbar"]');
                    r.icon_visible = icon !== null && icon.offsetParent !== null;
                    r.input_before =
                        !!document.querySelector('[data-testid="primers-topbar-search"]');
                    if (icon) icon.click();
                    setTimeout(function () {
                        var input = document.querySelector(
                            '[data-testid="primers-topbar-search"]');
                        r.input_after = !!input;
                        if (input) {
                            input.value = 'auto';
                            input.dispatchEvent(new Event('input', {bubbles: true}));
                        }
                        setTimeout(function () {
                            r.workspace_ids = Array.from(
                                document.querySelectorAll('[data-testid="workspace-row"]')
                            ).map(function (b) { return b.dataset.workspaceId; });
                            resolve(r);
                        }, 200);
                    }, 300);
                }, 600);
            });
            """
        )
        assert result.get("topbar_present"), result
        assert result.get("icon_visible"), result
        assert result.get("input_before") is False, result
        assert result.get("input_after"), result
        assert result.get("workspace_ids") == ["autonomy"], result
