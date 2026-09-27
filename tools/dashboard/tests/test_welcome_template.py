"""Welcome page template: step 3 must not claim a workspace that does not exist.

Windows walkthrough, 2026-09-26: after the organization step the page ticked
"First workspace: Open and waiting." before any session had been created, and
when the sign-in scan then failed nothing was open at all. Step 3 stays the
current step until goToWorkspace() has created the session and navigated.
"""

from pathlib import Path
import re

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "pages" / "welcome.html"


def _ready_block() -> str:
    text = TEMPLATE.read_text()
    start = text.index("step === 3")
    end = text.index("</template>", start)
    return text[start:end]


def test_workspace_step_is_current_not_done():
    block = _ready_block()
    assert "Open and waiting" not in block
    m = re.search(r'<div class="step (\w+)" data-testid="welcome-step-workspace">', block)
    assert m and m.group(1) == "current"


def test_only_identity_and_org_are_ticked():
    assert _ready_block().count('class="step done"') == 2
