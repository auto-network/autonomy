"""The persona's serving label is bound once at the registry; the dashboard
must reuse it rather than derive a new apex from a changed display name."""
from tools.dashboard import service_publication as sp


def test_reserve_origin_reuses_the_persona_already_bound_serving_label(monkeypatch):
    """A display-name change must not mint a new apex: the registry binds one
    immutable serving label per persona, so the first reservation's label wins
    (2026-09-07: jeremy-<suffix> vs bound persona-<suffix>)."""
    class _M:
        payload = {"persona_pub": "ab" * 32, "persona_label": "persona-77827e972ba4c37d4215", "app_label": "old"}
    monkeypatch.setattr(sp, "_reservation_members", lambda org: [_M()])
    assert sp.bound_persona_label("autonomy", "ab" * 32) == "persona-77827e972ba4c37d4215"
    assert sp.bound_persona_label("autonomy", "cd" * 32) is None


def test_reserve_origin_payload_prefers_bound_label_over_display_name(monkeypatch):
    calls = {}
    monkeypatch.setattr(sp, "_persona_for_org", lambda org: ("ab" * 32, "Jeremy"))
    monkeypatch.setattr(sp, "_member_by_key", lambda org, key: None)

    class _M:
        payload = {"persona_pub": "ab" * 32, "persona_label": "persona-77827e972ba4c37d4215", "app_label": "old"}
    monkeypatch.setattr(sp, "_reservation_members", lambda org: [_M()])

    def fake_upsert(*args, **kwargs):
        payload = kwargs.get("payload") or next((a for a in args if isinstance(a, dict) and "persona_label" in a), None)
        calls["payload"] = payload
        return None

    # Find the write call reserve_origin makes and stub it, whatever its name.
    import inspect
    src = inspect.getsource(sp.reserve_origin)
    writer = next(n for n in ("upsert_by_key", "_upsert", "_write_reservation", "upsert_member") if n in src)
    target = sp if hasattr(sp, writer) else None
    if target is None:
        import tools.graph.settings_ops as settings_ops
        monkeypatch.setattr(settings_ops, writer, fake_upsert)
    else:
        monkeypatch.setattr(sp, writer, fake_upsert)
    try:
        sp.reserve_origin("autonomy", "bakeoff")
    except Exception:
        pass  # projection of the stubbed write may fail; the payload is what matters
    assert calls.get("payload", {}).get("persona_label") == "persona-77827e972ba4c37d4215"
