"""Client contracts for routing Resume through the session's owning org."""

from pathlib import Path


_DASHBOARD = Path(__file__).resolve().parents[2]


def test_recent_session_resume_uses_row_org():
    source = (_DASHBOARD / "static/js/pages/sessions.js").read_text()

    assert "var resumeOrg = s.org && s.org.slug" in source
    assert "resumeHeaders['X-Graph-Org'] = resumeOrg" in source
    assert "headers: resumeHeaders" in source


def test_viewer_resume_carries_detail_org():
    source = (_DASHBOARD / "static/js/pages/session-viewer.js").read_text()

    assert "orgSlug: (data.org && data.org.slug) || ''" in source
    assert "resumeHeaders['X-Graph-Org'] = m.orgSlug" in source
    assert "headers: resumeHeaders" in source
