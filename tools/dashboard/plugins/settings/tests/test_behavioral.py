"""L2.B behavioral sweep for the Settings UI plugin (bead auto-yurkd).

Each test is one navigation + ONE batched JS eval that returns a
results dict; Python asserts on the dict values. Per the bead's
acceptance criteria: ``data-testid`` / ``offsetParent !== null`` /
fetch-spy assertions only — never CSS-class structure.

The tests stub ``window.fetch`` in the browser so the page exercises
its real API-wiring code without needing a server-side fixture for
``/api/orgs`` / ``/api/graph/sets`` / ``/api/graph/settings/<set_id>``.
The fetch stub captures every call for spy-style assertions.
"""
from __future__ import annotations

import json
import time

import pytest

from tools.dashboard.test_lib.l2b_harness import (
    _ab_eval_batch,
    _navigate_and_check,
)


# ── Fetch-stub installer ────────────────────────────────────────────────
#
# Installed once per test method (idempotent). Captures every call into
# ``window.__settingsFetchSpy``; routes URL prefixes to canned responses
# so the page renders deterministically. Tests can override the canned
# data by setting ``window.__settingsStubData`` before navigating.

_FETCH_STUB_JS = r"""
window.__settingsStubData = window.__settingsStubData || {
    orgs: [
        {org: {slug: 'autonomy'}},
        {org: {slug: 'anchore'}},
    ],
    sets: {
        autonomy: ['dashboard.plugin', 'autonomy.workspace'],
        anchore: ['anchore.policy'],
    },
    members: {
        'dashboard.plugin': [
            {key: 'settings',          payload: {enabled: true},  state: 'raw',
             stored_revision: 1, schema_revision: 1, deprecated: false},
            {key: 'coordinator-board', payload: {enabled: false}, state: 'raw',
             stored_revision: 1, schema_revision: 1, deprecated: false},
        ],
        'autonomy.workspace': [
            {key: 'autonomy',  payload: {label: 'Autonomy'},
             state: 'canonical', stored_revision: 1, schema_revision: 1, deprecated: false},
        ],
        'anchore.policy': [
            {key: 'rule-1', payload: {scope: 'cve'}, state: 'raw',
             stored_revision: 1, schema_revision: 1, deprecated: false},
        ],
    },
    saveResponse: {ok: true, status: 201, body: {id: 'new-id'}},
    refreshPluginsCalls: 0,
};

if (!window.__settingsFetchInstalled) {
    window.__settingsOriginalFetch = window.fetch;
    window.__settingsFetchSpy = [];
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
        var bodyText = (init && init.body) || null;
        window.__settingsFetchSpy.push({
            url: url, method: method, org: orgHeader, body: bodyText,
        });

        var stub = window.__settingsStubData;

        function jsonResp(body, status) {
            return Promise.resolve(new Response(
                JSON.stringify(body),
                {status: status || 200,
                 headers: {'Content-Type': 'application/json'}},
            ));
        }

        // /api/orgs
        if (url === '/api/orgs') {
            return jsonResp({orgs: stub.orgs}, 200);
        }

        // /api/graph/sets — org-scoped
        if (url.indexOf('/api/graph/sets') === 0) {
            var slug = orgHeader || 'autonomy';
            return jsonResp({set_ids: stub.sets[slug] || []}, 200);
        }

        // /api/graph/settings/<set_id>
        if (url.indexOf('/api/graph/settings/') === 0
            && method === 'GET') {
            var setId = decodeURIComponent(
                url.replace('/api/graph/settings/', '').split('?')[0],
            );
            return jsonResp({members: stub.members[setId] || []}, 200);
        }

        // POST /api/graph/setting
        if (url === '/api/graph/setting' && method === 'POST') {
            var resp = stub.saveResponse;
            // Persist into the in-memory members so re-fetches reflect
            // the write — mirrors the real DAO's latest-write-wins shape.
            try {
                var body = JSON.parse(bodyText);
                if (resp.ok) {
                    var list = stub.members[body.set_id]
                        || (stub.members[body.set_id] = []);
                    var existing = list.find(function (m) {
                        return m.key === body.key;
                    });
                    if (existing) {
                        existing.payload = body.payload;
                        existing.stored_revision =
                            (existing.stored_revision || 1) + 1;
                    } else {
                        list.push({
                            key: body.key,
                            payload: body.payload,
                            state: body.state || 'raw',
                            stored_revision: 1,
                            schema_revision: body.schema_revision || 1,
                            deprecated: false,
                        });
                    }
                }
            } catch (e) { /* ignore — server would reject anyway */ }
            return Promise.resolve(new Response(
                JSON.stringify(resp.body || {}),
                {status: resp.status || (resp.ok ? 201 : 400),
                 headers: {'Content-Type': 'application/json'}},
            ));
        }

        // Anything else — pass through to real fetch (handles
        // /api/plugins, /pages/settings fragment, /api/version, etc).
        return window.__settingsOriginalFetch.apply(this, arguments);
    };

    // Spy on Autonomy.refreshPlugins so the toggle test can assert it
    // was called after the POST resolved.
    if (window.Autonomy && window.Autonomy.refreshPlugins) {
        var orig = window.Autonomy.refreshPlugins;
        window.Autonomy.refreshPlugins = function () {
            window.__settingsStubData.refreshPluginsCalls += 1;
            return orig.apply(this, arguments);
        };
    }

    window.__settingsFetchInstalled = true;
}

window.__settingsFetchSpy = [];
"""


def _install_stub_and_navigate(stub_overrides: dict | None = None) -> None:
    """Install fetch stub (once per session) and navigate to /settings.

    *stub_overrides* deep-merges into ``window.__settingsStubData``
    before installation (or before navigation if already installed),
    so tests can swap in canned responses without re-defining the
    whole stub.
    """
    install_js = _FETCH_STUB_JS
    if stub_overrides is not None:
        install_js = (
            "window.__settingsStubData = "
            + json.dumps(stub_overrides)
            + "; "
            + install_js
        )
    # Install / refresh the stub state.
    _ab_eval_batch(install_js)


def _navigate_to_settings_and_check(
    js_checks: str,
    *,
    stub_overrides: dict | None = None,
    wait_ms: int = 900,
    pre_settings_path: str | None = None,
) -> dict:
    """Reset stub state, optionally bounce to *pre_settings_path*, then
    navigate to ``/settings`` and run *js_checks*.

    ``pre_settings_path`` exists so the navigation FROM another path TO
    ``/settings`` re-runs ``init()`` (otherwise navigateTo() short-
    circuits when already on /settings).
    """
    if pre_settings_path:
        _navigate_and_check(pre_settings_path, "", wait_ms=400)
    _install_stub_and_navigate(stub_overrides)
    return _navigate_and_check("/settings", js_checks, wait_ms=wait_ms)


# ── Tests ───────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("browser")
class TestSettingsPlugin:
    """L2.B sweep — Settings UI v1 (bead auto-yurkd).

    Each method is one batched JS eval per the L2.B contract:
    navigation + ONE eval with all checks, Python asserts on the
    returned dict.
    """

    @pytest.fixture(autouse=True)
    def _clear_state(self):
        """Reset localStorage selection + stub data before every test so
        persistence between methods doesn't bleed. The fetch stub stays
        installed across the module — only the data + spy are reset."""
        # Force stub data back to its default on the next install so an
        # earlier test that overrode the canned data doesn't leak.
        _ab_eval_batch(
            "try { localStorage.removeItem("
            "'autonomy.plugin.settings.selection'); } catch (e) {} "
            "if (window.__settingsFetchSpy) "
            "  window.__settingsFetchSpy.length = 0; "
            "window.__settingsStubData = null;"
        )
        yield

    def test_sidebar_entry_present(self):
        """Settings plugin loads by default; /settings shows the
        fragment root and the org picker defaults to ``autonomy``."""
        result = _navigate_to_settings_and_check(
            """
            r.frag_present = !!document.querySelector(
                '[data-testid="settings-fragment-root"]');
            var nav = document.querySelector('[data-page="settings"]');
            r.nav_present = nav !== null;
            r.nav_visible = nav !== null && nav.offsetParent !== null;
            var picker = document.querySelector(
                '[data-testid="org-picker"]');
            r.picker_present = picker !== null;
            r.picker_value = picker ? picker.value : null;
            """,
            pre_settings_path="/sessions",
        )
        assert result.get("nav_present"), result
        assert result.get("nav_visible"), result
        assert result.get("frag_present"), result
        assert result.get("picker_present"), result
        assert result.get("picker_value") == "autonomy", result

    def test_browse_sets_in_default_org(self):
        """The autonomy org's set_ids render in the left rail; clicking
        a set_id loads members and the row count matches the API
        ``members.length`` for that set."""
        _navigate_to_settings_and_check("", pre_settings_path="/sessions")
        # Allow the initial sets fetch to land.
        time.sleep(0.6)
        result = _ab_eval_batch(
            """
            var r = {};
            var rows = document.querySelectorAll('[data-testid="set-row"]');
            r.set_row_count = rows.length;
            r.set_ids = Array.from(rows).map(function (b) {
                return b.dataset.setId;
            });
            var target = Array.from(rows).find(function (b) {
                return b.dataset.setId === 'dashboard.plugin';
            });
            if (target) target.click();
            return new Promise(function (resolve) {
                setTimeout(function () {
                    var memberRows = document.querySelectorAll(
                        '[data-testid="member-row"]');
                    r.member_row_count = memberRows.length;
                    r.expected_member_count =
                        (window.__settingsStubData.members['dashboard.plugin']
                         || []).length;
                    r.member_keys = Array.from(memberRows).map(function (tr) {
                        return tr.dataset.memberKey;
                    });
                    resolve(r);
                }, 500);
            });
            """
        )
        assert result, "no result returned"
        assert result.get("set_row_count", 0) >= 1, result
        assert "dashboard.plugin" in (result.get("set_ids") or []), result
        assert result.get("member_row_count") == result.get(
            "expected_member_count"
        ), f"row count != API members.length: {result}"
        assert "settings" in (result.get("member_keys") or []), result

    def test_member_drawer_shows_payload(self):
        """Clicking a member row reveals the detail drawer with the
        formatted JSON payload."""
        _navigate_to_settings_and_check("", pre_settings_path="/sessions")
        time.sleep(0.6)
        result = _ab_eval_batch(
            """
            var r = {};
            var sets = document.querySelectorAll('[data-testid="set-row"]');
            var s = Array.from(sets).find(function (b) {
                return b.dataset.setId === 'dashboard.plugin';
            });
            if (s) s.click();
            return new Promise(function (resolve) {
                setTimeout(function () {
                    var rows = document.querySelectorAll(
                        '[data-testid="member-row"]');
                    var row = Array.from(rows).find(function (tr) {
                        return tr.dataset.memberKey === 'settings';
                    });
                    if (row) row.click();
                    setTimeout(function () {
                        var drawer = document.querySelector(
                            '[data-testid="member-detail"]');
                        r.drawer_present = drawer !== null;
                        r.drawer_visible = drawer !== null
                            && drawer.offsetParent !== null;
                        var payload = document.querySelector(
                            '[data-testid="member-payload"]');
                        r.payload_text = payload
                            ? payload.textContent.trim() : '';
                        r.payload_includes_enabled =
                            r.payload_text.indexOf('"enabled"') !== -1;
                        resolve(r);
                    }, 200);
                }, 400);
            });
            """
        )
        assert result.get("drawer_present"), result
        assert result.get("drawer_visible"), result
        assert result.get("payload_includes_enabled"), result

    def test_edit_payload_round_trip(self):
        """Open editor, modify JSON, save → POST fires; on success the
        member row re-renders with the new payload."""
        _navigate_to_settings_and_check("", pre_settings_path="/sessions")
        time.sleep(0.6)
        result = _ab_eval_batch(
            """
            var r = {};
            var sets = document.querySelectorAll('[data-testid="set-row"]');
            var s = Array.from(sets).find(function (b) {
                return b.dataset.setId === 'dashboard.plugin';
            });
            if (s) s.click();
            return new Promise(function (resolve) {
                setTimeout(function () {
                    var row = Array.from(document.querySelectorAll(
                        '[data-testid="member-row"]')).find(function (tr) {
                        return tr.dataset.memberKey === 'settings';
                    });
                    if (row) row.click();
                    setTimeout(function () {
                        var editBtn = document.querySelector(
                            '[data-testid="edit-button"]');
                        if (editBtn) editBtn.click();
                        setTimeout(function () {
                            var ta = document.querySelector(
                                '[data-testid="payload-editor"]');
                            r.editor_present = ta !== null;
                            // Mutate the textarea and notify Alpine.
                            if (ta) {
                                ta.value = '{"enabled": true, "label": "edited"}';
                                ta.dispatchEvent(new Event('input', {bubbles: true}));
                            }
                            // Reset the spy so we capture only the save POST.
                            window.__settingsFetchSpy = [];
                            var saveBtn = document.querySelector(
                                '[data-testid="save-button"]');
                            if (saveBtn) saveBtn.click();
                            setTimeout(function () {
                                var posts = window.__settingsFetchSpy.filter(
                                    function (c) {
                                        return c.url === '/api/graph/setting'
                                            && c.method === 'POST';
                                    });
                                r.post_count = posts.length;
                                r.post_body =
                                    posts.length ? posts[0].body : null;
                                var pre = document.querySelector(
                                    '[data-testid="member-payload"]');
                                r.rerendered_payload = pre
                                    ? pre.textContent : '';
                                r.rerendered_includes_label =
                                    r.rerendered_payload.indexOf('edited') !== -1;
                                resolve(r);
                            }, 600);
                        }, 200);
                    }, 200);
                }, 400);
            });
            """
        )
        assert result.get("editor_present"), result
        assert result.get("post_count") == 1, (
            f"expected one POST to /api/graph/setting; got {result}"
        )
        body = json.loads(result.get("post_body") or "{}")
        assert body.get("set_id") == "dashboard.plugin", body
        assert body.get("key") == "settings", body
        assert body.get("payload", {}).get("label") == "edited", body
        assert result.get("rerendered_includes_label"), result

    def test_invalid_payload_surfaces_server_error(self):
        """Server returns 4xx with an error message; the inline error
        chip surfaces it under ``data-testid="settings-error"``."""
        _install_stub_and_navigate({
            "orgs": [{"org": {"slug": "autonomy"}}, {"org": {"slug": "anchore"}}],
            "sets": {"autonomy": ["dashboard.plugin"], "anchore": []},
            "members": {
                "dashboard.plugin": [
                    {"key": "settings",
                     "payload": {"enabled": True},
                     "state": "raw",
                     "stored_revision": 1,
                     "schema_revision": 1,
                     "deprecated": False},
                ],
            },
            "saveResponse": {
                "ok": False,
                "status": 400,
                "body": {
                    "error": "schema validation failed",
                    "detail": "missing required field: enabled",
                },
            },
            "refreshPluginsCalls": 0,
        })
        _navigate_and_check("/sessions", "", wait_ms=400)
        _navigate_and_check("/settings", "", wait_ms=900)
        time.sleep(0.4)
        result = _ab_eval_batch(
            """
            var r = {};
            var s = Array.from(document.querySelectorAll(
                '[data-testid="set-row"]')).find(function (b) {
                return b.dataset.setId === 'dashboard.plugin';
            });
            if (s) s.click();
            return new Promise(function (resolve) {
                setTimeout(function () {
                    var row = Array.from(document.querySelectorAll(
                        '[data-testid="member-row"]')).find(function (tr) {
                        return tr.dataset.memberKey === 'settings';
                    });
                    if (row) row.click();
                    setTimeout(function () {
                        var editBtn = document.querySelector(
                            '[data-testid="edit-button"]');
                        if (editBtn) editBtn.click();
                        setTimeout(function () {
                            var ta = document.querySelector(
                                '[data-testid="payload-editor"]');
                            if (ta) {
                                ta.value = '{"enabled": "not-bool"}';
                                ta.dispatchEvent(
                                    new Event('input', {bubbles: true}));
                            }
                            var saveBtn = document.querySelector(
                                '[data-testid="save-button"]');
                            if (saveBtn) saveBtn.click();
                            setTimeout(function () {
                                var err = document.querySelector(
                                    '[data-testid="settings-error"]');
                                r.error_present = err !== null;
                                r.error_text = err
                                    ? err.textContent.trim() : '';
                                resolve(r);
                            }, 500);
                        }, 200);
                    }, 200);
                }, 400);
            });
            """
        )
        assert result.get("error_present"), result
        assert "schema validation failed" in (result.get("error_text") or ""), (
            f"error chip text missing server message: {result}"
        )
        # Reset the stub for subsequent tests.
        _install_stub_and_navigate({
            "orgs": [{"org": {"slug": "autonomy"}}, {"org": {"slug": "anchore"}}],
            "sets": {"autonomy": ["dashboard.plugin"], "anchore": []},
            "members": {
                "dashboard.plugin": [
                    {"key": "settings", "payload": {"enabled": True},
                     "state": "raw", "stored_revision": 1,
                     "schema_revision": 1, "deprecated": False},
                    {"key": "coordinator-board",
                     "payload": {"enabled": False}, "state": "raw",
                     "stored_revision": 1, "schema_revision": 1,
                     "deprecated": False},
                ],
            },
            "saveResponse": {
                "ok": True, "status": 201, "body": {"id": "new-id"},
            },
            "refreshPluginsCalls": 0,
        })

    def test_plugin_toggle_button_flips_enabled(self):
        """Clicking the per-row plugin-toggle on a ``dashboard.plugin#1``
        row issues exactly one POST that flips ``payload.enabled`` and
        triggers Autonomy.refreshPlugins() so the sidebar nav updates."""
        _navigate_to_settings_and_check("", pre_settings_path="/sessions")
        time.sleep(0.6)
        result = _ab_eval_batch(
            """
            var r = {};
            var s = Array.from(document.querySelectorAll(
                '[data-testid="set-row"]')).find(function (b) {
                return b.dataset.setId === 'dashboard.plugin';
            });
            if (s) s.click();
            return new Promise(function (resolve) {
                setTimeout(function () {
                    // Pre-state: coordinator-board row, enabled=false.
                    var rows = document.querySelectorAll(
                        '[data-testid="member-row"]');
                    var target = Array.from(rows).find(function (tr) {
                        return tr.dataset.memberKey === 'coordinator-board';
                    });
                    var toggle = target
                        ? target.querySelector('[data-testid="plugin-toggle"]')
                        : null;
                    r.toggle_present = toggle !== null;
                    // Reset spy so we count only this click's POST.
                    window.__settingsFetchSpy = [];
                    window.__settingsStubData.refreshPluginsCalls = 0;
                    if (toggle) toggle.click();
                    setTimeout(function () {
                        var posts = window.__settingsFetchSpy.filter(
                            function (c) {
                                return c.url === '/api/graph/setting'
                                    && c.method === 'POST';
                            });
                        r.post_count = posts.length;
                        r.post_body = posts.length ? posts[0].body : null;
                        r.refresh_plugins_called =
                            window.__settingsStubData.refreshPluginsCalls;
                        resolve(r);
                    }, 600);
                }, 400);
            });
            """
        )
        assert result.get("toggle_present"), result
        assert result.get("post_count") == 1, (
            f"expected exactly one POST per toggle click; got {result}"
        )
        body = json.loads(result.get("post_body") or "{}")
        assert body.get("set_id") == "dashboard.plugin", body
        assert body.get("key") == "coordinator-board", body
        assert body.get("payload", {}).get("enabled") is True, (
            f"toggle did not flip enabled to True: {body}"
        )
        assert result.get("refresh_plugins_called", 0) >= 1, (
            "Autonomy.refreshPlugins() was not invoked after the POST resolved"
        )

    def test_org_picker_changes_org(self):
        """Switching the picker to ``anchore`` triggers a fetch of
        ``/api/graph/sets`` carrying ``X-Graph-Org: anchore``."""
        _navigate_to_settings_and_check("", pre_settings_path="/sessions")
        time.sleep(0.6)
        result = _ab_eval_batch(
            """
            var r = {};
            window.__settingsFetchSpy = [];
            var picker = document.querySelector('[data-testid="org-picker"]');
            r.option_values = picker
                ? Array.from(picker.options).map(function (o) { return o.value; })
                : [];
            // Drive the picker via Alpine's data — synthetic ``change``
            // events on <select> are flaky across headless engines, so
            // we set the model directly and invoke the same handler the
            // picker would (semantically equivalent for the L2.B fetch
            // assertion).
            var root = document.querySelector(
                '[data-testid="settings-fragment-root"]');
            var data = Alpine.$data(root);
            data.selectedOrg = 'anchore';
            return Promise.resolve(data.onOrgChange()).then(function () {
                return new Promise(function (resolve) {
                    setTimeout(function () {
                        var setsCalls = window.__settingsFetchSpy.filter(
                            function (c) {
                                return c.url.indexOf('/api/graph/sets') === 0
                                    && c.url.indexOf('/api/graph/settings') !== 0;
                            });
                        r.sets_calls = setsCalls;
                        r.has_anchore_header = setsCalls.some(function (c) {
                            return c.org === 'anchore';
                        });
                        r.alpine_sets_anchore = (data.sets || {}).anchore;
                        r.alpine_selectedOrg = data.selectedOrg;
                        var setRows = document.querySelectorAll(
                            '[data-testid="set-row"]');
                        r.set_ids_after = Array.from(setRows).map(function (b) {
                            return b.dataset.setId;
                        });
                        r.picker_value_after = picker ? picker.value : null;
                        resolve(r);
                    }, 300);
                });
            });
            """
        )
        assert "anchore" in (result.get("option_values") or []), (
            f"picker did not include 'anchore' option: {result}"
        )
        assert result.get("has_anchore_header"), (
            f"no /api/graph/sets fetch carried X-Graph-Org=anchore: {result}"
        )
        # Anchore stub returns ['anchore.policy']
        assert "anchore.policy" in (result.get("set_ids_after") or []), result
        # Alpine's :value binding propagated selectedOrg → picker DOM.
        assert result.get("picker_value_after") == "anchore", result

    def test_state_persists_across_navigation(self):
        """Selecting org/set/member, navigating away, and returning
        restores the same selection from localStorage."""
        # Step 1: select org=anchore, set=anchore.policy, key=rule-1.
        _install_stub_and_navigate()
        _navigate_and_check("/sessions", "", wait_ms=400)
        _navigate_and_check("/settings", "", wait_ms=900)
        time.sleep(0.5)
        _ab_eval_batch(
            """
            var root = document.querySelector(
                '[data-testid="settings-fragment-root"]');
            var data = Alpine.$data(root);
            data.selectedOrg = 'anchore';
            return Promise.resolve(data.onOrgChange()).then(function () {
                var s = Array.from(document.querySelectorAll(
                    '[data-testid="set-row"]')).find(function (b) {
                    return b.dataset.setId === 'anchore.policy';
                });
                if (s) s.click();
                return new Promise(function (resolve) {
                    setTimeout(function () {
                        var row = Array.from(document.querySelectorAll(
                            '[data-testid="member-row"]')).find(function (tr) {
                            return tr.dataset.memberKey === 'rule-1';
                        });
                        if (row) row.click();
                        setTimeout(function () { resolve(true); }, 200);
                    }, 400);
                });
            });
            """
        )

        # Step 2: navigate away, then back.
        _navigate_and_check("/sessions", "", wait_ms=400)
        result = _navigate_and_check(
            "/settings",
            "",
            wait_ms=900,
        )
        # Give the restore path time to fetch sets + members.
        time.sleep(0.7)
        result = _ab_eval_batch(
            """
            var r = {};
            var picker = document.querySelector('[data-testid="org-picker"]');
            r.picker_value = picker ? picker.value : null;
            var rows = document.querySelectorAll('[data-testid="set-row"]');
            // The selected set has ring-1 / bg-indigo-900/40 styling, but
            // we only check via the data attr that the *highlighted* row
            // is anchore.policy (ring class would violate the no-CSS rule).
            // Read the persisted state out of localStorage instead.
            try {
                var raw = localStorage.getItem(
                    'autonomy.plugin.settings.selection');
                r.persisted = raw ? JSON.parse(raw) : null;
            } catch (e) { r.persisted = null; }
            var drawer = document.querySelector(
                '[data-testid="member-detail"]');
            r.drawer_visible = drawer !== null
                && drawer.offsetParent !== null;
            return r;
            """
        )
        assert result.get("picker_value") == "anchore", result
        persisted = result.get("persisted") or {}
        assert persisted.get("org") == "anchore", persisted
        assert persisted.get("set_id") == "anchore.policy", persisted
        assert persisted.get("key") == "rule-1", persisted
        assert result.get("drawer_visible"), (
            "member detail drawer did not re-render after returning to "
            f"/settings — selection was not restored: {result}"
        )
