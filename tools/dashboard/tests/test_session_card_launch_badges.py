"""Launch cards reserve their metadata row for the startup phase."""

from pathlib import Path


TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "templates" / "partials" / "session-card.html"
)


def test_launching_rows_hide_secondary_badges_but_keep_model_after_phase():
    markup = TEMPLATE.read_text()
    launch_guard = (
        "!(window.Autonomy && window.Autonomy.lifecycle && "
        "window.Autonomy.lifecycle.startupVisible(s))"
    )

    # Compact and normal/expanded rows each hide type, role, and plugin
    # contributions while launch progress owns the available width.
    assert markup.count(launch_guard) == 6
    assert markup.count('class="sc-session-contributions"') == 2

    # Each row orders the phase chip before the model badge, making the model
    # optional width rather than competing with the startup message.
    for row in ("sc-compact-row", "sc-stats"):
        start = markup.index(f'<div class="{row}">')
        end = markup.find("\n    </div>", start)
        row_markup = markup[start:end]
        assert row_markup.index("sc-phase-chip") < row_markup.index("sc-harness-wrap")
