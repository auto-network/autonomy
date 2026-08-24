from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard.plugins.voice_notes.entrypoints import api


def _client(monkeypatch):
    monkeypatch.setattr(api, "_scope", lambda request: ("autonomy", None))
    return TestClient(Starlette(routes=api.routes))


def test_list_notes_returns_newest_first(monkeypatch):
    members = [
        SimpleNamespace(payload={
            "note_id": "note-old", "title": "Old", "body": "Earlier",
            "created_at": "2026-08-20T00:00:00Z",
            "updated_at": "2026-08-20T01:00:00Z",
        }),
        SimpleNamespace(payload={
            "note_id": "note-new", "title": "New", "body": "Later",
            "created_at": "2026-08-21T00:00:00Z",
            "updated_at": "2026-08-21T01:00:00Z",
        }),
    ]
    monkeypatch.setattr(
        api.settings_ops,
        "read_owned_set",
        lambda set_id, org: SimpleNamespace(members=members),
    )

    response = _client(monkeypatch).get("/api/plugins/voice-notes/notes")

    assert response.status_code == 200
    assert [note["note_id"] for note in response.json()["notes"]] == [
        "note-new", "note-old",
    ]


def test_put_note_persists_private_org_record_and_preserves_created_at(monkeypatch):
    writes = []
    monkeypatch.setattr(
        api.settings_ops,
        "read_set_key",
        lambda *args, **kwargs: {
            "payload": {"created_at": "2026-08-01T00:00:00Z"},
        },
    )
    monkeypatch.setattr(
        api.settings_ops,
        "upsert_by_key",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )

    response = _client(monkeypatch).put(
        "/api/plugins/voice-notes/notes/note-123",
        json={"title": "  Field observation  ", "body": "Wind from the east."},
    )

    assert response.status_code == 200
    note = response.json()["note"]
    assert note["note_id"] == "note-123"
    assert note["title"] == "Field observation"
    assert note["created_at"] == "2026-08-01T00:00:00Z"
    assert len(writes) == 1
    args, kwargs = writes[0]
    assert args[2] == "note-123"
    assert args[3]["body"] == "Wind from the east."
    assert kwargs == {"org": "autonomy", "state": "raw"}


def test_put_note_rejects_unbounded_or_malformed_input(monkeypatch):
    monkeypatch.setattr(api.settings_ops, "read_set_key", lambda *a, **k: None)
    client = _client(monkeypatch)

    bad_id = client.put(
        "/api/plugins/voice-notes/notes/not%2Fa%2Fnote",
        json={"title": "x", "body": "y"},
    )
    assert bad_id.status_code in {400, 404}

    too_large = client.put(
        "/api/plugins/voice-notes/notes/note-oversize",
        json={"title": "x" * 241, "body": "y"},
    )
    assert too_large.status_code == 400
