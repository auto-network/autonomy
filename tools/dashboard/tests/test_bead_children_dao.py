"""Detail children include the blocking edges needed for implementation order."""
from tools.dashboard.dao import beads


def test_mock_detail_accepts_org_and_keeps_fixture_dependency_edges(monkeypatch):
    from tools.dashboard.dao import mock
    edge = {"type": "blocks", "depends_on_id": "a"}
    monkeypatch.setattr(mock, "_beads", lambda: [
        {"id": "epic"}, {"id": "b", "parent_id": "epic", "dependencies": [edge]}])
    assert mock.get_bead("epic", "autonomy")["children"][0]["dependencies"] == [edge]


def test_children_hydrate_dependencies_with_one_scoped_query(monkeypatch):
    class Cursor:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params):
            self.calls.append((sql, params))

        def fetchone(self):
            return {"id": "epic"}

        def fetchall(self):
            if len(self.calls) == 4:
                return [{"id": "b"}, {"id": "a"}]
            if len(self.calls) == 5:
                return [{"issue_id": "b", "depends_on_id": "a", "type": "blocks"}]
            return []

    cursor = Cursor()

    class Connection:
        def cursor(self):
            return cursor

    orgs = []

    def get_conn(org):
        orgs.append(org)
        return Connection()

    monkeypatch.setattr(beads, "_get_conn", get_conn)
    result = beads.get_bead("epic", "anchore")
    assert orgs == ["anchore"]
    assert result["children"] == [
        {"id": "b", "dependencies": [{"issue_id": "b", "depends_on_id": "a", "type": "blocks"}]},
        {"id": "a", "dependencies": []},
    ]
    assert len(cursor.calls) == 5
    assert cursor.calls[-1][1] == ("b", "a")
