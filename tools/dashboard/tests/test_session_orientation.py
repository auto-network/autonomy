"""Tests for the Settings-driven orientation message renderer.

auto-inhm3: ``api_session_create`` no longer hardcodes ``"Hello"`` —
it renders a per-workspace template from the
``dashboard.session.orientation`` Setting. Falls back to a hardcoded
default if the backend is unreachable. ``enabled=false`` skips
injection entirely.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tools.dashboard.session_orientation import render_orientation


class _Member:
    """Minimal mock of the SetMember protocol returned by read_set."""

    def __init__(self, key: str, payload: dict):
        self.key = key
        self.payload = payload


def _mock_members(members: list[_Member]):
    m = MagicMock()
    m.members = members
    return m


def test_no_backend_falls_back_to_default_template():
    """When read_set returns no members, the hardcoded DEFAULT_TEMPLATE renders."""
    with patch(
        "tools.graph.settings_ops.read_set", return_value=_mock_members([])
    ):
        out = render_orientation(
            tmux_name="auto-test1",
            workspace_id="autonomy",
            workspace_name="Autonomy",
            org="autonomy",
        )
    assert out is not None
    assert "Session auto-test1" in out
    assert "started in workspace Autonomy at" in out


def test_per_workspace_override_wins():
    """A workspace-specific row beats the default."""
    members = _mock_members([
        _Member("autonomy", {"template": "Custom {{tmux_name}}", "enabled": True}),
        _Member("__default__", {"template": "Default {{tmux_name}}", "enabled": True}),
    ])
    with patch("tools.graph.settings_ops.read_set", return_value=members):
        out = render_orientation(
            tmux_name="auto-9999",
            workspace_id="autonomy",
            workspace_name="X",
            org="autonomy",
        )
    assert out == "Custom auto-9999"


def test_host_override_reads_personal_host_key():
    """Native host sessions resolve the reserved host row from personal."""
    members = _mock_members([
        _Member("host", {
            "template": "Host {{tmux_name}} ready",
            "enabled": True,
        }),
    ])
    with patch(
        "tools.graph.settings_ops.read_set", return_value=members,
    ) as read_set:
        out = render_orientation(
            tmux_name="host-9999",
            workspace_id="host",
            workspace_name="host",
            org="personal",
        )
    assert out == "Host host-9999 ready"
    assert read_set.call_args.kwargs["org"] == "personal"


def test_server_host_helper_uses_reserved_personal_target():
    """The launch seam cannot drift back to the empty autonomy lookup."""
    from tools.dashboard import server

    with patch(
        "tools.dashboard.session_orientation.render_orientation",
        return_value="welcome",
    ) as render:
        out = server._render_host_orientation(tmux_name="host-1234")

    assert out == "welcome"
    render.assert_called_once_with(
        tmux_name="host-1234",
        workspace_id="host",
        workspace_name="host",
        org="personal",
        resumed=False,
    )


def test_default_fallback_when_workspace_unmapped():
    """No per-workspace row → __default__ row applies."""
    members = _mock_members([
        _Member("__default__", {"template": "Default for {{tmux_name}}", "enabled": True}),
    ])
    with patch("tools.graph.settings_ops.read_set", return_value=members):
        out = render_orientation(
            tmux_name="auto-new",
            workspace_id="unmapped",
            workspace_name="X",
            org="autonomy",
        )
    assert out == "Default for auto-new"


def test_disabled_returns_none():
    """enabled=false → caller skips injection entirely."""
    members = _mock_members([
        _Member("autonomy", {"template": "x", "enabled": False}),
    ])
    with patch("tools.graph.settings_ops.read_set", return_value=members):
        out = render_orientation(
            tmux_name="auto-disabled",
            workspace_id="autonomy",
            workspace_name="X",
            org="autonomy",
        )
    assert out is None


def test_operator_template_var_guarded():
    """Templates that reference {{operator}} without a guard error;
    with a guard the empty operator renders cleanly."""
    members = _mock_members([
        _Member("__default__", {
            "template": "{% if operator %}Op {{operator}}: {% endif %}Session {{tmux_name}}",
            "enabled": True,
        }),
    ])
    with patch("tools.graph.settings_ops.read_set", return_value=members):
        out = render_orientation(
            tmux_name="auto-no-op",
            workspace_id="any",
            workspace_name="X",
            org="autonomy",
        )
    assert out == "Session auto-no-op"


def test_operator_when_supplied():
    """When operator is non-empty, the guarded branch fires."""
    members = _mock_members([
        _Member("__default__", {
            "template": "{% if operator %}Op {{operator}}: {% endif %}Session {{tmux_name}}",
            "enabled": True,
        }),
    ])
    with patch("tools.graph.settings_ops.read_set", return_value=members):
        out = render_orientation(
            tmux_name="auto-with-op",
            workspace_id="any",
            workspace_name="X",
            org="autonomy",
            operator="alice",
        )
    assert out == "Op alice: Session auto-with-op"


def test_broken_template_falls_back_to_default():
    """A Jinja error logs a warning and renders the hardcoded default."""
    members = _mock_members([
        _Member("autonomy", {
            "template": "{% broken syntax",
            "enabled": True,
        }),
    ])
    with patch("tools.graph.settings_ops.read_set", return_value=members):
        out = render_orientation(
            tmux_name="auto-broken",
            workspace_id="autonomy",
            workspace_name="Y",
            org="autonomy",
        )
    # The fallback uses DEFAULT_TEMPLATE
    assert out is not None
    assert "started in workspace Y at" in out
    assert "auto-broken" in out


def test_resume_template_is_independently_customizable():
    """A stored resume template is valid schema surface and wins on resume."""
    members = _mock_members([
        _Member("host", {
            "template": "Fresh {{tmux_name}}",
            "resume_template": "Continue {{tmux_name}}",
            "enabled": True,
        }),
    ])
    with patch("tools.graph.settings_ops.read_set", return_value=members):
        out = render_orientation(
            tmux_name="host-resumed",
            workspace_id="host",
            workspace_name="host",
            org="personal",
            resumed=True,
        )
    assert out == "Continue host-resumed"


def test_broken_resume_template_falls_back_to_resume_default():
    """A malformed resume override must not re-orient a session as fresh."""
    members = _mock_members([
        _Member("host", {
            "template": "Fresh {{tmux_name}}",
            "resume_template": "{% broken syntax",
            "enabled": True,
        }),
    ])
    with patch("tools.graph.settings_ops.read_set", return_value=members):
        out = render_orientation(
            tmux_name="host-resumed",
            workspace_id="host",
            workspace_name="host",
            org="personal",
            resumed=True,
        )
    assert out is not None
    assert out.startswith("Session host-resumed resumed at ")
    assert "prior context is intact" in out


def test_settings_backend_exception_falls_back():
    """If read_set throws, render still produces the default template."""
    with patch(
        "tools.graph.settings_ops.read_set",
        side_effect=RuntimeError("backend down"),
    ):
        out = render_orientation(
            tmux_name="auto-no-backend",
            workspace_id="autonomy",
            workspace_name="Z",
            org="autonomy",
        )
    assert out is not None
    assert "Session auto-no-backend" in out
    assert "started in workspace Z at" in out


def test_default_template_format_matches_spec():
    """The default template must match the spec format
    ``Session <tmux_name> started in workspace <workspace_name> at <ts>.``
    (trailing "Awaiting instructions." was deliberately dropped in 4bb3dde)"""
    with patch(
        "tools.graph.settings_ops.read_set", return_value=_mock_members([])
    ):
        out = render_orientation(
            tmux_name="auto-fmt",
            workspace_id="autonomy",
            workspace_name="Auto",
            org="autonomy",
        )
    import re
    assert re.match(
        r"^Session auto-fmt started in workspace Auto at "
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00\.$",
        out,
    ), f"format mismatch: {out!r}"
