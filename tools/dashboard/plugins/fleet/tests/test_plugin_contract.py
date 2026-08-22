from pathlib import Path

from tools.dashboard.plugin_api import loader


PLUGIN_DIR = Path(__file__).resolve().parents[1]


def test_fleet_plugin_is_an_identity_menu_page_not_a_sidebar_app(monkeypatch):
    monkeypatch.setattr(loader, "_read_plugin_settings", lambda org=None: {})
    [plugin] = [
        item for item in loader.load_enabled(plugins_dir=PLUGIN_DIR.parent)
        if item.id == "fleet"
    ]
    assert plugin.id == "fleet"
    assert plugin.paths == ["/fleet"]
    assert plugin.manifest.nav.sidebar is False
    assert plugin.manifest.nav.identity_menu is True
    assert plugin.manifest.nav.identity_detail == "Personal fleet"
    assert [route.path for route in plugin.routes] == ["/api/plugins/fleet/view"]


def test_fleet_page_uses_only_generic_publish_and_signed_invite_registration():
    script = (PLUGIN_DIR / "page.js").read_text()
    assert script.count("/api/plugins/fleet/view") == 1
    assert script.count("/api/approvals") == 1
    assert script.count("/api/fleet/invitations/register") == 1
    assert "mintFleetInvite" in script
    assert "/api/fleet/enrollment/requests" not in script
    assert "/decision" not in script
