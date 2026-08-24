"""First-class attribution for authored graph content."""

from tools.graph.db import GraphDB
from tools.graph.models import Source, Thought


def test_authored_content_has_persona_and_optional_session_columns(tmp_path):
    db = GraphDB(tmp_path / "graph.db")
    try:
        expected = {"persona_id", "session_id"}
        for table in ("sources", "thoughts", "note_comments", "note_versions", "attachments"):
            columns = {row[1] for row in db.conn.execute(f"PRAGMA table_info({table})")}
            assert expected <= columns

        source = Source(
            type="note", file_path="note:attribution", persona_id="persona-a",
            session_id="auto-123",
        )
        db.insert_source(source)
        thought = Thought(
            source_id=source.id, content="body", persona_id="persona-a",
            session_id="auto-123",
        )
        db.insert_thought(thought)
        db.insert_note_version(
            source.id, 1, "body", persona_id="persona-a", session_id="auto-123",
        )
        comment = db.insert_comment(
            source.id, "comment", persona_id="persona-a", session_id="auto-123",
        )
        db.conn.commit()

        assert db.get_source(source.id)["persona_id"] == "persona-a"
        assert db.get_thoughts_by_source(source.id)[0]["session_id"] == "auto-123"
        assert db.get_note_version(source.id, 1)["persona_id"] == "persona-a"
        assert comment["session_id"] == "auto-123"
    finally:
        db.close()
