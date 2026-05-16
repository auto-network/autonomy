"""L1 guards for the session-viewer composer seam.

S1 extracts the composer helpers and introduces a mode gate without changing
default behavior. These tests pin the seam so later voice work can build on it
without re-welding send/draft logic back into the inline template.
"""


def test_session_view_template_uses_inline_composer_gate(test_client):
    resp = test_client.get("/pages/session-view")
    assert resp.status_code == 200
    html = resp.text
    assert 'x-show="showInlineComposer"' in html, (
        "session-view inline composer is not gated by showInlineComposer"
    )
    assert ':disabled="!canSendComposer || sending || uploading"' in html, (
        "session-view send button is not using the extracted canSendComposer guard"
    )


def test_design_panel_template_uses_inline_composer_gate(test_client):
    resp = test_client.get("/pages/design")
    assert resp.status_code == 200
    html = resp.text
    assert 'x-show="showInlineComposer"' in html, (
        "design panel composer is not gated by showInlineComposer"
    )
    assert ':disabled="!canSendComposer || sending"' in html, (
        "design panel send button is not using the extracted canSendComposer guard"
    )


def test_session_viewer_js_exposes_composer_helpers(test_client):
    resp = test_client.get("/static/js/pages/session-viewer.js")
    assert resp.status_code == 200
    body = resp.text
    for snippet in (
        'resolveComposerMode()',
        'refreshViewportWidth()',
        'getComposerStore()',
        'readComposerText()',
        'persistComposerDraft(text)',
        'writeComposerText(text)',
        'restoreComposerDraft()',
        'buildComposerBody(text)',
        'canSendComposerText(text)',
    ):
        assert snippet in body, f"session-viewer.js missing composer seam helper: {snippet}"
