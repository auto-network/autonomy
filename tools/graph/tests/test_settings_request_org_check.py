"""Lint-check tests for ``tools.graph.checks.settings_request_org``.

The repo-level test in this module is the *enforcement path*. A future
edit that introduces ``settings_ops.X(..., org=org)`` (or
``graph_ops.X(..., org=org)``) inside a request handler — where ``org``
came from a bare ``_caller_org(request)`` — will fail
``test_repo_has_no_request_handler_passing_raw_caller_org_to_settings``
and block merge. The other tests pin the detector's behaviour on
synthetic input so that pin doesn't drift silently.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from tools.graph.checks.settings_request_org import (
    SETTINGS_API_FUNCS,
    Violation,
    find_violations_in_repo,
    find_violations_in_source,
)


_REPO_ROOT = Path(__file__).resolve().parents[3]


# ── Bad patterns that MUST be flagged ─────────────────────────────


def test_local_var_assigned_caller_org_then_passed_bare_is_flagged():
    src = textwrap.dedent(
        '''
        async def handler(request):
            org = _caller_org(request)
            graph_ops.add_setting(
                "set", 1, "key", {}, org=org,
            )
        '''
    )
    vs = find_violations_in_source(src, "synthetic_local_var.py")
    assert len(vs) == 1, vs
    assert "_caller_org" in vs[0].reason
    assert "graph_ops.add_setting" in vs[0].reason


def test_inline_caller_org_call_without_fallback_is_flagged():
    src = textwrap.dedent(
        '''
        async def handler(request):
            graph_ops.read_set("set", org=_caller_org(request))
        '''
    )
    vs = find_violations_in_source(src, "synthetic_inline.py")
    assert len(vs) == 1, vs


def test_settings_ops_module_path_is_also_flagged():
    """Calls written as ``settings_ops.X`` must be caught the same way
    as the ``graph_ops.X`` re-export form."""
    src = textwrap.dedent(
        '''
        async def handler(request):
            org = _caller_org(request)
            settings_ops.upsert_by_key(
                "set", 1, "k", {}, org=org,
            )
        '''
    )
    vs = find_violations_in_source(src, "synthetic_settings_ops.py")
    assert len(vs) == 1, vs
    assert "settings_ops.upsert_by_key" in vs[0].reason


def test_every_settings_api_func_is_caught():
    """Pin the API surface — all functions in SETTINGS_API_FUNCS must
    trip the detector when called with a tainted ``org=``. Catches a
    future regression where a new public Settings function ships
    without being added to the guard list."""
    for func in sorted(SETTINGS_API_FUNCS):
        src = textwrap.dedent(
            f'''
            async def handler(request):
                org = _caller_org(request)
                graph_ops.{func}("a", org=org)
            '''
        )
        vs = find_violations_in_source(src, f"synthetic_{func}.py")
        assert len(vs) == 1, f"{func}: expected 1 violation, got {vs}"


def test_violation_in_sync_def_handler_too():
    """Not all dashboard handlers are async (some are wrapped via
    ``starlette.concurrency.run_in_threadpool``)."""
    src = textwrap.dedent(
        '''
        def handler(request):
            org = _caller_org(request)
            graph_ops.list_set_ids(org=org)
        '''
    )
    vs = find_violations_in_source(src, "synthetic_sync.py")
    assert len(vs) == 1, vs


# ── Clean patterns that MUST NOT be flagged ───────────────────────


def test_local_var_with_caller_org_fallback_passes():
    """The canonical fix: ``org or graph_ops.CALLER_ORG`` at the call site."""
    src = textwrap.dedent(
        '''
        async def handler(request):
            org = _caller_org(request)
            graph_ops.add_setting(
                "set", 1, "k", {}, org=org or graph_ops.CALLER_ORG,
            )
        '''
    )
    assert find_violations_in_source(src, "clean_fallback.py") == []


def test_settings_caller_org_helper_passes():
    """The dedicated ``_settings_caller_org(request)`` helper folds the
    fallback in, so passing its result bare is correct."""
    src = textwrap.dedent(
        '''
        async def handler(request):
            org = _settings_caller_org(request)
            graph_ops.add_setting(
                "set", 1, "k", {}, org=org,
            )
        '''
    )
    assert find_violations_in_source(src, "clean_helper.py") == []


def test_inline_caller_org_with_fallback_passes():
    src = textwrap.dedent(
        '''
        async def handler(request):
            graph_ops.read_set(
                "set", org=_caller_org(request) or graph_ops.CALLER_ORG,
            )
        '''
    )
    assert find_violations_in_source(src, "clean_inline_fallback.py") == []


def test_settings_ops_caller_org_attr_also_passes():
    """``or settings_ops.CALLER_ORG`` is identical to ``or
    graph_ops.CALLER_ORG`` since one re-exports the other."""
    src = textwrap.dedent(
        '''
        async def handler(request):
            org = _caller_org(request)
            settings_ops.add_setting(
                "set", 1, "k", {}, org=org or settings_ops.CALLER_ORG,
            )
        '''
    )
    assert find_violations_in_source(src, "clean_settings_ops_attr.py") == []


def test_assignment_with_fallback_untaints_subsequent_uses():
    """``org = _caller_org(request) or graph_ops.CALLER_ORG`` is a safe
    assignment; a downstream bare ``org=org`` on a Settings call is
    fine."""
    src = textwrap.dedent(
        '''
        async def handler(request):
            org = _caller_org(request) or graph_ops.CALLER_ORG
            graph_ops.add_setting("set", 1, "k", {}, org=org)
        '''
    )
    assert find_violations_in_source(src, "clean_assigned_fallback.py") == []


def test_reassignment_to_safe_value_clears_taint():
    """If a handler first taints then reassigns with a fallback, later
    bare ``org=org`` uses are fine. Mirrors the realistic refactor
    pattern where the fix lands without renaming the variable."""
    src = textwrap.dedent(
        '''
        async def handler(request):
            org = _caller_org(request)
            org = org or graph_ops.CALLER_ORG
            graph_ops.add_setting("set", 1, "k", {}, org=org)
        '''
    )
    assert find_violations_in_source(src, "clean_reassigned.py") == []


def test_literal_org_slug_passes():
    """Explicit literal slugs are out of scope per the bead."""
    src = textwrap.dedent(
        '''
        async def handler(request):
            graph_ops.add_setting("set", 1, "k", {}, org="autonomy")
        '''
    )
    assert find_violations_in_source(src, "clean_literal.py") == []


def test_no_request_param_function_passes():
    """Out of scope: helpers and CLI subsystems that don't take
    ``request`` at all. ``_caller_org`` shouldn't appear here, but
    even if it does the bead explicitly excludes non-request writers."""
    src = textwrap.dedent(
        '''
        def helper(org):
            graph_ops.add_setting("set", 1, "k", {}, org=org)
        '''
    )
    assert find_violations_in_source(src, "clean_helper_func.py") == []


def test_unrelated_function_call_with_org_kwarg_passes():
    """Functions that happen to share a public Settings API name on a
    different module are not in scope (e.g. ``mock.add_setting``)."""
    src = textwrap.dedent(
        '''
        async def handler(request):
            org = _caller_org(request)
            mock.add_setting("set", 1, "k", {}, org=org)
        '''
    )
    assert find_violations_in_source(src, "clean_unrelated_module.py") == []


def test_non_settings_call_with_caller_org_passes():
    """``graph_ops.add_comment(..., org=org)`` (or any non-Settings
    function) is not affected by required-org and must not be flagged.
    The bead's scope is the Settings public API surface only."""
    src = textwrap.dedent(
        '''
        async def handler(request):
            org = _caller_org(request)
            graph_ops.add_comment(source_id, "x", org=org)
        '''
    )
    assert find_violations_in_source(src, "clean_non_settings.py") == []


def test_dao_mock_call_passes():
    """The ``DASHBOARD_MOCK`` short-circuit lands at ``dao_mock.X``,
    not ``graph_ops.X``; mock fixture state is not the scopeless DB."""
    src = textwrap.dedent(
        '''
        async def handler(request):
            org = _caller_org(request)
            return dao_mock.get_settings_members("set", org=org)
        '''
    )
    assert find_violations_in_source(src, "clean_dao_mock.py") == []


def test_handler_calling_local_helper_passes():
    """A handler that hands ``org`` to a local helper (which then adds
    its own fallback before calling the Settings API) is the documented
    pattern in server.py — the helper takes an ``org`` parameter, not
    ``request``, so it isn't a handler and the call site here isn't a
    Settings call."""
    src = textwrap.dedent(
        '''
        async def handler(request):
            org = _caller_org(request)
            return _settings_diag_rows(org=org)
        '''
    )
    assert find_violations_in_source(src, "clean_local_helper.py") == []


# ── Repo-level enforcement ────────────────────────────────────────


def test_repo_has_no_request_handler_passing_raw_caller_org_to_settings():
    """Enforcement: scanning the repo produces zero violations.

    A future commit that introduces a request handler passing
    ``_caller_org(request)`` directly into ``settings_ops.X`` /
    ``graph_ops.X`` (where X is a Settings public API function) without
    a ``CALLER_ORG`` fallback will fail this test. This is the path the
    bead's acceptance hinges on — the check is not advisory.
    """
    roots = [_REPO_ROOT / "tools", _REPO_ROOT / "agents"]
    violations = find_violations_in_repo(roots)
    assert violations == [], (
        "Found request handlers passing raw _caller_org(request) into "
        "Settings public API:\n"
        + "\n".join(v.format(_REPO_ROOT) for v in violations)
    )


# ── Sanity: existing compliant handlers in server.py are clean ────


def test_existing_server_py_settings_handlers_are_recognised_as_clean():
    """The known-good Settings handlers in tools/dashboard/server.py
    must scan clean. If this fails, the detector has regressed: it
    either lost the fallback recognition or stopped tolerating the
    ``_settings_caller_org`` helper.

    We pin against ``server.py`` rather than the full repo because the
    repo-level enforcement already covers everything; this test is a
    targeted positive control on the file the bead names explicitly.
    """
    target = _REPO_ROOT / "tools" / "dashboard" / "server.py"
    if not target.exists():
        return
    src = target.read_text()
    violations = find_violations_in_source(src, str(target))
    assert violations == [], (
        f"detector regressed — flagged a known-clean Settings handler: "
        f"{[v.format(_REPO_ROOT) for v in violations]}"
    )


# ── Plant-and-detect: prove the guard catches a representative bad
# example introduced in real handler code ────────────────────────────


def test_plant_a_bad_example_into_a_realistic_handler_shape():
    """Acceptance criterion #2: prove the guard catches at least one
    representative bad example in a realistic dashboard-handler shape
    (mirrors :func:`api_graph_setting_create` in server.py, with the
    fallback removed). Uses synthetic source so the repo-level
    enforcement test stays at zero violations."""
    bad = textwrap.dedent(
        '''
        async def api_graph_setting_create(request):
            body = await request.json()
            required = ("set_id", "schema_revision", "key", "payload")
            for f in required:
                if f not in body:
                    return JSONResponse({"error": f"{f} required"}, status_code=400)
            org = _caller_org(request)
            try:
                sid = graph_ops.add_setting(
                    body["set_id"],
                    int(body["schema_revision"]),
                    body["key"],
                    body["payload"],
                    state=body.get("state", "raw"),
                    org=org,
                )
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            return JSONResponse({"id": sid}, status_code=201)
        '''
    )
    vs = find_violations_in_source(bad, "synthetic_realistic_bad.py")
    assert len(vs) == 1, vs
    assert "graph_ops.add_setting" in vs[0].reason
    assert "_caller_org" in vs[0].reason

    fixed = bad.replace("org=org,", "org=org or graph_ops.CALLER_ORG,")
    assert find_violations_in_source(fixed, "synthetic_realistic_fixed.py") == []


# ── Smoke: __main__ wiring ────────────────────────────────────────


def test_violation_format_includes_lineno():
    src = textwrap.dedent(
        '''
        async def handler(request):
            org = _caller_org(request)
            graph_ops.add_setting("s", 1, "k", {}, org=org)
        '''
    )
    vs = find_violations_in_source(src, "/tmp/synthetic_lineno.py")
    assert len(vs) == 1
    assert vs[0].lineno >= 4, vs
    formatted = vs[0].format()
    assert ":" in formatted
    assert isinstance(vs[0], Violation)
